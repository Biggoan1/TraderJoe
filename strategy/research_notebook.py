"""Research Notebook
===================

A persistent research notebook that captures daily observations, patterns,
hypotheses, and evidence from Trader Joe's trading data.

Never claims conclusions without supporting evidence. Its purpose is to
document learning over time so future strategy improvements can be driven
by measurable evidence.

Each trading day, the notebook:
  1. Captures market regime and conditions
  2. Identifies strongest/weakest performers
  3. Records interesting trade behavior
  4. Notes emerging patterns
  5. Formulates hypotheses (labeled as such)
  6. Records confidence levels
  7. Flags what additional data is needed

Usage:
    from strategy.research_notebook import ResearchNotebook

    notebook = ResearchNotebook()
    note = notebook.generate_daily_note()
    print(note.content)

    # Or generate programmatically
    note = notebook.generate_note(
        date="2026-07-02",
        regime="bullish",
        performance_data=perf,
        rs_data=rs,
    )
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ResearchNote:
    """A single daily research note."""
    date: str = ""
    content: str = ""
    market_regime: str = ""
    regime_confidence: float = 0.0
    strongest_sectors: List[str] = field(default_factory=list)
    weakest_sectors: List[str] = field(default_factory=list)
    rs_leaders: List[str] = field(default_factory=list)
    rs_laggards: List[str] = field(default_factory=list)
    trade_observations: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    hypotheses: List[str] = field(default_factory=list)
    confidence: str = ""
    data_needed: List[str] = field(default_factory=list)
    trade_count: int = 0
    daily_pl: float = 0.0
    win_rate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "date": self.date,
            "content": self.content,
            "market_regime": self.market_regime,
            "regime_confidence": self.regime_confidence,
            "strongest_sectors": self.strongest_sectors,
            "weakest_sectors": self.weakest_sectors,
            "rs_leaders": self.rs_leaders,
            "rs_laggards": self.rs_laggards,
            "trade_observations": self.trade_observations,
            "patterns": self.patterns,
            "hypotheses": self.hypotheses,
            "confidence": self.confidence,
            "data_needed": self.data_needed,
            "trade_count": self.trade_count,
            "daily_pl": self.daily_pl,
            "win_rate": self.win_rate,
        }

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Research Note — {self.date}",
            "",
        ]

        if self.market_regime:
            lines.append(f"**Market Regime:** {self.market_regime} "
                        f"(confidence: {self.regime_confidence:.0%})")
            lines.append("")

        if self.daily_pl or self.trade_count:
            lines.append(f"**Daily P/L:** ${self.daily_pl:,.2f}")
            lines.append(f"**Trade Count:** {self.trade_count}")
            if self.trade_count:
                lines.append(f"**Win Rate:** {self.win_rate:.1f}%")
            lines.append("")

        if self.content:
            lines.append("## Observations")
            lines.append(self.content)
            lines.append("")

        if self.rs_leaders:
            lines.append("## Relative Strength Leaders")
            for leader in self.rs_leaders:
                lines.append(f"- {leader}")
            lines.append("")

        if self.rs_laggards:
            lines.append("## Relative Strength Laggards")
            for lag in self.rs_laggards:
                lines.append(f"- {lag}")
            lines.append("")

        if self.trade_observations:
            lines.append("## Trade Observations")
            for obs in self.trade_observations:
                lines.append(f"- {obs}")
            lines.append("")

        if self.patterns:
            lines.append("## Emerging Patterns")
            for p in self.patterns:
                lines.append(f"- {p}")
            lines.append("")

        if self.hypotheses:
            lines.append("## Hypotheses")
            lines.append("*These are testable hypotheses, not conclusions.*")
            for h in self.hypotheses:
                lines.append(f"- {h}")
            lines.append("")

        if self.confidence:
            lines.append(f"**Confidence Level:** {self.confidence}")
            lines.append("")

        if self.data_needed:
            lines.append("## Additional Data Needed")
            for d in self.data_needed:
                lines.append(f"- {d}")
            lines.append("")

        lines.append("---")
        lines.append("*Generated by Trade Joe Research Notebook*")
        lines.append("*Never claims conclusions without supporting evidence*")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Notebook
# ---------------------------------------------------------------------------

class ResearchNotebook:
    """Persistent research notebook.

    Stores daily observations as JSON files and Markdown reports.
    Never claims conclusions without supporting evidence.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
        self.notes_dir = self.base_dir / "research_notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.md_dir = self.base_dir / "reports" / "research"
        self.md_dir.mkdir(parents=True, exist_ok=True)

    # ---- Public API -------------------------------------------------------

    def generate_daily_note(
        self,
        date: Optional[str] = None,
        regime_data: Optional[Dict[str, Any]] = None,
        performance_data: Optional[Dict[str, Any]] = None,
        rs_data: Optional[Dict[str, Any]] = None,
        trades: Optional[List[Dict[str, Any]]] = None,
    ) -> ResearchNote:
        """Generate a research note for a trading day.

        Args:
            date: Trading date (YYYY-MM-DD). Defaults to today.
            regime_data: Market regime data from MarketRegimeAnalyzer.
            performance_data: Performance metrics from TradeLogger.
            rs_data: Relative strength data.
            trades: List of trades for the day.

        Returns:
            ResearchNote with observations, patterns, and hypotheses.
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        note = ResearchNote(date=date)

        # Market regime
        if regime_data:
            note.market_regime = regime_data.get("regime", "unknown")
            note.regime_confidence = regime_data.get("confidence", 0.0)
            note.trade_observations.append(
                f"Market regime classified as {note.market_regime} "
                f"(confidence: {note.regime_confidence:.0%})"
            )

        # Performance
        if performance_data:
            note.trade_count = performance_data.get("total_trades", 0)
            note.daily_pl = performance_data.get("total_pl", 0.0)
            note.win_rate = performance_data.get("win_rate", 0.0)

            if note.trade_count > 0:
                pf = performance_data.get("profit_factor", 0.0)
                note.trade_observations.append(
                    f"Profit factor: {pf}"
                )

                avg_w = performance_data.get("average_winner", 0.0)
                avg_l = abs(performance_data.get("average_loser", 0.0))
                if avg_w and avg_l:
                    ratio = avg_w / avg_l
                    note.trade_observations.append(
                        f"Winner/Loser ratio: {ratio:.2f}x"
                    )

        # Relative strength
        if rs_data:
            all_symbols = rs_data.get("all_symbols", [])
            if all_symbols:
                sorted_syms = sorted(all_symbols, key=lambda x: x.get("rs_score", 0), reverse=True)
                for s in sorted_syms[:5]:
                    symbol = s.get("symbol", "?")
                    score = s.get("rs_score", 0)
                    note.rs_leaders.append(f"{symbol} (score: {score:.0f})")

                for s in sorted_syms[-5:]:
                    symbol = s.get("symbol", "?")
                    score = s.get("rs_score", 0)
                    note.rs_laggards.append(f"{symbol} (score: {score:.0f})")

        # Trade observations
        if trades:
            closed_trades = [t for t in trades if t.get("exit_price") is not None]
            if closed_trades:
                exits = {}
                for t in closed_trades:
                    reason = t.get("exit_reason", "unknown") or "unknown"
                    exits[reason] = exits.get(reason, 0) + 1

                if exits:
                    most_common = max(exits, key=lambda k: exits[k])
                    note.trade_observations.append(
                        f"Most common exit reason: {most_common} "
                        f"({exits[most_common]} trades)"
                    )

                # Check holding periods
                holding_times = [
                    t.get("holding_period_seconds", 0) or 0
                    for t in closed_trades
                ]
                if holding_times:
                    avg_hold = sum(holding_times) / len(holding_times)
                    hours = avg_hold / 3600
                    note.trade_observations.append(
                        f"Average holding period: {hours:.1f} hours"
                    )

        # Generate content
        note.content = self._generate_content(note)

        # Detect patterns
        note.patterns = self._detect_patterns(note)

        # Generate hypotheses
        note.hypotheses = self._generate_hypotheses(note)

        # Set confidence level
        note.confidence = self._assess_confidence(note)

        # Identify data needed
        note.data_needed = self._identify_data_gaps(note)

        # Save
        self.save_note(note)

        return note

    def get_recent_notes(self, limit: int = 10) -> List[ResearchNote]:
        """Load recent research notes."""
        notes = []
        for f in sorted(self.notes_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(f.read_text())
                notes.append(ResearchNote(**data))
            except Exception:
                pass
        return notes

    def save_note(self, note: ResearchNote) -> Path:
        """Save a research note as JSON."""
        path = self.notes_dir / f"{note.date}.json"
        path.write_text(json.dumps(note.to_dict(), indent=2, default=str))

        # Also save as markdown
        md_path = self.md_dir / f"{note.date}.md"
        md_path.write_text(note.to_markdown())

        return path

    # ---- Analysis helpers -------------------------------------------------

    def _generate_content(self, note: ResearchNote) -> str:
        """Generate the observation content for the note."""
        observations = []

        if note.market_regime:
            observations.append(
                f"Market in {note.market_regime} regime with "
                f"{note.regime_confidence:.0%} confidence."
            )

        if note.trade_count:
            if note.win_rate >= 70:
                observations.append(
                    f"Strong day: {note.trade_count} trades with "
                    f"{note.win_rate:.0f}% win rate."
                )
            elif note.win_rate >= 50:
                observations.append(
                    f"Average day: {note.trade_count} trades with "
                    f"{note.win_rate:.0f}% win rate."
                )
            else:
                observations.append(
                    f"Below-average day: {note.trade_count} trades with "
                    f"{note.win_rate:.0f}% win rate. "
                    f"More data needed before drawing conclusions."
                )

        if note.rs_leaders:
            observations.append(
                f"Relative strength leaders: {', '.join(note.rs_leaders[:3])}."
            )

        if not observations:
            observations.append(
                "Limited data available for today. "
                "Continuing to collect evidence."
            )

        return "\n".join(observations)

    def _detect_patterns(self, note: ResearchNote) -> List[str]:
        """Detect emerging patterns from today's data and recent history.

        Returns a list of pattern observations — always labeled as
        observations, not conclusions.
        """
        patterns = []

        # Check if strong RS stocks win more often
        if note.rs_leaders and note.trade_observations:
            patterns.append(
                "Observation: Relative strength leaders may correlate with "
                "better trade outcomes. Insufficient data to confirm."
            )

        # Check regime correlation
        if note.market_regime and note.trade_count > 0:
            if note.daily_pl > 0 and note.market_regime == "bullish":
                patterns.append(
                    f"Observation: Positive P/L ({note.daily_pl:+.2f}) "
                    f"observed in {note.market_regime} regime today. "
                    f"Sample size too small to establish correlation."
                )
            elif note.daily_pl < 0 and note.market_regime == "bullish":
                patterns.append(
                    f"Observation: Negative P/L ({note.daily_pl:+.2f}) "
                    f"despite {note.market_regime} regime. "
                    f"Regime alone may not predict daily outcomes."
                )

        # Look at recent history for multi-day patterns
        recent = self.get_recent_notes(limit=5)
        if len(recent) >= 3:
            bullish_days = [r for r in recent if r.market_regime == "bullish"]
            if bullish_days:
                bullish_pl = [r.daily_pl for r in bullish_days]
                avg_bullish = sum(bullish_pl) / len(bullish_pl)
                if avg_bullish > 0:
                    patterns.append(
                        f"Observation: Bullish regime days average "
                        f"${avg_bullish:+.2f} P/L over {len(bullish_days)} days. "
                        f"More samples needed to validate."
                    )

        return patterns

    def _generate_hypotheses(self, note: ResearchNote) -> List[str]:
        """Generate testable hypotheses from observations.

        Hypotheses are always clearly labeled as such — they are NOT
        conclusions or recommendations.
        """
        hypotheses = []

        if note.rs_leaders:
            hypotheses.append(
                "Hypothesis: Trades in high relative strength symbols "
                "(score > 70) may have higher win rates than the overall "
                "average. Requires 20+ trades to validate."
            )

        if note.market_regime:
            hypotheses.append(
                f"Hypothesis: {note.market_regime.capitalize()} market "
                f"regime may favor shorter holding periods with tighter "
                f"stops. Requires regime-tagged trade analysis to validate."
            )

        if note.trade_count > 0:
            hypotheses.append(
                "Hypothesis: Trade outcomes may be influenced by time of "
                "day. Entry time analysis needed to test this."
            )

        return hypotheses

    def _assess_confidence(self, note: ResearchNote) -> str:
        """Assess confidence level based on available data.

        Confidence scales with data quantity and consistency.
        """
        if note.trade_count < 5:
            return "Low — insufficient trade data for meaningful conclusions"
        elif note.trade_count < 20:
            return "Medium-Low — building evidence but more data needed"
        elif note.trade_count < 50:
            return "Medium — moderate evidence, patterns emerging"
        else:
            return "Medium-High — sufficient data for preliminary conclusions"

    def _identify_data_gaps(self, note: ResearchNote) -> List[str]:
        """Identify what additional data is needed to draw conclusions."""
        gaps = []

        if note.trade_count < 20:
            gaps.append("More closed trades needed (currently < 20)")

        if not note.rs_leaders:
            gaps.append("Relative strength data needed for trade correlation analysis")

        if not note.market_regime or note.market_regime == "unknown":
            gaps.append("Market regime data needed to test regime-based hypotheses")

        gaps.append("Entry/exit time data needed to test time-of-day effects")
        gaps.append("Sector classification data needed for sector analysis")

        return gaps


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Generate today's research note via CLI."""
    import sys

    base_dir = Path(__file__).resolve().parent.parent
    notebook = ResearchNotebook(str(base_dir))

    date_arg = None
    if len(sys.argv) > 1:
        date_arg = sys.argv[1]

    note = notebook.generate_daily_note(date=date_arg)
    print(note.to_markdown())


if __name__ == "__main__":
    main()
