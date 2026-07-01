import os
import re
import sqlite3
import json
from datetime import datetime, timedelta

import requests
import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
import daily_report

load_dotenv(".env")

DB_FILE = "trades.db"
DEFAULT_BUY_AMOUNT = 15000
MAX_BUY_AMOUNT = 25000
# Scale up to 5% of portfolio per position, min $5,000
SELL_ALERT_TIMEOUT_MINUTES = 5
TANKING_ALERT_THRESHOLD_PCT = -4.0
AI_MODEL = "gpt-5-mini"
AUTO_APPROVE_TRADES = True
ETF_WATCHLIST = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLF",
    "XLK",
    "XLE",
    "XLY",
    "XLP",
    "XLV",
    "XLI",
    "XLB",
    "XLU",
    "SMH",
    "TLT",
    "IEF",
    "LQD",
    "GLD",
    "VNQ",
    "ARKK",
]

STOCK_WATCHLIST = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AVGO",
    "AMD",
    "TSLA",
    "AMZN",
    "INTC",
    "MRVL",
    "GOOGL",
    "GOOG",
    "MU",
    "BRKB",
    "LLY",
    "META",
    "JPM",
    "XOM",
    "JNJ",
    "V",
    "WMT",
    "COST",
    "MA",
    "ABBV",
    "NFLX",
    "XTSLA",
    "USD",
    "BPSFT",
    "HWBM6",
]

DEFAULT_WATCHLIST = ETF_WATCHLIST + STOCK_WATCHLIST

client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
    paper=True
)


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram not configured.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    requests.post(url, json={
        "chat_id": chat_id,
        "text": message
    }, timeout=10)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            side TEXT DEFAULT 'BUY',
            price REAL,
            amount REAL,
            status TEXT
        )
    """)

    cur.execute("PRAGMA table_info(pending_approvals)")
    pending_columns = {row[1] for row in cur.fetchall()}

    if "side" not in pending_columns:
        cur.execute("ALTER TABLE pending_approvals ADD COLUMN side TEXT DEFAULT 'BUY'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            price REAL,
            sma20 REAL,
            sma50 REAL,
            signal TEXT,
            ai_analysis TEXT,
            user_decision TEXT,
            order_status TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sell_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            approval_id INTEGER,
            price REAL,
            change_pct REAL,
            deadline_at TEXT,
            status TEXT
        )
    """)

    for symbol in DEFAULT_WATCHLIST:
        cur.execute("""
            INSERT OR IGNORE INTO watchlist (symbol, created_at)
            VALUES (?, ?)
        """, (symbol, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def get_state(key, default=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    row = cur.execute(
        "SELECT value FROM bot_state WHERE key = ?",
        (key,)
    ).fetchone()

    conn.close()

    if not row:
        return default

    return row[0]


def set_state(key, value):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO bot_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))

    conn.commit()
    conn.close()


def get_pending_sell_alert(symbol):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT *
        FROM sell_alerts
        WHERE symbol = ?
          AND status = 'OPEN'
        ORDER BY id DESC
        LIMIT 1
    """, (symbol,)).fetchone()
    conn.close()
    return row


def create_sell_alert(symbol, approval_id, price, change_pct, deadline_minutes=SELL_ALERT_TIMEOUT_MINUTES):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    created_at = datetime.now().isoformat(timespec='seconds')
    deadline_at = (datetime.now() + timedelta(minutes=deadline_minutes)).isoformat(timespec='seconds')

    cur.execute("""
        INSERT INTO sell_alerts (
            created_at,
            symbol,
            approval_id,
            price,
            change_pct,
            deadline_at,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        created_at,
        symbol,
        approval_id,
        price,
        change_pct,
        deadline_at,
        'OPEN',
    ))

    alert_id = cur.lastrowid
    conn.commit()
    conn.close()
    return alert_id


def update_sell_alert_status(symbol, status):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        UPDATE sell_alerts
        SET status = ?
        WHERE symbol = ?
          AND status = 'OPEN'
    """, (status, symbol))
    conn.commit()
    conn.close()


def get_open_sell_alerts():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT *
        FROM sell_alerts
        WHERE status = 'OPEN'
        ORDER BY id
    """).fetchall()
    conn.close()
    return rows


def queue_sell_alert(symbol, price, change_pct):
    position = get_position(symbol)

    if not position:
        return None, f'No open position for {symbol}'

    existing = get_pending_sell_alert(symbol)

    if existing:
        return existing['approval_id'], f'Existing sell alert #{existing["approval_id"]}'

    amount = float(position.market_value)
    approval_id = create_pending_approval(symbol, 'SELL', price, amount)
    create_sell_alert(symbol, approval_id, price, change_pct)

    deadline = (datetime.now() + timedelta(minutes=SELL_ALERT_TIMEOUT_MINUTES)).isoformat(timespec='minutes')
    send_telegram(f"""
⚠️ Trader Joe tanking alert

Ticker: {symbol}
Price: ${price:.2f}
Change: {change_pct:.2f}% vs prior close
Position value: ${amount:,.2f}
Approval ID: {approval_id}
Decision window: {SELL_ALERT_TIMEOUT_MINUTES} minutes
Auto-action at: {deadline}

Reply with:
/trade-approve {approval_id} = sell now
/trade-reject {approval_id} = keep it
""")

    return approval_id, 'queued'


def scan_watchlist_for_tanking():
    results = []
    watchlist = get_watchlist()

    for symbol in watchlist:
        try:
            data = yf.download(
                symbol,
                period='5d',
                interval='1d',
                progress=False,
                auto_adjust=True,
            )
        except Exception as exc:
            results.append({'symbol': symbol, 'status': 'ERROR', 'error': str(exc)})
            continue

        if data.empty or len(data) < 2:
            results.append({'symbol': symbol, 'status': 'NO_DATA'})
            continue

        if getattr(data.columns, 'nlevels', 1) > 1:
            data.columns = data.columns.get_level_values(0)

        last_close = float(data['Close'].iloc[-1])
        prev_close = float(data['Close'].iloc[-2])
        change_pct = ((last_close - prev_close) / prev_close) * 100.0

        if change_pct <= TANKING_ALERT_THRESHOLD_PCT:
            approval_id, _ = queue_sell_alert(symbol, last_close, change_pct)
            results.append({
                'symbol': symbol,
                'status': 'ALERTED',
                'approval_id': approval_id,
                'change_pct': round(change_pct, 2),
            })
        else:
            results.append({'symbol': symbol, 'status': 'OK', 'change_pct': round(change_pct, 2)})

    return results


def reconcile_expired_sell_alerts():
    alerts = get_open_sell_alerts()
    if not alerts:
        return []

    now = datetime.now()
    processed = []

    for alert in alerts:
        try:
            deadline = datetime.fromisoformat(alert['deadline_at'])
        except Exception:
            continue

        if deadline > now:
            continue

        approval_id = alert['approval_id']
        approval = get_pending_approval(approval_id)

        if approval:
            process_message(f'{approval_id} y')
            update_sell_alert_status(alert['symbol'], 'AUTO_SOLD')
            processed.append({'symbol': alert['symbol'], 'approval_id': approval_id, 'status': 'AUTO_SOLD'})
        else:
            update_sell_alert_status(alert['symbol'], 'CLOSED')
            processed.append({'symbol': alert['symbol'], 'approval_id': approval_id, 'status': 'CLOSED'})

    return processed


def maybe_run_watchlist_scan():
    last_scan = get_state('last_watchlist_scan_at')
    if last_scan:
        try:
            last_scan_dt = datetime.fromisoformat(last_scan)
        except Exception:
            last_scan_dt = None
    else:
        last_scan_dt = None

    if last_scan_dt and (datetime.now() - last_scan_dt).total_seconds() < 300:
        return None

    set_state('last_watchlist_scan_at', datetime.now().isoformat())
    return scan_watchlist_for_tanking()


def parse_reply(text):
    text = text.strip().lower()

    match = re.match(r"^(\d+)\s+(.+)$", text)

    if not match:
        return None, None, None

    approval_id = int(match.group(1))
    decision = match.group(2).strip()

    if decision in ["n", "no", "skip"]:
        return approval_id, False, 0

    if decision in ["y", "yes"]:
        return approval_id, True, None

    try:
        amount = float(decision)
        if amount > 0:
            return approval_id, True, amount
    except ValueError:
        pass

    return approval_id, None, None


def get_pending_approval(approval_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    row = conn.execute("""
        SELECT *
        FROM pending_approvals
        WHERE id = ?
          AND status = 'PENDING'
    """, (approval_id,)).fetchone()

    conn.close()

    return row


def create_pending_approval(symbol, side, price, amount):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO pending_approvals (
            created_at,
            symbol,
            side,
            price,
            amount,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        symbol,
        side,
        price,
        amount,
        "PENDING"
    ))

    approval_id = cur.lastrowid

    conn.commit()
    conn.close()

    return approval_id


def update_approval(approval_id, status, amount=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    if amount is None:
        cur.execute("""
            UPDATE pending_approvals
            SET status = ?
            WHERE id = ?
        """, (status, approval_id))
    else:
        cur.execute("""
            UPDATE pending_approvals
            SET status = ?,
                amount = ?
            WHERE id = ?
        """, (status, amount, approval_id))

    conn.commit()
    conn.close()


def log_recommendation(symbol, price, user_decision, order_status, signal="TELEGRAM_APPROVAL", ai_analysis=""):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO recommendations (
            created_at,
            symbol,
            price,
            sma20,
            sma50,
            signal,
            ai_analysis,
            user_decision,
            order_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        symbol,
        price,
        None,
        None,
        signal,
        ai_analysis,
        user_decision,
        order_status
    ))

    conn.commit()
    conn.close()


def risk_check(symbol, dollars):
    forbidden = ["TQQQ", "SQQQ", "UVXY"]

    if symbol in forbidden:
        return False, "Forbidden leveraged/high-risk ticker"

    if dollars > MAX_BUY_AMOUNT:
        return False, f"Buy amount too large. Max allowed is ${MAX_BUY_AMOUNT}"

    acct = client.get_account()

    if float(acct.cash) < dollars:
        return False, "Not enough cash"

    return True, "OK"


def sell_risk_check(symbol, dollars):
    position = get_position(symbol)

    if not position:
        return False, f"No current {symbol} position"

    market_value = float(position.market_value)

    if dollars <= 0:
        return False, "Sell amount must be positive"

    if dollars > market_value:
        return False, f"Sell amount exceeds current position value (${market_value:,.2f})"

    return True, "OK"


def place_paper_buy(symbol, dollars):
    price_data = yf.Ticker(symbol).history(period="1d")

    if price_data.empty:
        raise RuntimeError(f"No price data for {symbol}")

    price = float(price_data["Close"].iloc[-1])
    qty = round(dollars / price, 4)

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY
    )

    return client.submit_order(order)


def place_paper_sell(symbol, dollars):
    position = get_position(symbol)

    if not position:
        raise RuntimeError(f"No current {symbol} position")

    price_data = yf.Ticker(symbol).history(period="1d")

    if price_data.empty:
        raise RuntimeError(f"No price data for {symbol}")

    price = float(price_data["Close"].iloc[-1])
    owned_qty = float(position.qty)
    qty = min(round(dollars / price, 4), owned_qty)

    if qty <= 0:
        raise RuntimeError(f"Calculated sell quantity is zero for {symbol}")

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY
    )

    return client.submit_order(order)


def normalize_symbol(symbol):
    return symbol.strip().upper()


def parse_amount(raw_amount):
    if raw_amount is None:
        return None

    try:
        amount = float(raw_amount)
    except ValueError:
        return None

    if amount <= 0:
        return None

    return amount


def parse_sell_amount(raw_amount):
    if raw_amount is None:
        return None, False

    if raw_amount.strip().lower() == "all":
        return None, True

    return parse_amount(raw_amount), False


def get_watchlist():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT symbol
        FROM watchlist
        ORDER BY symbol
    """).fetchall()

    conn.close()

    return [row[0] for row in rows]


def add_watchlist_symbol(symbol):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO watchlist (symbol, created_at)
        VALUES (?, ?)
    """, (symbol, datetime.now().isoformat()))

    inserted = cur.rowcount > 0

    conn.commit()
    conn.close()

    return inserted


def has_pending_approval(symbol, side):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    row = cur.execute("""
        SELECT id
        FROM pending_approvals
        WHERE symbol = ?
          AND COALESCE(side, 'BUY') = ?
          AND status = 'PENDING'
        LIMIT 1
    """, (symbol, side)).fetchone()

    conn.close()

    return row[0] if row else None


def get_position(symbol):
    for pos in client.get_all_positions():
        if pos.symbol == symbol:
            return pos

    return None


def get_signal(symbol):
    data = yf.download(
        symbol,
        period="6mo",
        interval="1d",
        progress=False,
        auto_adjust=True
    )

    if data.empty:
        return "NO DATA", None, None, None

    if getattr(data.columns, "nlevels", 1) > 1:
        data.columns = data.columns.get_level_values(0)

    data["SMA20"] = data["Close"].rolling(20).mean()
    data["SMA50"] = data["Close"].rolling(50).mean()

    last = data.iloc[-1]

    price = float(last["Close"])
    sma20 = float(last["SMA20"])
    sma50 = float(last["SMA50"])

    if sma20 > sma50:
        return "BUY", price, sma20, sma50

    return "HOLD", price, sma20, sma50


def ai_analyze_buy(symbol, price, sma20, sma50):
    prompt = f"""
You are Trader Joe, a cautious but lighthearted stock trading analyst.

Analyze this on-demand paper-trade buy request.

Ticker: {symbol}
Current price: {price:.2f}
20-day SMA: {sma20:.2f}
50-day SMA: {sma50:.2f}

Rules:
- This is paper trading.
- No options.
- No leverage.
- Conservative position sizing.
- Do not claim certainty.
- Do not give financial advice.
- Decide whether this is worth approving as a small test position.
- Keep the tone useful, clear, and mildly fun.

Return this exact format:

AI Recommendation: APPROVE or HOLD OFF
Confidence: 1-10
Reason: one short paragraph
Risk: LOW, MEDIUM, or HIGH
"""

    ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = ai.responses.create(
        model=AI_MODEL,
        input=prompt
    )

    return response.output_text.strip()


def ai_analyze_sell(symbol, price, position, amount):
    prompt = f"""
You are Trader Joe, a cautious but lighthearted stock trading analyst.

Analyze this on-demand paper-trade sell request.

Ticker: {symbol}
Current price: {price:.2f}
Position quantity: {float(position.qty):.4f}
Position market value: {float(position.market_value):.2f}
Unrealized P/L: {float(position.unrealized_pl):.2f}
Requested sell amount: {amount:.2f}

Rules:
- This is paper trading.
- No options.
- No leverage.
- Do not claim certainty.
- Do not give financial advice.
- Keep the tone useful, clear, and mildly fun.

Return this exact format:

AI Recommendation: APPROVE or HOLD OFF
Confidence: 1-10
Reason: one short paragraph
Risk: LOW, MEDIUM, or HIGH
"""

    ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = ai.responses.create(
        model=AI_MODEL,
        input=prompt
    )

    return response.output_text.strip()


def queue_buy_command(symbol, amount):
    signal, price, sma20, sma50 = get_signal(symbol)

    if price is None:
        send_telegram(f"⚠️ Trader Joe could not get market data for {symbol}.")
        return

    pending_id = has_pending_approval(symbol, "BUY")

    if pending_id:
        send_telegram(f"⚠️ {symbol} already has pending buy approval #{pending_id}.")
        return

    amount = amount or DEFAULT_BUY_AMOUNT

    try:
        analysis = ai_analyze_buy(symbol, price, sma20, sma50)
    except Exception as e:
        analysis = f"AI analysis failed: {e}"

    approval_id = create_pending_approval(symbol, "BUY", price, amount)

    send_telegram(f"""
💎 Trader Joe Buy Request

Approval ID: {approval_id}
Ticker: {symbol}
Price: ${price:.2f}
Signal: {signal}
SMA20: {sma20:.2f}
SMA50: {sma50:.2f}
Default Amount: ${amount:,.2f}

{analysis}

Auto-approve mode is ON, so Joe is pressing the button unless risk says otherwise.
""")

    log_recommendation(
        symbol,
        price,
        f"COMMAND_BUY_QUEUED #{approval_id}",
        "PENDING_TELEGRAM_APPROVAL",
        signal=signal,
        ai_analysis=analysis
    )

    if AUTO_APPROVE_TRADES:
        try:
            process_message(f"{approval_id} y")
        except Exception as exc:
            send_telegram(f"⚠️ Auto-approval failed for {symbol} buy #{approval_id}: {exc}")


def queue_sell_command(symbol, amount):
    position = get_position(symbol)

    if not position:
        send_telegram(f"⚠️ Trader Joe could not find a current {symbol} position.")
        return

    price_data = yf.Ticker(symbol).history(period="1d")

    if price_data.empty:
        send_telegram(f"⚠️ Trader Joe could not get market data for {symbol}.")
        return

    price = float(price_data["Close"].iloc[-1])
    amount = amount or float(position.market_value)

    pending_id = has_pending_approval(symbol, "SELL")

    if pending_id:
        send_telegram(f"⚠️ {symbol} already has pending sell approval #{pending_id}.")
        return

    allowed, reason = sell_risk_check(symbol, amount)

    if not allowed:
        send_telegram(f"⚠️ Trader Joe cannot queue {symbol} sell request.\n\nReason: {reason}")
        return

    try:
        analysis = ai_analyze_sell(symbol, price, position, amount)
    except Exception as e:
        analysis = f"AI analysis failed: {e}"

    approval_id = create_pending_approval(symbol, "SELL", price, amount)

    send_telegram(f"""
📉 Trader Joe Sell Request

Approval ID: {approval_id}
Ticker: {symbol}
Price: ${price:.2f}
Position Value: ${float(position.market_value):,.2f}
Unrealized P/L: ${float(position.unrealized_pl):,.2f}
Default Sell Amount: ${amount:,.2f}

{analysis}

Auto-approve mode is ON, so Joe is pressing the button unless risk says otherwise.
""")

    log_recommendation(
        symbol,
        price,
        f"COMMAND_SELL_QUEUED #{approval_id}",
        "PENDING_TELEGRAM_APPROVAL",
        signal="SELL_REQUEST",
        ai_analysis=analysis
    )

    if AUTO_APPROVE_TRADES:
        try:
            process_message(f"{approval_id} y")
        except Exception as exc:
            send_telegram(f"⚠️ Auto-approval failed for {symbol} sell #{approval_id}: {exc}")



def send_positions():
    positions = client.get_all_positions()

    if not positions:
        send_telegram("Trader Joe has no open positions.")
        return

    lines = ["Positions", "---------"]

    for p in positions:
        lines.append(
            f"{p.symbol:5} "
            f"qty={float(p.qty):,.4f} "
            f"value=${float(p.market_value):,.2f} "
            f"p/l=${float(p.unrealized_pl):,.2f}"
        )

    send_telegram("\n".join(lines))


def queue_sell_all_positions():
    positions = client.get_all_positions()

    if not positions:
        send_telegram("Trader Joe has no open positions to sell.")
        return True

    executed = []
    skipped = []

    for position in positions:
        symbol = position.symbol
        price_data = yf.Ticker(symbol).history(period="1d")

        if price_data.empty:
            skipped.append(f"{symbol}: no market data")
            continue

        price = float(price_data["Close"].iloc[-1])
        amount = float(position.market_value)
        pending_id = has_pending_approval(symbol, "SELL")

        if pending_id:
            skipped.append(f"{symbol}: pending approval #{pending_id}")
            continue

        approval_id = create_pending_approval(symbol, "SELL", price, amount)

        try:
            process_message(f"{approval_id} y")
            executed.append(f"{symbol} #{approval_id}")
        except Exception as exc:
            skipped.append(f"{symbol}: {exc}")

    summary = ["Trader Joe sell-all result:"]
    summary.extend(f"- executed {item}" for item in executed)
    if skipped:
        summary.append("Skipped:")
        summary.extend(f"- {item}" for item in skipped)
    send_telegram("\n".join(summary))
    return True


def process_command(text):
    command = text.strip()
    lower = command.lower()

    if lower == "list":
        watchlist = get_watchlist()
        send_telegram("Watchlist:\n" + "\n".join(watchlist))
        return True

    if lower in {"sell all", "sell-all", "sell everything", "liquidate"}:
        queue_sell_all_positions()
        return True

    if lower == "scan":
        results = scan_watchlist_for_tanking()
        send_telegram("Watchlist scan complete. Alerts will be sent separately if needed.")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return True

    if lower == "positions":
        send_positions()
        return True

    if lower == "report":
        send_telegram(daily_report.build_report())
        return True

    match = re.match(r"^watch\s+([a-zA-Z][a-zA-Z0-9.\-]*)$", command, re.IGNORECASE)

    if match:
        symbol = normalize_symbol(match.group(1))
        inserted = add_watchlist_symbol(symbol)
        status = "added to" if inserted else "already on"
        send_telegram(f"{symbol} is {status} Trader Joe's watchlist.")
        return True

    match = re.match(r"^(keep|hold)\s+([a-zA-Z][a-zA-Z0-9.\-]*)$", command, re.IGNORECASE)

    if match:
        symbol = normalize_symbol(match.group(2))
        pending = get_pending_sell_alert(symbol)
        if not pending:
            send_telegram(f"⚠️ No active sell alert found for {symbol}.")
            return True
        update_approval(pending["approval_id"], "SKIPPED", 0)
        update_sell_alert_status(symbol, "KEEP")
        send_telegram(f"✅ Trader Joe kept {symbol} after your reply.")
        return True

    match = re.match(r"^buy\s+([a-zA-Z][a-zA-Z0-9.\-]*)(?:\s+(\d+(?:\.\d+)?))?$", command, re.IGNORECASE)

    if match:
        symbol = normalize_symbol(match.group(1))
        amount = parse_amount(match.group(2))

        if match.group(2) is not None and amount is None:
            send_telegram("⚠️ Buy amount must be a positive number.")
            return True

        queue_buy_command(symbol, amount)
        return True

    match = re.match(r"^sell\s+([a-zA-Z][a-zA-Z0-9.\-]*)(?:\s+(\d+(?:\.\d+)?|all))?$", command, re.IGNORECASE)

    if match:
        symbol = normalize_symbol(match.group(1))
        amount, sell_all = parse_sell_amount(match.group(2))

        if match.group(2) is not None and amount is None and not sell_all:
            send_telegram("⚠️ Sell amount must be a positive number or all.")
            return True

        queue_sell_command(symbol, amount)
        return True

    return False


def get_updates():
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    last_update_id = get_state("telegram_last_update_id", "0")
    offset = int(last_update_id) + 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"

    response = requests.get(url, params={
        "offset": offset,
        "timeout": 5
    }, timeout=10)

    response.raise_for_status()

    return response.json().get("result", [])


def process_message(text):
    if process_command(text):
        return

    approval_id, should_buy, amount = parse_reply(text)

    if approval_id is None:
        print(f"Ignoring message: {text}")
        return

    if should_buy is None:
        send_telegram(
            f"⚠️ Trader Joe didn't understand approval #{approval_id}.\n\n"
            f"Use:\n"
            f"{approval_id} y\n"
            f"{approval_id} 1000\n"
            f"{approval_id} n"
        )
        return

    approval = get_pending_approval(approval_id)

    if not approval:
        send_telegram(
            f"⚠️ Trader Joe couldn't find pending approval #{approval_id}."
        )
        return

    symbol = approval["symbol"]
    side = approval["side"] or "BUY"
    price = float(approval["price"])
    default_amount = float(approval["amount"])
    amount = default_amount if amount is None else amount

    if not should_buy:
        update_approval(approval_id, "SKIPPED", 0)
        log_recommendation(symbol, price, f"TELEGRAM_SKIPPED #{approval_id}", "SKIPPED")

        send_telegram(f"📉 Trader Joe skipped {symbol} {side.lower()} approval #{approval_id}.")
        print(f"Skipped approval #{approval_id}")
        return

    if side == "SELL":
        allowed, reason = sell_risk_check(symbol, amount)
    else:
        allowed, reason = risk_check(symbol, amount)

    if not allowed:
        update_approval(approval_id, "BLOCKED", amount)
        log_recommendation(symbol, price, f"TELEGRAM_BLOCKED ${amount}", reason)

        send_telegram(
            f"⚠️ Trader Joe blocked {symbol} {side.lower()} approval #{approval_id}.\n\n"
            f"Amount: ${amount:,.2f}\n"
            f"Reason: {reason}"
        )

        print(f"Blocked approval #{approval_id}: {reason}")
        return

    if side == "SELL":
        order = place_paper_sell(symbol, amount)
    else:
        order = place_paper_buy(symbol, amount)

    update_approval(approval_id, "APPROVED", amount)
    log_recommendation(symbol, price, f"TELEGRAM_{side}_APPROVED ${amount}", str(order.status))

    send_telegram(f"""
💰 Trader Joe Order Submitted

Approval ID: {approval_id}
Ticker: {symbol}
Side: {side}
Amount: ${amount:,.2f}
Status: {order.status}

Joe has pressed the big shiny button responsibly. 💎
""")

    print(f"Approved approval #{approval_id}: {symbol} ${amount}")


def main():
    init_db()
    maybe_run_watchlist_scan()
    reconcile_expired_sell_alerts()

    updates = get_updates()

    if not updates:
        print("No new Telegram messages.")
        return

    for update in updates:
        set_state("telegram_last_update_id", update["update_id"])

        message = update.get("message")

        if not message:
            continue

        text = message.get("text", "")

        if not text:
            continue

        print(f"Processing: {text}")
        process_message(text)


if __name__ == "__main__":
    main()
