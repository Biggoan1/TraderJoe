"""Tests for strategy/lab/leaderboard.py — Card 4."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.config import reset_feature_flags
from strategy.historical_validation import HistoricalValidationConfig
from strategy.lab import (
    Leaderboard,
    LeaderboardEntry,
    compute_score_metrics,
    entry_from_bundle,
    rank_leaderboard,
    run_strategy_experiment,
    write_leaderboard,
)
from strategy.lab.champion import ChampionStrategy
from strategy.lab.momentum import MomentumStrategy
from strategy.lab.strategy import StrategyIdentity


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


def _make_scores(events):
    return {
        e.timestamp: {sym: 0.5 + 0.001 * i + 0.01 * j
                      for j, sym in enumerate(FIXTURE_SYMBOLS)}
        for i, e in enumerate(events)
    }


def _make_rs(events):
    return {
        e.timestamp: {"AAPL": 70.0, "MSFT": 55.0, "NVDA": 60.0}
        for e in events
    }


def _make_config(tmp_path, **overrides) -> HistoricalValidationConfig:
    events = _make_events()
    base = dict(
        dataset_id="leaderboard-fixture",
        symbols=FIXTURE_SYMBOLS,
        window_start=FIXTURE_WINDOW_START,
        window_end=FIXTURE_WINDOW_END,
        research_data_root=str(tmp_path / "research_data"),
        report_root=str(tmp_path / "reports"),
        in_sample_days=30,
        out_of_sample_days=15,
        step_days=15,
        fixture_events=tuple(events),
        fixture_champion_scores=_make_scores(events),
        fixture_rs_map=_make_rs(events),
    )
    base.update(overrides)
    return HistoricalValidationConfig(**base)


# ---------------------------------------------------------------------------
# Score metrics
# ---------------------------------------------------------------------------


class TestScoreMetrics:
    def test_compute_score_metrics_shape(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle = run_strategy_experiment(
            config, ChampionStrategy(),
            manifest_root=str(tmp_path / "experiments"),
        )
        metrics = compute_score_metrics(bundle)
        assert set(metrics.keys()) == {
            "total_events", "total_disagreements", "disagreement_rate",
            "mean_score_delta", "max_abs_score_delta",
            "selection_agreement_rate",
        }
        # Score-based metrics have valid ranges
        assert metrics["total_events"] > 0
        assert 0.0 <= metrics["disagreement_rate"] <= 1.0
        assert 0.0 <= metrics["selection_agreement_rate"] <= 1.0
        assert metrics["max_abs_score_delta"] >= 0.0

    def test_score_metrics_deterministic(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        b1 = run_strategy_experiment(config, ChampionStrategy(),
                                     manifest_root=str(tmp_path / "a"))
        b2 = run_strategy_experiment(config, ChampionStrategy(),
                                     manifest_root=str(tmp_path / "b"))
        assert compute_score_metrics(b1) == compute_score_metrics(b2)


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


class TestEntryFromBundle:
    def test_entry_populates_score_fields(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle = run_strategy_experiment(
            config, ChampionStrategy(),
            manifest_root=str(tmp_path / "experiments"),
        )
        entry = entry_from_bundle(bundle)
        assert entry.experiment_id == bundle.manifest.experiment_id
        assert entry.strategy.name == "champion"
        assert entry.sharpe is None
        assert entry.max_drawdown is None
        # In fixture mode the pipeline Champion returns fixture
        # scores while the injected ChampionStrategy runs the real
        # scorer on empty bars — some disagreement expected.
        assert 0.0 <= entry.selection_agreement_rate <= 1.0

    def test_entry_accepts_equity_metrics(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle = run_strategy_experiment(
            config, ChampionStrategy(),
            manifest_root=str(tmp_path / "experiments"),
        )
        entry = entry_from_bundle(bundle, equity_metrics={
            "sharpe": 1.5, "sortino": 2.1, "max_drawdown": -0.12,
            "cagr": 0.18, "win_rate": 0.55, "profit_factor": 1.35,
            "calmar": 1.4,
        })
        assert entry.sharpe == 1.5
        assert entry.sortino == 2.1
        assert entry.calmar == 1.4


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _entry(name, sharpe, sortino=None, max_dd=None, disagree=0):
    identity = StrategyIdentity(
        name=name, version="0.1.0", parameters={}, stable_hash="h" * 64,
    )
    return LeaderboardEntry(
        experiment_id=f"exp_{name}",
        strategy=identity,
        dataset_id="ds",
        window_start="2020-01-01",
        window_end="2020-12-31",
        total_events=100,
        total_disagreements=disagree,
        disagreement_rate=disagree / 100,
        mean_score_delta=0.0,
        max_abs_score_delta=0.0,
        selection_agreement_rate=1.0,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
    )


class TestRanking:
    def test_rank_by_sharpe_descending(self):
        entries = [
            _entry("low", 0.5),
            _entry("high", 2.0),
            _entry("mid", 1.0),
        ]
        lb = rank_leaderboard(entries, primary_metric="sharpe")
        assert [e.strategy.name for e in lb.entries] == ["high", "mid", "low"]

    def test_rank_by_max_drawdown_ascending(self):
        # Smaller (more negative) drawdown is WORSE for ordering;
        # closer to zero is better.  Sort ascending by numeric value
        # means larger (closer-to-zero) values come first.  Wait —
        # ascending means -0.20 < -0.10; the sort puts -0.20 first.
        # That's the WRONG direction for "better strategy first."
        # Our leaderboard treats max_drawdown as "ascending is
        # better" — closer to zero (0.0 > -0.10 > -0.20) is better,
        # so we want DESCENDING numerically.  The implementation
        # currently sorts ascending — which is fine if max_drawdown
        # is stored as a POSITIVE magnitude (0.05 = 5% dd, 0.20 =
        # 20% dd).  This test enforces the positive-magnitude
        # convention.
        entries = [
            _entry("bad", 1.0, max_dd=0.30),
            _entry("good", 1.0, max_dd=0.05),
            _entry("mid", 1.0, max_dd=0.15),
        ]
        lb = rank_leaderboard(entries, primary_metric="max_drawdown")
        assert [e.strategy.name for e in lb.entries] == ["good", "mid", "bad"]

    def test_missing_metric_sinks_to_bottom(self):
        entries = [
            _entry("known", 1.0),
            _entry("unknown", None),
        ]
        lb = rank_leaderboard(entries, primary_metric="sharpe")
        # 'unknown' (sharpe=None) sinks to bottom
        assert lb.entries[-1].strategy.name == "unknown"

    def test_empty_primary_metric_rejected(self):
        with pytest.raises(ValueError, match="primary_metric"):
            rank_leaderboard([], primary_metric="")


# ---------------------------------------------------------------------------
# Write + roundtrip
# ---------------------------------------------------------------------------


class TestWriteLeaderboard:
    def test_writes_json_and_markdown(self, tmp_path):
        entries = [_entry("a", 1.0), _entry("b", 2.0)]
        lb = rank_leaderboard(entries, primary_metric="sharpe")
        paths = write_leaderboard(lb, tmp_path)
        assert paths["json"].is_file()
        assert paths["markdown"].is_file()
        loaded = json.loads(paths["json"].read_text())
        assert loaded["primary_metric"] == "sharpe"
        assert loaded["entry_count"] == 2

    def test_markdown_contains_rank_and_strategy_names(self, tmp_path):
        entries = [_entry("apple", 2.0), _entry("banana", 1.0)]
        lb = rank_leaderboard(entries, primary_metric="sharpe")
        text = lb.to_markdown()
        assert "| 1 |" in text
        assert "| 2 |" in text
        assert "apple v0.1.0" in text
        assert "banana v0.1.0" in text

    def test_markdown_shows_na_for_missing_metrics(self, tmp_path):
        entries = [_entry("noeq", None)]
        lb = rank_leaderboard(entries, primary_metric="mean_score_delta")
        text = lb.to_markdown()
        assert "n/a" in text


# ---------------------------------------------------------------------------
# End-to-end: two strategies -> two entries -> leaderboard
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_two_strategies_yield_two_leaderboard_rows(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle_a = run_strategy_experiment(
            config, ChampionStrategy(),
            manifest_root=str(tmp_path / "a"),
        )
        bundle_b = run_strategy_experiment(
            config, MomentumStrategy(lookback=5),
            manifest_root=str(tmp_path / "b"),
        )
        entries = [entry_from_bundle(bundle_a), entry_from_bundle(bundle_b)]
        lb = rank_leaderboard(entries, primary_metric="mean_score_delta")
        assert len(lb.entries) == 2
        # Both have unique experiment_ids
        assert (
            lb.entries[0].experiment_id != lb.entries[1].experiment_id
        )


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.leaderboard")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.leaderboard")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.leaderboard")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
