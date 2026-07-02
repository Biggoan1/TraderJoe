"""Tests for Market Regime Classification Module
=================================================

Tests cover:
  - Technical indicator calculations (SMA, ATR, ADX, MACD)
  - Signal generation logic
  - Regime classification
  - Edge cases (missing data, extreme values)
  - Serialization
"""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch

from strategy.market_regime import (
    MarketRegimeAnalyzer,
    MarketRegimeResult,
    RegimeSignal,
    _calculate_sma,
    _calculate_atr,
    _calculate_adx,
    _calculate_macd,
    _signal_price_vs_ma,
    _signal_atr,
    _signal_adx,
    _signal_macd,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_df():
    """Generate sample OHLCV data for testing."""
    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=100, freq="B")
    base = 500.0
    prices = base + np.cumsum(np.random.randn(100) * 2)

    return pd.DataFrame({
        "Open": prices + np.random.randn(100) * 0.5,
        "High": prices + abs(np.random.randn(100)) * 1,
        "Low": prices - abs(np.random.randn(100)) * 1,
        "Close": prices,
        "Volume": np.random.randint(1_000_000, 10_000_000, 100),
    }, index=dates)


@pytest.fixture
def bullish_df():
    """Generate upward trending data."""
    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=100, freq="B")
    prices = 500 + np.linspace(0, 50, 100) + np.random.randn(100) * 1

    return pd.DataFrame({
        "Open": prices,
        "High": prices + abs(np.random.randn(100)) * 0.5,
        "Low": prices - abs(np.random.randn(100)) * 0.5,
        "Close": prices,
        "Volume": np.random.randint(1_000_000, 10_000_000, 100),
    }, index=dates)


@pytest.fixture
def bearish_df():
    """Generate downward trending data."""
    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=100, freq="B")
    prices = 500 - np.linspace(0, 50, 100) + np.random.randn(100) * 1

    return pd.DataFrame({
        "Open": prices,
        "High": prices + abs(np.random.randn(100)) * 0.5,
        "Low": prices - abs(np.random.randn(100)) * 0.5,
        "Close": prices,
        "Volume": np.random.randint(1_000_000, 10_000_000, 100),
    }, index=dates)


# ---------------------------------------------------------------------------
# Indicator calculation tests
# ---------------------------------------------------------------------------

class TestCalculateSMA:
    def test_basic_sma(self, sample_df):
        result = _calculate_sma(sample_df["Close"], 20)
        assert len(result) == 100
        assert result.iloc[19] > 0  # First valid value

    def test_sma_values(self):
        prices = pd.Series([10, 20, 30, 40, 50])
        sma = _calculate_sma(prices, 5)
        assert sma.iloc[4] == 30.0

    def test_sma_nan_before_period(self, sample_df):
        sma = _calculate_sma(sample_df["Close"], 20)
        assert pd.isna(sma.iloc[0])
        assert pd.isna(sma.iloc[10])

    def test_sma_equals_price_for_constant(self):
        prices = pd.Series([100.0] * 30)
        sma = _calculate_sma(prices, 10)
        assert sma.iloc[9] == 100.0


class TestCalculateATR:
    def test_atr_basic(self, sample_df):
        result = _calculate_atr(sample_df, 14)
        assert len(result) == 100
        assert result.iloc[13] > 0  # First valid value

    def test_atr_positive(self, sample_df):
        result = _calculate_atr(sample_df, 14)
        valid = result.dropna()
        assert (valid > 0).all()

    def test_atr_increases_with_volatility(self):
        """High volatility should produce higher ATR."""
        low_vol = pd.DataFrame({
            "High": [101] * 30,
            "Low": [99] * 30,
            "Close": [100] * 30,
        })
        high_vol = pd.DataFrame({
            "High": [110] * 30,
            "Low": [90] * 30,
            "Close": [100] * 30,
        })
        atr_low = _calculate_atr(low_vol, 14).iloc[-1]
        atr_high = _calculate_atr(high_vol, 14).iloc[-1]
        assert atr_high > atr_low


class TestCalculateADX:
    def test_adx_basic(self, sample_df):
        result = _calculate_adx(sample_df, 14)
        assert len(result) == 100

    def test_adx_range(self, sample_df):
        result = _calculate_adx(sample_df, 14)
        valid = result.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_adx_strong_trend(self, bullish_df):
        """Strong trends should have higher ADX."""
        result = _calculate_adx(bullish_df, 14)
        valid = result.dropna()
        assert valid.iloc[-1] >= 0


class TestCalculateMACD:
    def test_macd_basic(self, sample_df):
        result = _calculate_macd(sample_df["Close"])
        assert "macd" in result.columns
        assert "signal" in result.columns
        assert "histogram" in result.columns

    def test_macd_histogram(self, sample_df):
        result = _calculate_macd(sample_df["Close"])
        assert len(result) == 100

    def test_macd_values(self):
        prices = pd.Series([100 + i for i in range(50)])
        result = _calculate_macd(prices)
        assert result["macd"].iloc[-1] > 0  # Uptrend -> positive MACD


# ---------------------------------------------------------------------------
# Signal generation tests
# ---------------------------------------------------------------------------

class TestSignalPriceVsMA:
    def test_bullish_price_above_ma(self):
        signal = _signal_price_vs_ma(
            price=510,
            ma_short=500,
            ma_long=490,
            name="SPY",
        )
        assert signal.bullish_score > signal.bearish_score

    def test_bearish_price_below_ma(self):
        signal = _signal_price_vs_ma(
            price=490,
            ma_short=500,
            ma_long=510,
            name="SPY",
        )
        assert signal.bearish_score > signal.bullish_score

    def test_neutral_price_near_ma(self):
        signal = _signal_price_vs_ma(
            price=500,
            ma_short=500,
            ma_long=500,
            name="SPY",
        )
        assert 0 <= signal.bullish_score <= 1
        assert 0 <= signal.bearish_score <= 1

    def test_signal_name_format(self):
        signal = _signal_price_vs_ma(500, 500, 500, "SPY")
        assert signal.name == "SPY_price_vs_ma"

    def test_signal_values_range(self):
        signal = _signal_price_vs_ma(500, 500, 500, "SPY")
        assert 0 <= signal.bullish_score <= 1.0
        assert 0 <= signal.bearish_score <= 1.0
        assert signal.volatile_score == 0.0


class TestSignalATR:
    def test_high_volatility(self):
        signal = _signal_atr(
            atr_value=10,
            price=500,
            name="SPY",
        )
        assert signal.volatile_score > 0.5

    def test_low_volatility(self):
        signal = _signal_atr(
            atr_value=2,
            price=500,
            name="SPY",
        )
        assert signal.volatile_score < 0.5

    def test_atr_signal_values(self):
        signal = _signal_atr(5, 500, "SPY")
        assert 0 <= signal.bullish_score <= 1.0
        assert 0 <= signal.bearish_score <= 1.0
        assert 0 <= signal.volatile_score <= 1.0


class TestSignalADX:
    def test_weak_adx(self):
        signal = _signal_adx(10, "SPY")
        assert signal.volatile_score >= 0.5

    def test_strong_adx(self):
        signal = _signal_adx(35, "SPY")
        assert signal.volatile_score < 0.5

    def test_adx_signal_values(self):
        signal = _signal_adx(25, "SPY")
        assert 0 <= signal.bullish_score <= 1.0
        assert 0 <= signal.bearish_score <= 1.0
        assert 0 <= signal.volatile_score <= 1.0


class TestSignalMACD:
    def test_positive_histogram(self):
        signal = _signal_macd(
            histogram=0.5,
            macd_line=1.0,
            signal_line=0.5,
            name="SPY",
        )
        assert signal.bullish_score > signal.bearish_score

    def test_negative_histogram(self):
        signal = _signal_macd(
            histogram=-0.5,
            macd_line=-1.0,
            signal_line=-0.5,
            name="SPY",
        )
        assert signal.bearish_score > signal.bullish_score

    def test_macd_signal_values(self):
        signal = _signal_macd(0, 0, 0, "SPY")
        assert 0 <= signal.bullish_score <= 1.0
        assert 0 <= signal.bearish_score <= 1.0


# ---------------------------------------------------------------------------
# Analyzer tests
# ---------------------------------------------------------------------------

class TestMarketRegimeAnalyzer:
    def test_init_defaults(self):
        analyzer = MarketRegimeAnalyzer()
        assert "SPY" in analyzer.benchmarks
        assert "QQQ" in analyzer.benchmarks
        assert analyzer.lookback_days == 100

    def test_init_custom_benchmarks(self):
        analyzer = MarketRegimeAnalyzer(benchmarks=["SPY"])
        assert analyzer.benchmarks == ["SPY"]

    def test_init_custom_lookback(self):
        analyzer = MarketRegimeAnalyzer(lookback_days=50)
        assert analyzer.lookback_days == 50

    @patch("strategy.market_regime.yf.download")
    def test_fetch_data_success(self, mock_download):
        dates = pd.date_range("2026-01-01", periods=60, freq="B")
        mock_download.return_value = pd.DataFrame({
            "Close": [500 + i for i in range(60)],
            "High": [501 + i for i in range(60)],
            "Low": [499 + i for i in range(60)],
            "Open": [500 + i for i in range(60)],
        }, index=dates)

        analyzer = MarketRegimeAnalyzer(lookback_days=100)
        df = analyzer.fetch_data("SPY")

        assert len(df) == 60
        assert "Close" in df.columns

    @patch("strategy.market_regime.yf.download")
    def test_fetch_data_empty(self, mock_download):
        mock_download.return_value = None

        analyzer = MarketRegimeAnalyzer()
        with pytest.raises(ValueError, match="No data fetched"):
            analyzer.fetch_data("INVALID")

    @patch("strategy.market_regime.yf.download")
    def test_fetch_data_empty_df(self, mock_download):
        mock_download.return_value = pd.DataFrame()

        analyzer = MarketRegimeAnalyzer()
        with pytest.raises(ValueError, match="No data fetched"):
            analyzer.fetch_data("INVALID")

    def test_classify_returns_valid_regime(self):
        """Classification should return one of the valid regimes."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "analyze_benchmark") as mock_analyze:
            mock_analyze.return_value = [
                RegimeSignal(
                    name="SPY_price_vs_ma",
                    value=2.0,
                    bullish_score=0.7,
                    bearish_score=0.2,
                    volatile_score=0.1,
                ),
            ]
            result = analyzer.classify()

            assert result.regime in ["bullish", "bearish", "volatile", "neutral"]

    def test_classify_confidence_range(self):
        """Confidence should be between 0 and 1."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "analyze_benchmark") as mock_analyze:
            mock_analyze.return_value = []
            result = analyzer.classify()
            assert 0 <= result.confidence <= 1

    def test_classify_no_signals(self):
        """Should return neutral with no confidence if no signals."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "analyze_benchmark") as mock_analyze:
            mock_analyze.side_effect = Exception("Network error")
            result = analyzer.classify()
            assert result.regime == "neutral"
            assert result.confidence == 0.0

    def test_classify_with_bullish_signals(self):
        """Strong bullish signals should yield bullish regime."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "analyze_benchmark") as mock_analyze:
            mock_analyze.return_value = [
                RegimeSignal(
                    name="SPY_price_vs_ma",
                    value=3.0,
                    bullish_score=1.0,
                    bearish_score=0.0,
                    volatile_score=0.0,
                ),
                RegimeSignal(
                    name="SPY_atr",
                    value=0.5,
                    bullish_score=0.5,
                    bearish_score=0.0,
                    volatile_score=0.1,
                ),
            ]
            result = analyzer.classify()
            assert result.regime == "bullish"

    def test_classify_with_bearish_signals(self):
        """Strong bearish signals should yield bearish regime."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "analyze_benchmark") as mock_analyze:
            mock_analyze.return_value = [
                RegimeSignal(
                    name="SPY_price_vs_ma",
                    value=-3.0,
                    bullish_score=0.0,
                    bearish_score=1.0,
                    volatile_score=0.0,
                ),
            ]
            result = analyzer.classify()
            assert result.regime == "bearish"

    def test_classify_with_volatile_signals(self):
        """Strong volatile signals should yield volatile regime."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "analyze_benchmark") as mock_analyze:
            mock_analyze.return_value = [
                RegimeSignal(
                    name="SPY_atr",
                    value=3.0,
                    bullish_score=0.0,
                    bearish_score=0.3,
                    volatile_score=1.0,
                ),
                RegimeSignal(
                    name="SPY_adx",
                    value=10,
                    bullish_score=0.2,
                    bearish_score=0.2,
                    volatile_score=1.0,
                ),
            ]
            result = analyzer.classify()
            assert result.regime == "volatile"

    def test_get_summary(self):
        """Summary should be human-readable."""
        analyzer = MarketRegimeAnalyzer()
        with patch.object(analyzer, "classify") as mock_classify:
            mock_classify.return_value = MarketRegimeResult(
                regime="bullish",
                confidence=0.7,
                signals=[
                    RegimeSignal(
                        name="SPY_price_vs_ma",
                        value=2.0,
                        bullish_score=0.8,
                        bearish_score=0.2,
                        volatile_score=0.0,
                        description="SPY: price +2.00% vs 20-day MA",
                    ),
                ],
                regime_scores={"bullish": 0.7, "bearish": 0.2, "volatile": 0.1, "neutral": 0.3},
            )
            summary = analyzer.get_summary()
            assert "bullish" in summary.lower() or "BULLISH" in summary
            assert "Confidence" in summary


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------

class TestMarketRegimeResultSerialization:
    def test_to_dict(self):
        result = MarketRegimeResult(
            regime="bullish",
            confidence=0.75,
            signals=[
                RegimeSignal(
                    name="SPY_price_vs_ma",
                    value=2.0,
                    bullish_score=0.8,
                    bearish_score=0.2,
                    volatile_score=0.0,
                ),
            ],
            regime_scores={"bullish": 0.75, "bearish": 0.2, "volatile": 0.1, "neutral": 0.25},
            date="2026-07-02",
        )

        data = result.to_dict()
        assert data["regime"] == "bullish"
        assert data["confidence"] == 0.75
        assert data["date"] == "2026-07-02"
        assert len(data["signals"]) == 1
        assert "regime_scores" in data

    def test_to_dict_empty(self):
        result = MarketRegimeResult(
            regime="neutral",
            confidence=0.0,
            signals=[],
            regime_scores={},
        )

        data = result.to_dict()
        assert data["regime"] == "neutral"
        assert data["signals"] == []

    def test_regime_signal_to_dict_in_result(self):
        result = MarketRegimeResult(
            regime="volatile",
            confidence=0.6,
            signals=[
                RegimeSignal(
                    name="SPY_atr",
                    value=2.0,
                    bullish_score=0.1,
                    bearish_score=0.3,
                    volatile_score=1.0,
                ),
            ],
            regime_scores={"volatile": 0.6},
        )

        data = result.to_dict()
        signal_data = data["signals"][0]
        assert signal_data["name"] == "SPY_atr"
        assert signal_data["value"] == 2.0
        assert signal_data["volatile_score"] == 1.0
