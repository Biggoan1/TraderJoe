"""Tests for strategy/lab/multi_year_matrix.py — Card 10."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from strategy.config import reset_feature_flags
from strategy.lab.multi_year_matrix import (
    DEFAULT_REGIMES,
    DEFAULT_YEARS,
    MatrixWindow,
    MultiYearMatrixManifest,
    ReportCardCell,
    StrategyReportCard,
    build_default_windows,
    main,
    run_multi_year_matrix,
)


# ---------------------------------------------------------------------------
# Window builders
# ---------------------------------------------------------------------------


class TestBuildDefaultWindows:
    def test_years_within_today_included(self):
        windows = build_default_windows(
            years=(2020, 2021, 2022, 2023, 2024, 2025, 2026),
            regimes=(),
            today=date(2026, 7, 4),
        )
        labels = [w.label for w in windows]
        assert labels == ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]
        # 2026 is truncated at today
        assert windows[-1].end == "2026-07-04"

    def test_future_years_dropped(self):
        windows = build_default_windows(
            years=(2020, 2027, 2028),
            regimes=(),
            today=date(2026, 7, 4),
        )
        labels = [w.label for w in windows]
        assert "2027" not in labels
        assert "2028" not in labels

    def test_regime_windows_included(self):
        windows = build_default_windows(
            years=(),
            regimes=DEFAULT_REGIMES,
            today=date(2026, 7, 4),
        )
        labels = [w.label for w in windows]
        assert "COVID_crash" in labels
        assert "Bear_2022" in labels

    def test_regime_starting_in_future_dropped(self):
        windows = build_default_windows(
            years=(),
            regimes=(("Future_2027", "2027-01-01", "2027-06-30"),),
            today=date(2026, 7, 4),
        )
        assert windows == []


# ---------------------------------------------------------------------------
# Report card structure
# ---------------------------------------------------------------------------


class TestReportCardTypes:
    def test_cell_to_dict(self):
        cell = ReportCardCell(
            window=MatrixWindow("2020", "2020-01-01", "2020-12-31", "year"),
            ok=True,
            total_events=10,
            total_disagreements=3,
        )
        d = cell.to_dict()
        assert d["window"]["label"] == "2020"
        assert d["ok"] is True
        assert d["total_events"] == 10

    def test_manifest_to_dict(self):
        w = MatrixWindow("2020", "2020-01-01", "2020-12-31", "year")
        cell = ReportCardCell(window=w, ok=True)
        card = StrategyReportCard(
            strategy_name="s", strategy_version="0.1.0",
            cells=(cell,), passed=1, failed=0,
        )
        manifest = MultiYearMatrixManifest(
            run_id="r", generated_at="2026-07-04T00:00:00Z",
            windows=(w,), report_cards=(card,),
        )
        d = manifest.to_dict()
        assert d["run_id"] == "r"
        assert d["report_cards"][0]["strategy_name"] == "s"


# ---------------------------------------------------------------------------
# End-to-end run
# ---------------------------------------------------------------------------


class TestRunMultiYearMatrix:
    def test_matrix_produces_report_card_per_strategy(self, tmp_path):
        reset_feature_flags()
        windows = [
            MatrixWindow("2020", "2020-01-01", "2020-01-15", "year"),
            MatrixWindow("2021", "2021-01-01", "2021-01-15", "year"),
        ]
        manifest = run_multi_year_matrix(
            strategies=["champion", "momentum"],
            windows=windows,
            matrix_root=str(tmp_path / "matrix"),
            printer=lambda *a, **k: None,
        )
        assert len(manifest.report_cards) == 2
        for card in manifest.report_cards:
            assert len(card.cells) == 2
            assert card.strategy_version  # populated on success

    def test_matrix_writes_manifest_json(self, tmp_path):
        reset_feature_flags()
        windows = [MatrixWindow("2020", "2020-01-01", "2020-01-15", "year")]
        manifest = run_multi_year_matrix(
            strategies=["champion"],
            windows=windows,
            matrix_root=str(tmp_path / "matrix"),
            run_id="fixed-run",
            printer=lambda *a, **k: None,
        )
        manifest_path = tmp_path / "matrix" / "fixed-run" / "manifest.json"
        assert manifest_path.is_file()
        loaded = json.loads(manifest_path.read_text())
        assert loaded["run_id"] == "fixed-run"

    def test_bad_strategy_name_isolated(self, tmp_path):
        reset_feature_flags()
        windows = [MatrixWindow("2020", "2020-01-01", "2020-01-15", "year")]
        manifest = run_multi_year_matrix(
            strategies=["champion", "nonexistent"],
            windows=windows,
            matrix_root=str(tmp_path / "matrix"),
            printer=lambda *a, **k: None,
        )
        # Both strategies present as cards; the bad one has an error
        # in every cell
        cards_by_name = {c.strategy_name: c for c in manifest.report_cards}
        assert cards_by_name["nonexistent"].failed == 1
        assert cards_by_name["nonexistent"].passed == 0


class TestBenchmarkDrivers:
    def test_default_windows_span_full_history(self):
        today = date(2026, 7, 4)
        windows = build_default_windows(today=today)
        labels = {w.label for w in windows}
        # All 7 years
        assert {"2020", "2021", "2022", "2023", "2024", "2025", "2026"}.issubset(labels)
        # Regime windows folded in
        assert "COVID_crash" in labels
        assert "Bear_2022" in labels


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_no_regimes(self, tmp_path):
        reset_feature_flags()
        rc = main([
            "--strategies", "champion",
            "--years", "2020",
            "--no-regimes",
            "--matrix-root", str(tmp_path / "matrix"),
            "--run-id", "cli-test",
        ])
        assert rc == 0
        assert (tmp_path / "matrix" / "cli-test" / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.multi_year_matrix")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.multi_year_matrix")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.multi_year_matrix")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
