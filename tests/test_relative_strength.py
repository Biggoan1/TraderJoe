"""Tests for strategy/relative_strength.py

Covers:
  - RelativeStrengthCalculator (all public methods)
  - PriceFetcher
  - Percentile ranking
  - Trend direction
  - RS score calculation
  - Formatting functions
  - Edge cases (empty data, single symbol, missing benchmarks)
"""

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from strategy.relative_strength import (
    DEFAULT_BENCHMARKS,
    DEFAULT_LOOKBACK_PERIODS,
    PriceFetcher,
    RelativeStrengthCalculator,
    RelativeStrengthResult,
    calculate_watchlist_rs,
    format_rs_result,
    format_rs_summary,
    get_calculator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_close_df(prices, symbol: str = "TEST") -> pd.DataFrame:
    """Create a simple Close price DataFrame from a list of prices."""
    dates = pd.date_range("2025-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"Close": [float(p) for p in prices]}, index=dates)


def _mock_fetch(symbol: str, price_map: Dict[str, List[float]], period: str = "6mo"):
    """Return a DataFrame for the given symbol from the price_map."""
    prices = price_map.get(symbol)
    if prices is None:
        return None
    return _make_close_df(prices, symbol)


# ---------------------------------------------------------------------------
# RelativeStrengthResult tests
# ---------------------------------------------------------------------------

class TestRelativeStrengthResult:
    def test_default_values(self):
        r = RelativeStrengthResult(symbol="AAPL")
        assert r.symbol == "AAPL"
        assert r.rs_vs_benchmark == {}
        assert r.percentile_rank == {}
        assert r.trend_direction == "stable"
        assert r.rs_score == 50.0

    def test_to_dict(self):
        r = RelativeStrengthResult(
            symbol="AAPL",
            rs_vs_benchmark={"SPY": {"5d": 2.5, "20d": -1.3}},
            percentile_rank={"SPY": {"5d": 75.0, "20d": 40.0}},
            trend_direction="improving",
            rs_score=72.3,
        )
        d = r.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["rs_vs_benchmark"]["SPY"]["5d"] == 2.5
        assert d["percentile_rank"]["SPY"]["5d"] == 75.0
        assert d["trend_direction"] == "improving"
        assert d["rs_score"] == 72.3

    def test_to_dict_empty(self):
        r = RelativeStrengthResult(symbol="NOPE")
        d = r.to_dict()
        assert d["symbol"] == "NOPE"
        assert d["rs_vs_benchmark"] == {}
        assert d["rs_score"] == 50.0


# ---------------------------------------------------------------------------
# PriceFetcher tests
# ---------------------------------------------------------------------------

class TestPriceFetcher:
    @patch("strategy.relative_strength.yf.Ticker")
    def test_fetch_returns_data(self, mock_ticker_cls):
        df = pd.DataFrame({"Close": [100, 101, 102]}, index=pd.date_range("2025-01-01", periods=3))
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch("AAPL")
        assert result is not None
        assert len(result) == 3
        assert "Close" in result.columns

    @patch("strategy.relative_strength.yf.Ticker")
    def test_fetch_empty_data(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch("INVALID")
        assert result is None

    @patch("strategy.relative_strength.yf.Ticker")
    def test_fetch_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = Exception("Network error")
        result = PriceFetcher.fetch("ERR")
        assert result is None

    @patch("strategy.relative_strength.yf.Ticker")
    def test_fetch_multiindex_columns(self, mock_ticker_cls):
        # Simulate yfinance MultiIndex columns
        cols = pd.MultiIndex.from_tuples([("Close", "adj")], names=["level0", "level1"])
        df = pd.DataFrame({"Close": [100, 101]}, columns=cols, index=pd.date_range("2025-01-01", periods=2))
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch("MULTI")
        assert result is not None
        assert "Close" in result.columns


# ---------------------------------------------------------------------------
# RelativeStrengthCalculator tests
# ---------------------------------------------------------------------------

class TestRelativeStrengthCalculator:
    def test_default_config(self):
        calc = RelativeStrengthCalculator()
        assert calc.lookback_periods == [5, 20, 60]
        assert calc.benchmarks == ["SPY", "QQQ"]

    def test_custom_config(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[10, 30],
            benchmarks=["SPY"],
        )
        assert calc.lookback_periods == [10, 30]
        assert calc.benchmarks == ["SPY"]

    def test_calculate_watchlist_rs_no_data(self):
        calc = RelativeStrengthCalculator()
        with patch.object(calc, "_fetch_prices", return_value={}):
            results = calc.calculate_watchlist_rs(["AAPL", "MSFT"])
            assert results == []

    def test_calculate_watchlist_rs_basic(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20],
            benchmarks=["SPY"],
        )

        # Symbol outperforms SPY: symbol +5% over 5d, SPY +3%
        # 60 days of data
        spy_prices = list(range(100, 160))  # 100->159 = +59%
        # Symbol starts at 100, ends at 165 (+65%) — outperforms by 6%
        sym_prices = list(range(100, 161)) + [165]

        # Pad to same length
        while len(spy_prices) < len(sym_prices):
            spy_prices.append(spy_prices[-1])

        price_map = {
            "AAPL": _make_close_df(sym_prices),
            "SPY": _make_close_df(spy_prices),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            results = calc.calculate_watchlist_rs(["AAPL"])

        assert len(results) == 1
        assert results[0].symbol == "AAPL"
        assert "SPY" in results[0].rs_vs_benchmark

    def test_relative_return_calculation(self):
        # Symbol: 100 -> 105 = +5%
        # Bench:  100 -> 103 = +3%
        # Relative = 2.0
        sym_df = _make_close_df([100, 101, 102, 103, 104, 105])
        bench_df = _make_close_df([100, 100.5, 101, 101.5, 102, 103])

        result = RelativeStrengthCalculator._relative_return(sym_df, bench_df, 5)
        assert result is not None
        # symbol return = (105-100)/100 * 100 = 5.0%
        # bench return = (103-100)/100 * 100 = 3.0%
        # diff = 2.0%
        assert abs(result - 2.0) < 0.01

    def test_relative_return_underperform(self):
        # Symbol: 100 -> 98 = -2%
        # Bench:  100 -> 101 = +1%
        # Relative = -3.0
        sym_df = _make_close_df([100, 99, 99, 98.5, 98.2, 98])
        bench_df = _make_close_df([100, 100.2, 100.4, 100.6, 100.8, 101])

        result = RelativeStrengthCalculator._relative_return(sym_df, bench_df, 5)
        assert result is not None
        # symbol return = (98-100)/100 * 100 = -2.0%
        # bench return = (101-100)/100 * 100 = 1.0%
        # diff = -3.0%
        assert abs(result - (-3.0)) < 0.01

    def test_relative_return_insufficient_data(self):
        sym_df = _make_close_df([100, 101])
        bench_df = _make_close_df([100, 101])

        result = RelativeStrengthCalculator._relative_return(sym_df, bench_df, 20)
        assert result is None

    def test_relative_return_zero_start(self):
        sym_df = _make_close_df([0, 1, 2, 3, 4, 5])
        bench_df = _make_close_df([100, 101, 102, 103, 104, 105])

        result = RelativeStrengthCalculator._relative_return(sym_df, bench_df, 5)
        assert result is None

    def test_calculate_single_rs(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        sym_prices = list(range(100, 156))
        spy_prices = list(range(100, 151))

        price_map = {
            "AAPL": _make_close_df(sym_prices),
            "SPY": _make_close_df(spy_prices),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            result = calc.calculate_single_rs("AAPL")

        assert result is not None
        assert result.symbol == "AAPL"

    def test_calculate_single_rs_no_data(self):
        calc = RelativeStrengthCalculator()
        with patch.object(calc, "_fetch_prices", return_value={}):
            result = calc.calculate_single_rs("INVALID")
        assert result is None

    def test_get_rs_snapshot(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        sym_prices = list(range(100, 156))
        spy_prices = list(range(100, 151))

        price_map = {
            "AAPL": _make_close_df(sym_prices),
            "SPY": _make_close_df(spy_prices),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            snapshot = calc.get_rs_snapshot("AAPL")

        assert snapshot is not None
        assert "symbol" in snapshot
        assert "rs_vs_benchmark" in snapshot
        assert "rs_score" in snapshot
        # Should be JSON-serializable
        json.dumps(snapshot)

    def test_get_rs_snapshot_no_data(self):
        calc = RelativeStrengthCalculator()
        with patch.object(calc, "_fetch_prices", return_value={}):
            snapshot = calc.get_rs_snapshot("INVALID")
        assert snapshot is None


# ---------------------------------------------------------------------------
# Percentile ranking tests
# ---------------------------------------------------------------------------

class TestPercentileRanking:
    def _make_result(self, symbol: str, rs_values: Dict[str, Dict[str, float]]) -> RelativeStrengthResult:
        return RelativeStrengthResult(
            symbol=symbol,
            rs_vs_benchmark=rs_values,
        )

    def test_percentile_all_same_benchmark(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        results = [
            self._make_result("A", {"SPY": {"5d": 1.0}}),
            self._make_result("B", {"SPY": {"5d": 3.0}}),
            self._make_result("C", {"SPY": {"5d": 2.0}}),
        ]

        calc._calculate_percentiles(results)

        # B is highest (100th), C is middle (50th), A is lowest (0th)
        b_pct = results[1].percentile_rank["SPY"]["5d"]
        c_pct = results[2].percentile_rank["SPY"]["5d"]
        a_pct = results[0].percentile_rank["SPY"]["5d"]

        assert b_pct == 100.0
        assert c_pct == 50.0
        assert a_pct == 0.0

    def test_percentile_ties(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        results = [
            self._make_result("A", {"SPY": {"5d": 2.0}}),
            self._make_result("B", {"SPY": {"5d": 2.0}}),
            self._make_result("C", {"SPY": {"5d": 2.0}}),
        ]

        calc._calculate_percentiles(results)

        # All tied — they get sorted positions but same value
        for r in results:
            assert r.percentile_rank["SPY"]["5d"] >= 0
            assert r.percentile_rank["SPY"]["5d"] <= 100

    def test_percentile_single_symbol(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        results = [
            self._make_result("A", {"SPY": {"5d": 5.0}}),
        ]

        calc._calculate_percentiles(results)

        # Solo symbol gets 50th percentile
        assert results[0].percentile_rank["SPY"]["5d"] == 50.0

    def test_percentile_empty_results(self):
        calc = RelativeStrengthCalculator()
        calc._calculate_percentiles([])  # Should not raise

    def test_percentile_missing_benchmark(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY", "QQQ"],
        )

        results = [
            self._make_result("A", {"SPY": {"5d": 1.0}}),  # No QQQ data
            self._make_result("B", {"SPY": {"5d": 3.0}}),
        ]

        calc._calculate_percentiles(results)

        # SPY percentile should be calculated, QQQ should be skipped
        assert "SPY" in results[0].percentile_rank
        assert results[0].percentile_rank["SPY"]["5d"] == 0.0


# ---------------------------------------------------------------------------
# Trend direction tests
# ---------------------------------------------------------------------------

class TestTrendDirection:
    def _make_result(self, rs_values: Dict[str, Dict[str, float]]) -> RelativeStrengthResult:
        return RelativeStrengthResult(
            symbol="TEST",
            rs_vs_benchmark=rs_values,
            percentile_rank=rs_values,  # Doesn't matter for trend
        )

    def test_improving_trend(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20, 60],
            benchmarks=["SPY"],
        )

        # 5d RS much higher than 60d RS
        result = self._make_result({"SPY": {"5d": 5.0, "20d": 3.0, "60d": 0.5}})
        calc._calculate_trend(result)
        assert result.trend_direction == "improving"

    def test_declining_trend(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20, 60],
            benchmarks=["SPY"],
        )

        # 5d RS much lower than 60d RS
        result = self._make_result({"SPY": {"5d": -3.0, "20d": -1.0, "60d": 4.0}})
        calc._calculate_trend(result)
        assert result.trend_direction == "declining"

    def test_stable_trend(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20, 60],
            benchmarks=["SPY"],
        )

        # 5d and 60d RS close together
        result = self._make_result({"SPY": {"5d": 2.0, "20d": 2.5, "60d": 1.8}})
        calc._calculate_trend(result)
        assert result.trend_direction == "stable"

    def test_stable_no_data(self):
        calc = RelativeStrengthCalculator()
        result = RelativeStrengthResult(symbol="NOPE")
        calc._calculate_trend(result)
        assert result.trend_direction == "stable"

    def test_trend_fallback_20d_for_60d(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20],
            benchmarks=["SPY"],
        )

        # Only 5d and 20d available — 20d serves as long-term fallback
        result = self._make_result({"SPY": {"5d": 5.0, "20d": 0.5}})
        calc._calculate_trend(result)
        assert result.trend_direction == "improving"

    def test_trend_multiple_benchmarks(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 60],
            benchmarks=["SPY", "QQQ"],
        )

        # Improving across both benchmarks
        result = self._make_result({
            "SPY": {"5d": 4.0, "60d": 1.0},
            "QQQ": {"5d": 3.0, "60d": 0.5},
        })
        calc._calculate_trend(result)
        assert result.trend_direction == "improving"


# ---------------------------------------------------------------------------
# RS Score tests
# ---------------------------------------------------------------------------

class TestRScore:
    def _make_result(self, rs_values: Dict[str, Dict[str, float]],
                     pct_values: Optional[Dict[str, Dict[str, float]]] = None) -> RelativeStrengthResult:
        return RelativeStrengthResult(
            symbol="TEST",
            rs_vs_benchmark=rs_values,
            percentile_rank=pct_values or rs_values,
        )

    def test_score_strong_outperformance(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20, 60],
            benchmarks=["SPY"],
        )

        # Strong positive RS values and high percentiles
        result = self._make_result(
            rs_values={"SPY": {"5d": 8.0, "20d": 6.0, "60d": 5.0}},
            pct_values={"SPY": {"5d": 90.0, "20d": 85.0, "60d": 80.0}},
        )
        calc._calculate_score(result)
        # RS component: avg 6.33 -> 25 + 6.33*2.5 = 40.83 (capped at 50)
        # Pct component: avg 85/100*50 = 42.5
        # Total: ~83
        assert result.rs_score > 60

    def test_score_poor_performance(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20, 60],
            benchmarks=["SPY"],
        )

        result = self._make_result(
            rs_values={"SPY": {"5d": -8.0, "20d": -6.0, "60d": -5.0}},
            pct_values={"SPY": {"5d": 10.0, "20d": 15.0, "60d": 20.0}},
        )
        calc._calculate_score(result)
        # RS component: avg -6.33 -> 25 + (-6.33)*2.5 = 9.17
        # Pct component: avg 15/100*50 = 7.5
        # Total: ~17
        assert result.rs_score < 40

    def test_score_neutral(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20, 60],
            benchmarks=["SPY"],
        )

        result = self._make_result(
            rs_values={"SPY": {"5d": 0.0, "20d": 0.0, "60d": 0.0}},
            pct_values={"SPY": {"5d": 50.0, "20d": 50.0, "60d": 50.0}},
        )
        calc._calculate_score(result)
        # RS component: 25 + 0*2.5 = 25
        # Pct component: 50/100*50 = 25
        # Total: 50
        assert abs(result.rs_score - 50.0) < 0.1

    def test_score_no_data(self):
        calc = RelativeStrengthCalculator()
        result = RelativeStrengthResult(symbol="NOPE")
        calc._calculate_score(result)
        assert result.rs_score == 50.0

    def test_score_clamped(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        # Extreme positive RS
        result = self._make_result(
            rs_values={"SPY": {"5d": 50.0}},
            pct_values={"SPY": {"5d": 100.0}},
        )
        calc._calculate_score(result)
        # RS component: 25 + 50*2.5 = 150 -> capped at 50
        # Pct component: 100/100*50 = 50
        # Total: 100
        assert result.rs_score <= 100.0

    def test_score_clamped_negative(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        result = self._make_result(
            rs_values={"SPY": {"5d": -50.0}},
            pct_values={"SPY": {"5d": 0.0}},
        )
        calc._calculate_score(result)
        # RS component: 25 + (-50)*2.5 = -125 -> capped at 0
        # Pct component: 0/100*50 = 0
        # Total: 0
        assert result.rs_score >= 0.0


# ---------------------------------------------------------------------------
# Full pipeline tests
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_end_to_end_multiple_symbols(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5, 20],
            benchmarks=["SPY"],
        )

        # Create price data: 100 days
        days = 100
        spy_prices = [100 + i * 0.1 for i in range(days)]

        # AAPL outperforms SPY
        aapl_prices = [100 + i * 0.15 for i in range(days)]
        # MSFT underperforms SPY
        msft_prices = [100 + i * 0.05 for i in range(days)]

        price_map = {
            "AAPL": _make_close_df(aapl_prices),
            "MSFT": _make_close_df(msft_prices),
            "SPY": _make_close_df(spy_prices),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            results = calc.calculate_watchlist_rs(["AAPL", "MSFT"])

        assert len(results) == 2
        aapl_result = next(r for r in results if r.symbol == "AAPL")
        msft_result = next(r for r in results if r.symbol == "MSFT")

        # AAPL should have higher RS score (outperformed)
        assert aapl_result.rs_score > msft_result.rs_score

        # AAPL should have positive RS vs SPY
        assert aapl_result.rs_vs_benchmark["SPY"]["5d"] > 0
        assert aapl_result.rs_vs_benchmark["SPY"]["20d"] > 0

        # MSFT underperforms SPY by design (0.05/day vs 0.1/day)
        # so its RS should be negative — verify it's actually present
        assert "SPY" in msft_result.rs_vs_benchmark
        assert "20d" in msft_result.rs_vs_benchmark["SPY"]
        assert msft_result.rs_vs_benchmark["SPY"]["20d"] < 0  # Expected underperformance

    def test_end_to_end_with_both_benchmarks(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY", "QQQ"],
        )

        days = 30
        spy_prices = [100 + i * 0.1 for i in range(days)]
        qqq_prices = [100 + i * 0.12 for i in range(days)]
        nvda_prices = [100 + i * 0.2 for i in range(days)]

        price_map = {
            "NVDA": _make_close_df(nvda_prices),
            "SPY": _make_close_df(spy_prices),
            "QQQ": _make_close_df(qqq_prices),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            results = calc.calculate_watchlist_rs(["NVDA"])

        assert len(results) == 1
        nvda = results[0]
        assert "SPY" in nvda.rs_vs_benchmark
        assert "QQQ" in nvda.rs_vs_benchmark

        # NVDA outperforms both
        assert nvda.rs_vs_benchmark["SPY"]["5d"] > 0
        assert nvda.rs_vs_benchmark["QQQ"]["5d"] > 0


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_format_rs_result_with_details(self):
        r = RelativeStrengthResult(
            symbol="AAPL",
            rs_vs_benchmark={"SPY": {"5d": 2.5, "20d": -1.3}},
            percentile_rank={"SPY": {"5d": 75.0}},
            trend_direction="improving",
            rs_score=72.3,
        )
        text = format_rs_result(r, include_details=True)
        assert "AAPL" not in text  # Symbol not in formatted text
        assert "vs SPY" in text
        assert "5d: +2.50%" in text
        assert "Score: 72" in text
        assert "improving" in text

    def test_format_rs_result_no_details(self):
        r = RelativeStrengthResult(
            symbol="AAPL",
            rs_vs_benchmark={},
            percentile_rank={},
            trend_direction="stable",
            rs_score=50.0,
        )
        text = format_rs_result(r, include_details=False)
        assert "Score: 50" in text
        assert "stable" in text
        assert "vs SPY" not in text

    def test_format_rs_result_empty(self):
        r = RelativeStrengthResult(symbol="NOPE")
        text = format_rs_result(r, include_details=True)
        assert "Score: 50" in text
        assert "stable" in text

    def test_format_rs_summary_empty(self):
        text = format_rs_summary([])
        assert "No relative strength data" in text

    def test_format_rs_summary(self):
        results = [
            RelativeStrengthResult(symbol="A", rs_score=30.0, trend_direction="declining"),
            RelativeStrengthResult(symbol="B", rs_score=80.0, trend_direction="improving"),
            RelativeStrengthResult(symbol="C", rs_score=50.0, trend_direction="stable"),
        ]
        text = format_rs_summary(results)
        # Should be sorted by score descending
        lines = text.strip().split("\n")
        # First data line should be B (highest score)
        assert "B" in lines[2]
        assert "80" in lines[2]


# ---------------------------------------------------------------------------
# Module-level convenience tests
# ---------------------------------------------------------------------------

class TestModuleFunctions:
    def test_get_calculator_singleton(self):
        c1 = get_calculator()
        c2 = get_calculator()
        assert c1 is c2
        assert isinstance(c1, RelativeStrengthCalculator)

    def test_calculate_watchlist_rs_convenience(self):
        with patch("strategy.relative_strength.RelativeStrengthCalculator") as MockCalc:
            mock_instance = MagicMock()
            mock_instance.calculate_watchlist_rs.return_value = []
            MockCalc.return_value = mock_instance

            results = calculate_watchlist_rs(
                ["AAPL"],
                lookback_periods=[5],
                benchmarks=["SPY"],
            )

            MockCalc.assert_called_once_with(
                lookback_periods=[5],
                benchmarks=["SPY"],
            )
            mock_instance.calculate_watchlist_rs.assert_called_once_with(
                ["AAPL"],
                period="6mo",
            )


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_missing_benchmark_data(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY", "QQQ"],
        )

        # Only SPY data, no QQQ
        sym_prices = list(range(100, 160))
        spy_prices = list(range(100, 155))

        price_map = {
            "AAPL": _make_close_df(sym_prices),
            "SPY": _make_close_df(spy_prices),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            results = calc.calculate_watchlist_rs(["AAPL"])

        assert len(results) == 1
        aapl = results[0]
        assert "SPY" in aapl.rs_vs_benchmark
        # QQQ should not be present since data is missing
        assert "QQQ" not in aapl.rs_vs_benchmark

    def test_symbol_not_in_prices(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        price_map = {
            "SPY": _make_close_df(list(range(100, 155))),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            results = calc.calculate_watchlist_rs(["MISSING"])

        assert len(results) == 1
        assert results[0].symbol == "MISSING"
        assert results[0].rs_vs_benchmark == {}
        assert results[0].rs_score == 50.0

    def test_lookback_periods_order(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[60, 20, 5],  # Reversed order
            benchmarks=["SPY"],
        )
        # Should still work with any order
        assert set(calc.lookback_periods) == {5, 20, 60}

    def test_empty_symbol_list(self):
        calc = RelativeStrengthCalculator()
        with patch.object(calc, "_fetch_prices", return_value={}):
            results = calc.calculate_watchlist_rs([])
        assert results == []

    def test_benchmark_also_in_symbols(self):
        calc = RelativeStrengthCalculator(
            lookback_periods=[5],
            benchmarks=["SPY"],
        )

        spy_prices = list(range(100, 155))

        price_map = {
            "SPY": _make_close_df(spy_prices),
            "QQQ": _make_close_df([100 + i * 0.12 for i in range(55)]),
        }

        with patch.object(calc, "_fetch_prices", return_value=price_map):
            results = calc.calculate_watchlist_rs(["SPY", "QQQ"])

        assert len(results) == 2
        spy_result = next(r for r in results if r.symbol == "SPY")
        # SPY vs SPY should be ~0
        assert abs(spy_result.rs_vs_benchmark.get("SPY", {}).get("5d", 999)) < 0.01
