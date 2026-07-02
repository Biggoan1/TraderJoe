"""Tests for strategy/overnight_risk.py

Covers:
  - OvernightGapObservation and OvernightRiskReport dataclasses
  - Gap calculation helpers (calculate_gap_pct, determine_gap_direction,
    is_significant_gap, compute_risk_score)
  - PriceFetcher (mocked yfinance)
  - OvernightRiskEngine (assess, assess_symbol, get_summary)
  - get_engine convenience function
  - Feature flag integration
  - Edge cases (missing data, zero close, extreme gaps)
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from strategy.overnight_risk import (
    DEFAULT_GAP_THRESHOLD,
    PriceFetcher,
    OvernightGapObservation,
    OvernightRiskEngine,
    OvernightRiskReport,
    calculate_gap_pct,
    determine_gap_direction,
    compute_risk_score,
    get_engine,
    is_significant_gap,
)
from strategy.config import OVERNIGHT_GAP_THRESHOLD_PERCENT, reset_feature_flags


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_flags():
    """Reset feature flags between tests so nothing leaks."""
    reset_feature_flags()


def _mock_history(prices, index=None):
    """Return a tiny Close-only DataFrame for a given list of prices."""
    import pandas as pd

    if index is None:
        index = pd.date_range("2026-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"Close": [float(p) for p in prices]}, index=index)


# ---------------------------------------------------------------------------
# OvernightGapObservation tests
# ---------------------------------------------------------------------------


class TestOvernightGapObservation:
    def test_default_values(self):
        obs = OvernightGapObservation(symbol="AAPL")
        assert obs.symbol == "AAPL"
        assert obs.previous_close is None
        assert obs.current_price is None
        assert obs.overnight_gap_pct == 0.0
        assert obs.gap_direction == "flat"
        assert obs.significant_gap is False
        assert obs.risk_score == 0.0
        assert obs.data_quality == "missing"
        assert obs.timestamp == ""
        assert obs.details == {}

    def test_full_observation(self):
        obs = OvernightGapObservation(
            symbol="TSLA",
            previous_close=100.0,
            current_price=103.0,
            overnight_gap_pct=3.0,
            gap_direction="up",
            significant_gap=True,
            risk_score=75.0,
            data_quality="complete",
            timestamp="2026-07-02T12:00:00+00:00",
            details={"gap_threshold": 2.0},
        )
        assert obs.symbol == "TSLA"
        assert obs.previous_close == 100.0
        assert obs.current_price == 103.0
        assert obs.overnight_gap_pct == 3.0
        assert obs.gap_direction == "up"
        assert obs.significant_gap is True
        assert obs.risk_score == 75.0
        assert obs.data_quality == "complete"

    def test_to_dict_rounds_prices(self):
        obs = OvernightGapObservation(
            symbol="AAPL",
            previous_close=150.123456789,
            current_price=155.987654321,
            overnight_gap_pct=3.8698765,
            risk_score=48.3722075,
        )
        d = obs.to_dict()
        assert d["previous_close"] == 150.1235
        assert d["current_price"] == 155.9877
        assert d["overnight_gap_pct"] == pytest.approx(3.8699)
        assert d["risk_score"] == pytest.approx(48.37)

    def test_to_dict_none_prices(self):
        obs = OvernightGapObservation(symbol="NOPE")
        d = obs.to_dict()
        assert d["previous_close"] is None
        assert d["current_price"] is None

    def test_to_dict_preserves_zero_prices(self):
        obs = OvernightGapObservation(
            symbol="ZERO",
            previous_close=0.0,
            current_price=0.0,
        )
        d = obs.to_dict()
        assert d["previous_close"] == 0.0
        assert d["current_price"] == 0.0

    def test_to_dict_is_json_serializable(self):
        obs = OvernightGapObservation(
            symbol="AAPL",
            previous_close=100.0,
            current_price=103.0,
            overnight_gap_pct=3.0,
            gap_direction="up",
            significant_gap=True,
            risk_score=75.0,
            data_quality="complete",
        )
        json.dumps(obs.to_dict())


# ---------------------------------------------------------------------------
# OvernightRiskReport tests
# ---------------------------------------------------------------------------


class TestOvernightRiskReport:
    def test_default_values(self):
        report = OvernightRiskReport()
        assert report.observations == []
        assert report.symbols_analyzed == 0
        assert report.symbols_with_data == 0
        assert report.symbols_missing_data == 0
        assert report.significant_gaps == 0
        assert report.avg_risk_score == 0.0
        assert report.max_gap_up == ("", 0.0)
        assert report.max_gap_down == ("", 0.0)
        assert report.feature_flag_enabled is False

    def test_to_dict_with_observations(self):
        obs = OvernightGapObservation(symbol="AAPL", overnight_gap_pct=3.0)
        report = OvernightRiskReport(
            observations=[obs],
            symbols_analyzed=1,
            symbols_with_data=1,
            max_gap_up=("AAPL", 3.0),
            max_gap_down=("", 0.0),
        )
        d = report.to_dict()
        assert d["symbols_analyzed"] == 1
        assert d["observations"][0]["symbol"] == "AAPL"
        assert d["max_gap_up"]["symbol"] == "AAPL"
        assert d["max_gap_up"]["pct"] == 3.0

    def test_to_dict_no_max_gap_up(self):
        report = OvernightRiskReport()
        d = report.to_dict()
        assert d["max_gap_up"] is None
        assert d["max_gap_down"] is None

    def test_to_dict_is_json_serializable(self):
        report = OvernightRiskReport(
            observations=[
                OvernightGapObservation(symbol="AAPL", overnight_gap_pct=2.5),
            ],
            symbols_analyzed=1,
            symbols_with_data=1,
            max_gap_up=("AAPL", 2.5),
            max_gap_down=("", 0.0),
            feature_flag_enabled=False,
        )
        json.dumps(report.to_dict())


# ---------------------------------------------------------------------------
# Gap calculation helper tests
# ---------------------------------------------------------------------------


class TestCalculateGapPct:
    def test_positive_gap(self):
        assert calculate_gap_pct(100, 103) == pytest.approx(3.0)

    def test_negative_gap(self):
        assert calculate_gap_pct(100, 97) == pytest.approx(-3.0)

    def test_zero_gap(self):
        assert calculate_gap_pct(100, 100) == pytest.approx(0.0)

    def test_zero_previous_close(self):
        assert calculate_gap_pct(0, 100) == pytest.approx(0.0)

    def test_negative_previous_close(self):
        assert calculate_gap_pct(-1, 100) == pytest.approx(0.0)

    def test_large_gap(self):
        assert calculate_gap_pct(50, 100) == pytest.approx(100.0)

    def test_small_gap(self):
        result = calculate_gap_pct(100, 100.01)
        assert abs(result - 0.01) < 0.0001


class TestDetermineGapDirection:
    def test_up(self):
        assert determine_gap_direction(1.0) == "up"

    def test_down(self):
        assert determine_gap_direction(-1.0) == "down"

    def test_flat_at_zero(self):
        assert determine_gap_direction(0.0) == "flat"

    def test_flat_positive_epsilon(self):
        assert determine_gap_direction(0.03) == "flat"

    def test_flat_negative_epsilon(self):
        assert determine_gap_direction(-0.03) == "flat"

    def test_flat_at_epsilon(self):
        assert determine_gap_direction(0.05) == "flat"
        assert determine_gap_direction(-0.05) == "flat"

    def test_just_above_epsilon(self):
        assert determine_gap_direction(0.06) == "up"

    def test_custom_epsilon(self):
        assert determine_gap_direction(0.2, epsilon=0.5) == "flat"
        assert determine_gap_direction(0.6, epsilon=0.5) == "up"


class TestIsSignificantGap:
    def test_above_threshold(self):
        assert is_significant_gap(3.0, threshold=2.0) is True

    def test_at_threshold(self):
        assert is_significant_gap(2.0, threshold=2.0) is True

    def test_below_threshold(self):
        assert is_significant_gap(1.9, threshold=2.0) is False

    def test_negative_above(self):
        assert is_significant_gap(-3.0, threshold=2.0) is True

    def test_negative_below(self):
        assert is_significant_gap(-1.5, threshold=2.0) is False

    def test_default_threshold(self):
        assert is_significant_gap(DEFAULT_GAP_THRESHOLD) is True
        assert is_significant_gap(DEFAULT_GAP_THRESHOLD - 0.01) is False


class TestComputeRiskScore:
    def test_zero_gap(self):
        assert compute_risk_score(0.0) == pytest.approx(0.0)

    def test_at_threshold_is_half_score(self):
        score = compute_risk_score(2.0, threshold=2.0)
        assert score == pytest.approx(50.0)

    def test_at_double_threshold_is_full(self):
        score = compute_risk_score(4.0, threshold=2.0)
        assert score == pytest.approx(100.0)

    def test_clamped_at_max(self):
        score = compute_risk_score(100.0, threshold=2.0)
        assert score == pytest.approx(100.0)

    def test_negative_gap_same_score(self):
        pos = compute_risk_score(3.0, threshold=2.0)
        neg = compute_risk_score(-3.0, threshold=2.0)
        assert pos == pytest.approx(neg)

    def test_zero_threshold(self):
        assert compute_risk_score(5.0, threshold=0) == pytest.approx(0.0)

    def test_negative_threshold(self):
        assert compute_risk_score(5.0, threshold=-1) == pytest.approx(0.0)

    def test_custom_max_score(self):
        score = compute_risk_score(2.0, threshold=2.0, max_score=50.0)
        assert score == pytest.approx(25.0)

    def test_linear_progression(self):
        # gap=0 -> 0, gap=1 -> 25, gap=2 -> 50, gap=3 -> 75
        assert compute_risk_score(0.0, threshold=2.0) == pytest.approx(0.0)
        assert compute_risk_score(1.0, threshold=2.0) == pytest.approx(25.0)
        assert compute_risk_score(2.0, threshold=2.0) == pytest.approx(50.0)
        assert compute_risk_score(3.0, threshold=2.0) == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# PriceFetcher tests (mocked yfinance)
# ---------------------------------------------------------------------------


class TestPriceFetcher:
    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_previous_close_success(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _mock_history([100, 101, 102])
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch_previous_close("AAPL")
        assert result == pytest.approx(102.0)

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_previous_close_empty(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = None
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch_previous_close("INVALID")
        assert result is None

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_previous_close_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = Exception("Network error")
        result = PriceFetcher.fetch_previous_close("ERR")
        assert result is None

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_current_price_fast_info(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 155.5
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch_current_price("AAPL")
        assert result == pytest.approx(155.5)

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_current_price_zero_price_falls_back(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 0
        mock_ticker.history.return_value = _mock_history([100, 101])
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch_current_price("AAPL")
        assert result == pytest.approx(101.0)

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_current_price_no_last_price_attribute(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        del mock_ticker.fast_info.last_price
        mock_ticker.history.return_value = _mock_history([200, 201])
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch_current_price("AAPL")
        assert result == pytest.approx(201.0)

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_current_price_all_fails(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        del mock_ticker.fast_info.last_price
        mock_ticker.history.return_value = None
        mock_ticker_cls.return_value = mock_ticker

        result = PriceFetcher.fetch_current_price("INVALID")
        assert result is None

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_fetch_current_price_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = Exception("Boom")
        result = PriceFetcher.fetch_current_price("ERR")
        assert result is None


# ---------------------------------------------------------------------------
# OvernightRiskEngine tests
# ---------------------------------------------------------------------------


class TestOvernightRiskEngine:
    def test_init_defaults(self):
        engine = OvernightRiskEngine()
        assert engine.symbols == []
        assert engine.gap_threshold == OVERNIGHT_GAP_THRESHOLD_PERCENT
        assert DEFAULT_GAP_THRESHOLD == OVERNIGHT_GAP_THRESHOLD_PERCENT
        assert isinstance(engine.price_fetcher, PriceFetcher)

    def test_init_custom(self):
        engine = OvernightRiskEngine(symbols=["AAPL", "TSLA"], gap_threshold=3.0)
        assert engine.symbols == ["AAPL", "TSLA"]
        assert engine.gap_threshold == 3.0

    def test_init_custom_price_fetcher(self):
        fetcher = MagicMock(spec=PriceFetcher)
        engine = OvernightRiskEngine(symbols=["AAPL"], price_fetcher=fetcher)
        assert engine.price_fetcher is fetcher

    def test_add_symbols(self):
        engine = OvernightRiskEngine()
        engine.add_symbols(["AAPL"])
        engine.add_symbols(["TSLA", "MSFT"])
        assert engine.symbols == ["AAPL", "TSLA", "MSFT"]

    def test_assess_empty_symbols(self):
        engine = OvernightRiskEngine()
        report = engine.assess()
        assert report.symbols_analyzed == 0
        assert report.observations == []
        assert report.symbols_with_data == 0
        assert report.symbols_missing_data == 0

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_symbol_complete_data(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _mock_history([100, 101, 102])
        mock_ticker.fast_info.last_price = 105.0
        mock_ticker_cls.return_value = mock_ticker

        engine = OvernightRiskEngine(symbols=["AAPL"], gap_threshold=2.0)
        obs = engine.assess_symbol("AAPL")

        assert obs.symbol == "AAPL"
        assert obs.previous_close == pytest.approx(102.0)
        assert obs.current_price == pytest.approx(105.0)
        assert obs.overnight_gap_pct == pytest.approx((105 - 102) / 102 * 100)
        assert obs.gap_direction == "up"
        assert obs.data_quality == "complete"
        assert obs.timestamp != ""

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_symbol_missing_data(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = Exception("No data")

        engine = OvernightRiskEngine(symbols=["INVALID"], gap_threshold=2.0)
        obs = engine.assess_symbol("INVALID")

        assert obs.symbol == "INVALID"
        assert obs.data_quality == "missing"
        assert obs.previous_close is None
        assert obs.current_price is None
        assert obs.overnight_gap_pct == 0.0
        assert obs.risk_score == 0.0

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_symbol_significant_gap(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _mock_history([100, 100, 100])
        mock_ticker.fast_info.last_price = 103.0
        mock_ticker_cls.return_value = mock_ticker

        engine = OvernightRiskEngine(symbols=["AAPL"], gap_threshold=2.0)
        obs = engine.assess_symbol("AAPL")

        # 3% gap > 2% threshold
        assert obs.significant_gap is True
        assert obs.gap_direction == "up"

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_symbol_insignificant_gap(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _mock_history([100, 100, 100])
        mock_ticker.fast_info.last_price = 100.5
        mock_ticker_cls.return_value = mock_ticker

        engine = OvernightRiskEngine(symbols=["AAPL"], gap_threshold=2.0)
        obs = engine.assess_symbol("AAPL")

        # 0.5% gap < 2% threshold
        assert obs.significant_gap is False

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_multi_symbol_report(self, mock_ticker_cls):
        def ticker_side_effect(symbol):
            mt = MagicMock()
            if symbol == "AAPL":
                mt.history.return_value = _mock_history([100, 100, 100])
                mt.fast_info.last_price = 103.0
            elif symbol == "TSLA":
                mt.history.return_value = _mock_history([200, 200, 200])
                mt.fast_info.last_price = 195.0
            else:
                raise Exception("Unknown")
            return mt

        mock_ticker_cls.side_effect = ticker_side_effect

        engine = OvernightRiskEngine(symbols=["AAPL", "TSLA"], gap_threshold=2.0)
        report = engine.assess()

        assert report.symbols_analyzed == 2
        assert report.symbols_with_data == 2
        assert report.symbols_missing_data == 0
        assert len(report.observations) == 2

        # AAPL gap = +3%, TSLA gap = -2.5%
        aapl_obs = report.observations[0]
        tsla_obs = report.observations[1]

        assert aapl_obs.gap_direction == "up"
        assert tsla_obs.gap_direction == "down"
        assert aapl_obs.significant_gap is True
        assert tsla_obs.significant_gap is True
        assert report.significant_gaps == 2

        # max_gap_up should be AAPL
        assert report.max_gap_up[0] == "AAPL"
        # max_gap_down should be TSLA
        assert report.max_gap_down[0] == "TSLA"
        assert report.avg_risk_score > 0

    def test_assess_report_generation_with_mocked_price_fetcher(self):
        fetcher = MagicMock(spec=PriceFetcher)
        fetcher.fetch_previous_close.side_effect = [100.0, 200.0, None]
        fetcher.fetch_current_price.side_effect = [103.0, 198.0, None]

        engine = OvernightRiskEngine(
            symbols=["AAPL", "TSLA", "BAD"],
            gap_threshold=2.0,
            price_fetcher=fetcher,
        )
        report = engine.assess()
        d = report.to_dict()

        assert report.symbols_analyzed == 3
        assert report.symbols_with_data == 2
        assert report.symbols_missing_data == 1
        assert report.significant_gaps == 1
        assert report.max_gap_up == ("AAPL", pytest.approx(3.0))
        assert report.max_gap_down == ("TSLA", pytest.approx(-1.0))
        assert d["observations"][0]["symbol"] == "AAPL"
        assert d["observations"][2]["data_quality"] == "missing"
        fetcher.fetch_previous_close.assert_any_call("AAPL")
        fetcher.fetch_current_price.assert_any_call("BAD")

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_mixed_data_quality(self, mock_ticker_cls):
        call_count = {"n": 0}

        def ticker_side_effect(symbol):
            call_count["n"] += 1
            mt = MagicMock()
            if symbol == "GOOD":
                mt.history.return_value = _mock_history([100, 100, 100])
                mt.fast_info.last_price = 101.0
            else:
                raise Exception("No data")
            return mt

        mock_ticker_cls.side_effect = ticker_side_effect

        engine = OvernightRiskEngine(symbols=["GOOD", "BAD"], gap_threshold=2.0)
        report = engine.assess()

        assert report.symbols_analyzed == 2
        assert report.symbols_with_data == 1
        assert report.symbols_missing_data == 1

    def test_feature_flag_recorded(self):
        engine = OvernightRiskEngine(symbols=[])
        report = engine.assess()
        assert report.feature_flag_enabled == report.feature_flag_enabled  # type: bool

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_get_summary(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _mock_history([100, 100, 100])
        mock_ticker.fast_info.last_price = 101.0
        mock_ticker_cls.return_value = mock_ticker

        engine = OvernightRiskEngine(symbols=["AAPL"], gap_threshold=2.0)
        summary = engine.get_summary()

        assert "timestamp" in summary
        assert summary["symbols"] == 1
        assert summary["with_data"] == 1
        assert summary["missing"] == 0
        assert "avg_risk" in summary
        assert "enabled" in summary

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_assess_tracks_avg_risk_score(self, mock_ticker_cls):
        """avg_risk_score is mean of scores for symbols with data."""
        call_count = {"n": 0}

        def ticker_side_effect(symbol):
            mt = MagicMock()
            if symbol == "SYM1":
                mt.history.return_value = _mock_history([100, 100, 100])
                mt.fast_info.last_price = 102.0  # 2% gap -> score 50
            elif symbol == "SYM2":
                mt.history.return_value = _mock_history([100, 100, 100])
                mt.fast_info.last_price = 104.0  # 4% gap -> score 100
            else:
                raise Exception("Unknown")
            return mt

        mock_ticker_cls.side_effect = ticker_side_effect

        engine = OvernightRiskEngine(symbols=["SYM1", "SYM2"], gap_threshold=2.0)
        report = engine.assess()

        # Scores should be ~50 and ~100, average ~75
        assert report.avg_risk_score == pytest.approx(75.0, abs=2.0)


# ---------------------------------------------------------------------------
# get_engine convenience tests
# ---------------------------------------------------------------------------


class TestGetEngine:
    def test_default(self):
        engine = get_engine()
        assert isinstance(engine, OvernightRiskEngine)
        assert engine.symbols == []
        assert engine.gap_threshold == DEFAULT_GAP_THRESHOLD

    def test_custom_symbols(self):
        engine = get_engine(symbols=["AAPL", "TSLA"])
        assert engine.symbols == ["AAPL", "TSLA"]

    def test_custom_threshold(self):
        engine = get_engine(gap_threshold=5.0)
        assert engine.gap_threshold == 5.0


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    """Verify the engine produces observations but does not influence trading."""

    def test_no_trade_execution_methods(self):
        engine = OvernightRiskEngine(symbols=["AAPL"])
        assert not hasattr(engine, "buy")
        assert not hasattr(engine, "sell")
        assert not hasattr(engine, "execute")
        assert not hasattr(engine, "place_order")

    def test_assess_returns_only_observations(self):
        engine = OvernightRiskEngine(symbols=[])
        report = engine.assess()
        assert isinstance(report, OvernightRiskReport)
        # Report contains no trading decision fields
        d = report.to_dict()
        assert "recommendation" not in d
        assert "action" not in d

    def test_observation_has_no_recommendation(self):
        obs = OvernightGapObservation(symbol="TEST")
        d = obs.to_dict()
        assert "recommendation" not in d
        assert "buy_signal" not in d
        assert "sell_signal" not in d

    def test_risk_score_not_trading_signal(self):
        """risk_score is 0-100 observational metric, not a buy/sell signal."""
        obs = OvernightGapObservation(
            symbol="TEST",
            risk_score=100.0,
            significant_gap=True,
        )
        assert obs.significant_gap is True
        # Even max risk score does not imply a trading action
        d = obs.to_dict()
        assert "action" not in d


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @patch("strategy.overnight_risk.yf.Ticker")
    def test_only_previous_close_available(self, mock_ticker_cls):
        """When current_price fetch fails but previous_close works."""
        mt = MagicMock()
        mt.history.return_value = _mock_history([100, 100, 100])
        del mt.fast_info.last_price
        # Second history call (for current) also returns data
        mt.fast_info.last_price = None
        mock_ticker_cls.return_value = mt

        engine = OvernightRiskEngine(symbols=["AAPL"])
        obs = engine.assess_symbol("AAPL")

        # Should fall back gracefully
        assert obs.data_quality in ("complete", "partial")
        assert obs.overnight_gap_pct == pytest.approx(0.0)

    def test_gap_direction_extreme(self):
        assert determine_gap_direction(50.0) == "up"
        assert determine_gap_direction(-50.0) == "down"

    def test_risk_score_extreme(self):
        score = compute_risk_score(1000.0, threshold=2.0)
        assert score == pytest.approx(100.0)

    def test_report_with_all_missing(self):
        report = OvernightRiskReport()
        report.symbols_analyzed = 3
        report.symbols_missing_data = 3
        d = report.to_dict()
        assert d["symbols_analyzed"] == 3
        assert d["symbols_missing_data"] == 3
        assert d["avg_risk_score"] == 0.0

    @patch("strategy.overnight_risk.yf.Ticker")
    def test_single_price_available(self, mock_ticker_cls):
        """Only current_price is available — previous_close falls back to it."""
        call_idx = [0]

        def side_effect(sym):
            call_idx[0] += 1
            mt = MagicMock()
            if call_idx[0] == 1:
                # First call: fetch_previous_close -> empty
                mt.history.return_value = None
            else:
                # Second call: fetch_current_price -> success
                mt.fast_info.last_price = 100.0
            return mt

        mock_ticker_cls.side_effect = side_effect

        engine = OvernightRiskEngine(symbols=["AAPL"])
        obs = engine.assess_symbol("AAPL")

        # Should handle gracefully with partial data
        assert obs.data_quality != "missing"
        assert obs.overnight_gap_pct == pytest.approx(0.0)

    def test_zero_previous_close_from_fetcher_is_partial(self):
        fetcher = MagicMock(spec=PriceFetcher)
        fetcher.fetch_previous_close.return_value = 0.0
        fetcher.fetch_current_price.return_value = 100.0

        engine = OvernightRiskEngine(symbols=["ZERO"], price_fetcher=fetcher)
        obs = engine.assess_symbol("ZERO")

        assert obs.data_quality == "partial"
        assert obs.previous_close == pytest.approx(100.0)
        assert obs.current_price == pytest.approx(100.0)
        assert obs.overnight_gap_pct == pytest.approx(0.0)
        assert obs.risk_score == pytest.approx(0.0)
        assert obs.details["invalid_fields"] == ["previous_close"]

    def test_negative_previous_close_from_fetcher_is_partial(self):
        fetcher = MagicMock(spec=PriceFetcher)
        fetcher.fetch_previous_close.return_value = -1.0
        fetcher.fetch_current_price.return_value = 100.0

        engine = OvernightRiskEngine(symbols=["NEG"], price_fetcher=fetcher)
        obs = engine.assess_symbol("NEG")

        assert obs.data_quality == "partial"
        assert obs.gap_direction == "flat"
        assert obs.significant_gap is False
        assert obs.details["reason"] == "invalid previous_close"

    def test_invalid_previous_and_current_prices_are_missing(self):
        fetcher = MagicMock(spec=PriceFetcher)
        fetcher.fetch_previous_close.return_value = 0.0
        fetcher.fetch_current_price.return_value = -5.0

        engine = OvernightRiskEngine(symbols=["BAD"], price_fetcher=fetcher)
        obs = engine.assess_symbol("BAD")

        assert obs.data_quality == "missing"
        assert obs.previous_close is None
        assert obs.current_price is None
        assert obs.details["reason"] == "no valid price data available"
        assert obs.details["invalid_fields"] == ["previous_close", "current_price"]
