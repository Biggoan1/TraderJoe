import os
import sqlite3
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(".env")

DB_FILE = "trades.db"
START_VALUE = 100000.00


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram not configured.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(url, json={
        "chat_id": chat_id,
        "text": message
    }, timeout=10)

    print(response.status_code)
    print(response.text)


def build_report():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    latest_id = cur.execute(
        "select max(id) from portfolio_snapshots"
    ).fetchone()[0]

    latest_row = cur.execute(
        "select created_at from portfolio_snapshots where id = ?",
        (latest_id,)
    ).fetchone()

    if not latest_row:
        conn.close()
        return "No portfolio snapshots found."

    latest_ts = latest_row["created_at"]

    rows = cur.execute("""
        select *
        from portfolio_snapshots
        where created_at = ?
        order by unrealized_pl desc
    """, (latest_ts,)).fetchall()

    # --- Today's trade activity ---
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Buys executed today
    buys_row = cur.execute("""
        select count(*) as cnt
        from recommendations
        where created_at LIKE ?
          and (
            user_decision LIKE 'TELEGRAM_BUY_APPROVED%'
            or user_decision LIKE 'COMMAND_BUY_QUEUED%'
          )
    """, (today_str + '%',)).fetchone()
    today_buys = buys_row["cnt"]

    # Sells executed today
    sells_row = cur.execute("""
        select count(*) as cnt
        from recommendations
        where created_at LIKE ?
          and (
            user_decision LIKE 'AUTO_SELL%'
            or user_decision LIKE 'TELEGRAM_SELL_APPROVED%'
            or user_decision LIKE 'COMMAND_SELL_QUEUED%'
          )
    """, (today_str + '%',)).fetchone()
    today_sells = sells_row["cnt"]

    # Blocked trades today
    blocked_row = cur.execute("""
        select count(*) as cnt
        from recommendations
        where created_at LIKE ?
          and user_decision LIKE 'TELEGRAM_BLOCKED%'
    """, (today_str + '%',)).fetchone()
    today_blocked = blocked_row["cnt"]

    # Pending approvals (not yet resolved)
    pending_row = cur.execute("""
        select count(*) as cnt, group_concat(symbol, ', ') as symbols
        from recommendations
        where created_at LIKE ?
          and order_status = 'PENDING_TELEGRAM_APPROVAL'
    """, (today_str + '%',)).fetchone()
    today_pending = pending_row["cnt"]
    pending_symbols = pending_row["symbols"] or ""

    # Trades awaiting broker execution (PENDING_NEW / SUBMITTED)
    awaiting_row = cur.execute("""
        select count(*) as cnt
        from recommendations
        where created_at LIKE ?
          and (
            order_status LIKE 'OrderStatus.PENDING%'
            or order_status LIKE 'OrderStatus.SUBMITTED%'
          )
    """, (today_str + '%',)).fetchone()
    today_awaiting = awaiting_row["cnt"]

    # Recent trade details (today only)
    trades = cur.execute("""
        select id, created_at, symbol, user_decision, order_status
        from recommendations
        where created_at LIKE ?
        order by id desc
        limit 10
    """, (today_str + '%',)).fetchall()

    if not rows:
        conn.close()
        return "No portfolio rows found."

    portfolio_value = rows[0]["portfolio_value"]
    cash = rows[0]["cash"]
    total_pl = portfolio_value - START_VALUE

    best = rows[0]
    worst = rows[-1]

    mood = (
        "💰 Slightly smug, but still cautious."
        if total_pl >= 0
        else "⚠️ Humbled by the market gods."
    )

    lines = []

    lines.append("=================================")
    lines.append("   Trader Joe Daily Report 💎🚀")
    lines.append("=================================")
    lines.append(f"Report Time: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"Portfolio Value: ${portfolio_value:,.2f}")
    lines.append(f"Cash Available:  ${cash:,.2f}")
    lines.append(f"Total P/L:       ${total_pl:,.2f}")
    lines.append("")
    lines.append(f"Joe's Mood: {mood}")
    lines.append("")

    # --- Trade activity summary ---
    lines.append("📊 Today's Trade Activity")
    lines.append("------------------------")
    lines.append(f"🟢 Buys:  {today_buys}")
    lines.append(f"🔴 Sells: {today_sells}")
    lines.append(f"⚠️ Blocked: {today_blocked}")
    lines.append(f"⏳ Awaiting execution: {today_awaiting}")
    if today_pending:
        lines.append(f"⏸ Pending approval: {today_pending}")
        # Show unique pending symbols (truncate if long)
        unique_pending = ', '.join(dict.fromkeys(pending_symbols.split(', ')))
        if len(unique_pending) > 120:
            unique_pending = unique_pending[:117] + '...'
        lines.append(f"   Symbols: {unique_pending}")
    lines.append("")

    lines.append("🏆 Best Position")
    lines.append("----------------")
    lines.append(
        f"{best['symbol']} "
        f"${best['unrealized_pl']:,.2f} "
        f"on ${best['market_value']:,.2f}"
    )
    lines.append("")
    lines.append("📉 Worst Position")
    lines.append("-----------------")
    lines.append(
        f"{worst['symbol']} "
        f"${worst['unrealized_pl']:,.2f} "
        f"on ${worst['market_value']:,.2f}"
    )
    lines.append("")
    lines.append("Positions")
    lines.append("---------")

    for r in rows:
        emoji = "💎" if r["unrealized_pl"] >= 0 else "📉"
        lines.append(
            f"{emoji} {r['symbol']:5} "
            f"value=${r['market_value']:,.2f} "
            f"p/l=${r['unrealized_pl']:,.2f}"
        )

    # --- Crypto positions ---
    try:
        from alpaca.trading.client import TradingClient
        import os as _os
        load_dotenv(".env")
        _crypto_key = _os.getenv("CRYPTO_ALPACA_API_KEY", _os.getenv("ALPACA_API_KEY"))
        _crypto_secret = _os.getenv("CRYPTO_ALPACA_SECRET_KEY", _os.getenv("ALPACA_SECRET_KEY"))
        _crypto_client = TradingClient(_crypto_key, _crypto_secret, paper=True)
        _crypto_pos = [p for p in _crypto_client.get_all_positions() if getattr(p, 'asset_class', None) and 'CRYPTO' in str(p.asset_class)]
        if _crypto_pos:
            lines.append("")
            lines.append("Crypto Positions")
            lines.append("----------------")
            _total_cv = 0
            _total_cp = 0
            for cp in _crypto_pos:
                mv = float(cp.market_value)
                pl = float(cp.unrealized_pl)
                _total_cv += mv
                _total_cp += pl
                emoji = "💎" if pl > 0 else "📉"
                lines.append(
                    f"{emoji} {cp.symbol} qty={cp.qty} "
                    f"value=${mv:,.2f} P/L=${pl:,.2f}"
                )
            lines.append(
                f"Total crypto: ${_total_cv:,.2f} (P/L: ${_total_cp:,.2f})"
            )
    except Exception as _e:
        lines.append(f"\n⚠️ Crypto data error: {_e}")

    lines.append("")
    lines.append("Recent Trades")
    lines.append("-------------")

    if not trades:
        lines.append("No trades logged today.")
    else:
        for t in trades:
            decision = t['user_decision']
            # Truncate long decisions for readability
            if len(decision) > 80:
                decision = decision[:77] + '...'
            lines.append(
                f"{t['id']} | {t['symbol']} | "
                f"{decision} | {t['order_status']}"
            )

    lines.append("")
    lines.append("=================================")
    lines.append("  Joe says: don't get cocky. 😁")
    lines.append("=================================")

    report = "\n".join(lines)
    conn.close()

    return report


def main():
    report = build_report()

    print()
    print(report)
    print()

    send_telegram(report)


if __name__ == "__main__":
    main()
