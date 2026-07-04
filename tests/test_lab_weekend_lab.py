"""Tests for strategy/lab/weekend_lab.py — Card 6."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, List

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.lab.parameter_sweep import ParameterGrid
from strategy.lab.weekend_lab import (
    DEFAULT_LAB_ROOT,
    SweepSpec,
    WeekendLabManifest,
    build_config,
    render_dashboard_html,
    run_weekend_lab,
    main,
)


# ---------------------------------------------------------------------------
# End-to-end (fixture mode) — full pipeline runs in one call
# ---------------------------------------------------------------------------


class TestRunWeekendLab:
    def test_matrix_and_sweep(self, tmp_path):
        reset_feature_flags()
        manifest = run_weekend_lab(
            window_start="2026-05-01", window_end="2026-05-15",
            dataset_id="test-weekend",
            strategies=["champion", "momentum", "trend", "mean_reversion"],
            sweep_specs=[
                SweepSpec("momentum", ParameterGrid({"lookback": [5, 10]})),
            ],
            lab_root=str(tmp_path / "weekend"),
            printer=lambda *a, **k: None,
        )
        assert isinstance(manifest, WeekendLabManifest)
        assert len(manifest.matrix_experiment_ids) == 4
        assert len(manifest.sweep_ids) == 1
        # Every promotion state must stay disabled
        for xid, state in manifest.promotion_states.items():
            assert state == "disabled", f"{xid} advanced to {state}"
        # Feature flags stay all disabled
        assert manifest.feature_flags_all_disabled is True

    def test_leaderboard_written(self, tmp_path):
        reset_feature_flags()
        manifest = run_weekend_lab(
            window_start="2026-05-01", window_end="2026-05-15",
            dataset_id="lb-test",
            strategies=["champion", "momentum"],
            sweep_specs=[],
            lab_root=str(tmp_path / "weekend"),
            printer=lambda *a, **k: None,
        )
        assert Path(manifest.leaderboard_path).is_file()
        loaded = json.loads(Path(manifest.leaderboard_path).read_text())
        assert loaded["primary_metric"] == "mean_score_delta"
        assert loaded["entry_count"] >= 2

    def test_dashboard_written(self, tmp_path):
        reset_feature_flags()
        manifest = run_weekend_lab(
            window_start="2026-05-01", window_end="2026-05-15",
            dataset_id="dash-test",
            strategies=["champion"],
            sweep_specs=[],
            lab_root=str(tmp_path / "weekend"),
            printer=lambda *a, **k: None,
        )
        html = Path(manifest.dashboard_path).read_text()
        assert "<title>Weekend Lab" in html
        assert "Leaderboard" in html
        assert "champion" in html
        assert "all disabled" in html

    def test_manifest_json_written(self, tmp_path):
        reset_feature_flags()
        manifest = run_weekend_lab(
            window_start="2026-05-01", window_end="2026-05-15",
            dataset_id="mf-test",
            strategies=["champion"],
            sweep_specs=[],
            lab_root=str(tmp_path / "weekend"),
            printer=lambda *a, **k: None,
        )
        manifest_json = Path(tmp_path / "weekend") / manifest.run_id / "manifest.json"
        assert manifest_json.is_file()
        loaded = json.loads(manifest_json.read_text())
        assert loaded["run_id"] == manifest.run_id
        assert loaded["feature_flags_all_disabled"] is True


class TestFailureIsolation:
    def test_missing_strategy_reported_not_fatal(self, tmp_path):
        reset_feature_flags()
        # 'nonexistent' will raise inside registry.get; the sweep
        # spec's failure isolation should keep the run going.
        manifest = run_weekend_lab(
            window_start="2026-05-01", window_end="2026-05-15",
            dataset_id="isol-test",
            strategies=["champion"],
            sweep_specs=[
                SweepSpec("nonexistent", ParameterGrid({"a": [1]})),
            ],
            lab_root=str(tmp_path / "weekend"),
            printer=lambda *a, **k: None,
        )
        # The one bad sweep is dropped; matrix still runs
        assert len(manifest.matrix_experiment_ids) == 1


class TestHtmlRender:
    def test_html_escapes_strategy_names(self, tmp_path):
        reset_feature_flags()
        manifest = run_weekend_lab(
            window_start="2026-05-01", window_end="2026-05-05",
            dataset_id="<script>alert(1)</script>",
            strategies=["champion"],
            sweep_specs=[],
            lab_root=str(tmp_path / "weekend"),
            printer=lambda *a, **k: None,
        )
        html = Path(manifest.dashboard_path).read_text()
        # dataset_id escaped
        assert "<script>alert(1)" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_no_llm_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = main([
            "--window-start", "2026-05-01",
            "--window-end", "2026-05-05",
            "--dataset-id", "cli-test",
            "--strategies", "champion",
            "--no-sweeps",
            "--lab-root", str(tmp_path / "weekend"),
        ])
        assert rc == 0

    def test_llm_env_missing_returns_2(self, monkeypatch):
        # Ensure LLM env unset
        for var in ("RESEARCH_LLM_ENDPOINT", "RESEARCH_AI_MODEL"):
            monkeypatch.delenv(var, raising=False)
        rc = main([
            "--window-start", "2026-05-01",
            "--window-end", "2026-05-05",
            "--with-llm",
            "--lab-root", "/tmp/no-write",
        ])
        assert rc == 2


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.weekend_lab")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.weekend_lab")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.weekend_lab")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
