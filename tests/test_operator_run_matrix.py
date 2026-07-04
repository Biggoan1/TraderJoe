"""Tests for strategy/warehouse/operator/run_matrix.py."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from strategy.warehouse.operator import run_matrix
from strategy.warehouse.operator.run_matrix import (
    DEFAULT_WINDOWS,
    MatrixRow,
    MatrixRunnerError,
    MatrixSummary,
    Window,
    build_parser,
    main,
    resolve_window,
    run,
)


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


class TestResolveWindow:
    def test_60d(self):
        today = date(2026, 7, 4)
        w = resolve_window("60d", today)
        assert w.start == "2026-05-05"
        assert w.end == "2026-07-04"

    def test_90d(self):
        today = date(2026, 7, 4)
        w = resolve_window("90d", today)
        assert w.start == "2026-04-05"
        assert w.end == "2026-07-04"

    def test_6mo(self):
        today = date(2026, 7, 4)
        w = resolve_window("6mo", today)
        assert w.end == "2026-07-04"
        # 6mo -> 180 days
        assert w.start == "2026-01-05"

    def test_1y(self):
        today = date(2026, 7, 4)
        w = resolve_window("1y", today)
        assert w.end == "2026-07-04"
        assert w.start == "2025-07-04"

    def test_ytd(self):
        today = date(2026, 7, 4)
        w = resolve_window("ytd", today)
        assert w.start == "2026-01-01"
        assert w.end == "2026-07-04"

    def test_custom_range(self):
        today = date(2026, 7, 4)
        w = resolve_window("2020-05-01..2020-06-30", today)
        assert w.start == "2020-05-01"
        assert w.end == "2020-06-30"

    def test_unknown_label_rejected(self):
        with pytest.raises(MatrixRunnerError, match="unknown window"):
            resolve_window("wibble", date(2026, 7, 4))

    def test_bad_custom_range_rejected(self):
        with pytest.raises(MatrixRunnerError, match="invalid custom"):
            resolve_window("..2020-05-01", date(2026, 7, 4))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    def test_defaults_match_documented_windows(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.windows == list(DEFAULT_WINDOWS)


# ---------------------------------------------------------------------------
# Runner (fully mocked)
# ---------------------------------------------------------------------------


def _make_fake_bundle(disagreements=5, provenance_source="warehouse"):
    comparison = SimpleNamespace(disagreements=list(range(disagreements)))
    return SimpleNamespace(
        comparison=comparison,
        dataset_provenance={"source": provenance_source},
        promotion_entry=SimpleNamespace(current_state="disabled", approvals=[]),
        config=SimpleNamespace(
            symbols=("AAPL",), benchmarks=("SPY",),
            window_start="", window_end="",
            stable_hash=lambda: "cfg",
        ),
        dataset_manifest_path="",
        comparison_report=None,
        walk_forward_report=None,
        walk_forward_research_report=None,
        learning_report=None,
        analyst_reports=[],
        analyst_report_paths=[],
        warnings=[],
    )


class TestRunMatrixHappyPath:
    def test_multiple_windows_each_get_row(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(
            ["--windows", "60d", "90d",
             "--report-root", str(tmp_path / "reports"),
             "--research-data-root", str(tmp_path / "rd"),
             "--run-manifest-root", str(tmp_path / "manifests")]
        )
        calls: List[Dict[str, Any]] = []

        def fake_offline(ns, env=None, printer=None):
            calls.append({"dataset_id": ns.dataset_id})
            return _make_fake_bundle()

        def fake_build(bundle, run_id, started_at, completed_at=None, **k):
            return SimpleNamespace(
                run_id=run_id, completed_at=completed_at or "now",
                stable_hash=lambda: "h",
            )

        def fake_write(manifest, output_dir, write_latest_pointer=True):
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{manifest.run_id}.json"
            path.write_text("{}", encoding="utf-8")
            return {"json": path, "markdown": output_dir / f"{manifest.run_id}.md"}

        summary = run(
            args, env={}, today=date(2026, 7, 4),
            run_offline=fake_offline,
            build_manifest=fake_build,
            write_manifest=fake_write,
            printer=lambda *a, **k: None,
        )
        assert summary.total == 2
        assert summary.passed == 2
        assert summary.failed == 0
        assert len(calls) == 2

    def test_deterministic_row_ordering(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(
            ["--windows", "60d", "90d", "1y",
             "--report-root", str(tmp_path / "reports"),
             "--research-data-root", str(tmp_path / "rd"),
             "--run-manifest-root", str(tmp_path / "manifests")]
        )
        summary = run(
            args, env={}, today=date(2026, 7, 4),
            run_offline=lambda ns, env=None, printer=None: _make_fake_bundle(),
            build_manifest=lambda *a, **k: SimpleNamespace(
                run_id="r", completed_at="now"
            ),
            write_manifest=lambda m, d, write_latest_pointer=True: {
                "json": Path("/tmp/x"),
                "markdown": Path("/tmp/y"),
            },
            printer=lambda *a, **k: None,
        )
        labels = [r.window.label for r in summary.rows]
        assert labels == ["60d", "90d", "1y"]


class TestRunMatrixFailureIsolation:
    def test_one_failing_window_does_not_abort_others(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(
            ["--windows", "60d", "90d", "1y",
             "--report-root", str(tmp_path / "reports"),
             "--research-data-root", str(tmp_path / "rd"),
             "--run-manifest-root", str(tmp_path / "manifests")]
        )

        call_count = {"n": 0}
        def fake_offline(ns, env=None, printer=None):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("second window boom")
            return _make_fake_bundle()

        summary = run(
            args, env={}, today=date(2026, 7, 4),
            run_offline=fake_offline,
            build_manifest=lambda *a, **k: SimpleNamespace(
                run_id="r", completed_at="now"
            ),
            write_manifest=lambda m, d, write_latest_pointer=True: {
                "json": Path("/tmp/x"),
                "markdown": Path("/tmp/y"),
            },
            printer=lambda *a, **k: None,
        )
        assert summary.total == 3
        assert summary.passed == 2
        assert summary.failed == 1
        # Failing window carries the error message
        failing = [r for r in summary.rows if not r.ok]
        assert failing[0].error.startswith("RuntimeError")


class TestProviderNotCalledByDefault:
    def test_default_forbids_provider_via_offline_runner(self, tmp_path):
        # Uses the REAL offline runner but with an empty warehouse
        # -> should FAIL every window without --allow-provider-fallback.
        parser = build_parser()
        args = parser.parse_args(
            ["--windows", "60d",
             "--report-root", str(tmp_path / "reports"),
             "--research-data-root", str(tmp_path / "rd"),
             "--run-manifest-root", str(tmp_path / "manifests")]
        )
        # Fake the WAREHOUSE_ROOT so the runner walks into an empty tree
        env = {"WAREHOUSE_ROOT": str(tmp_path / "empty-warehouse")}
        summary = run(
            args, env=env, today=date(2026, 7, 4),
            # Use the real offline runner - it should fail with
            # OfflineValidationError when coverage is missing.
            printer=lambda *a, **k: None,
        )
        assert summary.total == 1
        assert summary.failed == 1
        assert "OfflineValidationError" in summary.rows[0].error


# ---------------------------------------------------------------------------
# Main + exit code
# ---------------------------------------------------------------------------


class TestMainExitCode:
    def test_exit_zero_on_all_pass(self, tmp_path, monkeypatch):
        # Redirect the runner via monkeypatched module attributes
        monkeypatch.setattr(
            run_matrix,
            "_default_run_offline",
            lambda: (lambda ns, env=None, printer=None: _make_fake_bundle()),
        )
        monkeypatch.setattr(
            run_matrix,
            "_default_build_manifest",
            lambda: (lambda *a, **k: SimpleNamespace(
                run_id="r", completed_at="now"
            )),
        )
        monkeypatch.setattr(
            run_matrix,
            "_default_write_manifest",
            lambda: (lambda m, d, write_latest_pointer=True: {
                "json": Path("/tmp/x"),
                "markdown": Path("/tmp/y"),
            }),
        )
        rc = main(
            ["--windows", "60d",
             "--report-root", str(tmp_path / "reports"),
             "--research-data-root", str(tmp_path / "rd"),
             "--run-manifest-root", str(tmp_path / "manifests")]
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.operator.run_matrix as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        source = _module_source()
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_order_path_tokens(self):
        source = _module_source()
        for token in (
            "submit_order", "place_order", "cancel_order",
            "TradingClient",
        ):
            assert token not in source

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source
