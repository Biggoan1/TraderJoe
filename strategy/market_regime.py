"""Market Regime Classification Module
======================================

Classifies the current market regime based on SPY/QQQ technical indicators.
Observational only — does NOT influence trading decisions.

Regimes:
  - bullish: Strong upward trend, low volatility
  - bearish: Downward trend, risk-off environment
  - volatile: High volatility, whipsaw conditions
  - neutral: Sideways, no clear direction

Indicators used:
  - SPY price vs 20-day MA (trend direction)
  - QQQ price vs 20-day MA (tech trend)
  - ATR(14) as % of price (volatility)
  - ADX(14) (trend strength)
  - MACD histogram (momentum)

Usage:
    from strategy.market_regime import MarketRegimeAnalyzer

    analyzer = MarketRegimeAnalyzer()
    regime = analyzer.classify()
    print(f"Market regime: {regime.regime}")
    print(f"Confidence: {regime.confidence:.0%}")

    # Get detailed analysis
    details = regime.to_dict()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Configurable defaults
# ---------------------------------------------------------------------------

DEFAULT_BENCHMARKS: List[str] = ["SPY", "QQQ"]

# MA periods
MA_SHORT = 20
MA_LONG = 50

# Thresholds — calibrated for daily data
TREND_ABOVE_MA_PCT = 0.5  # Price > MA by this % = bullish signal
TREND_BELOW_MA_PCT = -0.5  # Price < MA by this % = bearish signal

VOLATILITY_HIGH_PCT = 1.5  # ATR > this % of price = volatile
VOLATILITY_LOW_PCT = 0.8   # ATR < this % of price = low volatility

TREND_STRENGTH_WEAK = 20   # ADX < this = weak trend (volatile/neutral)
TREND_STRENGTH_STRONG = 25 # ADX > this = strong trend

# Score weights
WEIGHT_TREND = 0.35
WEIGHT_VOLATILITY = 0.25
WEIGHT_TECH = 0.25
WEIGHT_MOMENTUM = 0.15


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RegimeSignal:
    """Single indicator signal for regime classification."""
    name: str
    value: float
    bullish_score: float  # 0-1, higher = more bullish
    bearish_score: float  # 0-1, higher = more bearish
    volatile_score: float  # 0-1, higher = more volatile
    description: str = ""


@dataclass
class MarketRegimeResult:
    """Market regime classification result.

    Attributes:
        regime: One of 'bullish', 'bearish', 'volatile', 'neutral'
        confidence: 0.0 - 1.0, how confident the classification is
        signals: Individual indicator signals
        regime_scores: Raw scores for each regime (before normalization)
        date: Classification date
        details: Additional metadata
    """
    regime: str
    confidence: float
    signals: List[RegimeSignal] = field(default_factory=list)
    regime_scores: Dict[str, float] = field(default_factory=dict)
    date: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON / database storage."""
        return {
            "regime": self.regime,
            "confidence": round(self.confidence, 4),
            "regime_scores": {k: round(v, 4) for k, v in self.regime_scores.items()},
            "date": self.date,
            "signals": [
                {
                    "name": s.name,
                    "value": round(s.value, 4),
                    "bullish_score": round(s.bullish_score, 4),
                    "bearish_score": round(s.bearish_score, 4),
                    "volatile_score": round(s.volatile_score, 4),
                    "description": s.description,
                }
                for s in self.signals
            ],
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

def _calculate_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()  # type: ignore[return-value]


def _calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()  # type: ignore[return-value]


def _calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    # +DM and -DM
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[plus_dm == minus_dm] = 0

    # TR
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period, min_periods=period).mean() / atr)

    # DX
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx.rolling(window=period, min_periods=period).mean()  # type: ignore[return-value]


def _calculate_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD with histogram."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    })


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def _signal_price_vs_ma(
    price: float,
    ma_short: float,
    ma_long: float,
    name: str,
) -> RegimeSignal:
    """Signal based on price position relative to MAs."""
    pct_vs_short = ((price - ma_short) / ma_short) * 100
    pct_vs_long = ((price - ma_long) / ma_long) * 100

    # Bullish: price above both MAs, short MA above long MA
    bullish = 0.0
    bearish = 0.0

    if pct_vs_short > TREND_ABOVE_MA_PCT:
        bullish += 0.5
    elif pct_vs_short < TREND_BELOW_MA_PCT:
        bearish += 0.5

    if pct_vs_long > TREND_ABOVE_MA_PCT:
        bullish += 0.3
    elif pct_vs_long < TREND_BELOW_MA_PCT:
        bearish += 0.3

    if ma_short > ma_long:
        bullish += 0.2
    else:
        bearish += 0.2

    # Clamp to [0, 1]
    bullish = min(bullish, 1.0)
    bearish = min(bearish, 1.0)

    return RegimeSignal(
        name=f"{name}_price_vs_ma",
        value=pct_vs_short,
        bullish_score=round(bullish, 4),
        bearish_score=round(bearish, 4),
        volatile_score=0.0,
        description=f"{name}: price {pct_vs_short:+.2f}% vs 20-day MA, {pct_vs_long:+.2f}% vs 50-day MA",
    )


def _signal_atr(
    atr_value: float,
    price: float,
    name: str,
) -> RegimeSignal:
    """Signal based on ATR volatility."""
    atr_pct = (atr_value / price) * 100

    volatile = 0.0
    if atr_pct > VOLATILITY_HIGH_PCT:
        volatile = 1.0
    elif atr_pct > VOLATILITY_LOW_PCT:
        volatile = 0.5
    else:
        volatile = 0.1

    # High volatility slightly favors bearish (risk-off)
    bearish = volatile * 0.3
    bullish = (1.0 - volatile) * 0.5

    return RegimeSignal(
        name=f"{name}_atr",
        value=atr_pct,
        bullish_score=round(bullish, 4),
        bearish_score=round(bearish, 4),
        volatile_score=round(volatile, 4),
        description=f"{name}: ATR {atr_pct:.2f}% of price",
    )


def _signal_adx(
    adx_value: float,
    name: str,
) -> RegimeSignal:
    """Signal based on ADX trend strength."""
    # Weak ADX = volatile/neutral, strong ADX = trending
    volatile = 0.0
    if adx_value < TREND_STRENGTH_WEAK:
        volatile = 1.0
    elif adx_value < TREND_STRENGTH_STRONG:
        volatile = 0.5
    else:
        volatile = 0.0

    # Strong trend amplifies existing bullish/bearish signal
    trend_strength = min(adx_value / 50, 1.0)

    return RegimeSignal(
        name=f"{name}_adx",
        value=adx_value,
        bullish_score=round(trend_strength * 0.5, 4),
        bearish_score=round(trend_strength * 0.5, 4),
        volatile_score=round(volatile, 4),
        description=f"{name}: ADX {adx_value:.1f}",
    )


def _signal_macd(
    histogram: float,
    macd_line: float,
    signal_line: float,
    name: str,
) -> RegimeSignal:
    """Signal based on MACD momentum."""
    bullish = 0.5
    bearish = 0.5

    if histogram > 0:
        bullish += 0.3
    else:
        bearish += 0.3

    if macd_line > signal_line:
        bullish += 0.2
    else:
        bearish += 0.2

    # Cross momentum
    if histogram > 0 and macd_line > signal_line:
        bullish += 0.1
    elif histogram < 0 and macd_line < signal_line:
        bearish += 0.1

    bullish = min(bullish, 1.0)
    bearish = min(bearish, 1.0)

    return RegimeSignal(
        name=f"{name}_macd",
        value=histogram,
        bullish_score=round(bullish, 4),
        bearish_score=round(bearish, 4),
        volatile_score=0.0,
        description=f"{name}: MACD hist {histogram:.4f}",
    )


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------

class MarketRegimeAnalyzer:
    """Market regime classification engine.

    Analyzes SPY and QQQ technical indicators to determine the current
    market regime. Observational only — does not influence trading.
    """

    def __init__(
        self,
        benchmarks: Optional[List[str]] = None,
        lookback_days: int = 100,
    ):
        self.benchmarks = benchmarks or DEFAULT_BENCHMARKS
        self.lookback_days = lookback_days

    def fetch_data(self, symbol: str) -> pd.DataFrame:
        """Fetch historical data for a symbol."""
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")

        df = yf.download(symbol, start=start_date, end=end_date, progress=False)
        
        if df is None or df.empty:
            raise ValueError(f"No data fetched for {symbol}")

        # Handle multi-level columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df = df.xs(symbol, level="Ticker", axis=1)  # type: ignore[union-attr]

        return df  # type: ignore[return-value]

    def analyze_benchmark(self, symbol: str) -> List[RegimeSignal]:
        """Run all indicators for a single benchmark."""
        df = self.fetch_data(symbol)
        signals = []

        latest = df.iloc[-1]
        price = latest["Close"]

        # MAs
        ma_short = _calculate_sma(df["Close"], MA_SHORT)
        ma_long = _calculate_sma(df["Close"], MA_LONG)
        ma_short_val = ma_short.iloc[-1]
        ma_long_val = ma_long.iloc[-1]

        # Price vs MA
        signals.append(_signal_price_vs_ma(price, ma_short_val, ma_long_val, symbol))

        # ATR
        atr = _calculate_atr(df)
        atr_val = atr.iloc[-1]
        signals.append(_signal_atr(atr_val, price, symbol))

        # ADX
        adx = _calculate_adx(df)
        adx_val = adx.iloc[-1]
        signals.append(_signal_adx(adx_val, symbol))

        # MACD
        macd_df = _calculate_macd(df["Close"])
        histogram = macd_df["histogram"].iloc[-1]
        macd_line = macd_df["macd"].iloc[-1]
        signal_line = macd_df["signal"].iloc[-1]
        signals.append(_signal_macd(histogram, macd_line, signal_line, symbol))

        return signals

    def classify(self) -> MarketRegimeResult:
        """Classify the current market regime.

        Returns:
            MarketRegimeResult with regime classification and supporting signals.
        """
        all_signals = []

        for benchmark in self.benchmarks:
            try:
                signals = self.analyze_benchmark(benchmark)
                all_signals.extend(signals)
            except Exception as e:
                # If a benchmark fails, continue with others
                print(f"Warning: Failed to analyze {benchmark}: {e}")

        if not all_signals:
            return MarketRegimeResult(
                regime="neutral",
                confidence=0.0,
                signals=[],
                regime_scores={"bullish": 0.0, "bearish": 0.0, "volatile": 0.0, "neutral": 0.0},
                date=pd.Timestamp.now().strftime("%Y-%m-%d"),
                details={"error": "No signals available"},
            )

        # Weighted scoring
        # SPY gets slightly higher weight than QQQ
        spy_weight = 0.55
        qqq_weight = 0.45

        total_bullish = 0.0
        total_bearish = 0.0
        total_volatile = 0.0
        total_weight = 0.0

        for signal in all_signals:
            # Determine weight based on benchmark
            if signal.name.startswith("SPY"):
                weight = spy_weight
            elif signal.name.startswith("QQQ"):
                weight = qqq_weight
            else:
                weight = 1.0

            # Determine signal type weight
            if "ma" in signal.name:
                signal_weight = WEIGHT_TREND
            elif "atr" in signal.name:
                signal_weight = WEIGHT_VOLATILITY
            elif "macd" in signal.name:
                signal_weight = WEIGHT_MOMENTUM
            elif "adx" in signal.name:
                signal_weight = WEIGHT_TECH
            else:
                signal_weight = 0.25

            combined_weight = weight * signal_weight
            total_bullish += signal.bullish_score * combined_weight
            total_bearish += signal.bearish_score * combined_weight
            total_volatile += signal.volatile_score * combined_weight
            total_weight += combined_weight

        # Normalize
        if total_weight > 0:
            norm_bullish = total_bullish / total_weight
            norm_bearish = total_bearish / total_weight
            norm_volatile = total_volatile / total_weight
        else:
            norm_bullish = 0.25
            norm_bearish = 0.25
            norm_volatile = 0.25

        # Neutral = absence of strong signal
        norm_neutral = 1.0 - max(norm_bullish, norm_bearish, norm_volatile)

        scores = {
            "bullish": round(norm_bullish, 4),
            "bearish": round(norm_bearish, 4),
            "volatile": round(norm_volatile, 4),
            "neutral": round(norm_neutral, 4),
        }

        # Determine regime
        regime = max(scores, key=lambda k: scores[k])
        confidence = scores[regime]

        return MarketRegimeResult(
            regime=regime,
            confidence=confidence,
            signals=all_signals,
            regime_scores=scores,
            date=pd.Timestamp.now().strftime("%Y-%m-%d"),
            details={
                "benchmarks_analyzed": self.benchmarks,
                "total_signals": len(all_signals),
            },
        )

    def get_summary(self) -> str:
        """Human-readable regime summary."""
        result = self.classify()

        lines = [
            f"Market Regime: {result.regime.upper()}",
            f"Confidence: {result.confidence:.0%}",
            f"Scores: {', '.join(f'{k}={v:.2f}' for k, v in result.regime_scores.items())}",
        ]

        for signal in result.signals:
            lines.append(f"  {signal.description}")

        return "\n".join(lines)
