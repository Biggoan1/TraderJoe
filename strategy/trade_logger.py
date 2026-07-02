"""
Historical Trade Logger
=======================

Records every trade to a durable SQLite database with full context:
  - symbol, side, entry/exit times, prices, quantity
  - P/L, P/L percent, holding period
  - entry score, exit reason, market regime
  - active feature flags at trade entry and exit

Used for future analysis — does NOT affect trading decisions.

Usage:
    from strategy.trade_logger import TradeLogger

    logger = TradeLogger()

    # Log a trade entry (buy)
    logger.log_trade_entry(
        symbol="NVDA",
        side="buy",
        entry_price=120.50,
        quantity=100,
        entry_score=7.5,
        exit_reason=None,
        market_regime=None,
        active_flags=[],
    )

    # Log a trade exit (sell) — auto-calculates P/L
    logger.log_trade_exit(
        symbol="NVDA",
        exit_price=125.00,
        exit_reason="overbought RSI",
        market_regime="bull",
        active_flags=[],
    )

    # Get summary report
    report = logger.get_summary_report()
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Default database location
DEFAULT_DB_PATH = Path("trades_history.db")


class TradeLogger:
    """SQLite-backed trade logger with full context capture."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._init_db()

    def _init_db(self):
        """Create the trades table if it doesn't exist."""
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                pl REAL,
                pl_percent REAL,
                holding_period_seconds REAL,
                entry_score REAL,
                exit_reason TEXT,
                market_regime TEXT,
                active_flags_entry TEXT,
                active_flags_exit TEXT,
                rs_at_entry TEXT,
                rs_at_exit TEXT,
                trade_metadata_entry TEXT,
                trade_metadata_exit TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._ensure_column(cur, "rs_at_entry", "TEXT")
        self._ensure_column(cur, "rs_at_exit", "TEXT")
        self._ensure_column(cur, "trade_metadata_entry", "TEXT")
        self._ensure_column(cur, "trade_metadata_exit", "TEXT")

        # Indexes for fast lookups
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)
        """)

        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_column(cur: sqlite3.Cursor, column_name: str, column_type: str) -> None:
        """Add a nullable column for older trade databases."""
        columns = {
            row[1] for row in cur.execute("PRAGMA table_info(trades)").fetchall()
        }
        if column_name not in columns:
            cur.execute(f"ALTER TABLE trades ADD COLUMN {column_name} {column_type}")

    def log_trade_entry(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        entry_score: Optional[float] = None,
        exit_reason: Optional[str] = None,
        market_regime: Optional[str] = None,
        active_flags: Optional[List[str]] = None,
        rs_snapshot: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Log a trade entry (buy or sell).

        Args:
            symbol: Ticker symbol
            side: 'buy' or 'sell'
            entry_price: Price at entry
            quantity: Number of shares
            entry_score: Setup score if available
            exit_reason: Not applicable for entry (use None)
            market_regime: Market regime if known
            active_flags: List of active feature flags
            rs_snapshot: Relative strength snapshot dict (observational only)
            metadata: Additional observational trade context

        Returns:
            Trade ID
        """
        now = datetime.now(timezone.utc).isoformat()
        flags_json = json.dumps(active_flags or [])
        rs_json = json.dumps(rs_snapshot) if rs_snapshot else None
        metadata_json = json.dumps(metadata) if metadata else None

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO trades (
                symbol, side, entry_time, exit_time,
                entry_price, exit_price, quantity,
                pl, pl_percent, holding_period_seconds,
                entry_score, exit_reason, market_regime,
                active_flags_entry, active_flags_exit,
                rs_at_entry, rs_at_exit,
                trade_metadata_entry, trade_metadata_exit,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?,
                NULL,
                ?, NULL, ?,
                NULL, NULL, NULL,
                ?, ?, ?,
                ?, NULL,
                ?, NULL,
                ?, NULL,
                ?, ?
            )
        """, (
            symbol,
            side,
            now,
            entry_price,
            quantity,
            entry_score,
            exit_reason,
            market_regime,
            flags_json,
            rs_json,
            metadata_json,
            now,
            now,
        ))

        trade_id = cur.lastrowid
        conn.commit()
        conn.close()

        assert trade_id is not None and trade_id > 0
        return int(trade_id)

    def log_trade_exit(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: Optional[str] = None,
        market_regime: Optional[str] = None,
        active_flags: Optional[List[str]] = None,
        rs_snapshot: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Log a trade exit, closing out an open trade.

        Finds the most recent open trade for the symbol and updates it
        with exit info. Auto-calculates P/L and holding period.

        Args:
            symbol: Ticker symbol
            exit_price: Price at exit
            exit_reason: Reason for exit (e.g., 'overbought RSI')
            market_regime: Market regime at exit
            active_flags: List of active feature flags at exit
            rs_snapshot: Relative strength snapshot at exit (observational only)
            metadata: Additional observational trade context at exit

        Returns:
            Trade dict with calculated P/L, or None if no open trade found
        """
        now = datetime.now(timezone.utc).isoformat()
        flags_json = json.dumps(active_flags or [])
        rs_json = json.dumps(rs_snapshot) if rs_snapshot else None
        metadata_json = json.dumps(metadata) if metadata else None

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()

        # Find the most recent open trade for this symbol
        row = cur.execute("""
            SELECT id, symbol, side, entry_time, entry_price, quantity,
                   entry_score, market_regime, active_flags_entry
            FROM trades
            WHERE symbol = ? AND exit_time IS NULL
            ORDER BY entry_time DESC
            LIMIT 1
        """, (symbol,)).fetchone()

        if row is None:
            conn.close()
            return None

        trade_id = row[0]
        trade_symbol = row[1]
        side = row[2]
        entry_time_str = row[3]
        entry_price = float(row[4])
        quantity = float(row[5])
        entry_score = row[6]
        entry_regime = row[7]
        entry_flags = row[8]

        # Calculate P/L
        if side == "buy":
            pl = (exit_price - entry_price) * quantity
        else:  # sell
            pl = (entry_price - exit_price) * quantity

        pl_percent = (pl / (entry_price * quantity)) * 100 if entry_price > 0 else 0

        # Calculate holding period
        entry_time = datetime.fromisoformat(entry_time_str)
        exit_time = datetime.fromisoformat(now)
        holding_seconds = (exit_time - entry_time).total_seconds()

        # Update the trade
        cur.execute("""
            UPDATE trades
            SET exit_time = ?,
                exit_price = ?,
                pl = ?,
                pl_percent = ?,
                holding_period_seconds = ?,
                exit_reason = ?,
                market_regime = ?,
                active_flags_exit = ?,
                rs_at_exit = ?,
                trade_metadata_exit = ?,
                updated_at = ?
            WHERE id = ?
        """, (
            now,
            exit_price,
            pl,
            pl_percent,
            holding_seconds,
            exit_reason,
            market_regime,
            flags_json,
            rs_json,
            metadata_json,
            now,
            trade_id,
        ))

        conn.commit()

        # Return the completed trade
        result = cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        columns = [desc[0] for desc in cur.description]
        trade_dict = dict(zip(columns, result))

        conn.close()

        return trade_dict

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Get all open (unclosed) trades."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = cur.execute("""
            SELECT * FROM trades
            WHERE exit_time IS NULL
            ORDER BY entry_time DESC
        """).fetchall()

        trades = [dict(row) for row in rows]
        conn.close()

        return trades

    def get_closed_trades(self) -> List[Dict[str, Any]]:
        """Get all closed trades."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = cur.execute("""
            SELECT * FROM trades
            WHERE exit_time IS NOT NULL
            ORDER BY exit_time DESC
        """).fetchall()

        trades = [dict(row) for row in rows]
        conn.close()

        return trades

    def get_all_trades(self) -> List[Dict[str, Any]]:
        """Get all trades."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = cur.execute("""
            SELECT * FROM trades
            ORDER BY entry_time DESC
        """).fetchall()

        trades = [dict(row) for row in rows]
        conn.close()

        return trades

    def get_symbol_trades(self, symbol: str) -> List[Dict[str, Any]]:
        """Get all trades for a specific symbol."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = cur.execute("""
            SELECT * FROM trades
            WHERE symbol = ?
            ORDER BY entry_time DESC
        """, (symbol,)).fetchall()

        trades = [dict(row) for row in rows]
        conn.close()

        return trades

    def get_day_trades(self, date: str) -> List[Dict[str, Any]]:
        """Get all trades that occurred on a specific date (UTC).

        Args:
            date: Date string in YYYY-MM-DD format.

        Returns:
            List of trade dicts for that day.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        day_start = f"{date}T00:00:00"
        day_end = f"{date}T23:59:59"

        rows = cur.execute("""
            SELECT * FROM trades
            WHERE entry_time >= ? AND entry_time <= ?
            ORDER BY entry_time ASC
        """, (day_start, day_end)).fetchall()

        trades = [dict(row) for row in rows]
        conn.close()

        return trades

    def get_summary_report(self) -> Dict[str, Any]:
        """Generate a summary report from all closed trades.

        Returns dict with:
            - total_trades
            - win_rate
            - average_winner
            - average_loser
            - profit_factor
            - total_pl
            - total_pl_percent
            - avg_holding_period_seconds
            - best_trade
            - worst_trade
        """
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()

        # Total closed trades
        total = cur.execute("""
            SELECT COUNT(*) FROM trades WHERE exit_time IS NOT NULL
        """).fetchone()[0]

        if total == 0:
            conn.close()
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "average_winner": 0.0,
                "average_loser": 0.0,
                "profit_factor": 0.0,
                "total_pl": 0.0,
                "total_pl_percent": 0.0,
                "avg_holding_period_seconds": 0.0,
                "best_trade": None,
                "worst_trade": None,
                "trades_by_side": {},
                "trades_by_exit_reason": {},
            }

        # Wins and losses
        wins = cur.execute("""
            SELECT COUNT(*) FROM trades
            WHERE exit_time IS NOT NULL AND pl > 0
        """).fetchone()[0]

        losses = cur.execute("""
            SELECT COUNT(*) FROM trades
            WHERE exit_time IS NOT NULL AND pl < 0
        """).fetchone()[0]

        # Average winner
        avg_winner_row = cur.execute("""
            SELECT AVG(pl) FROM trades
            WHERE exit_time IS NOT NULL AND pl > 0
        """).fetchone()
        avg_winner = avg_winner_row[0] if avg_winner_row[0] is not None else 0.0

        # Average loser (absolute value)
        avg_loser_row = cur.execute("""
            SELECT AVG(pl) FROM trades
            WHERE exit_time IS NOT NULL AND pl < 0
        """).fetchone()
        avg_loser = avg_loser_row[0] if avg_loser_row[0] is not None else 0.0

        # Profit factor = gross_profit / |gross_loss|
        gross_profit = cur.execute("""
            SELECT SUM(pl) FROM trades
            WHERE exit_time IS NOT NULL AND pl > 0
        """).fetchone()[0] or 0.0

        gross_loss = cur.execute("""
            SELECT SUM(pl) FROM trades
            WHERE exit_time IS NOT NULL AND pl < 0
        """).fetchone()[0] or 0.0

        profit_factor = (
            gross_profit / abs(gross_loss) if gross_loss != 0 else float("inf")
        )

        # Total P/L
        total_pl_row = cur.execute("""
            SELECT SUM(pl) FROM trades WHERE exit_time IS NOT NULL
        """).fetchone()
        total_pl = total_pl_row[0] if total_pl_row[0] is not None else 0.0

        # Average holding period
        avg_hold_row = cur.execute("""
            SELECT AVG(holding_period_seconds) FROM trades
            WHERE exit_time IS NOT NULL
        """).fetchone()
        avg_hold = avg_hold_row[0] if avg_hold_row[0] is not None else 0.0

        # Best trade
        best_row = cur.execute("""
            SELECT symbol, pl, pl_percent, entry_time, exit_time, exit_reason
            FROM trades
            WHERE exit_time IS NOT NULL
            ORDER BY pl DESC
            LIMIT 1
        """).fetchone()

        best_trade = {
            "symbol": best_row[0],
            "pl": best_row[1],
            "pl_percent": best_row[2],
            "entry_time": best_row[3],
            "exit_time": best_row[4],
            "exit_reason": best_row[5],
        } if best_row else None

        # Worst trade
        worst_row = cur.execute("""
            SELECT symbol, pl, pl_percent, entry_time, exit_time, exit_reason
            FROM trades
            WHERE exit_time IS NOT NULL
            ORDER BY pl ASC
            LIMIT 1
        """).fetchone()

        worst_trade = {
            "symbol": worst_row[0],
            "pl": worst_row[1],
            "pl_percent": worst_row[2],
            "entry_time": worst_row[3],
            "exit_time": worst_row[4],
            "exit_reason": worst_row[5],
        } if worst_row else None

        # Trades by side
        side_rows = cur.execute("""
            SELECT side, COUNT(*) FROM trades
            WHERE exit_time IS NOT NULL
            GROUP BY side
        """).fetchall()
        trades_by_side = {row[0]: row[1] for row in side_rows}

        # Trades by exit reason
        reason_rows = cur.execute("""
            SELECT COALESCE(exit_reason, 'UNKNOWN'), COUNT(*)
            FROM trades
            WHERE exit_time IS NOT NULL
            GROUP BY exit_reason
            ORDER BY COUNT(*) DESC
        """).fetchall()
        trades_by_exit_reason = {row[0]: row[1] for row in reason_rows}

        conn.close()

        win_rate = (wins / total * 100) if total > 0 else 0.0

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
            "average_winner": round(avg_winner, 2),
            "average_loser": round(avg_loser, 2),
            "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else "inf",
            "total_pl": round(total_pl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "avg_holding_period_seconds": round(avg_hold, 2),
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "trades_by_side": trades_by_side,
            "trades_by_exit_reason": trades_by_exit_reason,
        }

    def format_report(self, report: Optional[Dict[str, Any]] = None) -> str:
        """Format a summary report as a human-readable string."""
        if report is None:
            report = self.get_summary_report()

        if report["total_trades"] == 0:
            return "No closed trades yet in the database."

        lines = [
            "📊 Trade History Summary",
            "=" * 40,
            f"Total Trades: {report['total_trades']}",
            f"Wins: {report['wins']} | Losses: {report['losses']}",
            f"Win Rate: {report['win_rate']:.1f}%",
            "",
            f"Average Winner: ${report['average_winner']:,.2f}",
            f"Average Loser: ${report['average_loser']:,.2f}",
            f"Profit Factor: {report['profit_factor']}",
            "",
            f"Gross Profit: ${report['gross_profit']:,.2f}",
            f"Gross Loss: ${report['gross_loss']:,.2f}",
            f"Total P/L: ${report['total_pl']:,.2f}",
            "",
            f"Avg Hold Time: {report['avg_holding_period_seconds'] / 3600:.1f} hours",
        ]

        if report["best_trade"]:
            lines.append("")
            lines.append("Best Trade:")
            bt = report["best_trade"]
            lines.append(
                f"  {bt['symbol']} +${bt['pl']:,.2f} ({bt['pl_percent']:.1f}%) "
                f"— exit: {bt['exit_reason']}"
            )

        if report["worst_trade"]:
            lines.append("")
            lines.append("Worst Trade:")
            wt = report["worst_trade"]
            lines.append(
                f"  {wt['symbol']} ${wt['pl']:,.2f} ({wt['pl_percent']:.1f}%) "
                f"— exit: {wt['exit_reason']}"
            )

        if report["trades_by_exit_reason"]:
            lines.append("")
            lines.append("Exit Reasons:")
            for reason, count in report["trades_by_exit_reason"].items():
                lines.append(f"  {reason}: {count}")

        return "\n".join(lines)

    def export_jsonl(self, output_path: Optional[str] = None) -> str:
        """Export all trades to JSONL format.

        Args:
            output_path: Optional path for the JSONL file.
                         Defaults to trades_history.jsonl

        Returns:
            Path to the exported file
        """
        if output_path is None:
            output_path = str(self.db_path.with_suffix(".jsonl"))

        trades = self.get_all_trades()

        with open(output_path, "w") as f:
            for trade in trades:
                # Parse JSON fields
                if trade.get("active_flags_entry"):
                    try:
                        trade["active_flags_entry"] = json.loads(trade["active_flags_entry"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if trade.get("active_flags_exit"):
                    try:
                        trade["active_flags_exit"] = json.loads(trade["active_flags_exit"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if trade.get("rs_at_entry"):
                    try:
                        trade["rs_at_entry"] = json.loads(trade["rs_at_entry"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if trade.get("rs_at_exit"):
                    try:
                        trade["rs_at_exit"] = json.loads(trade["rs_at_exit"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if trade.get("trade_metadata_entry"):
                    try:
                        trade["trade_metadata_entry"] = json.loads(trade["trade_metadata_entry"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if trade.get("trade_metadata_exit"):
                    try:
                        trade["trade_metadata_exit"] = json.loads(trade["trade_metadata_exit"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                f.write(json.dumps(trade) + "\n")

        return output_path
