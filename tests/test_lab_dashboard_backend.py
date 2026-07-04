"""Tests for strategy/lab/dashboard_backend.py — Card 8."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from strategy.config import reset_feature_flags
from strategy.lab.dashboard_backend import (
    analyst_summaries,
    dataset_provenance,
    experiment_detail,
    hypothesis_queue,
    latest_leaderboard,
    latest_weekend_run,
    leaderboard,
    list_experiments,
    list_weekend_runs,
    main,
    strategy_rankings,
    top_parameter_sets,
    warehouse_status,
    weekend_run_detail,
)
from strategy.lab.parameter_sweep import ParameterGrid
from strategy.lab.weekend_lab import SweepSpec, run_weekend_lab


# ---------------------------------------------------------------------------
# Fixture: a real weekend-lab run under a temp root
# ---------------------------------------------------------------------------


@pytest.fixture
def lab_tree(tmp_path):
    reset_feature_flags()
    lab_root = tmp_path / "weekend_lab"
    manifest = run_weekend_lab(
        window_start="2026-05-01", window_end="2026-05-10",
        dataset_id="dash-test",
        strategies=["champion", "momentum", "trend"],
        sweep_specs=[
            SweepSpec("momentum", ParameterGrid({"lookback": [5, 10]})),
        ],
        lab_root=str(lab_root),
        printer=lambda *a, **k: None,
    )
    return {
        "lab_root": str(lab_root),
        "run_id": manifest.run_id,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_envelope_shape(self, lab_tree):
        r = latest_weekend_run(lab_tree["lab_root"])
        assert set(r.keys()) == {"data", "warnings", "generated_at"}


# ---------------------------------------------------------------------------
# Weekend runs
# ---------------------------------------------------------------------------


class TestWeekendRuns:
    def test_list_returns_one_run(self, lab_tree):
        r = list_weekend_runs(lab_tree["lab_root"])
        assert len(r["data"]) == 1
        assert r["data"][0]["run_id"] == lab_tree["run_id"]

    def test_list_missing_root(self, tmp_path):
        r = list_weekend_runs(str(tmp_path / "nope"))
        assert r["data"] == []
        assert r["warnings"]

    def test_latest_matches_first(self, lab_tree):
        latest = latest_weekend_run(lab_tree["lab_root"])
        assert latest["data"]["run_id"] == lab_tree["run_id"]

    def test_detail_returns_manifest(self, lab_tree):
        r = weekend_run_detail(lab_tree["run_id"], lab_tree["lab_root"])
        assert r["data"]["run_id"] == lab_tree["run_id"]

    def test_missing_run_returns_not_found(self, lab_tree):
        r = weekend_run_detail("nonexistent", lab_tree["lab_root"])
        assert r.get("error") == "not_found"


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


class TestLeaderboard:
    def test_leaderboard_by_run_id(self, lab_tree):
        r = leaderboard(lab_tree["run_id"], lab_tree["lab_root"])
        assert "primary_metric" in r["data"]
        assert r["data"]["entry_count"] >= 3  # 3 strategies + sweep rows

    def test_latest_leaderboard(self, lab_tree):
        r = latest_leaderboard(lab_tree["lab_root"])
        assert "primary_metric" in r["data"]

    def test_missing_run_leaderboard_not_found(self, lab_tree):
        r = leaderboard("nonexistent", lab_tree["lab_root"])
        assert r.get("error") == "not_found"


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


class TestExperiments:
    def test_list_experiments_from_matrix(self, lab_tree):
        # experiment manifests live under
        # <lab_root>/<run_id>/matrix/<dataset_id>/*.json
        experiment_root = str(
            Path(lab_tree["lab_root"]) / lab_tree["run_id"] / "matrix"
        )
        r = list_experiments(experiment_root)
        assert len(r["data"]) >= 3
        for entry in r["data"]:
            assert entry["experiment_id"]

    def test_experiment_detail_by_id(self, lab_tree):
        experiment_root = str(
            Path(lab_tree["lab_root"]) / lab_tree["run_id"] / "matrix"
        )
        listing = list_experiments(experiment_root)
        exp_id = listing["data"][0]["experiment_id"]
        r = experiment_detail(exp_id, experiment_root)
        assert r["data"]["experiment_id"] == exp_id

    def test_missing_experiment_not_found(self, lab_tree):
        experiment_root = str(
            Path(lab_tree["lab_root"]) / lab_tree["run_id"] / "matrix"
        )
        r = experiment_detail("exp_nonexistent", experiment_root)
        assert r.get("error") == "not_found"


class TestStrategyRankings:
    def test_rankings_dedupe_by_name(self, lab_tree):
        r = strategy_rankings(lab_tree["lab_root"])
        strategies = r["data"]["strategies"]
        names = [entry["entry"]["strategy"]["name"] for entry in strategies]
        # No duplicates
        assert len(names) == len(set(names))


class TestTopParameterSets:
    def test_top_parameter_sets_returns_ok_rows(self, lab_tree):
        r = top_parameter_sets(
            lab_tree["run_id"], "momentum", lab_tree["lab_root"],
        )
        assert r["data"]["strategy_name"] == "momentum"
        assert r["data"]["grid"] == {"lookback": [5, 10]}
        assert r["data"]["total"] == 2

    def test_missing_sweep_not_found(self, lab_tree):
        r = top_parameter_sets(
            lab_tree["run_id"], "nonexistent", lab_tree["lab_root"],
        )
        assert r.get("error") == "not_found"


# ---------------------------------------------------------------------------
# Analyst / provenance / warehouse / hypothesis
# ---------------------------------------------------------------------------


class TestAnalystProvenance:
    def test_analyst_summaries_empty_when_no_llm(self, lab_tree):
        # No LLM was attached, so analyst_report_ids on every
        # experiment manifest is [].
        experiment_root = str(
            Path(lab_tree["lab_root"]) / lab_tree["run_id"] / "matrix"
        )
        r = analyst_summaries(experiment_root)
        assert r["data"] == []

    def test_dataset_provenance_by_experiment(self, lab_tree):
        experiment_root = str(
            Path(lab_tree["lab_root"]) / lab_tree["run_id"] / "matrix"
        )
        listing = list_experiments(experiment_root)
        exp_id = listing["data"][0]["experiment_id"]
        r = dataset_provenance(exp_id, experiment_root)
        assert "dataset_id" in r["data"]
        assert "live_fetch_used" in r["data"]


class TestWarehouseStatus:
    def test_missing_manifest_root(self, tmp_path):
        r = warehouse_status(str(tmp_path / "nope"))
        assert r["data"]["total"] == 0
        assert r["warnings"]


class TestHypothesisQueue:
    def test_empty_when_root_missing(self, tmp_path):
        r = hypothesis_queue(str(tmp_path / "nope"))
        assert r["data"] == []

    def test_reads_jsonl(self, tmp_path):
        root = tmp_path / "queue"
        root.mkdir()
        (root / "q.jsonl").write_text(
            json.dumps({"proposal_id": "hyp_a", "status": "proposed",
                        "title": "T", "strategy_name": "s",
                        "suggested_parameters": {}, "confidence": 0.5}) + "\n"
            + json.dumps({"proposal_id": "hyp_b", "status": "approved",
                          "title": "T", "strategy_name": "s",
                          "suggested_parameters": {}, "confidence": 0.5}) + "\n",
            encoding="utf-8",
        )
        r = hypothesis_queue(str(root))
        assert len(r["data"]) == 2
        r2 = hypothesis_queue(str(root), status="approved")
        assert len(r2["data"]) == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_dumps_json(self, lab_tree, capsys):
        rc = main([
            "list_weekend_runs",
            "--lab-root", lab_tree["lab_root"],
        ])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "data" in parsed

    def test_cli_missing_run_id_returns_2(self, capsys):
        rc = main(["leaderboard"])
        assert rc == 2

    def test_cli_top_param_sets_needs_both(self, capsys, lab_tree):
        rc = main([
            "top_parameter_sets",
            "--lab-root", lab_tree["lab_root"],
            "--run-id", lab_tree["run_id"],
        ])
        assert rc == 2


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.dashboard_backend")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.dashboard_backend")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.dashboard_backend")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s

    def test_no_credential_env_reads(self):
        s = _module_source("strategy.lab.dashboard_backend")
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, s)

    def test_no_write_helpers(self):
        s = _module_source("strategy.lab.dashboard_backend")
        # The backend must not open files for writing
        for pattern in (
            r'open\([^)]*[\'"]w[\'"]',
            r'\.write_text\(',
            r'\.write_bytes\(',
        ):
            assert not re.search(pattern, s), (
                f"backend contains a write path: {pattern!r}"
            )
