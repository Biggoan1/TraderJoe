"""Tests for strategy/lab/traderjoe_research.py — Task 4."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from strategy.lab.traderjoe_research import (
    build_parser,
    cmd_plan,
    cmd_run,
    cmd_summarize,
    main,
)


# ---------------------------------------------------------------------------
# plan subcommand
# ---------------------------------------------------------------------------


class TestCmdPlan:
    def test_plan_writes_file_and_returns_zero(self, tmp_path, capsys):
        out = tmp_path / "plan.json"
        rc = main([
            "plan", "Sweep RS weight from 0.2 to 1.0",
            "--output", str(out),
        ])
        assert rc == 0
        assert out.is_file()
        loaded = json.loads(out.read_text())
        assert loaded["kind"] == "rs_weight_sweep"

    def test_plan_unrecognised_returns_3(self, tmp_path):
        out = tmp_path / "plan.json"
        rc = main([
            "plan", "nonsense query",
            "--output", str(out),
        ])
        assert rc == 3
        assert out.is_file()  # still writes the unrecognised plan
        loaded = json.loads(out.read_text())
        assert loaded["kind"] == "unrecognised"

    def test_plan_prints_when_requested(self, tmp_path, capsys):
        out = tmp_path / "plan.json"
        main([
            "plan", "Sweep RS weight from 0.2 to 1.0",
            "--output", str(out), "--print",
        ])
        printed = capsys.readouterr().out
        # Both the human summary AND the JSON blob appear
        assert "plan written" in printed
        assert '"kind"' in printed


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------


class TestCmdRun:
    def _write_plan(self, tmp_path, kind: str = "rs_weight_sweep"):
        plan_path = tmp_path / "plan.json"
        payload = {
            "kind": kind,
            "question": "test",
            "parameters": {
                "overlay_weights": [0.2, 0.3],
                "window": {"label": "1y", "start": "2025-07-04", "end": "2026-07-04"},
            },
            "steps": ["step1"],
            "reasons": ["r1"],
            "warnings": [],
            "generated_at": "2026-07-04T00:00:00+00:00",
        }
        plan_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_path

    def test_run_missing_plan_file(self, tmp_path):
        rc = main(["run", str(tmp_path / "missing.json")])
        assert rc == 2

    def test_run_unknown_kind_rejected(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(
            json.dumps({"kind": "unrecognised", "question": "q",
                        "parameters": {}, "steps": [], "reasons": [],
                        "warnings": [], "generated_at": ""}),
            encoding="utf-8",
        )
        rc = main(["run", str(plan_path)])
        assert rc == 2

    def test_run_dry_run(self, tmp_path, capsys):
        plan_path = self._write_plan(tmp_path, kind="rs_weight_sweep")
        rc = main([
            "run", str(plan_path),
            "--run-root", str(tmp_path / "runs"),
            "--dry-run",
        ])
        assert rc == 0
        printed = capsys.readouterr().out
        assert "DRY RUN" in printed
        # No manifest written on dry-run
        assert not (tmp_path / "runs").exists() or \
               not list((tmp_path / "runs").iterdir())

    def test_run_writes_manifest(self, tmp_path, capsys):
        plan_path = self._write_plan(tmp_path, kind="rs_weight_sweep")
        rc = main([
            "run", str(plan_path),
            "--run-root", str(tmp_path / "runs"),
        ])
        assert rc == 0
        # exactly one run dir
        runs = list((tmp_path / "runs").iterdir())
        assert len(runs) == 1
        manifest = json.loads((runs[0] / "manifest.json").read_text())
        assert manifest["plan"]["kind"] == "rs_weight_sweep"
        assert "status" in manifest["result"]

    def test_run_regime_missing_source_fails(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps({
            "kind": "regime_analysis",
            "question": "q",
            "parameters": {
                "regime_dimensions": ["vol"],
                "source_manifest": str(tmp_path / "nonexistent.json"),
                "window": {"label": "1y", "start": "", "end": ""},
            },
            "steps": [], "reasons": [], "warnings": [], "generated_at": "",
        }), encoding="utf-8")
        rc = main([
            "run", str(plan_path),
            "--run-root", str(tmp_path / "runs"),
        ])
        assert rc == 2

    def test_provider_fallback_missing_env_rejected(self, tmp_path, monkeypatch):
        for k in ("RESEARCH_ALPACA_API_KEY", "RESEARCH_ALPACA_SECRET_KEY",
                  "RESEARCH_ALPACA_ENDPOINT", "RESEARCH_ALPACA_DATA_ENDPOINT"):
            monkeypatch.delenv(k, raising=False)
        plan_path = self._write_plan(tmp_path, kind="rs_weight_sweep")
        rc = main([
            "run", str(plan_path),
            "--run-root", str(tmp_path / "runs"),
            "--allow-provider-fallback",
        ])
        assert rc == 2


# ---------------------------------------------------------------------------
# summarize subcommand
# ---------------------------------------------------------------------------


class TestCmdSummarize:
    def _seed_run(self, run_root: Path, run_id: str, plan_kind: str = "rs_weight_sweep"):
        d = run_root / run_id
        d.mkdir(parents=True)
        manifest = {
            "run_id": run_id,
            "generated_at": "2026-07-04T12:00:00+00:00",
            "plan": {
                "kind": plan_kind,
                "question": "Sweep RS weight from 0.2 to 1.0",
                "parameters": {},
                "steps": ["step 1", "step 2"],
                "reasons": [], "warnings": [], "generated_at": "",
            },
            "result": {"status": "documented", "delegated_to": "somewhere.md"},
        }
        (d / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    def test_summarize_specific_run(self, tmp_path, capsys):
        self._seed_run(tmp_path, "run-abc")
        rc = main([
            "summarize", "run-abc",
            "--run-root", str(tmp_path),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "run-abc" in out
        assert "Sweep RS weight" in out

    def test_summarize_latest(self, tmp_path, capsys):
        # Two runs; alphabetic reverse sort picks 'run-b'
        self._seed_run(tmp_path, "run-a")
        self._seed_run(tmp_path, "run-b")
        rc = main([
            "summarize", "latest",
            "--run-root", str(tmp_path),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "run-b" in out

    def test_summarize_missing_run_id(self, tmp_path):
        (tmp_path / "known-run").mkdir()
        rc = main([
            "summarize", "unknown-run",
            "--run-root", str(tmp_path),
        ])
        assert rc == 2

    def test_summarize_no_runs(self, tmp_path):
        rc = main([
            "summarize", "latest",
            "--run-root", str(tmp_path),
        ])
        assert rc == 2


# ---------------------------------------------------------------------------
# End-to-end plan+run+summarize
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_plan_then_run_then_summarize(self, tmp_path):
        plans_dir = tmp_path / "plans"
        runs_dir = tmp_path / "runs"
        plan_file = plans_dir / "plan.json"
        rc = main([
            "plan", "Sweep RS weight from 0.2 to 1.0",
            "--output", str(plan_file),
        ])
        assert rc == 0
        rc = main([
            "run", str(plan_file),
            "--run-root", str(runs_dir),
        ])
        assert rc == 0
        rc = main([
            "summarize", "latest",
            "--run-root", str(runs_dir),
        ])
        assert rc == 0


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _source() -> str:
    import strategy.lab.traderjoe_research as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _source()
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _source()
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion(self):
        s = _source()
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
        assert "FeatureFlags(" not in s

    def test_provider_fallback_requires_flag(self):
        s = _source()
        # The default posture is warehouse-only.  Check the flag
        # gating text is present.
        assert "--allow-provider-fallback" in s
        assert "Warehouse-only" in s or "warehouse-only" in s.lower()
