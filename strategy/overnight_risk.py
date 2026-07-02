"""Overnight Risk Engine — Observational Gap Analysis
=====================================================

Measures overnight market risk by comparing previous close to
pre-market/after-hours prices. Purely observational — produces NO
trading signals, places NO trades, and does NOT influence strategy scoring.

Observations produced:
  - previous_close: Last regular-session close price
  - current_price: Current pre-market or after-hours price (if available)
  - overnight_gap_pct: Percentage gap from close to current
  - gap_direction: 'up', 'down', or 'flat'
  - significant_gap: Boolean — exceeds configured threshold
  - risk_score: 0-100 observation score (not a trading signal)
  - data_quality: 'complete', 'partial', or 'missing'

Usage:
    from strategy.overnight_risk import OvernightRiskEngine

    engine = OvernightRiskEngine(symbols=["AAPL", "TSLA"])
    report = engine.assess()
    for obs in report.observations:
        print(f"{obs.symbol}: gap {obs.overnight_gap_pct:+.2f}% "
              f"({obs.gap_direction})")

Config (strategy/config.py):
    enable_overnight_risk_engine  — feature flag (default: False)
    OVERNIGHT_GAP_THRESHOLD_PERCENT — gap % threshold for significant gaps
    OVERNIGHT_RISK_THRESHOLD      — future risk-score threshold (not wired yet)
    OVERNIGHT_EVAL_START_HOUR     — evaluation start hour ET (not wired yet)
    OVERNIGHT_EVAL_START_MINUTE   — evaluation start minute ET (not wired yet)
    NOTE: Timing/risk-score configs are defined for future scheduler
    integration. The engine uses the gap threshold because percent gap is
    the natural unit for observational overnight analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yfinance as yf

from strategy.config import OVERNIGHT_GAP_THRESHOLD_PERCENT, get_feature_flags

# ---------------------------------------------------------------------------
# Configurable defaults
# ---------------------------------------------------------------------------

DEFAULT_GAP_THRESHOLD = OVERNIGHT_GAP_THRESHOLD_PERCENT  # Percent — flag as significant
DEFAULT_PREMARKET_PERIOD = "1d"    # yfinance period for price fetch
DEFAULT_LOOKBACK_DAYS = 10         # Extra history to find previous close

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class OvernightGapObservation:
    """Observation for a single symbol's overnight gap.

    Attributes:
        symbol: Ticker symbol
        previous_close: Last regular-session close price
        current_price: Current pre-market/after-hours price (None if unavailable)
        overnight_gap_pct: Percentage gap from previous_close to current_price
        gap_direction: 'up', 'down', or 'flat'
        significant_gap: True if abs(gap) exceeds threshold
        risk_score: 0-100 observation score (higher = larger gap magnitude)
        data_quality: 'complete', 'partial', or 'missing'
        timestamp: ISO-8601 timestamp of observation
        details: Extra context (source prices, thresholds used)
    """
    symbol: str
    previous_close: Optional[float] = None
    current_price: Optional[float] = None
    overnight_gap_pct: float = 0.0
    gap_direction: str = "flat"
    significant_gap: bool = False
    risk_score: float = 0.0
    data_quality: str = "missing"
    timestamp: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON / database storage."""
        return {
            "symbol": self.symbol,
            "previous_close": round(self.previous_close, 4) if self.previous_close is not None else None,
            "current_price": round(self.current_price, 4) if self.current_price is not None else None,
            "overnight_gap_pct": round(self.overnight_gap_pct, 4),
            "gap_direction": self.gap_direction,
            "significant_gap": self.significant_gap,
            "risk_score": round(self.risk_score, 2),
            "data_quality": self.data_quality,
            "timestamp": self.timestamp,
            "details": self.details,
        }


@dataclass
class OvernightRiskReport:
    """Aggregate overnight risk report for a watchlist.

    Attributes:
        observations: Per-symbol observations
        timestamp: Report generation time
        symbols_analyzed: Number of symbols processed
        symbols_with_data: Number of symbols with valid data
        symbols_missing_data: Number of symbols with missing data
        significant_gaps: Number of symbols with significant gaps
        avg_risk_score: Mean risk score across all symbols
        max_gap_up: Largest positive gap (symbol, pct)
        max_gap_down: Largest negative gap (symbol, pct)
        feature_flag_enabled: Whether the engine was enabled when run
    """
    observations: List[OvernightGapObservation] = field(default_factory=list)
    timestamp: str = ""
    symbols_analyzed: int = 0
    symbols_with_data: int = 0
    symbols_missing_data: int = 0
    significant_gaps: int = 0
    avg_risk_score: float = 0.0
    max_gap_up: tuple = ("", 0.0)
    max_gap_down: tuple = ("", 0.0)
    feature_flag_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the full report."""
        return {
            "timestamp": self.timestamp,
            "symbols_analyzed": self.symbols_analyzed,
            "symbols_with_data": self.symbols_with_data,
            "symbols_missing_data": self.symbols_missing_data,
            "significant_gaps": self.significant_gaps,
            "avg_risk_score": round(self.avg_risk_score, 2),
            "max_gap_up": {"symbol": self.max_gap_up[0], "pct": round(self.max_gap_up[1], 4)} if self.max_gap_up[0] else None,
            "max_gap_down": {"symbol": self.max_gap_down[0], "pct": round(self.max_gap_down[1], 4)} if self.max_gap_down[0] else None,
            "feature_flag_enabled": self.feature_flag_enabled,
            "observations": [obs.to_dict() for obs in self.observations],
        }


# ---------------------------------------------------------------------------
# Gap calculation helpers
# ---------------------------------------------------------------------------


def calculate_gap_pct(previous_close: float, current_price: float) -> float:
    """Calculate percentage gap between previous close and current price.

    Args:
        previous_close: Last regular-session close
        current_price: Current price (pre-market or regular)

    Returns:
        Gap percentage. Positive = price rose overnight.
    """
    if previous_close <= 0:
        return 0.0
    return ((current_price - previous_close) / previous_close) * 100.0


def determine_gap_direction(gap_pct: float, epsilon: float = 0.05) -> str:
    """Classify gap direction.

    Args:
        gap_pct: Percentage gap
        epsilon: Tolerance for 'flat' classification

    Returns:
        'up', 'down', or 'flat'
    """
    if gap_pct > epsilon:
        return "up"
    elif gap_pct < -epsilon:
        return "down"
    return "flat"


def is_significant_gap(gap_pct: float, threshold: float = DEFAULT_GAP_THRESHOLD) -> bool:
    """Check if gap magnitude exceeds the significance threshold.

    Args:
        gap_pct: Absolute percentage gap
        threshold: Significance threshold in percent

    Returns:
        True if the gap is considered significant.
    """
    return abs(gap_pct) >= threshold


def compute_risk_score(
    gap_pct: float,
    threshold: float = DEFAULT_GAP_THRESHOLD,
    max_score: float = 100.0,
) -> float:
    """Compute a 0-100 observation score from gap magnitude.

    Score is linear: 0% gap = 0, threshold = 50, 2x threshold = 100.
    Clamped to [0, 100].

    Args:
        gap_pct: Absolute percentage gap
        threshold: The configured significance threshold
        max_score: Maximum possible score

    Returns:
        Risk score in [0, max_score].
    """
    if threshold <= 0:
        return 0.0
    abs_gap = abs(gap_pct)
    # Linear: gap=0 -> score=0, gap=threshold -> score=50, gap=2*threshold -> score=100
    score = (abs_gap / threshold) * (max_score / 2)
    return min(score, max_score)


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------


class PriceFetcher:
    """Fetch previous close and current price via yfinance."""

    @staticmethod
    def fetch_previous_close(symbol: str) -> Optional[float]:
        """Fetch the last regular-session close price.

        Returns None if data unavailable.
        """
        try:
            ticker = yf.Ticker(symbol)
            # Fetch recent history to find previous close
            hist = ticker.history(period=f"{DEFAULT_LOOKBACK_DAYS}d")
            if hist is None or hist.empty:
                return None
            return float(hist["Close"].iloc[-1])
        except Exception:
            return None

    @staticmethod
    def fetch_current_price(symbol: str) -> Optional[float]:
        """Fetch the latest available price (includes pre/after hours).

        Returns None if data unavailable.
        """
        try:
            ticker = yf.Ticker(symbol)
            # fast_info.last_price gives the latest price
            info = ticker.fast_info
            price = getattr(info, "last_price", None)
            if price is not None and price > 0:
                return float(price)
            # Fallback: use history
            hist = ticker.history(period="1d")
            if hist is not None and not hist.empty:
                return float(hist["Close"].iloc[-1])
            return None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class OvernightRiskEngine:
    """Observational overnight gap analysis engine.

    Does NOT generate trading signals. Does NOT place trades.
    Only produces structured observations for future consumption.

    Args:
        symbols: List of ticker symbols to analyze
        gap_threshold: Percent threshold for significant gaps
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        gap_threshold: Optional[float] = None,
        price_fetcher: Optional[PriceFetcher] = None,
    ):
        self.symbols = symbols or []
        self.gap_threshold = (
            DEFAULT_GAP_THRESHOLD if gap_threshold is None else gap_threshold
        )
        self.price_fetcher = price_fetcher or PriceFetcher()

    def add_symbols(self, symbols: List[str]) -> None:
        """Add symbols to the watchlist."""
        self.symbols.extend(symbols)

    def assess(self) -> OvernightRiskReport:
        """Run the overnight risk assessment on all watched symbols.

        Returns:
            OvernightRiskReport with per-symbol observations.
        """
        flags = get_feature_flags()
        report = OvernightRiskReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbols_analyzed=len(self.symbols),
            feature_flag_enabled=flags.enable_overnight_risk_engine,
        )

        if not self.symbols:
            return report

        observations: List[OvernightGapObservation] = []
        scores: List[float] = []

        for symbol in self.symbols:
            obs = self._assess_symbol(symbol)
            observations.append(obs)
            if obs.data_quality != "missing":
                report.symbols_with_data += 1
                scores.append(obs.risk_score)
            else:
                report.symbols_missing_data += 1
            if obs.significant_gap:
                report.significant_gaps += 1

            # Track extremes
            if obs.overnight_gap_pct > report.max_gap_up[1]:
                report.max_gap_up = (symbol, obs.overnight_gap_pct)
            if obs.overnight_gap_pct < report.max_gap_down[1]:
                report.max_gap_down = (symbol, obs.overnight_gap_pct)

        report.observations = observations
        report.avg_risk_score = (sum(scores) / len(scores)) if scores else 0.0

        return report

    def assess_symbol(self, symbol: str) -> OvernightGapObservation:
        """Assess a single symbol's overnight gap.

        Args:
            symbol: Ticker to analyze

        Returns:
            OvernightGapObservation for the symbol.
        """
        return self._assess_symbol(symbol)

    def _assess_symbol(self, symbol: str) -> OvernightGapObservation:
        """Internal: fetch prices and calculate gap observation.

        Handles missing data gracefully — never raises.
        """
        now = datetime.now(timezone.utc).isoformat()

        previous_close = self.price_fetcher.fetch_previous_close(symbol)
        current_price = self.price_fetcher.fetch_current_price(symbol)
        invalid_fields: List[str] = []

        if previous_close is not None and previous_close <= 0:
            invalid_fields.append("previous_close")
            previous_close = None

        if current_price is not None and current_price <= 0:
            invalid_fields.append("current_price")
            current_price = None

        # Missing data handling
        if previous_close is None and current_price is None:
            reason = "no valid price data available" if invalid_fields else "no price data available"
            return OvernightGapObservation(
                symbol=symbol,
                data_quality="missing",
                timestamp=now,
                details={
                    "reason": reason,
                    "invalid_fields": invalid_fields,
                },
            )

        quality = "complete"
        details: Dict[str, Any] = {
            "gap_threshold": self.gap_threshold,
        }

        if previous_close is None:
            # Use current_price as both previous_close and current
            # Gap will be 0 but quality is partial
            previous_close = current_price
            quality = "partial"
            details["reason"] = (
                "invalid previous_close" if "previous_close" in invalid_fields
                else "previous_close unavailable"
            )

        if current_price is None:
            current_price = previous_close
            quality = "partial"
            details["reason"] = (
                "invalid current_price" if "current_price" in invalid_fields
                else "current_price unavailable"
            )

        # At this point both are guaranteed non-None
        assert previous_close is not None
        assert current_price is not None

        # Calculate observations
        gap_pct = calculate_gap_pct(previous_close, current_price)
        direction = determine_gap_direction(gap_pct)
        significant = is_significant_gap(gap_pct, self.gap_threshold)
        risk_score = compute_risk_score(gap_pct, self.gap_threshold)

        details.update({
            "previous_close": previous_close,
            "current_price": current_price,
        })
        if invalid_fields:
            details["invalid_fields"] = invalid_fields

        return OvernightGapObservation(
            symbol=symbol,
            previous_close=previous_close,
            current_price=current_price,
            overnight_gap_pct=gap_pct,
            gap_direction=direction,
            significant_gap=significant,
            risk_score=risk_score,
            data_quality=quality,
            timestamp=now,
            details=details,
        )

    def get_summary(self) -> Dict[str, Any]:
        """Run assess() and return a compact summary dict."""
        report = self.assess()
        return {
            "timestamp": report.timestamp,
            "symbols": report.symbols_analyzed,
            "with_data": report.symbols_with_data,
            "missing": report.symbols_missing_data,
            "significant_gaps": report.significant_gaps,
            "avg_risk": round(report.avg_risk_score, 2),
            "enabled": report.feature_flag_enabled,
        }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def get_engine(
    symbols: Optional[List[str]] = None,
    gap_threshold: Optional[float] = None,
) -> OvernightRiskEngine:
    """Create an OvernightRiskEngine with optional defaults.

    Args:
        symbols: Symbols to watch. Defaults to empty list.
        gap_threshold: Gap threshold for significant flag.
    """
    return OvernightRiskEngine(
        symbols=symbols or [],
        gap_threshold=gap_threshold,
    )
