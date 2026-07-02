"""Market Breadth Analysis -- observational participation tracking.

Measures how broadly a watchlist is participating in market moves. This module
is read-only: it produces market context for research and reporting, but does
not generate trading signals or change strategy behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

import pandas as pd

from strategy.relative_strength import PriceFetcher


DEFAULT_BREADTH_SYMBOLS: List[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AVGO",
    "JPM",
    "UNH",
    "V",
    "MA",
    "XOM",
    "LLY",
    "COST",
    "HD",
    "PG",
    "NFLX",
    "BAC",
    "WMT",
]
DEFAULT_MA_PERIODS: List[int] = [20, 50]
DEFAULT_RETURN_LOOKBACK = 1
DEFAULT_EXTREME_LOOKBACK = 20
DEFAULT_HISTORY_PERIOD = "6mo"


class PriceProvider(Protocol):
    def fetch(self, symbol: str, period: str = DEFAULT_HISTORY_PERIOD) -> Optional[pd.DataFrame]: ...


@dataclass
class BreadthSymbolObservation:
    """Market breadth observation for one symbol."""

    symbol: str
    latest_close: Optional[float] = None
    moving_averages: Dict[str, float] = field(default_factory=dict)
    above_moving_average: Dict[str, bool] = field(default_factory=dict)
    returns: Dict[str, float] = field(default_factory=dict)
    advancing: Optional[bool] = None
    declining: Optional[bool] = None
    new_high: bool = False
    new_low: bool = False
    data_quality: str = "missing"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "latest_close": round(self.latest_close, 4) if self.latest_close is not None else None,
            "moving_averages": {
                key: round(value, 4) for key, value in self.moving_averages.items()
            },
            "above_moving_average": dict(self.above_moving_average),
            "returns": {key: round(value, 4) for key, value in self.returns.items()},
            "advancing": self.advancing,
            "declining": self.declining,
            "new_high": self.new_high,
            "new_low": self.new_low,
            "data_quality": self.data_quality,
            "details": self.details,
        }


@dataclass
class MarketBreadthReport:
    """Aggregate market breadth report."""

    timestamp: str
    symbols_analyzed: int = 0
    symbols_with_data: int = 0
    symbols_missing_data: int = 0
    above_ma_counts: Dict[str, int] = field(default_factory=dict)
    above_ma_percentages: Dict[str, float] = field(default_factory=dict)
    advancing_count: int = 0
    declining_count: int = 0
    unchanged_count: int = 0
    advance_decline_ratio: float = 0.0
    new_high_count: int = 0
    new_low_count: int = 0
    breadth_score: float = 50.0
    breadth_regime: str = "missing"
    observations: List[BreadthSymbolObservation] = field(default_factory=list)
    data_quality: str = "missing"
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "symbols_analyzed": self.symbols_analyzed,
            "symbols_with_data": self.symbols_with_data,
            "symbols_missing_data": self.symbols_missing_data,
            "above_ma_counts": dict(self.above_ma_counts),
            "above_ma_percentages": {
                key: round(value, 2) for key, value in self.above_ma_percentages.items()
            },
            "advancing_count": self.advancing_count,
            "declining_count": self.declining_count,
            "unchanged_count": self.unchanged_count,
            "advance_decline_ratio": round(self.advance_decline_ratio, 4),
            "new_high_count": self.new_high_count,
            "new_low_count": self.new_low_count,
            "breadth_score": round(self.breadth_score, 2),
            "breadth_regime": self.breadth_regime,
            "observations": [observation.to_dict() for observation in self.observations],
            "data_quality": self.data_quality,
            "errors": list(self.errors),
        }


class MarketBreadthAnalyzer:
    """Analyze watchlist participation breadth.

    Observational only. Results are intended for market context and future
    validation, not current buy/sell decisions.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        ma_periods: Optional[List[int]] = None,
        return_lookback: int = DEFAULT_RETURN_LOOKBACK,
        extreme_lookback: int = DEFAULT_EXTREME_LOOKBACK,
        price_provider: Optional[PriceProvider] = None,
        history_period: str = DEFAULT_HISTORY_PERIOD,
    ):
        self.symbols = symbols or DEFAULT_BREADTH_SYMBOLS
        self.ma_periods = ma_periods or DEFAULT_MA_PERIODS
        self.return_lookback = return_lookback
        self.extreme_lookback = extreme_lookback
        self.price_provider = price_provider or PriceFetcher
        self.history_period = history_period

    def analyze(self) -> MarketBreadthReport:
        """Generate a market breadth report."""
        report = MarketBreadthReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbols_analyzed=len(self.symbols),
        )

        observations: List[BreadthSymbolObservation] = []
        for symbol in self.symbols:
            observation = self._analyze_symbol(symbol)
            observations.append(observation)
            if observation.data_quality == "missing":
                report.symbols_missing_data += 1
            else:
                report.symbols_with_data += 1

        report.observations = observations
        self._aggregate(report)
        return report

    def _analyze_symbol(self, symbol: str) -> BreadthSymbolObservation:
        df = self._fetch(symbol)
        if df is None:
            return BreadthSymbolObservation(
                symbol=symbol,
                data_quality="missing",
                details={"reason": "price data unavailable"},
            )

        latest_close = get_latest_close(df)
        if latest_close is None:
            return BreadthSymbolObservation(
                symbol=symbol,
                data_quality="missing",
                details={"reason": "latest close unavailable"},
            )

        moving_averages: Dict[str, float] = {}
        above_ma: Dict[str, bool] = {}
        missing_periods: List[str] = []

        for period in self.ma_periods:
            key = f"{period}d"
            ma_value = calculate_moving_average(df, period)
            if ma_value is None:
                missing_periods.append(key)
                continue
            moving_averages[key] = ma_value
            above_ma[key] = latest_close > ma_value

        period_return = calculate_period_return(df, self.return_lookback)
        returns = {}
        advancing: Optional[bool] = None
        declining: Optional[bool] = None
        if period_return is not None:
            returns[f"{self.return_lookback}d"] = period_return
            advancing = period_return > 0
            declining = period_return < 0

        new_high = is_new_high(df, self.extreme_lookback)
        new_low = is_new_low(df, self.extreme_lookback)
        quality = "complete"
        if len(above_ma) < len(self.ma_periods) or period_return is None:
            quality = "partial"

        return BreadthSymbolObservation(
            symbol=symbol,
            latest_close=latest_close,
            moving_averages=moving_averages,
            above_moving_average=above_ma,
            returns=returns,
            advancing=advancing,
            declining=declining,
            new_high=new_high,
            new_low=new_low,
            data_quality=quality,
            details={"missing_periods": missing_periods},
        )

    def _aggregate(self, report: MarketBreadthReport) -> None:
        valid = [obs for obs in report.observations if obs.data_quality != "missing"]
        if not valid:
            report.data_quality = "missing"
            report.breadth_regime = "missing"
            report.breadth_score = 50.0
            return

        for period in self.ma_periods:
            key = f"{period}d"
            eligible = [obs for obs in valid if key in obs.above_moving_average]
            count = sum(1 for obs in eligible if obs.above_moving_average[key])
            report.above_ma_counts[key] = count
            report.above_ma_percentages[key] = percentage(count, len(eligible))

        report.advancing_count = sum(1 for obs in valid if obs.advancing is True)
        report.declining_count = sum(1 for obs in valid if obs.declining is True)
        report.unchanged_count = sum(
            1
            for obs in valid
            if obs.advancing is False and obs.declining is False
        )
        report.advance_decline_ratio = calculate_advance_decline_ratio(
            report.advancing_count,
            report.declining_count,
        )
        report.new_high_count = sum(1 for obs in valid if obs.new_high)
        report.new_low_count = sum(1 for obs in valid if obs.new_low)
        report.breadth_score = compute_breadth_score(
            above_ma_percentages=report.above_ma_percentages,
            advancing_count=report.advancing_count,
            declining_count=report.declining_count,
            unchanged_count=report.unchanged_count,
            new_high_count=report.new_high_count,
            new_low_count=report.new_low_count,
            symbols_with_data=report.symbols_with_data,
        )
        report.breadth_regime = classify_breadth_regime(report.breadth_score)

        if report.symbols_with_data == report.symbols_analyzed and report.symbols_analyzed:
            report.data_quality = "complete"
        else:
            report.data_quality = "partial"

    def _fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            return self.price_provider.fetch(symbol, period=self.history_period)
        except Exception:
            return None


def get_latest_close(df: Optional[pd.DataFrame]) -> Optional[float]:
    """Return the latest positive close."""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if close.empty:
        return None
    latest = float(close.iloc[-1])
    if latest <= 0:
        return None
    return latest


def calculate_moving_average(df: Optional[pd.DataFrame], period: int) -> Optional[float]:
    """Calculate the latest simple moving average."""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    if period <= 0:
        return None
    close = df["Close"].dropna()
    if len(close) < period:
        return None
    return round(float(close.tail(period).mean()), 4)


def calculate_period_return(df: Optional[pd.DataFrame], lookback_days: int) -> Optional[float]:
    """Return percentage performance over a lookback window."""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    if lookback_days <= 0:
        return None
    close = df["Close"].dropna()
    if len(close) < lookback_days + 1:
        return None
    start = float(close.iloc[-(lookback_days + 1)])
    end = float(close.iloc[-1])
    if start <= 0:
        return None
    return round(((end - start) / start) * 100.0, 4)


def is_new_high(df: Optional[pd.DataFrame], lookback_days: int) -> bool:
    """True when the latest close exceeds the prior lookback high."""
    if df is None or df.empty or "Close" not in df.columns or lookback_days <= 0:
        return False
    close = df["Close"].dropna()
    if len(close) < lookback_days:
        return False
    latest = float(close.iloc[-1])
    prior_window = close.tail(lookback_days).iloc[:-1]
    if prior_window.empty:
        return False
    return latest > float(prior_window.max())


def is_new_low(df: Optional[pd.DataFrame], lookback_days: int) -> bool:
    """True when the latest close undercuts the prior lookback low."""
    if df is None or df.empty or "Close" not in df.columns or lookback_days <= 0:
        return False
    close = df["Close"].dropna()
    if len(close) < lookback_days:
        return False
    latest = float(close.iloc[-1])
    prior_window = close.tail(lookback_days).iloc[:-1]
    if prior_window.empty:
        return False
    return latest < float(prior_window.min())


def percentage(count: int, total: int) -> float:
    """Return a percentage with zero-safe denominator handling."""
    if total <= 0:
        return 0.0
    return round((count / total) * 100.0, 2)


def calculate_advance_decline_ratio(advancing: int, declining: int) -> float:
    """Calculate advance/decline ratio with zero-safe handling."""
    if declining <= 0:
        return float(advancing) if advancing > 0 else 0.0
    return round(advancing / declining, 4)


def compute_breadth_score(
    above_ma_percentages: Dict[str, float],
    advancing_count: int,
    declining_count: int,
    unchanged_count: int,
    new_high_count: int,
    new_low_count: int,
    symbols_with_data: int,
) -> float:
    """Convert breadth components into a 0-100 participation score."""
    if symbols_with_data <= 0:
        return 50.0

    ma_score = (
        sum(above_ma_percentages.values()) / len(above_ma_percentages)
        if above_ma_percentages
        else 50.0
    )
    directional_total = advancing_count + declining_count + unchanged_count
    advance_score = percentage(advancing_count, directional_total)
    extreme_score = 50.0 + (
        (percentage(new_high_count, symbols_with_data) - percentage(new_low_count, symbols_with_data))
        / 2.0
    )
    score = (ma_score * 0.5) + (advance_score * 0.3) + (extreme_score * 0.2)
    return round(max(0.0, min(100.0, score)), 2)


def classify_breadth_regime(score: float) -> str:
    """Classify breadth score into a plain-language regime."""
    if score >= 70:
        return "strong"
    if score >= 55:
        return "healthy"
    if score >= 40:
        return "mixed"
    return "weak"


def format_market_breadth(report: MarketBreadthReport) -> str:
    """Format a compact market breadth report."""
    if not report.observations:
        return "No market breadth data available."

    lines = [
        "Market Breadth",
        f"Regime: {report.breadth_regime} (score {report.breadth_score:.0f})",
        "Observational only -- not used in trading decisions.",
        "",
    ]
    for key in sorted(report.above_ma_percentages):
        lines.append(f"Above {key} MA: {report.above_ma_percentages[key]:.0f}%")
    lines.append(
        f"Advancers/decliners: {report.advancing_count}/{report.declining_count} "
        f"(A/D {report.advance_decline_ratio:.2f})"
    )
    lines.append(f"New highs/lows: {report.new_high_count}/{report.new_low_count}")
    return "\n".join(lines)


def get_analyzer(symbols: Optional[List[str]] = None) -> MarketBreadthAnalyzer:
    """Create a MarketBreadthAnalyzer with default settings."""
    return MarketBreadthAnalyzer(symbols=symbols)
