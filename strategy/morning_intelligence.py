"""Morning Intelligence Agent -- observational pre-market briefing.

Builds a morning market context report from existing observational modules:
market regime, overnight risk, and relative strength. The report is read-only;
it produces no trading signals, places no orders, and does not alter Champion
or Challenger strategy behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from strategy.config import get_feature_flags
from strategy.market_regime import MarketRegimeAnalyzer
from strategy.overnight_risk import OvernightRiskEngine
from strategy.relative_strength import RelativeStrengthCalculator


DEFAULT_TOP_RS_COUNT = 5


class RegimeProvider(Protocol):
    def classify(self) -> Any: ...


class OvernightProvider(Protocol):
    def assess(self) -> Any: ...


class RelativeStrengthProvider(Protocol):
    def calculate_watchlist_rs(self, symbols: List[str], period: str = "6mo") -> List[Any]: ...


@dataclass
class MorningIntelligenceReport:
    """Structured morning context report.

    All fields are observational. Avoid adding recommendation/action fields
    unless a later sprint explicitly promotes validated decision logic.
    """

    date: str
    timestamp: str
    watchlist: List[str] = field(default_factory=list)
    feature_flag_enabled: bool = False
    market_regime: Optional[Dict[str, Any]] = None
    overnight_risk: Optional[Dict[str, Any]] = None
    relative_strength: List[Dict[str, Any]] = field(default_factory=list)
    headline: str = ""
    key_points: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    opportunities: List[str] = field(default_factory=list)
    data_quality: str = "missing"
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON storage or downstream reporting."""
        return {
            "date": self.date,
            "timestamp": self.timestamp,
            "watchlist": list(self.watchlist),
            "feature_flag_enabled": self.feature_flag_enabled,
            "market_regime": self.market_regime,
            "overnight_risk": self.overnight_risk,
            "relative_strength": list(self.relative_strength),
            "headline": self.headline,
            "key_points": list(self.key_points),
            "risks": list(self.risks),
            "opportunities": list(self.opportunities),
            "data_quality": self.data_quality,
            "errors": list(self.errors),
        }


class MorningIntelligenceRenderer:
    """Render MorningIntelligenceReport as compact Markdown."""

    @staticmethod
    def render(report: MorningIntelligenceReport) -> str:
        lines: List[str] = [
            "# Trade Joe Morning Intelligence",
            f"Date: {report.date}",
            "",
            report.headline or "Morning context unavailable.",
            "",
            "Observational only. Not used in trading decisions.",
            "",
        ]

        if report.key_points:
            lines.append("## Key Points")
            lines.extend(f"- {point}" for point in report.key_points)
            lines.append("")

        if report.risks:
            lines.append("## Risks To Watch")
            lines.extend(f"- {risk}" for risk in report.risks)
            lines.append("")

        if report.opportunities:
            lines.append("## Areas To Watch")
            lines.extend(f"- {item}" for item in report.opportunities)
            lines.append("")

        if report.errors:
            lines.append("## Data Gaps")
            lines.extend(f"- {err}" for err in report.errors)
            lines.append("")

        lines.append(f"Data quality: {report.data_quality}")
        return "\n".join(lines)


class MorningIntelligenceAgent:
    """Generate an observational morning briefing from existing analyzers."""

    def __init__(
        self,
        watchlist: Optional[List[str]] = None,
        regime_provider: Optional[RegimeProvider] = None,
        overnight_provider: Optional[OvernightProvider] = None,
        relative_strength_provider: Optional[RelativeStrengthProvider] = None,
        top_rs_count: int = DEFAULT_TOP_RS_COUNT,
    ):
        self.watchlist = watchlist or []
        self.regime_provider = regime_provider
        self.overnight_provider = overnight_provider
        self.relative_strength_provider = relative_strength_provider
        self.top_rs_count = max(0, top_rs_count)

    def generate(self) -> MorningIntelligenceReport:
        """Generate the morning intelligence report.

        Failures in one source are captured as data gaps so the remaining
        context can still be reported.
        """
        now = datetime.now(timezone.utc)
        flags = get_feature_flags()
        report = MorningIntelligenceReport(
            date=now.strftime("%Y-%m-%d"),
            timestamp=now.isoformat(),
            watchlist=list(self.watchlist),
            feature_flag_enabled=flags.enable_morning_intelligence,
        )

        self._attach_market_regime(report)
        self._attach_overnight_risk(report)
        self._attach_relative_strength(report)
        self._summarize(report)
        return report

    def render(self) -> str:
        """Generate and render the report."""
        return MorningIntelligenceRenderer.render(self.generate())

    def _attach_market_regime(self, report: MorningIntelligenceReport) -> None:
        provider = self.regime_provider or MarketRegimeAnalyzer()
        try:
            result = provider.classify()
            report.market_regime = _to_dict(result)
        except Exception as exc:
            report.errors.append(f"Market regime unavailable: {exc}")

    def _attach_overnight_risk(self, report: MorningIntelligenceReport) -> None:
        provider = self.overnight_provider or OvernightRiskEngine(symbols=self.watchlist)
        try:
            result = provider.assess()
            report.overnight_risk = _to_dict(result)
        except Exception as exc:
            report.errors.append(f"Overnight risk unavailable: {exc}")

    def _attach_relative_strength(self, report: MorningIntelligenceReport) -> None:
        if not self.watchlist:
            report.errors.append("Relative strength unavailable: empty watchlist")
            return

        provider = self.relative_strength_provider or RelativeStrengthCalculator()
        try:
            results = provider.calculate_watchlist_rs(self.watchlist)
            serialized = [_to_dict(result) for result in results]
            report.relative_strength = sorted(
                serialized,
                key=lambda item: item.get("rs_score", 50.0),
                reverse=True,
            )[: self.top_rs_count]
        except Exception as exc:
            report.errors.append(f"Relative strength unavailable: {exc}")

    def _summarize(self, report: MorningIntelligenceReport) -> None:
        regime = report.market_regime or {}
        overnight = report.overnight_risk or {}
        rs = report.relative_strength

        regime_name = regime.get("regime", "unknown")
        confidence = regime.get("confidence")
        if confidence is not None:
            report.key_points.append(
                f"Market regime: {regime_name} ({confidence:.0%} confidence)"
            )
        elif report.market_regime:
            report.key_points.append(f"Market regime: {regime_name}")

        significant_gaps = overnight.get("significant_gaps")
        analyzed = overnight.get("symbols_analyzed")
        if significant_gaps is not None and analyzed is not None:
            report.key_points.append(
                f"Overnight gaps: {significant_gaps} significant across {analyzed} symbols"
            )

        if rs:
            top = rs[0]
            report.key_points.append(
                f"Relative strength leader: {top.get('symbol', '?')} "
                f"({top.get('rs_score', 50):.0f}/100)"
            )

        self._summarize_risks(report, regime_name, overnight)
        self._summarize_opportunities(report, regime_name, rs)

        components = [
            report.market_regime is not None,
            report.overnight_risk is not None,
            bool(report.relative_strength),
        ]
        present = sum(1 for item in components if item)
        if present == len(components):
            report.data_quality = "complete"
        elif present > 0:
            report.data_quality = "partial"
        else:
            report.data_quality = "missing"

        gap_phrase = "unknown overnight gaps"
        if significant_gaps is not None:
            gap_phrase = f"{significant_gaps} significant overnight gaps"
        report.headline = (
            f"Morning context: {regime_name} regime, {gap_phrase}, "
            f"{len(rs)} relative strength leaders tracked."
        )

    @staticmethod
    def _summarize_risks(
        report: MorningIntelligenceReport,
        regime_name: str,
        overnight: Dict[str, Any],
    ) -> None:
        if regime_name in {"bearish", "volatile"}:
            report.risks.append(f"{regime_name.capitalize()} market regime")

        significant_gaps = overnight.get("significant_gaps", 0)
        if significant_gaps:
            report.risks.append(f"{significant_gaps} significant overnight gap(s)")

        missing = overnight.get("symbols_missing_data", 0)
        if missing:
            report.risks.append(f"{missing} symbol(s) missing overnight data")

    @staticmethod
    def _summarize_opportunities(
        report: MorningIntelligenceReport,
        regime_name: str,
        relative_strength: List[Dict[str, Any]],
    ) -> None:
        if regime_name == "bullish":
            report.opportunities.append("Bullish regime context")

        for item in relative_strength:
            score = item.get("rs_score", 50.0)
            trend = item.get("trend_direction", "stable")
            if score >= 70 and trend == "improving":
                report.opportunities.append(
                    f"{item.get('symbol', '?')} relative strength improving "
                    f"({score:.0f}/100)"
                )


def _to_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return dict(value)
    return dict(value.__dict__)


def get_agent(
    watchlist: Optional[List[str]] = None,
    top_rs_count: int = DEFAULT_TOP_RS_COUNT,
) -> MorningIntelligenceAgent:
    """Create a MorningIntelligenceAgent with default providers."""
    return MorningIntelligenceAgent(
        watchlist=watchlist or [],
        top_rs_count=top_rs_count,
    )
