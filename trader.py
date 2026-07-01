import os
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI
from alpaca.trading.client import TradingClient

# --- Champion/Challenger framework ---
# All feature flags disabled by default → system behaves identically to
# the codebase before this change. Challenger runs in shadow only.
from strategy.runner import ChampionChallengerRunner, trade_logger

_cc_runner = ChampionChallengerRunner()

import telegram_approvals

load_dotenv(".env")

PAPER = True
AGENT_NAME = "Trader Joe"
AI_MODEL = "gpt-5-mini"
DB_FILE = "trades.db"
COOLDOWN_MINUTES = 30  # Don't re-buy a symbol for 30 min after selling it

ET = ZoneInfo("America/New_York")

# --- ORB (Opening Range Breakout) config ---
ORB_PERIOD_MINUTES = 15  # Standard ORB: first 15 min after open
ORB_MAX_TRADE_MINUTES = 120  # Stop ORB entries 2h after open
ORB_RETEST_REQUIRED = True  # Require retest confirmation (TV indicator approach)

DEFAULT_BUY_AMOUNT = 15000
MAX_BUY_AMOUNT = 25000
MAX_POSITIONS = 20
TARGET_MAX_POSITIONS = 15  # Start trimming when we exceed this

ETF_UNIVERSE = {
    "core": [
        "SPY",   # S&P 500
        "QQQ",   # Nasdaq 100
        "IWM",   # Russell 2000
        "DIA",   # Dow Jones
    ],
    "sector_rotation": [
        "XLF",   # Financials
        "XLK",   # Technology
        "XLE",   # Energy
        "XLY",   # Consumer Discretionary
        "XLP",   # Consumer Staples
        "XLV",   # Healthcare
        "XLI",   # Industrials
        "XLB",   # Materials
        "XLU",   # Utilities
        "SMH",   # Semiconductors
    ],
    "defensive": [
        "TLT",   # Long Treasuries
        "IEF",   # Intermediate Treasuries
        "LQD",   # Investment Grade Credit
        "GLD",   # Gold
        "VNQ",   # Real Estate
    ],
    "momentum": [
        "ARKK",  # Innovation/Growth
    ],
}

STOCK_WATCHLIST = {
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
}

# Flattened default universe for scanning / approvals.
DEFAULT_WATCHLIST = [
    *[symbol for group in ETF_UNIVERSE.values() for symbol in group],
    *sorted(STOCK_WATCHLIST),
]

SETUP_BUCKET_PRIORITY = {
    "core": 4,
    "sector_rotation": 3,
    "defensive": 2,
    "stocks": 1,
    "momentum": 0,
    "other": -1,
}


def get_symbol_bucket(symbol):
    for bucket, symbols in ETF_UNIVERSE.items():
        if symbol in symbols:
            return bucket
    if symbol in STOCK_WATCHLIST:
        return "stocks"
    return "other"


def evaluate_etf_setup(symbol):
    data = yf.download(
        symbol,
        period="6mo",
        interval="1d",
        progress=False,
        auto_adjust=True,
    )

    if data.empty:
        return None

    if getattr(data.columns, "nlevels", 1) > 1:
        data.columns = data.columns.get_level_values(0)

    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(data.columns)):
        return None

    if len(data) < 50:
        return None

    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    bb_std = close.rolling(20).std()
    bb_lower = sma20 - (2 * bb_std)
    bb_upper = sma20 + (2 * bb_std)

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    adx = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal

    volume_avg20 = volume.rolling(20).mean()

    last = data.iloc[-1]
    prev = data.iloc[-2]

    price = float(last["Close"])
    sma20_last = float(sma20.iloc[-1])
    sma50_last = float(sma50.iloc[-1])
    bb_lower_last = float(bb_lower.iloc[-1])
    bb_upper_last = float(bb_upper.iloc[-1])
    rsi_last = float(rsi.iloc[-1])
    adx_last = float(adx.iloc[-1])
    macd_hist_last = float(macd_hist.iloc[-1])
    macd_hist_prev = float(macd_hist.iloc[-2])
    volume_last = float(last["Volume"])
    volume_avg_last = float(volume_avg20.iloc[-1])

    if any(pd.isna(value) for value in [price, sma20_last, sma50_last, bb_lower_last, rsi_last, adx_last, macd_hist_last, macd_hist_prev, volume_last, volume_avg_last]):
        return None

    gates = {
        "above_sma20": price > sma20_last,
        "sma20_above_sma50": sma20_last > sma50_last,
        "rsi_not_overbought": rsi_last < 70,
        "macd_positive": macd_hist_last > 0,
        "adx_strength": adx_last > 15,
        "volume_confirmation": volume_last > volume_avg_last * 0.8,
    }

    score = 0.0
    score += 1.5 if gates["above_sma20"] else 0.0
    score += 1.5 if gates["sma20_above_sma50"] else 0.0
    score += 1.0 if gates["rsi_not_overbought"] else 0.0
    score += 1.5 if gates["macd_positive"] else 0.0
    score += 1.0 if gates["adx_strength"] else 0.0
    score += 0.5 if gates["volume_confirmation"] else 0.0
    score += min(max(adx_last - 15.0, 0.0) / 10.0, 1.5)
    score += min(max((volume_last / volume_avg_last) - 0.8, 0.0), 1.0)

    return {
        "symbol": symbol,
        "bucket": get_symbol_bucket(symbol),
        "price": price,
        "sma20": sma20_last,
        "sma50": sma50_last,
        "bb_lower": bb_lower_last,
        "bb_upper": bb_upper_last,
        "rsi": rsi_last,
        "adx": adx_last,
        "macd_hist": macd_hist_last,
        "macd_hist_prev": macd_hist_prev,
        "volume": volume_last,
        "volume_avg20": volume_avg_last,
        "gates": gates,
        "gate_count": sum(1 for passed in gates.values() if passed),
        "score": score,
        "qualified": sum(1 for passed in gates.values() if passed) >= 4,
    }


# --- ORB: Opening Range Breakout with retest confirmation ---
#
# Logic from TradingView "ORB Breakout & Retest" indicator:
#   Phase 1 — Break: Price closes beyond ORB high/low
#   Phase 2 — Retest: Price wicks back to touch the broken level
#   Phase 3 — Confirm: Price closes beyond the broken level again → ENTRY
#
# Multi-timeframe ORBs tracked simultaneously:
#   5m  (aggressive)  — 9:30-9:35
#   15m (balanced)    — 9:30-9:45  ← primary signal
#   30m (conservative) — 9:30-10:00

def _get_orb_minutes(period="15m"):
    """Return number of bars for the ORB period given an interval."""
    interval_map = {"1m": int(period[:-1]), "5m": int(period[:-1]) // 5}
    return interval_map.get("5m", 3)  # default to 15-min ORB on 5m


def fetch_intraday_data(symbol, interval="5m", period="5d"):
    """Fetch intraday OHLCV for today's session."""
    data = yf.download(
        symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=True,
    )
    if data.empty:
        return None
    if getattr(data.columns, "nlevels", 1) > 1:
        data.columns = data.columns.get_level_values(0)
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(data.columns)):
        return None
    return data


def get_today_open_et():
    """Return today's market open time (9:30 ET) as a datetime."""
    now_et = datetime.now(ET)
    return now_et.replace(hour=9, minute=30, second=0, microsecond=0)


def get_orb_range(data, open_dt, orb_minutes=ORB_PERIOD_MINUTES, interval_minutes=5):
    """Calculate the ORB high/low from the first N minutes after market open.

    Returns (orb_high, orb_low, orb_bars_count) or None if insufficient data.
    """
    bars_needed = max(orb_minutes // interval_minutes, 1)

    # Filter to today's data after market open
    today_data = data[data.index >= open_dt]
    if len(today_data) < bars_needed:
        return None

    orb_bars = today_data.iloc[:bars_needed]
    return (float(orb_bars["High"].max()), float(orb_bars["Low"].min()), len(orb_bars))


def detect_orb_retest(data, open_dt, orb_high, orb_low, direction,
                      interval_minutes=5, orb_minutes=ORB_PERIOD_MINUTES):
    """Detect three-phase ORB confirmation: Break → Retest → Confirm.

    Direction: 'bull' (break above orb_high) or 'bear' (break below orb_low).

    Returns dict with phase info or None if not confirmed.
    """
    bars_after_orb = max(orb_minutes // interval_minutes, 1)
    today_data = data[data.index >= open_dt]
    if len(today_data) <= bars_after_orb:
        return None

    post_orb = today_data.iloc[bars_after_orb:]
    closes = post_orb["Close"]
    highs = post_orb["High"]
    lows = post_orb["Low"]

    # Phase 1: Breakout — a bar closes beyond the ORB boundary
    break_idx = None
    for i in range(len(post_orb)):
        if direction == "bull" and closes.iloc[i] > orb_high:
            break_idx = i
            break
        elif direction == "bear" and closes.iloc[i] < orb_low:
            break_idx = i
            break

    if break_idx is None or break_idx >= len(post_orb) - 1:
        return None  # No breakout or no bars left for retest

    # Phase 2: Retest — price wicks back to touch the broken level
    retest_idx = None
    for i in range(break_idx + 1, len(post_orb)):
        if direction == "bull" and lows.iloc[i] <= orb_high:
            retest_idx = i
            break
        elif direction == "bear" and highs.iloc[i] >= orb_low:
            retest_idx = i
            break

    if retest_idx is None or retest_idx >= len(post_orb) - 1:
        return None  # No retest or no bars left for confirmation

    # Phase 3: Confirmation — price closes beyond the broken level again
    for i in range(retest_idx + 1, len(post_orb)):
        if direction == "bull" and closes.iloc[i] > orb_high:
            return {
                "direction": "long",
                "entry_price": float(closes.iloc[i]),
                "stop_loss": orb_low,
                "take_profit": orb_high + (orb_high - orb_low),  # 1:1 risk/reward minimum
                "orb_high": orb_high,
                "orb_low": orb_low,
                "break_bar": int(break_idx),
                "retest_bar": int(retest_idx),
                "confirm_bar": int(i),
                "confirm_time": str(post_orb.index[i]),
            }
        elif direction == "bear" and closes.iloc[i] < orb_low:
            return {
                "direction": "short",
                "entry_price": float(closes.iloc[i]),
                "stop_loss": orb_high,
                "take_profit": orb_low - (orb_high - orb_low),
                "orb_high": orb_high,
                "orb_low": orb_low,
                "break_bar": int(break_idx),
                "retest_bar": int(retest_idx),
                "confirm_bar": int(i),
                "confirm_time": str(post_orb.index[i]),
            }

    return None  # Rejected — no confirmation after retest


def save_orb_state(symbol, timeframe, orb_high, orb_low, state, data=None):
    """Persist ORB state to DB so subsequent scans can continue tracking."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orb_state (
            symbol TEXT,
            timeframe TEXT,
            date TEXT,
            orb_high REAL,
            orb_low REAL,
            state TEXT,
            updated_at TEXT,
            data TEXT,
            PRIMARY KEY (symbol, timeframe, date)
        )
    """)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    json_data = json.dumps(data) if data else None
    cur.execute("""
        INSERT OR REPLACE INTO orb_state
            (symbol, timeframe, date, orb_high, orb_low, state, updated_at, data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, timeframe, today, orb_high, orb_low, state,
          datetime.now().isoformat(), json_data))
    conn.commit()
    conn.close()


def load_orb_state(symbol, timeframe):
    """Load ORB state from DB."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    row = cur.execute("""
        SELECT orb_high, orb_low, state, data
        FROM orb_state
        WHERE symbol = ? AND timeframe = ? AND date = ?
    """, (symbol, timeframe, today)).fetchone()
    conn.close()
    if row is None:
        return None
    import json as _json
    return {
        "orb_high": row[0], "orb_low": row[1],
        "state": row[2],
        "data": _json.loads(row[3]) if row[3] else None,
    }


def clear_orb_state():
    """Clear stale ORB state (call at start of each scan run)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    yesterday = (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orb_state (
            symbol TEXT,
            timeframe TEXT,
            date TEXT, orb_high REAL, orb_low REAL,
            state TEXT, updated_at TEXT, data TEXT,
            PRIMARY KEY (symbol, timeframe, date)
        )
    """)
    cur.execute("DELETE FROM orb_state WHERE date < ?", (yesterday,))
    conn.commit()
    conn.close()


def evaluate_orb_setup(symbol, timeframe="15m"):
    """Evaluate ORB setup for a symbol using intraday 5-minute data.

    Returns dict with ORB analysis or None.
    """
    now_et = datetime.now(ET)
    market_open = get_today_open_et()

    # Check if we're in the ORB trading window
    minutes_since_open = (now_et - market_open).total_seconds() / 60
    if minutes_since_open < 0:
        return None  # Market not open yet
    if minutes_since_open > ORB_MAX_TRADE_MINUTES:
        return None  # ORB window closed

    # Fetch 5-min intraday data
    data = fetch_intraday_data(symbol, interval="5m", period="5d")
    if data is None or len(data) < 10:
        return None

    # Convert timezone to ET for comparison
    if data.index.tz is None:
        data.index = data.index.tz_localize("UTC").tz_convert(ET)
    else:
        data.index = data.index.tz_convert(ET)

    # Determine ORB period in minutes from timeframe string
    orb_minutes = int(timeframe.replace("m", ""))

    # Get ORB range
    orb_result = get_orb_range(data, market_open, orb_minutes=orb_minutes, interval_minutes=5)
    if orb_result is None:
        return None  # ORB period not yet complete

    orb_high, orb_low, orb_bars = orb_result

    # Skip if range is too small (< 0.1%) — noise
    range_pct = (orb_high - orb_low) / orb_low
    if range_pct < 0.001:
        return None

    # Check for retest confirmation (bull direction — long only)
    retest = detect_orb_retest(data, market_open, orb_high, orb_low,
                               direction="bull", interval_minutes=5,
                               orb_minutes=orb_minutes)

    # Save state
    state = "confirmed" if retest else ("active" if minutes_since_open > orb_minutes else "forming")
    save_orb_state(symbol, timeframe, orb_high, orb_low, state, {
        "orb_bars": orb_bars,
        "range_pct": range_pct,
        "retest": retest is not None,
    })

    # Calculate daily trend bias (prefer trades aligned with daily trend)
    daily_data = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
    daily_bias = "neutral"
    if not daily_data.empty and len(daily_data) >= 2:
        if getattr(daily_data.columns, "nlevels", 1) > 1:
            daily_data.columns = daily_data.columns.get_level_values(0)
        close = daily_data["Close"]
        sma20_daily = close.rolling(20).mean()
        if not sma20_daily.empty:
            sma20_val = float(sma20_daily.iloc[-1])
            price_val = float(close.iloc[-1])
            if not pd.isna(sma20_val):
                daily_bias = "bull" if price_val > sma20_val else "bear"

    if retest:
        # Only take long setups when daily bias is bullish or neutral
        if daily_bias == "bear":
            return None

        entry_price = retest["entry_price"]
        stop_loss = retest["stop_loss"]  # orb_low
        take_profit = retest["take_profit"]
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
        risk_reward = reward / risk if risk > 0 else 0

        return {
            "symbol": symbol,
            "strategy": "ORB",
            "timeframe": timeframe,
            "direction": "long",
            "orb_high": orb_high,
            "orb_low": orb_low,
            "range_pct": range_pct * 100,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": risk_reward,
            "confirm_time": retest["confirm_time"],
            "daily_bias": daily_bias,
            "state": "confirmed",
            "score": 8.0 + min(risk_reward, 2.0),  # Base 8 + RR bonus
        }
    else:
        return {
            "symbol": symbol,
            "strategy": "ORB",
            "timeframe": timeframe,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "range_pct": range_pct * 100,
            "state": state,
            "daily_bias": daily_bias,
            "minutes_since_open": minutes_since_open,
        }

client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
    paper=PAPER
)

ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message
        }, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}")


def should_send_summary():
    """Only send the trade summary once per hour, on the hour."""
    now = datetime.now()
    return now.minute < 5


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

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
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            cash REAL,
            portfolio_value REAL,
            symbol TEXT,
            qty REAL,
            market_value REAL,
            unrealized_pl REAL
        )
    """)

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
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_cooldowns (
            symbol TEXT PRIMARY KEY,
            sold_at TEXT
        )
    """)

    cur.execute("DELETE FROM watchlist WHERE symbol IN ({})".format(",".join("?" for _ in STOCK_WATCHLIST)), tuple(STOCK_WATCHLIST))

    for symbol in DEFAULT_WATCHLIST:
        cur.execute("""
            INSERT OR IGNORE INTO watchlist (symbol, created_at)
            VALUES (?, ?)
        """, (symbol, datetime.now().isoformat()))

    conn.commit()
    conn.close()


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


def log_recommendation(symbol, price, sma20, sma50, signal, ai_analysis, user_decision, order_status):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO recommendations (
            created_at, symbol, price, sma20, sma50,
            signal, ai_analysis, user_decision, order_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        symbol,
        price,
        sma20,
        sma50,
        signal,
        ai_analysis,
        user_decision,
        order_status
    ))

    conn.commit()
    conn.close()


def log_portfolio_snapshot():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    acct = client.get_account()
    positions = client.get_all_positions()
    now = datetime.now().isoformat()

    if not positions:
        cur.execute("""
            INSERT INTO portfolio_snapshots (
                created_at, cash, portfolio_value, symbol, qty, market_value, unrealized_pl
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            now,
            float(acct.cash),
            float(acct.portfolio_value),
            "CASH",
            0,
            0,
            0
        ))
    else:
        for p in positions:
            cur.execute("""
                INSERT INTO portfolio_snapshots (
                    created_at, cash, portfolio_value, symbol, qty, market_value, unrealized_pl
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                now,
                float(acct.cash),
                float(acct.portfolio_value),
                p.symbol,
                float(p.qty),
                float(p.market_value),
                float(p.unrealized_pl)
            ))

    conn.commit()
    conn.close()


def already_owned(symbol):
    for pos in client.get_all_positions():
        if pos.symbol == symbol:
            return True
    return False


def has_open_order(symbol):
    for order in client.get_orders():
        if order.symbol == symbol:
            return True
    return False


def has_pending_approval(symbol, side="BUY"):
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

    return row is not None


def record_cooldown(symbol):
    """Record that a symbol was sold so we don't immediately re-buy it."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO trade_cooldowns (symbol, sold_at)
        VALUES (?, ?)
    """, (symbol, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def on_cooldown(symbol):
    """Return True if the symbol was sold within COOLDOWN_MINUTES."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    row = cur.execute("""
        SELECT sold_at
        FROM trade_cooldowns
        WHERE symbol = ?
    """, (symbol,)).fetchone()
    conn.close()

    if row is None:
        return False

    sold_at = datetime.fromisoformat(row[0])
    return (datetime.now() - sold_at) < timedelta(minutes=COOLDOWN_MINUTES)


def find_weakest_position():
    """Find the worst-holding position to sell based on setup score."""
    positions = client.get_all_positions()
    if not positions:
        return None

    worst = None
    worst_score = float('inf')

    for pos in positions:
        symbol = pos.symbol
        if on_cooldown(symbol):
            continue  # Don't re-sell something we just sold

        setup = evaluate_etf_setup(symbol)
        if setup is None:
            continue

        # Lower score = weaker setup. Also penalize negative P/L.
        pl = float(pos.unrealized_pl)
        adjusted_score = setup["score"] + (pl / 1000)  # small P/L adjustment

        if adjusted_score < worst_score:
            worst_score = adjusted_score
            worst = {
                "symbol": symbol,
                "score": setup["score"],
                "rsi": setup["rsi"],
                "pl": pl,
                "market_value": float(pos.market_value),
                "setup": setup,
            }

    return worst


def create_pending_approval(symbol, price, side="BUY", amount=DEFAULT_BUY_AMOUNT):
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

    if isinstance(data.columns, pd.MultiIndex):
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


def ai_analyze(symbol, price, sma20, sma50, bucket):
    prompt = f"""
You are Trader Joe, a cautious but lighthearted stock trading analyst.

Analyze this possible paper-trade setup.

Ticker: {symbol}
Universe bucket: {bucket}
Current price: {price:.2f}
20-day SMA: {sma20:.2f}
50-day SMA: {sma50:.2f}
Signal: SMA20 is above SMA50

Rules:
- This is paper trading.
- No options.
- No leverage.
- No day trading.
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

    response = ai.responses.create(
        model=AI_MODEL,
        input=prompt
    )

    return response.output_text.strip()


init_db()
log_portfolio_snapshot()

print("""
=================================
      Trader Joe v1.0 💎🚀
=================================
  AI-Assisted Paper Trading
=================================
""")

acct = client.get_account()
clock = client.get_clock()

print(f"Cash Available: ${float(acct.cash):,.2f}")
print(f"Portfolio Value: ${float(acct.portfolio_value):,.2f}")
print(f"Market Open: {clock.is_open}")
print(f"Market Time: {clock.timestamp}")
print()

print("Open Orders:")
print("------------")

open_orders = client.get_orders()

if not open_orders:
    print("No open orders")

for order in open_orders:
    print(
        f"{order.symbol:5} "
        f"{order.side} "
        f"qty={order.qty} "
        f"status={order.status}"
    )

print()

print("Positions:")
print("----------")

positions = client.get_all_positions()

if not positions:
    print("No positions")

for p in positions:
    emoji = "💎" if float(p.unrealized_pl) > 0 else "📉"

    print(
        f"{emoji} {p.symbol:5} "
        f"qty={p.qty} "
        f"value=${float(p.market_value):,.2f} "
        f"unrealized=${float(p.unrealized_pl):,.2f}"
    )

print()

if not clock.is_open:
    print("\nMarket is closed; skipping market auto-trade selection.\n")
else:
    # Accumulate all trade actions for hourly summary
    trade_actions = []

    # --- Phase 1: Evaluate sell signals on existing positions ---
    print("\nSell Signal Scan (existing positions):")
    print("--------------------------------------")

    sell_evaluated = 0

    for pos in positions:
        setup = evaluate_etf_setup(pos.symbol)

        if setup is None:
            print(f"  {pos.symbol}: NO DATA / INSUFFICIENT HISTORY")
            continue

        rsi = setup["rsi"]
        price = setup["price"]
        bb_upper = setup["bb_upper"]
        macd_hist_last = setup["macd_hist"]
        macd_hist_prev = setup["macd_hist_prev"]

        sell_reasons = []

        # Gate 1: Overbought RSI (>70)
        if rsi > 70:
            sell_reasons.append(f"overbought RSI ({rsi:.1f})")

        # Gate 2: Price at or above upper Bollinger Band
        if price >= bb_upper:
            sell_reasons.append(f"at/above upper Bollinger (${bb_upper:.2f})")

        # Gate 3: MACD death cross (histogram turning negative)
        if macd_hist_last < 0 and macd_hist_last < macd_hist_prev:
            sell_reasons.append("MACD death cross")

        if sell_reasons:
            pos_value = float(pos.market_value)
            print(f"  📉 {pos.symbol}: SELL — {', '.join(sell_reasons)} (value: ${pos_value:,.2f})")

            trade_actions.append(
                f"📉 Sold {pos.symbol} @ ${price:.2f} (${pos_value:,.2f}) — {', '.join(sell_reasons)}"
            )

            try:
                telegram_approvals.place_paper_sell(pos.symbol, pos_value)
                # Log the sell to history
                trade_logger.log_trade_exit(
                    symbol=pos.symbol,
                    exit_price=float(price),
                    exit_reason=', '.join(sell_reasons),
                )
                record_cooldown(pos.symbol)
                telegram_approvals.log_recommendation(
                    pos.symbol, price, "AUTO_SELL", "OrderStatus.SUBMITTED",
                    signal="SELL_SIGNAL",
                    ai_analysis=f"Auto-sell: {', '.join(sell_reasons)}"
                )
                print(f"  ✅ {pos.symbol} sell order submitted")
                sell_evaluated += 1
            except Exception as e:
                print(f"  ❌ {pos.symbol} sell failed: {e}")
        else:
            pl = float(pos.unrealized_pl)
            emoji = "💎" if pl > 0 else "📉"
            print(f"  {emoji} {pos.symbol}: HOLD (RSI={rsi:.1f}, BB_upper=${bb_upper:.2f})")

    print()

    # --- Phase 1.5: ORB scan (opening range breakout with retest) ---
    orb_actions = []
    now_et = datetime.now(ET)
    market_open = get_today_open_et()
    minutes_since_open = (now_et - market_open).total_seconds() / 60

    if 0 < minutes_since_open <= ORB_MAX_TRADE_MINUTES:
        print("\nORB Breakout Scan (Opening Range Breakout):")
        print("-------------------------------------------")
        print(f"  Window: {minutes_since_open:.0f} min after open (closes at {ORB_MAX_TRADE_MINUTES} min)")

        clear_orb_state()

        orb_candidates = []
        orb_timeframes = ["15m", "5m", "30m"]  # Primary first

        for symbol in get_watchlist():
            if already_owned(symbol):
                continue
            if has_open_order(symbol):
                continue
            if on_cooldown(symbol):
                continue

            for tf in orb_timeframes:
                orb = evaluate_orb_setup(symbol, timeframe=tf)
                if orb and orb.get("state") == "confirmed":
                    orb["timeframe"] = tf
                    orb_candidates.append(orb)
                    print(f"  🚀 {symbol} [{tf}] ORB CONFIRMED — entry=${orb['entry_price']:.2f} "
                          f"range={orb['range_pct']:.2f}% RR={orb['risk_reward']:.1f} "
                          f"stop=${orb['stop_loss']:.2f} target=${orb['take_profit']:.2f}")
                    break  # One confirmed ORB per symbol is enough
                elif orb:
                    print(f"  ⏳ {symbol} [{tf}] ORB {orb['state']} — range={orb['range_pct']:.2f}% bias={orb['daily_bias']}")
                    break

        if orb_candidates:
            orb_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

            for orb in orb_candidates[:2]:
                current_positions = len(client.get_all_positions())
                if current_positions >= MAX_POSITIONS:
                    print(f"  Hit hard cap of {MAX_POSITIONS} positions — no more ORB buys this scan.")
                    break

                buy_amount = min(MAX_BUY_AMOUNT, max(DEFAULT_BUY_AMOUNT, round(float(acct.portfolio_value) * 0.15, 2)))
                acct = client.get_account()
                available_cash = float(acct.cash)
                if available_cash < buy_amount * 0.9:
                    print(f"  Not enough cash (${available_cash:,.2f}) for ${buy_amount:,.2f} ORB buy. Skipping.")
                    continue

                sym = orb["symbol"]
                orb_actions.append(
                    f"🚀 ORB Bought {sym} [{orb['timeframe']}] @ ${orb['entry_price']:.2f} — "
                    f"${buy_amount:,.2f} (range: {orb['range_pct']:.2f}%, RR: {orb['risk_reward']:.1f}, "
                    f"stop: ${orb['stop_loss']:.2f}, target: ${orb['take_profit']:.2f})"
                )

                print(f"\n  Executing ORB: {sym} [{orb['timeframe']}], ${buy_amount:,.2f}")
                # Log the ORB buy to history
                trade_logger.log_trade_entry(
                    symbol=sym,
                    side="buy",
                    entry_price=float(orb['entry_price']),
                    quantity=buy_amount / float(orb['entry_price']),
                    entry_score=orb.get('score', 0),
                    market_regime="orb",
                )
                telegram_approvals.queue_buy_command(sym, buy_amount)
                acct = client.get_account()
    else:
        if minutes_since_open <= 0:
            print("\nMarket not yet open — skipping ORB scan.\n")
        else:
            print(f"\nORB window closed ({minutes_since_open:.0f} min > {ORB_MAX_TRADE_MINUTES} min limit).\n")

    # --- Phase 2: Scan for new buy setups ---
    print("Buy Setup Scan:")
    print("---------------")

    candidates = []

    for symbol in get_watchlist():
        # Skip if we just sold this — wait for next scan
        if already_owned(symbol):
            continue

        if has_open_order(symbol):
            continue

        if on_cooldown(symbol):
            print(f"  {symbol}: on cooldown after recent sell, skipping")
            continue

        setup = evaluate_etf_setup(symbol)
        bucket = get_symbol_bucket(symbol)

        if setup is None:
            continue

        print(
            f"{symbol:5} [{bucket}] "
            f"score={setup['score']:.2f} "
            f"gates={setup['gate_count']}/6 | "
            f"Price={setup['price']:.2f} "
            f"RSI={setup['rsi']:.1f} "
            f"BBU={setup['bb_upper']:.2f} "
            f"MA20={setup['sma20']:.2f} "
            f"ADX={setup['adx']:.1f}"
        )

        if setup["gate_count"] < 4:
            continue

        if has_pending_approval(symbol):
            print("  -> pending approval already exists")
            continue

        candidates.append(setup)

    # Allow up to 3 new buys per scan (not just one)
    if candidates:
        candidates.sort(
            key=lambda item: (
                item["score"],
                item["adx"],
                item["volume"] / item["volume_avg20"] if item["volume_avg20"] else 0.0,
                SETUP_BUCKET_PRIORITY.get(item["bucket"], 0),
            ),
            reverse=True,
        )

        current_positions = len(client.get_all_positions())

        for best_setup in candidates[:3]:
            # --- Position cap: trim weakest if over TARGET_MAX_POSITIONS ---
            current_positions = len(client.get_all_positions())
            if current_positions >= MAX_POSITIONS:
                print(f"  Hit hard cap of {MAX_POSITIONS} positions — no more buys this scan.")
                break

            if current_positions >= TARGET_MAX_POSITIONS:
                weakest = find_weakest_position()
                if weakest:
                    # Log the weakest-position sell to history
                    trade_logger.log_trade_exit(
                        symbol=weakest['symbol'],
                        exit_price=float(weakest['entry_price']),
                        exit_reason="weakest_position_trim",
                    )
                    print(f"  Trimming weakest: {weakest['symbol']} (${weakest['market_value']:,.2f})")
                    try:
                        telegram_approvals.place_paper_sell(weakest['symbol'], weakest['market_value'])
                        record_cooldown(weakest['symbol'])
                        trade_actions.append(
                            f"🗑️ Trimmed {weakest['symbol']} (P/L: ${weakest['pl']:.2f}) to make room for {best_setup['symbol']}"
                        )
                        print(f"  ✅ {weakest['symbol']} trimmed")
                    except Exception as e:
                        print(f"  ❌ Trim {weakest['symbol']} failed: {e}")
                else:
                    print(f"  Over target but no position to trim. Skipping buy.")
                    continue

            buy_amount = min(MAX_BUY_AMOUNT, max(DEFAULT_BUY_AMOUNT, round(float(acct.portfolio_value) * 0.15, 2)))

            # Check we actually have the cash
            acct = client.get_account()
            available_cash = float(acct.cash)
            if available_cash < buy_amount * 0.9:
                print(f"  Not enough cash (${available_cash:,.2f}) for ${buy_amount:,.2f} buy. Skipping.")
                continue

            print(
                f"\nBest setup: {best_setup['symbol']} [{best_setup['bucket']}] "
                f"score={best_setup['score']:.2f} gates={best_setup['gate_count']}/6"
            )
            print(f"Auto-trading ${buy_amount:,.2f} without asking.")

            trade_actions.append(
                f"🏁 Bought {best_setup['symbol']} @ ${best_setup['price']:.2f} — ${buy_amount:,.2f} "
                f"(score: {best_setup['score']:.2f}, RSI: {best_setup['rsi']:.1f}, ADX: {best_setup['adx']:.1f})"
            )

            # Log the buy to history
            trade_logger.log_trade_entry(
                symbol=best_setup["symbol"],
                side="buy",
                entry_price=float(best_setup["price"]),
                quantity=buy_amount / float(best_setup["price"]),
                entry_score=best_setup.get("score", 0),
                market_regime="setup",
            )

            telegram_approvals.queue_buy_command(best_setup["symbol"], buy_amount)

            # Refresh cash for next iteration
            acct = client.get_account()
    else:
        print("\nNo buy setups met all six gates.\n")

    # --- Hourly summary (stocks + crypto) ---
    all_actions = orb_actions + trade_actions
    if all_actions and should_send_summary():
        acct = client.get_account()
        summary_lines = [
            f"📊 Trader Joe — Hourly Summary",
            f"⏰ {datetime.now().strftime('%H:%M')}",
            f"💰 Portfolio: ${float(acct.portfolio_value):,.2f}",
            f"💵 Cash: ${float(acct.cash):,.2f}",
            "",
        ]
        if orb_actions:
            summary_lines.append("ORB Trades:")
            summary_lines.extend(orb_actions)
            summary_lines.append("")
        if trade_actions:
            summary_lines.append("Stock Trades:")
            summary_lines.extend(trade_actions)
            summary_lines.append("")

        # --- Crypto summary ---
        try:
            from alpaca.trading.client import TradingClient
            from dotenv import load_dotenv
            import os as _os
            load_dotenv(".env")
            crypto_key = _os.getenv("CRYPTO_ALPACA_API_KEY", _os.getenv("ALPACA_API_KEY"))
            crypto_secret = _os.getenv("CRYPTO_ALPACA_SECRET_KEY", _os.getenv("ALPACA_SECRET_KEY"))
            crypto_client = TradingClient(crypto_key, crypto_secret, paper=True)
            crypto_pos = [p for p in crypto_client.get_all_positions() if getattr(p, 'asset_class', None) and 'CRYPTO' in str(p.asset_class)]
            if crypto_pos:
                total_crypto_value = 0
                total_crypto_pl = 0
                summary_lines.append("Crypto Positions:")
                for cp in crypto_pos:
                    mv = float(cp.market_value)
                    pl = float(cp.unrealized_pl)
                    total_crypto_value += mv
                    total_crypto_pl += pl
                    emoji = "💎" if pl > 0 else "📉"
                    summary_lines.append(
                        f"  {emoji} {cp.symbol} qty={cp.qty} "
                        f"value=${mv:,.2f} P/L=${pl:,.2f}"
                    )
                summary_lines.append(
                    f"  Total crypto: ${total_crypto_value:,.2f} "
                    f"(P/L: ${total_crypto_pl:,.2f})"
                )
                summary_lines.append("")
        except Exception as _e:
            print(f"  ⚠️ Crypto summary error: {_e}")

        summary_lines.append("Stock strategy: Trend-following — price above MAs, MACD positive.")
        summary_lines.append("ORB strategy: Opening Range Breakout — 3-phase retest confirmation on intraday data.")
        summary_lines.append("Crypto strategy: Elliott Wave — Wave 2 pullback bounce into Wave 3.")
        send_telegram("\n".join(summary_lines))
        print(f"\n📊 Hourly summary sent ({len(all_actions)} trades).")
    elif all_actions:
        print(f"\nTrade actions recorded ({len(all_actions)}), waiting for next hour mark to notify.")

print("\nDone.\n")
