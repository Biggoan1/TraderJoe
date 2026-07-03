"""Tests for strategy/champion_scoring.py."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.champion_scoring import (
    ChampionScorer,
    ChampionScoringConfig,
    RESEARCH_CHAMPION_STRATEGY_ID,
    score_bars_for_event,
)


def _bars(prices, volumes=None, high_pct=1.005, low_pct=0.995):
    volumes = volumes if volumes is not None else [1_000_000] * len(prices)
    out: List[Dict[str, float]] = []
    start = date(2026, 5, 1)
    for i, price in enumerate(prices):
        t = f"{(start + timedelta(days=i)).isoformat()}T14:30:00+00:00"
        out.append(
            {
                "t": t,
                "o": price,
                "h": price * high_pct,
                "l": price * low_pct,
                "c": price,
                "v": volumes[i],
            }
        )
    return out


def _uptrend_bars(days: int = 60, ratio: float = 1.01):
    prices = [100.0]
    for _ in range(days - 1):
        prices.append(prices[-1] * ratio)
    return _bars(prices)


def _flat_bars(days: int = 60, price: float = 100.0):
    return _bars([price] * days)


def _downtrend_bars(days: int = 60, ratio: float = 0.99):
    prices = [100.0]
    for _ in range(days - 1):
        prices.append(prices[-1] * ratio)
    return _bars(prices)


class TestScoreBarsForEvent:
    def test_insufficient_history_marks_rejected(self):
        bars = _uptrend_bars(days=10)
        score, exp = score_bars_for_event(
            bars,
            strategy_id="c",
            symbol="AAPL",
            event_timestamp="2026-05-10",
        )
        assert score == 0.0
        assert exp.rejected is True
        assert any(
            "insufficient_history" in reason for reason in exp.rejection_reasons
        )

    def test_uptrend_fires_multiple_gates(self):
        bars = _uptrend_bars(days=60)
        score, exp = score_bars_for_event(
            bars,
            strategy_id="c",
            symbol="AAPL",
            event_timestamp=bars[-1]["t"],
        )
        # A steady 1%/day uptrend should fire above_sma20, sma20>sma50,
        # macd_positive, and adx_strength.  Depending on the exact RSI
        # value it may or may not fire rsi_not_overbought.
        component_names = {c.name for c in exp.components}
        assert "above_sma20" in component_names
        assert "sma20_above_sma50" in component_names
        assert "macd_positive" in component_names
        assert "adx_strength" in component_names
        assert exp.rejected is False
        assert exp.confidence is not None and exp.confidence >= 0.5
        assert score > 0

    def test_downtrend_fails_trend_gates(self):
        bars = _downtrend_bars(days=60)
        _, exp = score_bars_for_event(
            bars,
            strategy_id="c",
            symbol="XYZ",
            event_timestamp=bars[-1]["t"],
        )
        component_names = {c.name for c in exp.components}
        # In a strict downtrend the trend gates must NOT fire
        assert "above_sma20" not in component_names
        assert "sma20_above_sma50" not in component_names

    def test_all_components_sum_to_final_score(self):
        bars = _uptrend_bars(days=60)
        score, exp = score_bars_for_event(
            bars,
            strategy_id="c",
            symbol="AAPL",
            event_timestamp=bars[-1]["t"],
        )
        component_sum = sum(c.contribution for c in exp.components)
        bonus_sum = sum(m for _, m in exp.bonuses)
        assert exp.final_score == pytest.approx(
            component_sum + bonus_sum, abs=1e-6
        )

    def test_deterministic_output(self):
        bars = _uptrend_bars(days=60)
        a_score, a_exp = score_bars_for_event(
            bars, strategy_id="c", symbol="AAPL", event_timestamp="t"
        )
        b_score, b_exp = score_bars_for_event(
            bars, strategy_id="c", symbol="AAPL", event_timestamp="t"
        )
        assert a_score == b_score
        assert a_exp.stable_hash() == b_exp.stable_hash()

    def test_explanation_carries_ranking_factors(self):
        bars = _uptrend_bars(days=60)
        _, exp = score_bars_for_event(
            bars, strategy_id="c", symbol="AAPL", event_timestamp="t"
        )
        # Ranking factors are the component names in insertion order
        assert list(exp.ranking_factors) == [c.name for c in exp.components]

    def test_momentum_contribution_present_when_adx_strong(self):
        bars = _uptrend_bars(days=60)
        _, exp = score_bars_for_event(
            bars, strategy_id="c", symbol="AAPL", event_timestamp="t"
        )
        assert exp.momentum_contribution is not None
        assert exp.momentum_contribution > 0


class TestChampionScorerEvaluator:
    def test_evaluates_per_symbol_at_event(self):
        bars_by_symbol = {
            "AAPL": _uptrend_bars(days=60),
            "MSFT": _flat_bars(days=60),
            "NVDA": _downtrend_bars(days=60),
        }
        scorer = ChampionScorer(
            strategy_id=RESEARCH_CHAMPION_STRATEGY_ID,
            bars_by_symbol=bars_by_symbol,
            symbols=("AAPL", "MSFT", "NVDA"),
        )
        event = BacktestEvent(
            timestamp=bars_by_symbol["AAPL"][-1]["t"],
            event_type="market_snapshot",
            sequence=1,
        )
        evaluation = scorer.evaluate(event)
        assert evaluation.strategy_id == RESEARCH_CHAMPION_STRATEGY_ID
        assert set(evaluation.scores) == {"AAPL", "MSFT", "NVDA"}
        # Structured explanations populated for every symbol
        assert set(evaluation.structured_explanations) == {"AAPL", "MSFT", "NVDA"}
        # AAPL should outscore NVDA in an uptrend vs downtrend regime
        assert evaluation.scores["AAPL"] > evaluation.scores["NVDA"]
        # Free-text explanations mirror the structured summary
        for symbol in evaluation.structured_explanations:
            summary = evaluation.structured_explanations[symbol].to_summary_str()
            assert evaluation.explanations[symbol] == summary

    def test_rankings_only_include_non_rejected(self):
        bars_by_symbol = {
            "AAPL": _uptrend_bars(days=60),
            "XYZ": _flat_bars(days=10),  # too short
        }
        scorer = ChampionScorer(
            strategy_id="c",
            bars_by_symbol=bars_by_symbol,
            symbols=("AAPL", "XYZ"),
        )
        event = BacktestEvent(
            timestamp=bars_by_symbol["AAPL"][-1]["t"],
            event_type="market_snapshot",
            sequence=1,
        )
        evaluation = scorer.evaluate(event)
        ranked_symbols = {r["symbol"] for r in evaluation.rankings}
        assert "AAPL" in ranked_symbols
        # XYZ is rejected -> excluded from rankings
        assert "XYZ" not in ranked_symbols

    def test_evaluate_survives_missing_symbol(self):
        # Symbol declared but no bars supplied — expect a blank
        # explanation, no crash, symbol excluded from rankings.
        scorer = ChampionScorer(
            strategy_id="c",
            bars_by_symbol={"AAPL": _uptrend_bars(days=60)},
            symbols=("AAPL", "MISSING"),
        )
        event = BacktestEvent(
            timestamp="2026-06-30T14:30:00+00:00",
            event_type="market_snapshot",
            sequence=1,
        )
        evaluation = scorer.evaluate(event)
        assert "MISSING" in evaluation.structured_explanations
        assert "MISSING" not in evaluation.scores
        ranked = {r["symbol"] for r in evaluation.rankings}
        assert "MISSING" not in ranked

    def test_replay_is_byte_identical(self):
        # The same bars + same event -> byte-identical output.
        bars_by_symbol = {
            "AAPL": _uptrend_bars(days=60),
            "NVDA": _downtrend_bars(days=60),
        }
        scorer1 = ChampionScorer(
            strategy_id="c", bars_by_symbol=bars_by_symbol,
            symbols=("AAPL", "NVDA"),
        )
        scorer2 = ChampionScorer(
            strategy_id="c", bars_by_symbol=bars_by_symbol,
            symbols=("AAPL", "NVDA"),
        )
        event = BacktestEvent(
            timestamp=bars_by_symbol["AAPL"][-1]["t"],
            event_type="market_snapshot",
            sequence=1,
        )
        eval_a = scorer1.evaluate(event)
        eval_b = scorer2.evaluate(event)
        assert eval_a.to_dict() == eval_b.to_dict()

    def test_evaluate_slices_bars_up_to_event_timestamp(self):
        bars = _uptrend_bars(days=60)
        cutoff = bars[40]["t"]
        scorer = ChampionScorer(
            strategy_id="c",
            bars_by_symbol={"AAPL": bars},
            symbols=("AAPL",),
        )
        event = BacktestEvent(
            timestamp=cutoff, event_type="market_snapshot", sequence=1
        )
        evaluation = scorer.evaluate(event)
        exp = evaluation.structured_explanations["AAPL"]
        assert exp.event_timestamp == cutoff


class TestChampionScoringConfig:
    def test_min_history_reflects_longest_indicator_window(self):
        cfg = ChampionScoringConfig()
        # SMA(50) needs 51 bars; MACD needs slow + signal = 35;
        # ADX needs 2*14+1 = 29; final requirement is max of these.
        assert cfg.min_history_required() >= 51

    def test_custom_config_shifts_min_history(self):
        cfg = ChampionScoringConfig(sma_long=200)
        assert cfg.min_history_required() >= 201
