"""Daily Performance Digest — end-of-day review from trade history.

Generates a post-game review from the SQLite trade log:
  - Daily P/L, win rate, avg winner, avg loser, profit factor
  - Best trade, worst trade, trade count
  - Open positions summary (if broker connected)
  - Active feature flags snapshot
  - "No trades today" message when applicable

Sends to Telegram via callback and saves a dated markdown file locally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from strategy.trade_logger import TradeLogger

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DigestStats:
    """Aggregate stats for a single trading day."""
    date: str
    total_trades: int
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    daily_pl: float
    avg_winner: float
    avg_loser: float
    profit_factor: float
    best_trade_pl: float
    worst_trade_pl: float
    total_fees: float = 0.0
    avg_holding_seconds: float = 0.0

@dataclass
class DigestMetadataSummary:
    """Observational metadata summary for trades in the digest."""
    symbols_traded: List[str] = field(default_factory=list)
    exit_reason_counts: Dict[str, int] = field(default_factory=dict)
    market_regime_counts: Dict[str, int] = field(default_factory=dict)
    active_flag_counts: Dict[str, int] = field(default_factory=dict)
    avg_entry_score: float = 0.0
    trades_with_entry_rs: int = 0
    trades_with_exit_rs: int = 0
    trades_with_metadata: int = 0
    strongest_rs_symbol: Optional[str] = None
    strongest_rs_score: Optional[float] = None
    weakest_rs_symbol: Optional[str] = None
    weakest_rs_score: Optional[float] = None
    missing_exit_reasons: int = 0

@dataclass
class OpenPosition:
    """Snapshot of a currently open position."""
    symbol: str
    qty: float
    entry_price: float
    current_price: Optional[float]
    unrealized_pl: Optional[float]
    entry_time: str

@dataclass
class DailyDigest:
    """Complete daily digest ready for rendering."""
    stats: DigestStats
    trades: List[dict]
    open_positions: List[OpenPosition] = field(default_factory=list)
    feature_flags: dict = field(default_factory=dict)
    runner_info: dict = field(default_factory=dict)
    rs_data: Optional[dict] = None
    metadata_summary: DigestMetadataSummary = field(default_factory=DigestMetadataSummary)

# ---------------------------------------------------------------------------
# Protocol for Telegram delivery
# ---------------------------------------------------------------------------

class TelegramSender(Protocol):
    """Anything that can send a Telegram message."""
    def send_message(self, message: str) -> None: ...

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class DailyDigestBuilder:
    """Build a DailyDigest from TradeLogger data and optional broker state."""

    def __init__(
        self,
        logger: TradeLogger,
        date: Optional[str] = None,
    ):
        self.logger = logger
        self.date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ---- public API -------------------------------------------------------

    def build(
        self,
        open_positions: Optional[List[OpenPosition]] = None,
        feature_flags: Optional[dict] = None,
        runner_info: Optional[dict] = None,
        rs_data: Optional[dict] = None,
    ) -> DailyDigest:
        """Build the full digest."""
        trades = self.logger.get_day_trades(self.date)
        stats = self._compute_stats(trades, self.date)
        metadata_summary = self._compute_metadata_summary(trades)

        return DailyDigest(
            stats=stats,
            trades=trades,
            open_positions=open_positions or [],
            feature_flags=feature_flags or {},
            runner_info=runner_info or {},
            rs_data=rs_data,
            metadata_summary=metadata_summary,
        )

    # ---- internal ---------------------------------------------------------

    @staticmethod
    def _compute_stats(trades: List[dict], date: Optional[str] = None) -> DigestStats:
        closed = [t for t in trades if t.get("exit_price") is not None]
        wins = [t for t in closed if (t.get("pl") or 0) > 0]
        losses = [t for t in closed if (t.get("pl") or 0) <= 0]

        total_pl = sum(t.get("pl", 0) for t in closed)
        total_fees = sum(abs(t.get("fees", 0) or 0) for t in closed)

        avg_winner = (sum(t["pl"] for t in wins) / len(wins)) if wins else 0.0
        avg_loser = (sum(t["pl"] for t in losses) / len(losses)) if losses else 0.0

        gross_profit = sum(t["pl"] for t in wins)
        gross_loss = abs(sum(t["pl"] for t in losses))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0

        best = max((t.get("pl", 0) for t in closed), default=0.0)
        worst = min((t.get("pl", 0) for t in closed), default=0.0)

        holding_secs = [
            (t.get("holding_period") or t.get("holding_period_seconds") or 0)
            for t in closed
        ]
        avg_holding = (sum(holding_secs) / len(holding_secs)) if holding_secs else 0.0

        return DigestStats(
            date=date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            total_trades=len(trades),
            closed_trades=len(closed),
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            daily_pl=round(total_pl, 2),
            avg_winner=round(avg_winner, 2),
            avg_loser=round(avg_loser, 2),
            profit_factor=profit_factor,
            best_trade_pl=round(best, 2),
            worst_trade_pl=round(worst, 2),
            total_fees=round(total_fees, 2),
            avg_holding_seconds=avg_holding,
        )

    @staticmethod
    def _compute_metadata_summary(trades: List[dict]) -> DigestMetadataSummary:
        summary = DigestMetadataSummary()
        symbols = set()
        entry_scores: List[float] = []
        rs_scores: List[tuple] = []

        for trade in trades:
            symbol = trade.get("symbol")
            if symbol:
                symbols.add(str(symbol))

            reason = trade.get("exit_reason")
            if reason:
                _increment(summary.exit_reason_counts, str(reason))
            elif trade.get("exit_price") is not None:
                summary.missing_exit_reasons += 1

            regime = trade.get("market_regime")
            if regime:
                _increment(summary.market_regime_counts, str(regime))

            for flag in _parse_json_list(trade.get("active_flags_entry")):
                _increment(summary.active_flag_counts, str(flag))
            for flag in _parse_json_list(trade.get("active_flags_exit")):
                _increment(summary.active_flag_counts, str(flag))

            score = trade.get("entry_score")
            if score is None:
                score = trade.get("score")
            if score is not None:
                try:
                    entry_scores.append(float(score))
                except (TypeError, ValueError):
                    pass

            entry_rs = _parse_json_dict(trade.get("rs_at_entry"))
            if entry_rs:
                summary.trades_with_entry_rs += 1
                _collect_rs_score(rs_scores, symbol, entry_rs)

            exit_rs = _parse_json_dict(trade.get("rs_at_exit"))
            if exit_rs:
                summary.trades_with_exit_rs += 1
                _collect_rs_score(rs_scores, symbol, exit_rs)

            entry_meta = _parse_json_dict(trade.get("trade_metadata_entry"))
            exit_meta = _parse_json_dict(trade.get("trade_metadata_exit"))
            if entry_meta or exit_meta:
                summary.trades_with_metadata += 1

        summary.symbols_traded = sorted(symbols)
        summary.avg_entry_score = (
            round(sum(entry_scores) / len(entry_scores), 2) if entry_scores else 0.0
        )

        if rs_scores:
            strongest_symbol, strongest_score = max(rs_scores, key=lambda item: item[1])
            weakest_symbol, weakest_score = min(rs_scores, key=lambda item: item[1])
            summary.strongest_rs_symbol = strongest_symbol
            summary.strongest_rs_score = round(strongest_score, 2)
            summary.weakest_rs_symbol = weakest_symbol
            summary.weakest_rs_score = round(weakest_score, 2)

        return summary

# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class DigestRenderer:
    """Render a DailyDigest as formatted text."""

    @staticmethod
    def render(digest: DailyDigest) -> str:
        if digest.stats.total_trades == 0:
            return DigestRenderer._no_trades(digest)

        lines: List[str] = []
        lines.append("📊 *TRADE JOE — DAILY DIGEST*")
        lines.append(f"📅 {digest.stats.date}")
        lines.append("")

        # --- Summary ---
        lines.append("## *PERFORMANCE SUMMARY*")
        pl_sign = "+" if digest.stats.daily_pl >= 0 else ""
        lines.append(f"💰 Daily P/L: *{pl_sign}${digest.stats.daily_pl:,.2f}*")
        lines.append(f"📈 Win Rate: *{digest.stats.win_rate}%*")
        lines.append(f"🔢 Total Trades: *{digest.stats.total_trades}*")
        lines.append(f"✅ Wins: *{digest.stats.wins}* | ❌ Losses: *{digest.stats.losses}*")
        lines.append(f"🏆 Profit Factor: *{digest.stats.profit_factor}*")
        lines.append("")

        # --- Averages ---
        lines.append("## *AVERAGES*")
        lines.append(f"📊 Avg Winner: *+${digest.stats.avg_winner:,.2f}*")
        lines.append(f"📉 Avg Loser: *${digest.stats.avg_loser:,.2f}*")
        avg_held = digest.stats.avg_holding_seconds
        if avg_held >= 3600:
            held_str = f"{avg_held / 3600:.1f}h"
        elif avg_held >= 60:
            held_str = f"{avg_held / 60:.1f}m"
        else:
            held_str = f"{avg_held:.0f}s"
        lines.append(f"⏱ Avg Holding: *{held_str}*")
        lines.append("")

        # --- Best / Worst ---
        lines.append("## *EXTREMES*")
        lines.append(f"🥇 Best Trade: *+${digest.stats.best_trade_pl:,.2f}*")
        lines.append(f"🥉 Worst Trade: *${digest.stats.worst_trade_pl:,.2f}*")
        lines.append("")

        DigestRenderer._append_metadata_sections(lines, digest)

        # --- Open positions ---
        if digest.open_positions:
            lines.append("## *OPEN POSITIONS*")
            for pos in digest.open_positions:
                qty_str = f"{pos.qty:.2f}" if pos.qty != int(pos.qty) else f"{int(pos.qty)}"
                line = f"📂 {pos.symbol} | {qty_str} shares @ ${pos.entry_price:.2f}"
                if pos.current_price:
                    unreal = pos.unrealized_pl or 0
                    u_sign = "+" if unreal >= 0 else ""
                    line += f" | Now: ${pos.current_price:.2f} ({u_sign}${unreal:,.2f})"
                lines.append(line)
            lines.append("")

        # --- Feature flags ---
        if digest.feature_flags:
            active = {k: v for k, v in digest.feature_flags.items() if v}
            if active:
                lines.append("## *ACTIVE FEATURES*")
                for flag, val in active.items():
                    lines.append(f"🔹 {flag}: `{val}`")
                lines.append("")

        # --- Relative Strength ---
        if digest.rs_data:
            lines.append("## *RELATIVE STRENGTH*")
            lines.append("*(Observational only — not used in trading decisions)*")
            lines.append("")
            rs_results = digest.rs_data.get("results", [])
            if rs_results:
                # Sort by score descending
                sorted_results = sorted(rs_results, key=lambda r: r.get("rs_score", 50), reverse=True)
                for r in sorted_results:
                    sym = r.get("symbol", "?")
                    score = r.get("rs_score", 50)
                    trend = r.get("trend_direction", "stable")
                    trend_icon = {"improving": "🟢", "declining": "🔴", "stable": "⚪"}.get(trend, "⚪")
                    line = f"{trend_icon} {sym}: Score {score:.0f} ({trend})"
                    # Show RS vs benchmarks if available
                    rs_bench = r.get("rs_vs_benchmark", {})
                    bench_parts = []
                    for bench, periods in rs_bench.items():
                        for period, val in periods.items():
                            sign = "+" if val >= 0 else ""
                            bench_parts.append(f"{bench} {period}: {sign}{val:.1f}%")
                    if bench_parts:
                        line += f" | {' | '.join(bench_parts)}"
                    lines.append(line)
            else:
                lines.append("No RS data available for today.")
            lines.append("")

        # --- Runner info ---
        if digest.runner_info:
            lines.append("## *RUNNER*")
            for k, v in digest.runner_info.items():
                lines.append(f"• {k}: `{v}`")
            lines.append("")

        # --- Trade details ---
        if digest.trades:
            lines.append("## *TRADE DETAILS*")
            for i, t in enumerate(digest.trades, 1):
                sym = t.get("symbol", "?")
                side = t.get("side", "buy").upper()
                entry = t.get("entry_price", 0) or 0
                exit_p = t.get("exit_price")
                qty = t.get("qty")
                if qty is None:
                    qty = t.get("quantity", 0) or 0
                pl = t.get("pl")
                reason = t.get("exit_reason", "")
                score = t.get("score")
                if score is None:
                    score = t.get("entry_score")
                holding = t.get("holding_period")
                if holding is None:
                    holding = t.get("holding_period_seconds")

                detail = f"{i}. **{sym}** {side}"
                detail += f" | {qty} @ ${entry:.2f}"
                if exit_p is not None:
                    detail += f" → ${exit_p:.2f}"
                    if pl is not None:
                        s = "+" if pl >= 0 else ""
                        detail += f" ({s}${pl:,.2f})"
                if score is not None:
                    detail += f" | Score: {score}"
                if reason:
                    detail += f" | {reason}"
                if holding:
                    h_hrs = holding / 3600
                    if h_hrs >= 1:
                        detail += f" | Held: {h_hrs:.1f}h"
                    else:
                        detail += f" | Held: {holding}s"
                lines.append(detail)
            lines.append("")

        lines.append("— *Trade Joe out* —")
        return "\n".join(lines)

    @staticmethod
    def _append_metadata_sections(lines: List[str], digest: DailyDigest) -> None:
        meta = digest.metadata_summary
        has_metadata = any([
            meta.symbols_traded,
            meta.exit_reason_counts,
            meta.market_regime_counts,
            meta.active_flag_counts,
            meta.avg_entry_score,
            meta.trades_with_entry_rs,
            meta.trades_with_exit_rs,
            meta.trades_with_metadata,
            meta.missing_exit_reasons,
        ])
        if not has_metadata:
            return

        lines.append("## *TRADE METADATA*")
        lines.append("*(Observational only — not used in trading decisions)*")
        if meta.symbols_traded:
            lines.append(f"Symbols: {', '.join(meta.symbols_traded)}")
        if meta.avg_entry_score:
            lines.append(f"Avg Entry Score: *{meta.avg_entry_score:.2f}*")
        if meta.market_regime_counts:
            lines.append(f"Market Context: {_format_counts(meta.market_regime_counts)}")
        if meta.exit_reason_counts:
            lines.append(f"Exit Reasons: {_format_counts(meta.exit_reason_counts)}")
        if meta.active_flag_counts:
            lines.append(f"Flags Observed: {_format_counts(meta.active_flag_counts)}")
        if meta.missing_exit_reasons:
            lines.append(f"Missing Exit Reasons: {meta.missing_exit_reasons}")
        if meta.trades_with_metadata:
            lines.append(f"Custom Metadata Captured: {meta.trades_with_metadata}")
        lines.append("")

        if meta.trades_with_entry_rs or meta.trades_with_exit_rs:
            lines.append("## *RELATIVE STRENGTH SNAPSHOTS*")
            lines.append(
                f"Entry snapshots: {meta.trades_with_entry_rs} | "
                f"Exit snapshots: {meta.trades_with_exit_rs}"
            )
            if meta.strongest_rs_symbol is not None:
                lines.append(
                    f"Strongest: {meta.strongest_rs_symbol} "
                    f"({meta.strongest_rs_score:.0f}/100)"
                )
            if meta.weakest_rs_symbol is not None:
                lines.append(
                    f"Weakest: {meta.weakest_rs_symbol} "
                    f"({meta.weakest_rs_score:.0f}/100)"
                )
            lines.append("")

    @staticmethod
    def _no_trades(digest: DailyDigest) -> str:
        lines: List[str] = []
        lines.append("📊 *TRADE JOE — DAILY DIGEST*")
        lines.append(f"📅 {digest.stats.date}")
        lines.append("")
        lines.append("🔇 *No trades today.*")
        lines.append("")
        lines.append("The scanner watched but no setups passed all six gates.")
        lines.append("Capital preserved. See you tomorrow.")

        if digest.open_positions:
            lines.append("")
            lines.append("## *OPEN POSITIONS*")
            for pos in digest.open_positions:
                qty_str = f"{pos.qty:.2f}" if pos.qty != int(pos.qty) else f"{int(pos.qty)}"
                line = f"📂 {pos.symbol} | {qty_str} shares @ ${pos.entry_price:.2f}"
                if pos.current_price:
                    unreal = pos.unrealized_pl or 0
                    u_sign = "+" if unreal >= 0 else ""
                    line += f" | Now: ${pos.current_price:.2f} ({u_sign}${unreal:,.2f})"
                lines.append(line)

        if digest.feature_flags:
            active = {k: v for k, v in digest.feature_flags.items() if v}
            if active:
                lines.append("")
                lines.append("## *ACTIVE FEATURES*")
                for flag, val in active.items():
                    lines.append(f"🔹 {flag}: `{val}`")

        DigestRenderer._append_metadata_sections(lines, digest)

        lines.append("")
        lines.append("— *Trade Joe out* —")
        return "\n".join(lines)


def _increment(counts: Dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _parse_json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _collect_rs_score(
    rs_scores: List[tuple],
    fallback_symbol: Optional[str],
    snapshot: Dict[str, Any],
) -> None:
    score = snapshot.get("rs_score")
    if score is None:
        return
    try:
        rs_scores.append((str(snapshot.get("symbol") or fallback_symbol or "?"), float(score)))
    except (TypeError, ValueError):
        return


def _format_counts(counts: Dict[str, int]) -> str:
    return ", ".join(
        f"{key}: {count}"
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


# ---------------------------------------------------------------------------
# File saver
# ---------------------------------------------------------------------------

class DigestSaver:
    """Save digest as a dated markdown file."""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = Path(output_dir) if output_dir else Path.home() / "hermes-trader" / "reports"

    def save(self, digest: DailyDigest, content: str) -> str:
        """Save and return the file path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        date_str = digest.stats.date
        filename = f"daily_digest_{date_str}.md"
        filepath = self.output_dir / filename

        filepath.write_text(content, encoding="utf-8")
        return str(filepath)

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class DailyDigestService:
    """Build, render, save, and deliver the daily digest."""

    def __init__(
        self,
        logger: TradeLogger,
        telegram_sender: Optional[TelegramSender] = None,
        output_dir: Optional[str] = None,
    ):
        self.logger = logger
        self.telegram_sender = telegram_sender
        self.saver = DigestSaver(output_dir)

    def generate_and_deliver(
        self,
        date: Optional[str] = None,
        open_positions: Optional[List[OpenPosition]] = None,
        feature_flags: Optional[dict] = None,
        runner_info: Optional[dict] = None,
        rs_data: Optional[dict] = None,
    ) -> tuple:
        """Full pipeline: build -> render -> save -> send.

        Returns (content: str, filepath: str).
        """
        builder = DailyDigestBuilder(self.logger, date)
        digest = builder.build(open_positions, feature_flags, runner_info, rs_data)
        content = DigestRenderer.render(digest)
        filepath = self.saver.save(digest, content)

        if self.telegram_sender:
            self.telegram_sender.send_message(content)

        return (content, filepath)
