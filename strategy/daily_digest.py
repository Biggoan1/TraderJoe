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
import os
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol

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
    ) -> DailyDigest:
        """Build the full digest."""
        trades = self.logger.get_day_trades(self.date)
        stats = self._compute_stats(trades)

        return DailyDigest(
            stats=stats,
            trades=trades,
            open_positions=open_positions or [],
            feature_flags=feature_flags or {},
            runner_info=runner_info or {},
        )

    # ---- internal ---------------------------------------------------------

    @staticmethod
    def _compute_stats(trades: List[dict]) -> DigestStats:
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

        holding_secs = [t.get("holding_period", 0) or 0 for t in closed]
        avg_holding = (sum(holding_secs) / len(holding_secs)) if holding_secs else 0.0

        return DigestStats(
            date=datetime.now().strftime("%Y-%m-%d"),
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
                qty = t.get("qty", 0) or 0
                pl = t.get("pl")
                reason = t.get("exit_reason", "")
                score = t.get("score")
                holding = t.get("holding_period")

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

        lines.append("")
        lines.append("— *Trade Joe out* —")
        return "\n".join(lines)

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
    ) -> tuple:
        """Full pipeline: build -> render -> save -> send.

        Returns (content: str, filepath: str).
        """
        builder = DailyDigestBuilder(self.logger, date)
        digest = builder.build(open_positions, feature_flags, runner_info)
        content = DigestRenderer.render(digest)
        filepath = self.saver.save(digest, content)

        if self.telegram_sender:
            self.telegram_sender.send_message(content)

        return (content, filepath)
