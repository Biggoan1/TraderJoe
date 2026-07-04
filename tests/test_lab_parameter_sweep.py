"""Tests for strategy/lab/parameter_sweep.py — Card 5."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, List, Mapping

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.config import reset_feature_flags
from strategy.historical_validation import HistoricalValidationConfig
from strategy.lab import (
    ParameterGrid,
    SweepManifest,
    SweepRow,
    run_parameter_sweep,
)
from strategy.lab.champion_rs import ChampionRelativeStrengthStrategy
from strategy.lab.momentum import MomentumStrategy
from strategy.lab.parameter_sweep import SweepError


FIXTURE_WINDOW_START = "2026-05-01"
FIXTURE_WINDOW_END = "2026-06-30"
FIXTURE_SYMBOLS = ("AAPL", "MSFT", "NVDA")


def _iter_dates(start: str, end: str):
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while current <= stop:
        yield current
        current = current + timedelta(days=1)


def _make_events():
    return [
        BacktestEvent(
            timestamp=f"{d.isoformat()}T14:30:00+00:00",
            event_type="market_snapshot",
            sequence=i + 1,
        )
        for i, d in enumerate(_iter_dates(FIXTURE_WINDOW_START, FIXTURE_WINDOW_END))
    ]


def _make_config(tmp_path) -> HistoricalValidationConfig:
    events = _make_events()
    scores = {
        e.timestamp: {sym: 0.5 + 0.001 * i + 0.01 * j
                      for j, sym in enumerate(FIXTURE_SYMBOLS)}
        for i, e in enumerate(events)
    }
    rs = {
        e.timestamp: {"AAPL": 70.0, "MSFT": 55.0, "NVDA": 60.0}
        for e in events
    }
    return HistoricalValidationConfig(
        dataset_id="sweep-fixture",
        symbols=FIXTURE_SYMBOLS,
        window_start=FIXTURE_WINDOW_START,
        window_end=FIXTURE_WINDOW_END,
        research_data_root=str(tmp_path / "research_data"),
        report_root=str(tmp_path / "reports"),
        in_sample_days=30,
        out_of_sample_days=15,
        step_days=15,
        fixture_events=tuple(events),
        fixture_champion_scores=scores,
        fixture_rs_map=rs,
    )


# ---------------------------------------------------------------------------
# ParameterGrid
# ---------------------------------------------------------------------------


class TestParameterGrid:
    def test_size_and_combos(self):
        g = ParameterGrid({"a": [1, 2, 3], "b": [10, 20]})
        assert g.size() == 6
        combos = g.combos()
        assert len(combos) == 6
        # Deterministic order — axes sorted, then Cartesian
        assert combos[0] == {"a": 1, "b": 10}
        assert combos[1] == {"a": 1, "b": 20}
        assert combos[2] == {"a": 2, "b": 10}

    def test_empty_grid_yields_single_empty_combo(self):
        g = ParameterGrid({})
        assert g.size() == 1
        assert g.combos() == [{}]

    def test_axis_with_no_values_rejected(self):
        with pytest.raises(SweepError, match="non-empty"):
            ParameterGrid({"a": []})

    def test_empty_name_rejected(self):
        with pytest.raises(SweepError, match="axis name"):
            ParameterGrid({"": [1, 2]})

    def test_deterministic_across_insertion_order(self):
        a = ParameterGrid({"a": [1, 2], "b": [10]}).combos()
        b = ParameterGrid({"b": [10], "a": [1, 2]}).combos()
        assert a == b

    def test_to_dict_shape(self):
        g = ParameterGrid({"a": [1, 2]})
        assert g.to_dict() == {"a": [1, 2]}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class TestRunSweep:
    def test_momentum_lookback_sweep(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        grid = ParameterGrid({"lookback": [5, 10, 20]})
        manifest = run_parameter_sweep(
            MomentumStrategy, grid, config,
            manifest_root=str(tmp_path / "sweeps"),
        )
        assert manifest.total == 3
        assert manifest.passed == 3
        assert manifest.failed == 0
        assert manifest.strategy_name == "momentum"
        # Each row carries a bundle with a distinct experiment_id
        ids = {row.bundle.manifest.experiment_id for row in manifest.rows}
        assert len(ids) == 3

    def test_rs_weight_sweep(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        grid = ParameterGrid({
            "overlay_weight": [0.05, 0.1, 0.15, 0.2, 0.25],
        })
        manifest = run_parameter_sweep(
            ChampionRelativeStrengthStrategy, grid, config,
            manifest_root=str(tmp_path / "sweeps"),
        )
        assert manifest.total == 5
        assert manifest.passed == 5

    def test_grid_writes_row_manifests(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        grid = ParameterGrid({"lookback": [5, 10]})
        manifest = run_parameter_sweep(
            MomentumStrategy, grid, config,
            sweep_id="test-sweep",
            manifest_root=str(tmp_path / "sweeps"),
        )
        # Row manifests dropped under <root>/<sweep_id>/rows/
        row_root = tmp_path / "sweeps" / "test-sweep" / "rows"
        assert row_root.is_dir()
        # Sweep manifest sibling
        assert (tmp_path / "sweeps" / "test-sweep" / "sweep.json").is_file()

    def test_disable_manifest_writes(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        grid = ParameterGrid({"lookback": [5]})
        manifest = run_parameter_sweep(
            MomentumStrategy, grid, config,
            sweep_id="quiet-sweep",
            manifest_root=str(tmp_path / "sweeps"),
            write_row_manifests=False,
            write_sweep_manifest=False,
        )
        assert not (tmp_path / "sweeps" / "quiet-sweep").exists()

    def test_manifest_json_roundtrip(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        grid = ParameterGrid({"lookback": [5, 10]})
        manifest = run_parameter_sweep(
            MomentumStrategy, grid, config,
            sweep_id="rt-sweep",
            manifest_root=str(tmp_path / "sweeps"),
        )
        blob = json.loads(
            (tmp_path / "sweeps" / "rt-sweep" / "sweep.json").read_text()
        )
        assert blob["sweep_id"] == "rt-sweep"
        assert blob["strategy_name"] == "momentum"
        assert blob["total"] == 2
        assert blob["grid"] == {"lookback": [5, 10]}


class TestFailureIsolation:
    def test_factory_error_isolated(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)

        def bad_factory(lookback):
            if lookback == 10:
                raise ValueError("boom on 10")
            return MomentumStrategy(lookback=lookback)

        grid = ParameterGrid({"lookback": [5, 10, 20]})
        manifest = run_parameter_sweep(
            bad_factory, grid, config,
            manifest_root=str(tmp_path / "sweeps"),
        )
        assert manifest.total == 3
        assert manifest.passed == 2
        assert manifest.failed == 1
        # The failing row has an error message
        failing = [r for r in manifest.rows if not r.ok]
        assert len(failing) == 1
        assert "boom" in failing[0].error

    def test_no_isolation_reraises(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)

        def bad_factory(lookback):
            raise RuntimeError("always fail")

        grid = ParameterGrid({"lookback": [5]})
        with pytest.raises(RuntimeError, match="always fail"):
            run_parameter_sweep(
                bad_factory, grid, config,
                manifest_root=str(tmp_path / "sweeps"),
                isolate_failures=False,
            )


class TestDeterminism:
    def test_two_sweeps_same_ids(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        grid = ParameterGrid({"lookback": [5, 10]})
        a = run_parameter_sweep(
            MomentumStrategy, grid, config,
            manifest_root=str(tmp_path / "a"),
            generated_at="2026-07-04T00:00:00+00:00",
        )
        b = run_parameter_sweep(
            MomentumStrategy, grid, config,
            manifest_root=str(tmp_path / "b"),
            generated_at="2026-07-04T00:00:00+00:00",
        )
        # Every combo pair yields the same experiment_id
        for row_a, row_b in zip(a.rows, b.rows):
            assert (row_a.bundle.manifest.experiment_id
                    == row_b.bundle.manifest.experiment_id)


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.parameter_sweep")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.parameter_sweep")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.parameter_sweep")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
