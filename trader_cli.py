#!/usr/bin/env python3
"""JSON command bridge for Hermes Agent and local automation."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import telegram_approvals

ROOT = Path(__file__).resolve().parent
DB_FILE = ROOT / "trades.db"


def _pending_approvals():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, created_at, symbol, COALESCE(side, 'BUY') AS side,
               price, amount, status
        FROM pending_approvals
        WHERE status = 'PENDING'
        ORDER BY id
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _run_message(text):
    messages = []
    original_send = telegram_approvals.send_telegram
    telegram_approvals.send_telegram = messages.append

    try:
        telegram_approvals.process_message(text)
    finally:
        telegram_approvals.send_telegram = original_send

    return messages


def _trending():
    """Load and return the trending watchlist from the market screener."""
    trending_file = ROOT / "trending_watchlist.json"
    if not trending_file.exists():
        return {
            "ok": True,
            "action": "trending",
            "messages": ["No trending data yet. Run the screener first (trader_cli.py screener)."],
        }
    with open(trending_file) as f:
        data = json.load(f)
    return {
        "ok": True,
        "action": "trending",
        "timestamp": data.get("timestamp", ""),
        "total_scanned": data.get("total_scanned", 0),
        "qualified_count": data.get("qualified_count", 0),
        "trending_count": data.get("trending_count", 0),
        "top_qualified": data.get("top_qualified", []),
        "trending": data.get("trending", []),
    }


def _run_screener():
    """Run the market screener now."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("market_screener", ROOT / "market_screener.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.run_screener()
        return {
            "ok": True,
            "action": "screener",
            "timestamp": result.get("timestamp", ""),
            "total_scanned": result.get("total_scanned", 0),
            "qualified_count": result.get("qualified_count", 0),
            "trending_count": result.get("trending_count", 0),
            "trending": result.get("trending", [])[:10],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _usage():
    return {
        "ok": False,
        "error": (
            "Usage: trader_cli.py "
            "watch SYMBOL | list | buy SYMBOL [AMOUNT] | "
            "sell SYMBOL [AMOUNT|all] | sell-all | keep SYMBOL | positions | report | pending | "
            "approve ID [AMOUNT] | reject ID | scan"
        ),
    }


def main(argv):
    telegram_approvals.init_db()

    if not argv:
        return _usage()

    action = argv[0].lower()

    if action == "pending" and len(argv) == 1:
        return {"ok": True, "action": action, "pending": _pending_approvals()}

    if action == "approve" and len(argv) in {2, 3}:
        reply = f"{argv[1]} {argv[2] if len(argv) == 3 else 'y'}"
        return {"ok": True, "action": action, "messages": _run_message(reply)}

    if action == "reject" and len(argv) == 2:
        return {"ok": True, "action": action, "messages": _run_message(f"{argv[1]} n")}

    if action in {"sell-all", "sellall", "liquidate"}:
        return {"ok": True, "action": action, "messages": _run_message("sell all")}

    if action == "keep" and len(argv) == 2:
        return {"ok": True, "action": action, "messages": _run_message(f"keep {argv[1]}")}

    if action == "scan" and len(argv) == 1:
        return {"ok": True, "action": action, "messages": _run_message("scan")}

    if action == "trending" and len(argv) == 1:
        return _trending()

    if action == "screener" and len(argv) == 1:
        return _run_screener()

    command = " ".join(argv)
    supported = {"watch", "list", "buy", "sell", "positions", "report", "keep", "sell-all", "sellall", "liquidate", "scan", "trending", "screener"}

    if action not in supported:
        return _usage()

    return {"ok": True, "action": action, "messages": _run_message(command)}


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1:]), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
