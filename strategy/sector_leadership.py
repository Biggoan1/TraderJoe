"""Sector Leadership Tracking -- observational sector rotation analysis.

Ranks sector ETFs by relative performance against a benchmark. This module is
read-only: it produces market context for reports and research, but does not
generate trading signals or change strategy behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

import pandas as pd

from strategy.relative_strength import PriceFetcher


DEFAULT_SECTOR_ETFS: Dict[str, str] = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}
DEFAULT_BENCHMARK = "SPY"
DEFAULT_LOOKBACK_PERIODS: List[int] = [5, 20, 60]
DEFAULT_HISTORY_PERIOD = "6mo"


class PriceProvider(Protocol):
    def fetch(self, symbol: str, period: str = DEFAULT_HISTORY_PERIOD) -> Optional[pd.DataFrame]: ...


@dataclass
class SectorLeadershipResult:
    """Leadership result for one sector ETF."""

    sector: str
    symbol: str
    returns: Dict[str, float] = field(default_factory=dict)
    relative_returns: Dict[str, float] = field(default_factory=dict)
    leadership_score: float = 50.0
    rank: int = 0
    trend_direction: str = "stable"
    data_quality: str = "missing"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sector": self.sector,
            "symbol": self.symbol,
            "returns": {k: round(v, 4) for k, v in self.returns.items()},
            "relative_returns": {
                k: round(v, 4) for k, v in self.relative_returns.items()
            },
            "leadership_score": round(self.leadership_score, 2),
            "rank": self.rank,
            "trend_direction": self.trend_direction,
            "data_quality": self.data_quality,
            "details": self.details,
        }


@dataclass
class SectorLeadershipReport:
    """Aggregate sector leadership report."""

    timestamp: str
    benchmark: str
    lookback_periods: List[int]
    sectors_analyzed: int = 0
    sectors_with_data: int = 0
    sectors_missing_data: int = 0
    strongest_sectors: List[str] = field(default_factory=list)
    weakest_sectors: List[str] = field(default_factory=list)
    results: List[SectorLeadershipResult] = field(default_factory=list)
    data_quality: str = "missing"
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "benchmark": self.benchmark,
            "lookback_periods": list(self.lookback_periods),
            "sectors_analyzed": self.sectors_analyzed,
            "sectors_with_data": self.sectors_with_data,
            "sectors_missing_data": self.sectors_missing_data,
            "strongest_sectors": list(self.strongest_sectors),
            "weakest_sectors": list(self.weakest_sectors),
            "results": [result.to_dict() for result in self.results],
            "data_quality": self.data_quality,
            "errors": list(self.errors),
        }


class SectorLeadershipAnalyzer:
    """Rank sectors by relative performance.

    Observational only. Results are intended for market context and future
    validation, not current buy/sell decisions.
    """

    def __init__(
        self,
        sectors: Optional[Dict[str, str]] = None,
        benchmark: str = DEFAULT_BENCHMARK,
        lookback_periods: Optional[List[int]] = None,
        price_provider: Optional[PriceProvider] = None,
        history_period: str = DEFAULT_HISTORY_PERIOD,
    ):
        self.sectors = sectors or DEFAULT_SECTOR_ETFS
        self.benchmark = benchmark
        self.lookback_periods = lookback_periods or DEFAULT_LOOKBACK_PERIODS
        self.price_provider = price_provider or PriceFetcher
        self.history_period = history_period

    def analyze(self) -> SectorLeadershipReport:
        """Generate a sector leadership report."""
        report = SectorLeadershipReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            benchmark=self.benchmark,
            lookback_periods=list(self.lookback_periods),
            sectors_analyzed=len(self.sectors),
        )

        benchmark_df = self._fetch(self.benchmark)
        if benchmark_df is None:
            report.errors.append(f"Benchmark data unavailable: {self.benchmark}")

        results: List[SectorLeadershipResult] = []
        for sector, symbol in self.sectors.items():
            result = self._analyze_sector(sector, symbol, benchmark_df)
            results.append(result)
            if result.data_quality == "missing":
                report.sectors_missing_data += 1
            else:
                report.sectors_with_data += 1

        ranked = sorted(
            results,
            key=lambda item: (
                item.data_quality == "missing",
                -item.leadership_score,
                item.sector,
            ),
        )
        rank = 1
        for result in ranked:
            if result.data_quality != "missing":
                result.rank = rank
                rank += 1

        report.results = ranked
        report.strongest_sectors = [
            result.sector for result in ranked if result.data_quality != "missing"
        ][:3]
        report.weakest_sectors = [
            result.sector
            for result in reversed(ranked)
            if result.data_quality != "missing"
        ][:3]

        if report.sectors_with_data == report.sectors_analyzed and report.sectors_analyzed:
            report.data_quality = "complete"
        elif report.sectors_with_data:
            report.data_quality = "partial"
        else:
            report.data_quality = "missing"

        return report

    def _analyze_sector(
        self,
        sector: str,
        symbol: str,
        benchmark_df: Optional[pd.DataFrame],
    ) -> SectorLeadershipResult:
        sector_df = self._fetch(symbol)
        if sector_df is None:
            return SectorLeadershipResult(
                sector=sector,
                symbol=symbol,
                data_quality="missing",
                details={"reason": "sector data unavailable"},
            )

        returns: Dict[str, float] = {}
        relative_returns: Dict[str, float] = {}
        missing_periods: List[str] = []

        for lookback in self.lookback_periods:
            key = f"{lookback}d"
            sector_return = calculate_period_return(sector_df, lookback)
            if sector_return is None:
                missing_periods.append(key)
                continue
            returns[key] = sector_return

            benchmark_return = (
                calculate_period_return(benchmark_df, lookback)
                if benchmark_df is not None
                else None
            )
            if benchmark_return is not None:
                relative_returns[key] = round(sector_return - benchmark_return, 4)

        if not returns:
            return SectorLeadershipResult(
                sector=sector,
                symbol=symbol,
                data_quality="missing",
                details={"reason": "insufficient sector history"},
            )

        quality = "complete"
        if len(returns) < len(self.lookback_periods) or not relative_returns:
            quality = "partial"

        score = compute_leadership_score(relative_returns or returns)
        return SectorLeadershipResult(
            sector=sector,
            symbol=symbol,
            returns=returns,
            relative_returns=relative_returns,
            leadership_score=score,
            trend_direction=determine_sector_trend(relative_returns or returns),
            data_quality=quality,
            details={
                "benchmark": self.benchmark,
                "missing_periods": missing_periods,
            },
        )

    def _fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            return self.price_provider.fetch(symbol, period=self.history_period)
        except Exception:
            return None


def calculate_period_return(df: Optional[pd.DataFrame], lookback_days: int) -> Optional[float]:
    """Return percentage performance over a lookback window."""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    if lookback_days <= 0 or len(df) < lookback_days + 1:
        return None

    start = float(df["Close"].iloc[-(lookback_days + 1)])
    end = float(df["Close"].iloc[-1])
    if start <= 0:
        return None
    return round(((end - start) / start) * 100.0, 4)


def compute_leadership_score(values_by_period: Dict[str, float]) -> float:
    """Convert relative returns into a 0-100 leadership score."""
    if not values_by_period:
        return 50.0

    values = list(values_by_period.values())
    avg = sum(values) / len(values)
    score = 50.0 + (avg * 4.0)
    return round(max(0.0, min(100.0, score)), 2)


def determine_sector_trend(values_by_period: Dict[str, float]) -> str:
    """Classify sector leadership trend from relative returns."""
    if not values_by_period:
        return "stable"

    short = values_by_period.get("5d")
    medium = values_by_period.get("20d")
    long = values_by_period.get("60d")

    if short is not None and medium is not None:
        if short - medium > 1.5:
            return "improving"
        if medium - short > 1.5:
            return "declining"

    avg = sum(values_by_period.values()) / len(values_by_period)
    if avg > 2.0:
        return "leading"
    if avg < -2.0:
        return "lagging"
    if long is not None and short is not None:
        if short > 0 and long < 0:
            return "improving"
        if short < 0 and long > 0:
            return "declining"
    return "stable"


def format_sector_leadership(report: SectorLeadershipReport, top_n: int = 5) -> str:
    """Format a compact sector leadership report."""
    if not report.results:
        return "No sector leadership data available."

    lines = [
        "Sector Leadership",
        f"Benchmark: {report.benchmark}",
        "Observational only -- not used in trading decisions.",
        "",
    ]
    for result in report.results[:top_n]:
        score = f"{result.leadership_score:.0f}"
        rel_20d = result.relative_returns.get("20d")
        rel_text = f", 20d vs {report.benchmark}: {rel_20d:+.1f}%" if rel_20d is not None else ""
        lines.append(
            f"{result.rank}. {result.sector} ({result.symbol}) "
            f"score {score}, {result.trend_direction}{rel_text}"
        )
    return "\n".join(lines)


def get_analyzer(
    sectors: Optional[Dict[str, str]] = None,
    benchmark: str = DEFAULT_BENCHMARK,
) -> SectorLeadershipAnalyzer:
    """Create a SectorLeadershipAnalyzer with default settings."""
    return SectorLeadershipAnalyzer(sectors=sectors, benchmark=benchmark)
