"""Tests for strategy/lab/performance_metrics.py — Card 9."""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from strategy.comparison_harness import (
    ChampionChallengerComparison,
    ChampionChallengerRunMetadata,
    ScoreRow,
    ScoreTable,
)
from strategy.config import reset_feature_flags
from strategy.lab import (
    HoldingPeriod,
    PerformanceMetrics,
    build_equity_curve,
    compute_performance_metrics,
)
from strategy.lab.experiment_runner import ExperimentBundle, ExperimentManifest
from strategy.lab.strategy import StrategyIdentity


SYMBOLS = ("AAPL", "MSFT", "NVDA")


# ---------------------------------------------------------------------------
# Synthetic bundle builder — bypass fixture mode
# ---------------------------------------------------------------------------


def _iso_days(count: int, start: str = "2026-05-01"):
    d = date.fromisoformat(start)
    for i in range(count):
        yield f"{(d + timedelta(days=i)).isoformat()}T14:30:00+00:00"


def _score_table(ts, sequence, ranked_symbol, all_symbols=SYMBOLS):
    rows: List[ScoreRow] = []
    for sym in all_symbols:
        is_top = sym == ranked_symbol
        rows.append(ScoreRow(
            symbol=sym,
            champion_score=1.0 if is_top else 0.5,
            challenger_score=1.0 if is_top else 0.5,
            champion_rank=1 if is_top else None,
            challenger_rank=1 if is_top else None,
        ))
    return ScoreTable(
        event_timestamp=ts,
        event_type="market_snapshot",
        event_sequence=sequence,
        champion_id="champion-v0.4.0",
        challenger_id="test-strategy",
        rows=rows,
    )


def _synthetic_bundle(picks: Sequence[str]):
    """Build a minimal ExperimentBundle whose comparison has one
    score table per pick.  ``picks[i]`` is the top-ranked symbol at
    event i.  Use "" for "hold cash".
    """
    timestamps = list(_iso_days(len(picks)))
    tables = [
        _score_table(ts, i + 1, pick or None)
        for i, (ts, pick) in enumerate(zip(timestamps, picks))
    ]
    metadata = ChampionChallengerRunMetadata(
        run_id="rr_synth",
        champion_id="champion-v0.4.0",
        challenger_id="test-strategy",
        dataset_id="synth",
        event_count=len(picks),
        score_delta_threshold=0.0,
        seed=0,
        generated_at="2026-07-04T00:00:00+00:00",
    )
    comparison = ChampionChallengerComparison(
        metadata=metadata,
        score_tables=tables,
        disagreements=[],
        warnings=[],
    )
    validation_bundle = SimpleNamespace(comparison=comparison)
    identity = StrategyIdentity(
        name="test", version="0.1.0",
        parameters={}, stable_hash="h" * 64,
    )
    manifest = SimpleNamespace(
        experiment_id="exp_test",
        strategy=identity,
        dataset_id="synth",
        window_start=timestamps[0][:10],
        window_end=timestamps[-1][:10],
        warnings=(),
    )
    return ExperimentBundle(
        manifest=manifest,
        validation_bundle=validation_bundle,
        manifest_path="",
    )


def _bars_for(symbol_prices: Mapping[str, List[float]], timestamps: Sequence[str]):
    return {
        sym: [
            {"t": ts, "o": p, "h": p + 0.5, "l": p - 0.5,
             "c": p, "v": 1_000_000}
            for ts, p in zip(timestamps, prices)
        ]
        for sym, prices in symbol_prices.items()
    }


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


class TestEquityCurve:
    def test_all_up_produces_positive_equity(self):
        n = 30
        picks = ["AAPL"] * n
        bundle = _synthetic_bundle(picks)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        # AAPL rises 50%; other symbols flat
        bars = _bars_for(
            {"AAPL": [100.0 + i * 1.5 for i in range(n)],
             "MSFT": [100.0] * n, "NVDA": [100.0] * n},
            timestamps,
        )
        holdings, equity = build_equity_curve(bundle, bars)
        assert len(equity) == len(holdings) + 1
        assert equity[-1] > 1.0

    def test_all_down_produces_negative_equity(self):
        n = 30
        picks = ["AAPL"] * n
        bundle = _synthetic_bundle(picks)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {"AAPL": [200.0 - i * 3.0 for i in range(n)],
             "MSFT": [100.0] * n, "NVDA": [100.0] * n},
            timestamps,
        )
        holdings, equity = build_equity_curve(bundle, bars)
        assert equity[-1] < 1.0

    def test_missing_bars_yields_cash_hold(self):
        picks = ["AAPL"] * 10
        bundle = _synthetic_bundle(picks)
        holdings, equity = build_equity_curve(bundle, bars_by_symbol={})
        assert equity[-1] == pytest.approx(1.0)
        assert all(h.period_return == 0.0 for h in holdings)

    def test_invalid_side_rejected(self):
        bundle = _synthetic_bundle(["AAPL"] * 5)
        with pytest.raises(ValueError, match="side"):
            build_equity_curve(bundle, {}, side="bogus")

    def test_top_pick_switches_symbol(self):
        picks = ["AAPL", "AAPL", "MSFT", "MSFT", "NVDA"]
        bundle = _synthetic_bundle(picks)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {"AAPL": [100.0] * 5, "MSFT": [50.0] * 5, "NVDA": [200.0] * 5},
            timestamps,
        )
        holdings, _ = build_equity_curve(bundle, bars)
        # 4 holding periods for 5 events; picks reflect the FROM event
        symbols_held = [h.symbol for h in holdings]
        assert symbols_held == ["AAPL", "AAPL", "MSFT", "MSFT"]


# ---------------------------------------------------------------------------
# Metric shape + math
# ---------------------------------------------------------------------------


class TestMetricComputation:
    def test_metric_block_complete(self):
        n = 30
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {sym: [100.0 + i * 0.5 for i in range(n)] for sym in SYMBOLS},
            timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        for field in (
            "cagr", "annualized_return", "sharpe", "sortino", "calmar",
            "max_drawdown", "ulcer_index", "win_rate", "avg_gain",
            "avg_loss", "profit_factor", "exposure", "turnover",
        ):
            value = getattr(metrics, field)
            assert isinstance(value, float)
            assert not math.isnan(value)

    def test_all_up_returns_positive_sharpe(self):
        n = 60
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        # AAPL rises 0.5% per day; other symbols irrelevant
        bars = _bars_for(
            {"AAPL": [100.0 * (1.005 ** i) for i in range(n)],
             "MSFT": [100.0] * n, "NVDA": [100.0] * n},
            timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        assert metrics.sharpe > 0
        assert metrics.win_rate > 0.9  # near-monotonic uptrend
        assert metrics.max_drawdown >= 0.0

    def test_max_drawdown_captured(self):
        n = 20
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        # Rise 100 → 150 over first 5 events, then crash to 90
        # over the next 5, then recover to 110.
        prices = (
            [100.0, 110.0, 120.0, 140.0, 150.0]
            + [140.0, 120.0, 100.0, 95.0, 90.0]
            + [95.0, 100.0, 105.0, 108.0, 110.0]
            + [110.0] * (n - 15)
        )
        prices = prices[:n]
        bars = _bars_for(
            {"AAPL": prices,
             "MSFT": [100.0] * n, "NVDA": [100.0] * n},
            timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        # Peak equity at price 150 (index 4); trough at 90 (index 9).
        # Equity drawdown = (150 - 90) / 150 = 0.40
        assert metrics.max_drawdown > 0.30

    def test_determinism(self):
        n = 30
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {sym: [100.0 + i * 0.5 for i in range(n)] for sym in SYMBOLS},
            timestamps,
        )
        a = compute_performance_metrics(bundle, bars)
        b = compute_performance_metrics(bundle, bars)
        assert a.to_dict() == b.to_dict()

    def test_leaderboard_kwargs_subset(self):
        bundle = _synthetic_bundle(["AAPL"] * 10)
        metrics = compute_performance_metrics(bundle, {})
        kwargs = metrics.to_leaderboard_kwargs()
        assert set(kwargs) == {
            "cagr", "sharpe", "sortino", "calmar",
            "max_drawdown", "win_rate", "profit_factor",
        }

    def test_turnover_zero_on_hold(self):
        # Always pick same symbol -> 1 trade total, high turnover
        # denominator issue is handled: one entry + one hold.
        n = 20
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {"AAPL": [100.0] * n, "MSFT": [100.0] * n, "NVDA": [100.0] * n},
            timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        assert metrics.turnover < 0.2  # at most one trade
        assert metrics.exposure == 1.0

    def test_switching_symbols_increases_turnover(self):
        # Alternate picks: AAPL, MSFT, AAPL, MSFT ... = many trades
        picks = ["AAPL", "MSFT"] * 10
        bundle = _synthetic_bundle(picks)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {sym: [100.0] * len(picks) for sym in SYMBOLS},
            timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        assert metrics.turnover > 0.5


class TestNoTrades:
    def test_zero_returns_produce_zero_sharpe(self):
        n = 30
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {sym: [100.0] * n for sym in SYMBOLS}, timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        assert metrics.sharpe == 0.0
        assert metrics.sortino == 0.0
        assert metrics.max_drawdown == 0.0


class TestLeaderboardIntegration:
    def test_metrics_backfill_leaderboard_entry(self):
        from strategy.lab import entry_from_bundle
        n = 20
        bundle = _synthetic_bundle(["AAPL"] * n)
        timestamps = [t.event_timestamp
                      for t in bundle.validation_bundle.comparison.score_tables]
        bars = _bars_for(
            {sym: [100.0 + i * 0.5 for i in range(n)] for sym in SYMBOLS},
            timestamps,
        )
        metrics = compute_performance_metrics(bundle, bars)
        entry = entry_from_bundle(bundle,
                                  equity_metrics=metrics.to_leaderboard_kwargs())
        assert entry.sharpe is not None
        assert entry.max_drawdown is not None
        assert entry.cagr is not None
        assert entry.win_rate is not None


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.performance_metrics")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.performance_metrics")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.performance_metrics")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
