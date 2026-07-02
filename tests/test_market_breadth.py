"""Tests for strategy/market_breadth.py."""

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from strategy.config import reset_feature_flags
from strategy.market_breadth import (
    DEFAULT_BREADTH_SYMBOLS,
    BreadthSymbolObservation,
    MarketBreadthAnalyzer,
    MarketBreadthReport,
    calculate_advance_decline_ratio,
    calculate_moving_average,
    calculate_period_return,
    classify_breadth_regime,
    compute_breadth_score,
    format_market_breadth,
    get_analyzer,
    get_latest_close,
    is_new_high,
    is_new_low,
    percentage,
)


def _close_df(prices):
    return pd.DataFrame(
        {"Close": [float(price) for price in prices]},
        index=pd.date_range("2026-01-01", periods=len(prices), freq="B"),
    )


class TestCalculations:
    def test_get_latest_close(self):
        assert get_latest_close(_close_df([100, 101, 102])) == 102.0

    def test_get_latest_close_missing_or_invalid(self):
        assert get_latest_close(None) is None
        assert get_latest_close(pd.DataFrame()) is None
        assert get_latest_close(pd.DataFrame({"Open": [1]})) is None
        assert get_latest_close(_close_df([100, 0])) is None

    def test_calculate_moving_average(self):
        df = _close_df([100, 101, 102, 103, 104])
        assert calculate_moving_average(df, 5) == pytest.approx(102.0)

    def test_calculate_moving_average_missing_data(self):
        assert calculate_moving_average(None, 5) is None
        assert calculate_moving_average(pd.DataFrame(), 5) is None
        assert calculate_moving_average(pd.DataFrame({"Open": [1, 2]}), 2) is None

    def test_calculate_moving_average_invalid_period_or_history(self):
        assert calculate_moving_average(_close_df([100, 101]), 0) is None
        assert calculate_moving_average(_close_df([100, 101]), 5) is None

    def test_calculate_period_return(self):
        df = _close_df([100, 102, 104])
        assert calculate_period_return(df, 2) == pytest.approx(4.0)

    def test_calculate_period_return_missing_or_invalid(self):
        assert calculate_period_return(None, 1) is None
        assert calculate_period_return(pd.DataFrame(), 1) is None
        assert calculate_period_return(pd.DataFrame({"Open": [1, 2]}), 1) is None
        assert calculate_period_return(_close_df([100]), 1) is None
        assert calculate_period_return(_close_df([100, 101]), 0) is None
        assert calculate_period_return(_close_df([0, 101]), 1) is None

    def test_new_high_and_new_low(self):
        assert is_new_high(_close_df([100, 101, 102, 103]), 4) is True
        assert is_new_low(_close_df([103, 102, 101, 100]), 4) is True
        assert is_new_high(_close_df([100, 103, 102, 101]), 4) is False
        assert is_new_low(_close_df([100, 99, 101, 102]), 4) is False

    def test_new_high_low_missing_or_insufficient(self):
        assert is_new_high(None, 4) is False
        assert is_new_low(pd.DataFrame(), 4) is False
        assert is_new_high(pd.DataFrame({"Open": [1, 2]}), 2) is False
        assert is_new_low(_close_df([100, 101]), 4) is False
        assert is_new_high(_close_df([100, 101]), 0) is False

    def test_percentage(self):
        assert percentage(2, 4) == 50.0
        assert percentage(1, 3) == pytest.approx(33.33)
        assert percentage(2, 0) == 0.0

    def test_calculate_advance_decline_ratio(self):
        assert calculate_advance_decline_ratio(6, 3) == 2.0
        assert calculate_advance_decline_ratio(4, 0) == 4.0
        assert calculate_advance_decline_ratio(0, 0) == 0.0

    def test_compute_breadth_score(self):
        score = compute_breadth_score(
            above_ma_percentages={"20d": 80.0, "50d": 70.0},
            advancing_count=8,
            declining_count=2,
            unchanged_count=0,
            new_high_count=2,
            new_low_count=0,
            symbols_with_data=10,
        )
        assert score == pytest.approx(73.5)

    def test_compute_breadth_score_missing_data(self):
        assert compute_breadth_score({}, 0, 0, 0, 0, 0, 0) == 50.0

    def test_compute_breadth_score_clamped(self):
        high = compute_breadth_score({"20d": 1000.0}, 10, 0, 0, 10, 0, 10)
        low = compute_breadth_score({"20d": -100.0}, 0, 10, 0, 0, 10, 10)
        assert high == 100.0
        assert low == 0.0

    def test_classify_breadth_regime(self):
        assert classify_breadth_regime(70.0) == "strong"
        assert classify_breadth_regime(55.0) == "healthy"
        assert classify_breadth_regime(40.0) == "mixed"
        assert classify_breadth_regime(39.9) == "weak"


class TestSerialization:
    def test_observation_to_dict(self):
        observation = BreadthSymbolObservation(
            symbol="AAPL",
            latest_close=123.45678,
            moving_averages={"20d": 120.12345},
            above_moving_average={"20d": True},
            returns={"1d": 1.23456},
            advancing=True,
            declining=False,
            new_high=True,
            data_quality="complete",
            details={"source": "mock"},
        )
        d = observation.to_dict()
        assert d["latest_close"] == pytest.approx(123.4568)
        assert d["moving_averages"]["20d"] == pytest.approx(120.1235)
        assert d["returns"]["1d"] == pytest.approx(1.2346)
        assert d["above_moving_average"]["20d"] is True
        json.dumps(d)

    def test_report_to_dict(self):
        report = MarketBreadthReport(
            timestamp="2026-07-02T12:00:00+00:00",
            symbols_analyzed=1,
            symbols_with_data=1,
            above_ma_counts={"20d": 1},
            above_ma_percentages={"20d": 100.0},
            advancing_count=1,
            advance_decline_ratio=1.0,
            breadth_score=80.123,
            breadth_regime="strong",
            observations=[BreadthSymbolObservation(symbol="AAPL")],
            data_quality="complete",
        )
        d = report.to_dict()
        assert d["breadth_score"] == pytest.approx(80.12)
        assert d["observations"][0]["symbol"] == "AAPL"
        json.dumps(d)


class TestMarketBreadthAnalyzer:
    def test_init_defaults(self):
        analyzer = MarketBreadthAnalyzer()
        assert analyzer.symbols == DEFAULT_BREADTH_SYMBOLS
        assert analyzer.ma_periods == [20, 50]
        assert analyzer.return_lookback == 1

    def test_analyze_with_mocked_provider(self):
        provider = MagicMock()
        price_map = {
            "AAA": _close_df([100 + i for i in range(60)]),
            "BBB": _close_df([100 - i for i in range(60)]),
            "CCC": _close_df([100] * 60),
        }
        provider.fetch.side_effect = lambda symbol, period="6mo": price_map.get(symbol)

        analyzer = MarketBreadthAnalyzer(
            symbols=["AAA", "BBB", "CCC"],
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.symbols_analyzed == 3
        assert report.symbols_with_data == 3
        assert report.symbols_missing_data == 0
        assert report.data_quality == "complete"
        assert report.above_ma_counts["20d"] == 1
        assert report.above_ma_percentages["20d"] == pytest.approx(33.33)
        assert report.advancing_count == 1
        assert report.declining_count == 1
        assert report.unchanged_count == 1
        assert report.new_high_count == 1
        assert report.new_low_count == 1
        assert report.breadth_regime in {"weak", "mixed", "healthy", "strong"}
        provider.fetch.assert_any_call("AAA", period="6mo")

    def test_analyze_handles_missing_symbol_data(self):
        provider = MagicMock()
        provider.fetch.side_effect = lambda symbol, period="6mo": (
            _close_df([100 + i for i in range(60)]) if symbol == "AAA" else None
        )

        analyzer = MarketBreadthAnalyzer(
            symbols=["AAA", "BBB"],
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.symbols_analyzed == 2
        assert report.symbols_with_data == 1
        assert report.symbols_missing_data == 1
        assert report.data_quality == "partial"
        assert report.observations[-1].symbol == "BBB"
        assert report.observations[-1].data_quality == "missing"

    def test_analyze_handles_insufficient_history_as_partial(self):
        provider = MagicMock()
        provider.fetch.return_value = _close_df([100, 101, 102])

        analyzer = MarketBreadthAnalyzer(
            symbols=["AAA"],
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.symbols_with_data == 1
        assert report.data_quality == "complete"
        assert report.observations[0].data_quality == "partial"
        assert report.above_ma_percentages["20d"] == 0.0

    def test_analyze_handles_provider_exception(self):
        provider = MagicMock()
        provider.fetch.side_effect = RuntimeError("network down")

        analyzer = MarketBreadthAnalyzer(
            symbols=["AAA"],
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.data_quality == "missing"
        assert report.symbols_missing_data == 1
        assert report.breadth_regime == "missing"

    def test_analyze_empty_symbol_list_uses_defaults(self):
        provider = MagicMock()
        provider.fetch.return_value = _close_df([100 + i for i in range(60)])

        analyzer = MarketBreadthAnalyzer(symbols=[], price_provider=provider)
        report = analyzer.analyze()

        assert report.symbols_analyzed == len(DEFAULT_BREADTH_SYMBOLS)

    def test_format_market_breadth(self):
        report = MarketBreadthReport(
            timestamp="2026-07-02T12:00:00+00:00",
            above_ma_percentages={"20d": 75.0},
            advancing_count=3,
            declining_count=1,
            advance_decline_ratio=3.0,
            new_high_count=1,
            new_low_count=0,
            breadth_score=70.0,
            breadth_regime="strong",
            observations=[BreadthSymbolObservation(symbol="AAPL")],
        )
        text = format_market_breadth(report)
        assert "Market Breadth" in text
        assert "Observational only" in text
        assert "Above 20d MA: 75%" in text
        assert "Advancers/decliners: 3/1" in text

    def test_format_market_breadth_empty(self):
        report = MarketBreadthReport(timestamp="2026-07-02T12:00:00+00:00")
        assert format_market_breadth(report) == "No market breadth data available."

    def test_get_analyzer(self):
        analyzer = get_analyzer(symbols=["AAA"])
        assert isinstance(analyzer, MarketBreadthAnalyzer)
        assert analyzer.symbols == ["AAA"]

    def test_observational_only_contract(self):
        flags = reset_feature_flags()
        analyzer = MarketBreadthAnalyzer(symbols=["AAA"])
        assert flags.enable_market_breadth is False
        assert "enable_market_breadth" not in flags.enabled_flags
        assert not hasattr(analyzer, "buy")
        assert not hasattr(analyzer, "sell")
        assert not hasattr(analyzer, "execute")
        assert not hasattr(analyzer, "place_order")

        report = MarketBreadthReport(timestamp="2026-07-02T12:00:00+00:00")
        d = report.to_dict()
        assert "recommendation" not in d
        assert "action" not in d
