#!/usr/bin/env python
"""Generate and send the daily performance digest.

Usage:
    python scripts/generate_daily_digest.py [--date YYYY-MM-DD] [--dry-run]

When run, this script:
  1. Pulls today's trades from the SQLite trade log
  2. Calculates all performance metrics
  3. Sends the formatted report via Telegram
  4. Saves a dated markdown file in ~/hermes-trader/reports/

The --date flag lets you generate a digest for any date.
The --dry-run flag saves the file but skips Telegram.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from strategy.trade_logger import TradeLogger
from strategy.daily_digest import DailyDigestService, OpenPosition


def get_open_positions_from_broker():
    """Get current positions from Alpaca broker."""
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            os.getenv("ALPACA_API_KEY"),
            os.getenv("ALPACA_SECRET_KEY"),
            paper=True,
        )
        positions = client.get_all_positions()

        result = []
        for pos in positions:
            try:
                import yfinance as yf
                ticker = yf.Ticker(pos.symbol)
                hist = ticker.history(period="1d")
                current_price = hist["Close"].iloc[-1] if not hist.empty else None
            except Exception:
                current_price = None

            entry_cost = float(pos.avg_entry_price) if hasattr(pos, "avg_entry_price") else 0.0
            qty = float(pos.qty)
            mkt_val = float(pos.market_value) if pos.market_value else 0.0
            cost_basis = entry_cost * qty
            unrealized_pl = mkt_val - cost_basis if entry_cost > 0 else 0

            result.append(OpenPosition(
                symbol=pos.symbol,
                qty=qty,
                entry_price=entry_cost,
                current_price=current_price,
                unrealized_pl=unrealized_pl,
                entry_time=str(pos.avg_entry_price or ""),  # Placeholder
            ))

        return result
    except Exception as e:
        print(f"Could not fetch broker positions: {e}")
        return []


def get_feature_flags():
    """Get current feature flag state."""
    try:
        from strategy.config import get_feature_flags
        flags = get_feature_flags()
        # Only include boolean attributes (not methods)
        return {name: getattr(flags, name) for name in dir(flags)
                if not name.startswith("_") and not callable(getattr(flags, name))}
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser(description="Generate daily trading digest")
    parser.add_argument("--date", type=str, default=None, help="Date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Save file but skip Telegram")
    args = parser.parse_args()

    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Initialize logger
    db_path = project_root / "trades_history.db"
    logger = TradeLogger(db_path=str(db_path))

    # Get positions
    open_positions = get_open_positions_from_broker()

    # Get feature flags
    feature_flags = get_feature_flags()

    # Build runner info
    runner_info = {
        "date": date,
        "db_path": str(db_path),
    }

    # Telegram sender
    telegram_sender = None
    if not args.dry_run:
        class TelegramWrapper:
            def send_message(self, message: str):
                from telegram_approvals import send_telegram
                send_telegram(message)

        telegram_sender = TelegramWrapper()

    # Generate and deliver
    service = DailyDigestService(
        logger=logger,
        telegram_sender=telegram_sender,
        output_dir=str(project_root / "reports"),
    )

    content, filepath = service.generate_and_deliver(
        date=date,
        open_positions=open_positions,
        feature_flags=feature_flags,
        runner_info=runner_info,
    )

    print(f"Daily digest generated for {date}")
    print(f"Saved to: {filepath}")
    if not args.dry_run:
        print("Sent via Telegram ✓")
    else:
        print("Dry run — Telegram skipped")

    # Print the digest content
    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)


if __name__ == "__main__":
    main()
