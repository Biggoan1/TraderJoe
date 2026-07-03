"""
Crypto Trader Joe — Elliott Wave Strategy for BTC, ETH, SOL
============================================================

Strategy: Second-wave pullback into Wave 3 launch
- Elliott Wave theory: In a 5-wave impulse, Wave 2 retraces a portion of Wave 1
- We look for: Wave 1 completed (upthrust), Wave 2 pullback (retracement to 38.2%-61.8% of W1)
- Entry: Price bouncing off the retracement zone with momentum confirmation
- Target: Wave 3 (typically 1.618x or 2.618x the length of Wave 1)
- Exit: RSI overbought (>75), upper Bollinger, or MACD death cross (crypto-adjusted)
- Crypto trades 24/7 — no market hours check needed

Alpaca crypto symbols: BTC/USD, ETH/USD, SOL/USD
yfinance symbols:      BTC-USD, ETH-USD, SOL-USD

Author: Trader Joe
"""

import os
import sqlite3
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import requests
import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv(".env")

# --- Config ---
# ---------------------------------------------------------------------------
# PRODUCTION SAFEGUARD — DO NOT FLIP WITHOUT AN ApprovalRecord
# ---------------------------------------------------------------------------
# ``PAPER`` gates ``TradingClient(..., paper=PAPER)``.  Setting
# ``PAPER = False`` promotes the crypto runner to live trading with real
# money.
#
# Before this line may be changed to ``False``:
#   1. An ApprovalRecord (see ``strategy/promotion_gates.py``) must exist
#      naming approver, dated approval, flag scope, monitoring dashboard,
#      and rollback plan.
#   2. The relevant PromotionEntry must sit at STATE_APPROVED or
#      STATE_PRODUCTION with no triggered rollback alerts.
#   3. Walk-forward + paper-trading evidence per ROADMAP
#      "v1.0 Production Readiness" must pass.
#   4. `.env.production` must be populated with the approved credentials
#      in the same commit that flips this constant.
#   5. The approving ApprovalRecord id must be cited in the PR body.
#
# The Research Alpaca account (``strategy/research_account.py``,
# ``RESEARCH_ALPACA_*`` env namespace) MUST NEVER supply credentials to
# this runner.  See ``docs/agent/env-isolation.md``.
# ---------------------------------------------------------------------------
PAPER = True
AGENT_NAME = "Crypto Trader Joe"
AI_MODEL = "gpt-5-mini"
DB_FILE = "trades.db"
ET = ZoneInfo("US/Eastern")

# ORB config (crypto)
ORB_MAX_TRADE_MINUTES = 120  # Stop looking for ORB setups after 2h

# Crypto watchlist — Alpaca symbols for trading, yfinance for data
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
CRYPTO_YF_MAP = {
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "SOL/USD": "SOL-USD",
}

# Cooldown: don't re-buy within 60 minutes of selling (crypto is 24/7)
COOLDOWN_MINUTES = 60

# Position sizing — crypto is volatile, so cap per position
DEFAULT_BUY_AMOUNT = 3000
MAX_BUY_AMOUNT = 8000
MAX_POSITIONS = 3  # Max 3 crypto positions at once
TARGET_MAX_POSITIONS = 2

# RSI thresholds (crypto is more volatile)
RSI_OVERBOUGHT = 75
RSI_OVERROLD = 25

# Elliott Wave parameters
EW_LOOKBACK_BARS = 200   # Bars to look back for wave detection
EW_MIN_WAVE1_LENGTH = 0.03   # Wave 1 must be at least 3% move
EW_RETRACE_LOW = 0.382       # Minimum retracement for Wave 2
EW_RETRACE_HIGH = 0.786      # Maximum retracement for Wave 2 (golden zone up to 78.6%)
EW_MOMENTUM_CONFIRM = 4      # Min consecutive bars of momentum for Wave 3 launch
EW_WAVE3_TARGET_MULT = 1.618 # Wave 3 target = 1.618x Wave 1 length

# Sell signal thresholds (crypto-adjusted)
RSI_SELL = 75
BB_SELL_STD = 2.0
MACD_SELL_BARS = 2  # MACD histogram negative for N consecutive bars


# --- Alpaca client (crypto paper trading) ---
# Use crypto-specific API keys if available, fall back to stock keys
CRYPTO_ALPACA_KEY = os.getenv("CRYPTO_ALPACA_API_KEY", os.getenv("ALPACA_API_KEY"))
CRYPTO_ALPACA_SECRET = os.getenv("CRYPTO_ALPACA_SECRET_KEY", os.getenv("ALPACA_SECRET_KEY"))

client = TradingClient(CRYPTO_ALPACA_KEY, CRYPTO_ALPACA_SECRET, paper=PAPER)
ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def send_telegram(message):
    """Send a Telegram message."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}")


def get_crypto_yf_data(symbol, period="3mo", interval="1h"):
    """Fetch OHLCV data from yfinance for a crypto symbol.
    
    Uses 1-hour candles for better wave detection on crypto.
    """
    yf_symbol = CRYPTO_YF_MAP.get(symbol, symbol.replace("/", "-"))
    data = yf.download(
        yf_symbol,
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
    if len(data) < 50:
        return None
    return data


def compute_indicators(data):
    """Compute all technical indicators needed for Elliott Wave + sell signals."""
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    # SMAs
    data["SMA20"] = close.rolling(20).mean()
    data["SMA50"] = close.rolling(50).mean()
    data["SMA100"] = close.rolling(100).mean()

    # Bollinger Bands
    data["BB_STD"] = close.rolling(20).std()
    data["BB_LOWER"] = data["SMA20"] - (BB_SELL_STD * data["BB_STD"])
    data["BB_UPPER"] = data["SMA20"] + (BB_SELL_STD * data["BB_STD"])

    # RSI (14-period)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    data["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    data["MACD"] = ema12 - ema26
    data["MACD_SIGNAL"] = data["MACD"].ewm(span=9, adjust=False).mean()
    data["MACD_HIST"] = data["MACD"] - data["MACD_SIGNAL"]

    # ADX
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    data["ADX"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

    # Volume average
    data["VOL_AVG20"] = volume.rolling(20).mean()

    # ATR for stop loss calculation
    data["ATR"] = atr

    return data


def detect_swings(data, lookback=EW_LOOKBACK_BARS):
    """Detect local minima and maxima (swings) in the price series.
    
    Uses a ZIGZAG-like approach: a swing high is a peak that's at least
    a threshold above its neighbors, and a swing low is a trough.
    
    Returns list of (index, price, type) where type is 'H' or 'L'.
    """
    close = data["Close"].iloc[-lookback:]
    
    # Use Fractal-like swing detection
    # A swing high: higher than N bars before and after
    # A swing low: lower than N bars before and after
    swing_radius = 5  # bars on each side
    
    swings = []
    values = close.values
    
    for i in range(swing_radius, len(values) - swing_radius):
        window_before = values[i-swing_radius:i]
        window_after = values[i:i+swing_radius]
        
        if values[i] > window_before.max() and values[i] > window_after.max():
            swings.append((i, values[i], 'H'))
        elif values[i] < window_before.min() and values[i] < window_after.min():
            swings.append((i, values[i], 'L'))
    
    return swings


def identify_elliott_waves(data, lookback=EW_LOOKBACK_BARS):
    """Identify potential Elliott Wave patterns.
    
    Looks for:
    1. Wave 1: Initial impulse up from a low
    2. Wave 2: Pullback (retracement) to 38.2%-78.6% of Wave 1
    3. Wave 3: Launch point — price bouncing with momentum
    
    Returns wave analysis dict or None if no pattern found.
    """
    swings = detect_swings(data, lookback)
    if len(swings) < 4:
        return None
    
    close = data["Close"]
    total_len = len(close)
    
    # Filter swings to find the most recent potential Wave 1-2 sequence
    # We need: L -> H (Wave 1) -> L (Wave 2) -> checking for bounce (Wave 3)
    # Or: H -> L -> H -> L for a bearish setup (we focus on bullish)
    
    best_pattern = None
    best_score = -1
    
    for i in range(len(swings) - 2):
        s1, s2, s3 = swings[i], swings[i+1], swings[i+2]
        
        # Bullish pattern: Low -> High (Wave 1) -> Low (Wave 2 pullback)
        if s1[2] != 'L' or s2[2] != 'H' or s3[2] != 'L':
            continue
        
        wave1_start = s1[1]  # Low
        wave1_end = s2[1]    # High
        wave2_end = s3[1]    # Pullback low
        
        # Wave 1 must be upward
        wave1_length_pct = (wave1_end - wave1_start) / wave1_start
        if wave1_length_pct < EW_MIN_WAVE1_LENGTH:
            continue
        
        # Wave 2 retracement ratio
        retracement = (wave1_end - wave2_end) / (wave1_end - wave1_start)
        
        if not (EW_RETRACE_LOW <= retracement <= EW_RETRACE_HIGH):
            continue
        
        # How recent is this pattern? (more recent = better)
        recency = (total_len - s3[0]) / total_len
        
        # Score: prefer golden ratio retracements (0.5, 0.618, 0.786)
        golden_ratios = [0.382, 0.5, 0.618, 0.786]
        golden_dist = min(abs(retracement - gr) for gr in golden_ratios)
        golden_score = max(0, 1 - golden_dist * 10)
        
        score = recency * 0.6 + golden_score * 0.4
        
        if score > best_score:
            best_score = score
            best_pattern = {
                "wave1_start": wave1_start,
                "wave1_end": wave1_end,
                "wave2_end": wave2_end,
                "wave1_length_pct": wave1_length_pct,
                "retracement": retracement,
                "recency": recency,
                "score": score,
                "wave1_index": s1[0],
                "wave2_peak_index": s2[0],
                "wave2_end_index": s3[0],
            }
    
    return best_pattern


def check_wave3_launch(data, pattern):
    """Check if Wave 3 is launching from the Wave 2 pullback zone.
    
    Conditions:
    1. Price has bounced from the Wave 2 low
    2. RSI is rising from oversold/neutral territory
    3. MACD histogram is turning positive
    4. Price is above the bounce low with momentum
    5. Volume confirms the move
    
    Returns: (is_launching, confidence, entry_price, stop_loss, target)
    """
    close = data["Close"]
    rsi = data["RSI"]
    macd_hist = data["MACD_HIST"]
    volume = data["Volume"]
    vol_avg = data["VOL_AVG20"]
    
    current_price = float(close.iloc[-1])
    wave2_low = pattern["wave2_end"]
    wave1_start = pattern["wave1_start"]
    wave1_end = pattern["wave1_end"]
    wave1_length = wave1_end - wave1_start
    
    # Condition 1: Price bounced from Wave 2 low
    bounce_pct = (current_price - wave2_low) / wave2_low
    if bounce_pct < 0.005:  # Must be at least 0.5% above wave 2 low
        return False, 0, None, None, None
    
    # Condition 2: RSI is rising and not overbought
    current_rsi = float(rsi.iloc[-1])
    rsi_3_ago = float(rsi.iloc[-3]) if len(rsi) >= 3 else current_rsi
    rsi_rising = current_rsi > rsi_3_ago
    if current_rsi > RSI_OVERBOUGHT:
        return False, 0, None, None, None
    if current_rsi < 30:
        return False, 0, None, None, None  # Still oversold, wait
    
    # Condition 3: MACD histogram turning positive
    macd_current = float(macd_hist.iloc[-1])
    macd_prev = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else macd_current
    
    # Check for MACD cross-up or at least positive momentum
    macd_positive = macd_current > 0 or (macd_current > macd_prev and macd_current > 0)
    
    # Condition 4: Volume confirmation (above average)
    vol_ratio = float(volume.iloc[-1]) / float(vol_avg.iloc[-1]) if float(vol_avg.iloc[-1]) > 0 else 1.0
    
    # Condition 5: ADX shows trend strength
    adx = float(data["ADX"].iloc[-1])
    
    # Calculate confidence score
    confidence = 0.0
    
    if rsi_rising:
        confidence += 0.2
    if macd_positive:
        confidence += 0.25
    if vol_ratio > 1.0:
        confidence += 0.15
    if adx > 20:
        confidence += 0.15
    
    # Bounce quality — prefer small bounces (early entry)
    if 0.005 <= bounce_pct <= 0.05:
        confidence += 0.15
    elif bounce_pct > 0.10:
        confidence -= 0.05  # Already bounced too much
    
    # Recency bonus from pattern
    confidence += pattern["score"] * 0.1
    
    if confidence < 0.4:
        return False, confidence, None, None, None
    
    # Calculate entry, stop loss, and target
    entry_price = current_price
    
    # Stop loss: below Wave 2 low + 1 ATR
    atr = float(data["ATR"].iloc[-1])
    stop_loss = wave2_low - atr
    
    # Target: Wave 3 = 1.618x Wave 1 length from Wave 2 low
    target_price = wave2_low + (EW_WAVE3_TARGET_MULT * wave1_length)
    
    # Also calculate risk/reward
    risk = entry_price - stop_loss
    reward = target_price - entry_price
    risk_reward = reward / risk if risk > 0 else 0
    
    if risk_reward < 1.5:
        confidence -= 0.1
    
    is_launching = confidence >= 0.4
    
    return is_launching, confidence, entry_price, stop_loss, target_price


def evaluate_crypto_setup(symbol):
    """Full Elliott Wave analysis for a crypto symbol.
    
    Returns dict with wave analysis, entry signal, and key metrics.
    """
    data = get_crypto_yf_data(symbol)
    if data is None or len(data) < 60:
        return None
    
    data = compute_indicators(data)
    
    # Check overall trend (must be bullish for long entries)
    current_price = float(data["Close"].iloc[-1])
    sma20 = float(data["SMA20"].iloc[-1])
    sma50 = float(data["SMA50"].iloc[-1])
    rsi = float(data["RSI"].iloc[-1])
    adx = float(data["ADX"].iloc[-1])
    macd_hist = float(data["MACD_HIST"].iloc[-1])
    bb_upper = float(data["BB_UPPER"].iloc[-1])
    bb_lower = float(data["BB_LOWER"].iloc[-1])
    
    if pd.isna(sma20) or pd.isna(sma50) or pd.isna(rsi) or pd.isna(adx):
        return None
    
    # Elliott Wave detection
    pattern = identify_elliott_waves(data)
    
    wave3_launch = False
    confidence = 0
    entry_price = None
    stop_loss = None
    target = None
    
    if pattern:
        wave3_launch, confidence, entry_price, stop_loss, target = check_wave3_launch(data, pattern)
    
    result = {
        "symbol": symbol,
        "price": current_price,
        "sma20": sma20,
        "sma50": sma50,
        "rsi": rsi,
        "adx": adx,
        "macd_hist": macd_hist,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "trend_bullish": sma20 > sma50,
        "above_sma20": current_price > sma20,
        "elliott_pattern": pattern is not None,
        "wave3_launch": wave3_launch,
        "confidence": confidence,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target": target,
    }
    
    if pattern:
        result["wave1_start"] = pattern["wave1_start"]
        result["wave1_end"] = pattern["wave1_end"]
        result["wave2_end"] = pattern["wave2_end"]
        result["retracement"] = pattern["retracement"]
        result["wave1_pct"] = pattern["wave1_length_pct"]
    
    # Sell signals
    result["sell_signals"] = []
    if rsi > RSI_SELL:
        result["sell_signals"].append(f"RSI overbought ({rsi:.1f})")
    if current_price >= bb_upper:
        result["sell_signals"].append(f"Upper Bollinger (${bb_upper:.2f})")
    
    # MACD death cross
    macd_hist_prev = float(data["MACD_HIST"].iloc[-2])
    if macd_hist < 0 and macd_hist < macd_hist_prev:
        result["sell_signals"].append("MACD death cross")
    
    return result


# --- Crypto ORB: Opening Range Breakout with retest confirmation ---
#
# Uses 9:30 AM ET as the reference "open" to align with US market
# volatility spikes. Same 3-phase logic as stocks (Break → Retest → Confirm).

def _get_crypto_open_et():
    """Return today's 9:30 AM ET as the crypto ORB reference open."""
    now_et = datetime.now(ET)
    return now_et.replace(hour=9, minute=30, second=0, microsecond=0)


def _fetch_crypto_intraday(yf_symbol, interval="5m", period="5d"):
    """Fetch intraday OHLCV for crypto via yfinance."""
    data = yf.download(
        yf_symbol,
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


def _get_crypto_orb_range(data, open_dt, orb_minutes=15, interval_minutes=5):
    """Get ORB high/low from first N minutes after reference open."""
    bars_needed = max(orb_minutes // interval_minutes, 1)
    today_data = data[data.index >= open_dt]
    if len(today_data) < bars_needed:
        return None
    orb_bars = today_data.iloc[:bars_needed]
    return (float(orb_bars["High"].max()), float(orb_bars["Low"].min()), len(orb_bars))


def _detect_crypto_orb_retest(data, open_dt, orb_high, orb_low, direction,
                               interval_minutes=5, orb_minutes=15):
    """Three-phase ORB detection for crypto."""
    bars_after_orb = max(orb_minutes // interval_minutes, 1)
    today_data = data[data.index >= open_dt]
    if len(today_data) <= bars_after_orb:
        return None

    post_orb = today_data.iloc[bars_after_orb:]
    closes = post_orb["Close"]
    highs = post_orb["High"]
    lows = post_orb["Low"]

    break_idx = None
    for i in range(len(post_orb)):
        if direction == "bull" and closes.iloc[i] > orb_high:
            break_idx = i
            break
        elif direction == "bear" and closes.iloc[i] < orb_low:
            break_idx = i
            break

    if break_idx is None or break_idx >= len(post_orb) - 1:
        return None

    retest_idx = None
    for i in range(break_idx + 1, len(post_orb)):
        if direction == "bull" and lows.iloc[i] <= orb_high:
            retest_idx = i
            break
        elif direction == "bear" and highs.iloc[i] >= orb_low:
            retest_idx = i
            break

    if retest_idx is None or retest_idx >= len(post_orb) - 1:
        return None

    for i in range(retest_idx + 1, len(post_orb)):
        if direction == "bull" and closes.iloc[i] > orb_high:
            return {
                "direction": "long",
                "entry_price": float(closes.iloc[i]),
                "stop_loss": orb_low,
                "take_profit": orb_high + (orb_high - orb_low),
                "orb_high": orb_high,
                "orb_low": orb_low,
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
                "confirm_time": str(post_orb.index[i]),
            }

    return None


def _save_crypto_orb_state(symbol, timeframe, orb_high, orb_low, state, extra=None):
    """Persist crypto ORB state."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crypto_orb_state (
            symbol TEXT, timeframe TEXT, date TEXT,
            orb_high REAL, orb_low REAL, state TEXT,
            updated_at TEXT, extra TEXT,
            PRIMARY KEY (symbol, timeframe, date)
        )
    """)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    extra_json = json.dumps(extra) if extra else None
    cur.execute("""
        INSERT OR REPLACE INTO crypto_orb_state
            (symbol, timeframe, date, orb_high, orb_low, state, updated_at, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, timeframe, today, orb_high, orb_low, state,
          datetime.now().isoformat(), extra_json))
    conn.commit()
    conn.close()


def _clear_crypto_orb_state():
    """Clear stale crypto ORB state."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    yesterday = (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crypto_orb_state (
            symbol TEXT, timeframe TEXT, date TEXT,
            orb_high REAL, orb_low REAL, state TEXT,
            updated_at TEXT, extra TEXT,
            PRIMARY KEY (symbol, timeframe, date)
        )
    """)
    cur.execute("DELETE FROM crypto_orb_state WHERE date < ?", (yesterday,))
    conn.commit()
    conn.close()


def evaluate_crypto_orb_setup(symbol, timeframe="15m"):
    """Evaluate ORB setup for a crypto symbol.

    Uses 9:30 AM ET as reference open. Returns dict or None.
    """
    now_et = datetime.now(ET)
    crypto_open = _get_crypto_open_et()
    minutes_since_open = (now_et - crypto_open).total_seconds() / 60

    if minutes_since_open < 0:
        return None
    if minutes_since_open > ORB_MAX_TRADE_MINUTES:
        return None

    yf_symbol = CRYPTO_YF_MAP.get(symbol)
    if not yf_symbol:
        return None

    data = _fetch_crypto_intraday(yf_symbol, interval="5m", period="5d")
    if data is None or len(data) < 10:
        return None

    if data.index.tz is None:
        data.index = data.index.tz_localize("UTC").tz_convert(ET)
    else:
        data.index = data.index.tz_convert(ET)

    orb_minutes = int(timeframe.replace("m", ""))

    orb_result = _get_crypto_orb_range(data, crypto_open, orb_minutes=orb_minutes, interval_minutes=5)
    if orb_result is None:
        return None

    orb_high, orb_low, orb_bars = orb_result
    range_pct = (orb_high - orb_low) / orb_low
    if range_pct < 0.001:
        return None

    retest = _detect_crypto_orb_retest(data, crypto_open, orb_high, orb_low,
                                        direction="bull", interval_minutes=5,
                                        orb_minutes=orb_minutes)

    state = "confirmed" if retest else ("active" if minutes_since_open > orb_minutes else "forming")
    _save_crypto_orb_state(symbol, timeframe, orb_high, orb_low, state, {
        "orb_bars": orb_bars,
        "range_pct": range_pct,
        "retest": retest is not None,
    })

    if retest:
        entry_price = retest["entry_price"]
        stop_loss = retest["stop_loss"]
        take_profit = retest["take_profit"]
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
        risk_reward = reward / risk if risk > 0 else 0

        return {
            "symbol": symbol,
            "strategy": "CRYPTO_ORB",
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
            "state": "confirmed",
            "score": 8.0 + min(risk_reward, 2.0),
        }
    else:
        return {
            "symbol": symbol,
            "strategy": "CRYPTO_ORB",
            "timeframe": timeframe,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "range_pct": range_pct * 100,
            "state": state,
            "minutes_since_open": minutes_since_open,
        }


def get_crypto_positions():
    """Get all crypto positions from Alpaca."""
    all_positions = client.get_all_positions()
    return [p for p in all_positions if getattr(p, 'asset_class', None) and str(p.asset_class) == 'AssetClass.CRYPTO']


def crypto_already_owned(symbol):
    """Check if we already own this crypto symbol."""
    for pos in get_crypto_positions():
        if pos.symbol == symbol:
            return True
    return False


def crypto_has_open_order(symbol):
    """Check if there's an open order for this symbol."""
    for order in client.get_orders():
        if order.symbol == symbol:
            return True
    return False


def record_crypto_cooldown(symbol):
    """Record cooldown in DB."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Use crypto_cooldowns table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crypto_cooldowns (
            symbol TEXT PRIMARY KEY,
            sold_at TEXT
        )
    """)
    cur.execute("""
        INSERT OR REPLACE INTO crypto_cooldowns (symbol, sold_at)
        VALUES (?, ?)
    """, (symbol, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def crypto_on_cooldown(symbol):
    """Check if symbol is on cooldown."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crypto_cooldowns (
            symbol TEXT PRIMARY KEY,
            sold_at TEXT
        )
    """)
    row = cur.execute("""
        SELECT sold_at FROM crypto_cooldowns WHERE symbol = ?
    """, (symbol,)).fetchone()
    conn.close()
    if row is None:
        return False
    sold_at = datetime.fromisoformat(row[0])
    return (datetime.now() - sold_at) < timedelta(minutes=COOLDOWN_MINUTES)


def place_crypto_buy(symbol, dollars):
    """Place a crypto buy order via Alpaca."""
    # Get current price from yfinance
    yf_symbol = CRYPTO_YF_MAP.get(symbol, symbol.replace("/", "-"))
    price_data = yf.Ticker(yf_symbol).history(period="1h")
    
    if price_data.empty:
        raise RuntimeError(f"No price data for {symbol}")
    
    price = float(price_data["Close"].iloc[-1])
    qty = round(dollars / price, 8)  # Crypto supports fractional to 8 decimals
    
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY
    )
    
    return client.submit_order(order)


def place_crypto_sell(symbol, dollars=None, sell_all=True):
    """Place a crypto sell order via Alpaca."""
    position = None
    for p in client.get_all_positions():
        if p.symbol == symbol:
            position = p
            break
    
    if not position:
        raise RuntimeError(f"No current {symbol} position")
    
    owned_qty = float(position.qty)
    
    if sell_all:
        qty = owned_qty
    else:
        yf_symbol = CRYPTO_YF_MAP.get(symbol, symbol.replace("/", "-"))
        price_data = yf.Ticker(yf_symbol).history(period="1h")
        if price_data.empty:
            raise RuntimeError(f"No price data for {symbol}")
        price = float(price_data["Close"].iloc[-1])
        qty = round(dollars / price, 8)
        qty = min(qty, owned_qty)
    
    if qty <= 0:
        raise RuntimeError(f"Calculated sell quantity is zero for {symbol}")
    
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY
    )
    
    return client.submit_order(order)


def ai_analyze_crypto(symbol, setup):
    """Get AI analysis of a crypto Elliott Wave setup."""
    wave_info = ""
    if setup.get("elliott_pattern"):
        wave_info = f"""
Elliott Wave Pattern Detected:
- Wave 1: ${setup.get('wave1_start', 0):,.2f} → ${setup.get('wave1_end', 0):,.2f} ({setup.get('wave1_pct', 0)*100:.1f}%)
- Wave 2 Retracement: {setup.get('retracement', 0)*100:.1f}% (target: 38.2%-78.6%)
- Wave 2 Low: ${setup.get('wave2_end', 0):,.2f}
"""
        if setup.get("wave3_launch"):
            wave_info += f"""
- Wave 3 Launch: YES (confidence: {setup.get('confidence', 0):.2f})
- Entry: ${setup.get('entry_price', 0):,.2f}
- Stop Loss: ${setup.get('stop_loss', 0):,.2f}
- Target: ${setup.get('target', 0):,.2f}
"""
    
    prompt = f"""You are Crypto Trader Joe, specializing in Elliott Wave theory for cryptocurrency trading.

Analyze this crypto Elliott Wave setup:

Ticker: {symbol}
Current Price: ${setup['price']:,.2f}
RSI (14): {setup['rsi']:.1f}
ADX: {setup['adx']:.1f}
MACD Histogram: {setup['macd_hist']:.4f}
Trend: {'Bullish (SMA20 > SMA50)' if setup['trend_bullish'] else 'Bearish (SMA20 < SMA50)'}
Price vs SMA20: {'Above' if setup['above_sma20'] else 'Below'}

{wave_info}

Rules:
- This is paper trading on Alpaca.
- Strategy: Enter on Wave 2 pullback bounce into Wave 3.
- Wave 2 should retrace 38.2%-78.6% of Wave 1.
- Wave 3 is typically the strongest wave (1.618x Wave 1).
- Crypto trades 24/7 — no market hours constraints.
- Do not claim certainty — Elliott Wave counting is interpretive.
- Keep the tone useful, clear, and mildly fun.

Return this exact format:

AI Recommendation: APPROVE or HOLD OFF
Confidence: 1-10
Reason: one short paragraph
Risk: LOW, MEDIUM, or HIGH"""

    try:
        response = ai.responses.create(
            model=AI_MODEL,
            input=prompt
        )
        return response.output_text.strip()
    except Exception as e:
        return f"AI analysis failed: {e}"


def log_crypto_trade(symbol, side, amount, price, reason):
    """Log a crypto trade to the database."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crypto_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            side TEXT,
            amount REAL,
            price REAL,
            reason TEXT
        )
    """)
    cur.execute("""
        INSERT INTO crypto_trades (created_at, symbol, side, amount, price, reason)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        symbol,
        side,
        amount,
        price,
        reason,
    ))
    conn.commit()
    conn.close()


def crypto_risk_check(symbol, dollars):
    """Risk check for crypto buys."""
    if dollars > MAX_BUY_AMOUNT:
        return False, f"Crypto buy amount too large. Max allowed is ${MAX_BUY_AMOUNT}"
    
    acct = client.get_account()
    if float(acct.cash) < dollars:
        return False, "Not enough cash"
    
    return True, "OK"


def crypto_sell_risk_check(symbol, dollars):
    """Risk check for crypto sells."""
    position = None
    for p in client.get_all_positions():
        if p.symbol == symbol:
            position = p
            break
    
    if not position:
        return False, f"No current {symbol} position"
    
    market_value = float(position.market_value)
    if dollars <= 0:
        return False, "Sell amount must be positive"
    if dollars > market_value:
        return False, f"Sell amount exceeds position value (${market_value:,.2f})"
    
    return True, "OK"


def run_crypto_scan():
    """Main crypto scan — checks sell signals, then buy setups."""
    trade_actions = []
    
    # --- Phase 1: Sell signals on existing crypto positions ---
    crypto_positions = get_crypto_positions()
    
    if crypto_positions:
        print("\n=== CRYPTO SELL SIGNAL SCAN ===")
        for pos in crypto_positions:
            symbol = pos.symbol
            setup = evaluate_crypto_setup(symbol)
            
            if setup is None:
                print(f"  {symbol}: NO DATA")
                continue
            
            sell_reasons = setup.get("sell_signals", [])
            
            if sell_reasons:
                pos_value = float(pos.market_value)
                print(f"  📉 {symbol}: SELL — {', '.join(sell_reasons)} (value: ${pos_value:,.2f})")
                
                trade_actions.append(
                    f"📉 Sold {symbol} @ ${setup['price']:,.2f} (${pos_value:,.2f}) — {', '.join(sell_reasons)}"
                )
                
                try:
                    place_crypto_sell(symbol, sell_all=True)
                    record_crypto_cooldown(symbol)
                    log_crypto_trade(symbol, "SELL", pos_value, setup['price'], ", ".join(sell_reasons))
                    print(f"  ✅ {symbol} sell order submitted")
                except Exception as e:
                    print(f"  ❌ {symbol} sell failed: {e}")
            else:
                pl = float(pos.unrealized_pl)
                emoji = "💎" if pl > 0 else "📉"
                print(f"  {emoji} {symbol}: HOLD (RSI={setup['rsi']:.1f}, BB_upper=${setup['bb_upper']:,.2f})")
    
    # --- Phase 1.5: ORB scan (crypto opening range breakout with retest) ---
    orb_actions = []
    now_et = datetime.now(ET)
    crypto_open = _get_crypto_open_et()
    minutes_since_open = (now_et - crypto_open).total_seconds() / 60
    orb_timeframes = ["15m", "5m", "30m"]
    orb_candidates = []
    if 0 < minutes_since_open <= ORB_MAX_TRADE_MINUTES:
        print("\n=== CRYPTO ORB BREAKOUT SCAN ===")
        print(f"  Window: {minutes_since_open:.0f} min after US open (closes at {ORB_MAX_TRADE_MINUTES} min)")
        _clear_crypto_orb_state()
        for symbol in CRYPTO_SYMBOLS:
            if crypto_already_owned(symbol):
                continue
            if crypto_has_open_order(symbol):
                continue
            if crypto_on_cooldown(symbol):
                continue
            for tf in orb_timeframes:
                orb = evaluate_crypto_orb_setup(symbol, timeframe=tf)
                if orb and orb.get("state") == "confirmed":
                    orb["timeframe"] = tf
                    orb_candidates.append(orb)
                    print(f"  🚀 {symbol} [{tf}] ORB CONFIRMED — entry=${orb['entry_price']:,.2f} "
                          f"range={orb['range_pct']:.2f}% RR={orb['risk_reward']:.1f} "
                          f"stop=${orb['stop_loss']:,.2f} target=${orb['take_profit']:,.2f}")
                    break
                elif orb:
                    print(f"  ⏳ {symbol} [{tf}] ORB {orb['state']} — range={orb['range_pct']:.2f}%")
                    break
    else:
        if minutes_since_open <= 0:
            print("\nUS market not yet open — skipping crypto ORB scan.\n")
        else:
            print(f"\nCrypto ORB window closed ({minutes_since_open:.0f} min > {ORB_MAX_TRADE_MINUTES} min).\n")
    # Process ORB candidates first (higher priority during ORB window)
    for orb_setup in orb_candidates[:1]:
        allowed, reason = crypto_risk_check(orb_setup["symbol"], DEFAULT_BUY_AMOUNT)
        if not allowed:
            print(f"  ORB risk check failed: {reason}")
            orb_actions.append(f"⚠️ ORB rejected {orb_setup['symbol']}: {reason}")
            continue
        symbol = orb_setup["symbol"]
        buy_amount = DEFAULT_BUY_AMOUNT
        try:
            analysis = ai_analyze_crypto(symbol, orb_setup)
            orb_actions.append(
                f"🚀 ORB Buy {symbol} @ ${orb_setup['entry_price']:,.2f} "
                f"— ${buy_amount:,.2f} (15m ORB, RR={orb_setup['risk_reward']:.1f}, "
                f"range={orb_setup['range_pct']:.2f}%)"
            )
            order = place_crypto_buy(symbol, buy_amount)
            log_crypto_trade(symbol, "BUY", buy_amount, orb_setup['entry_price'],
                            f"ORB retest confirm, RR={orb_setup['risk_reward']:.1f}")
            print(f"  ✅ {symbol} ORB buy submitted: {order.status}")
            send_telegram(f"""
🚀 Crypto Trader Joe — ORB Breakout Entry

Symbol: {symbol}
Strategy: Opening Range Breakout (Retest Confirmed)
Price: ${orb_setup['entry_price']:,.2f}
Amount: ${buy_amount:,.2f}
ORB Range: ${orb_setup['orb_low']:,.2f} - ${orb_setup['orb_high']:,.2f}
Stop Loss: ${orb_setup['stop_loss']:,.2f}
Target: ${orb_setup['take_profit']:,.2f}
Risk/Reward: {orb_setup['risk_reward']:.1f}

{analysis}
""")
        except Exception as e:
            print(f"  ❌ {symbol} ORB buy failed: {e}")
            orb_actions.append(f"❌ ORB buy failed {symbol}: {e}")

    # --- Phase 2: Buy setups ---
    print("\n=== CRYPTO BUY SETUP SCAN ===")
    candidates = []
    
    for symbol in CRYPTO_SYMBOLS:
        if crypto_already_owned(symbol):
            print(f"  {symbol}: Already owned, skipping")
            continue
        
        if crypto_has_open_order(symbol):
            print(f"  {symbol}: Open order, skipping")
            continue
        
        if crypto_on_cooldown(symbol):
            print(f"  {symbol}: On cooldown, skipping")
            continue
        
        setup = evaluate_crypto_setup(symbol)
        
        if setup is None:
            print(f"  {symbol}: NO DATA / INSUFFICIENT HISTORY")
            continue
        
        print(
            f"  {symbol:8} "
            f"Price=${setup['price']:,.2f} "
            f"RSI={setup['rsi']:.1f} "
            f"ADX={setup['adx']:.1f} "
            f"MACD={setup['macd_hist']:.4f} "
            f"EW={setup['elliott_pattern']} "
            f"W3={setup['wave3_launch']}"
        )
        
        if setup["wave3_launch"]:
            candidates.append(setup)
    
    # Process up to 1 new buy per scan (crypto is volatile, be careful)
    current_positions = len(get_crypto_positions())
    
    for best_setup in candidates[:1]:
        if current_positions >= MAX_POSITIONS:
            print(f"  Hit hard cap of {MAX_POSITIONS} crypto positions — no more buys.")
            break
        
        buy_amount = min(
            MAX_BUY_AMOUNT,
            max(DEFAULT_BUY_AMOUNT, round(float(client.get_account().portfolio_value) * 0.03, 2))
        )
        
        allowed, reason = crypto_risk_check(best_setup["symbol"], buy_amount)
        
        if not allowed:
            print(f"  Risk check failed: {reason}")
            continue
        
        symbol = best_setup["symbol"]
        
        print(
            f"\n  🚀 Crypto buy: {symbol} "
            f"confidence={best_setup['confidence']:.2f} "
            f"retrace={best_setup.get('retracement', 0)*100:.1f}%"
        )
        
        try:
            # Get AI analysis
            analysis = ai_analyze_crypto(symbol, best_setup)
            
            trade_actions.append(
                f"🚀 Bought {symbol} @ ${best_setup['price']:,.2f} — ${buy_amount:,.2f} "
                f"(EW Wave3, confidence: {best_setup['confidence']:.2f}, "
                f"retracement: {best_setup.get('retracement', 0)*100:.1f}%)"
            )
            
            order = place_crypto_buy(symbol, buy_amount)
            log_crypto_trade(symbol, "BUY", buy_amount, best_setup['price'], 
                           f"EW Wave3 launch, confidence={best_setup['confidence']:.2f}")
            
            print(f"  ✅ {symbol} buy order submitted: {order.status}")
            
            # Send immediate notification for crypto buys (user asked for crypto in hourly)
            send_telegram(f"""
🚀 Crypto Trader Joe — Elliott Wave Entry

Symbol: {symbol}
Price: ${best_setup['price']:,.2f}
Amount: ${buy_amount:,.2f}
Confidence: {best_setup['confidence']:.2f}
Wave 2 Retracement: {best_setup.get('retracement', 0)*100:.1f}%
Target: ${best_setup.get('target', 0):,.2f}
Stop Loss: ${best_setup.get('stop_loss', 0):,.2f}

{analysis}
""")
            
        except Exception as e:
            print(f"  ❌ {symbol} buy failed: {e}")
    
    return trade_actions


def get_crypto_summary():
    """Get crypto portfolio summary for inclusion in hourly report."""
    crypto_positions = get_crypto_positions()
    
    total_crypto_value = 0
    total_crypto_pl = 0
    lines = []
    
    for pos in crypto_positions:
        mv = float(pos.market_value)
        pl = float(pos.unrealized_pl)
        total_crypto_value += mv
        total_crypto_pl += pl
        emoji = "💎" if pl > 0 else "📉"
        lines.append(f"  {emoji} {pos.symbol} qty={pos.qty} value=${mv:,.2f} P/L=${pl:,.2f}")
    
    return {
        "positions": crypto_positions,
        "total_value": total_crypto_value,
        "total_pl": total_crypto_pl,
        "lines": lines,
    }


# --- Main execution ---
if __name__ == "__main__":
    print("""
=================================
   Crypto Trader Joe v1.0 🌙
=================================
  Elliott Wave Crypto Strategy
  BTC / ETH / SOL — 24/7 Trading
=================================
""")
    
    acct = client.get_account()
    print(f"Cash: ${float(acct.cash):,.2f}")
    print(f"Portfolio: ${float(acct.portfolio_value):,.2f}")
    print()
    
    trade_actions = run_crypto_scan()

    # Hourly summary for crypto
    if trade_actions:
        acct = client.get_account()
        summary_lines = [
            f"🌙 Crypto Trader Joe — Update",
            f"⏰ {datetime.now().strftime('%H:%M %Z')}",
            f"💰 Portfolio: ${float(acct.portfolio_value):,.2f}",
            f"💵 Cash: ${float(acct.cash):,.2f}",
            "",
            "Crypto Trades:",
        ]
        summary_lines.extend(trade_actions)
        summary_lines.append("")
        summary_lines.append("Crypto strategies: Elliott Wave + ORB Breakout (during US open).")
        send_telegram("\n".join(summary_lines))
        print(f"\n🌙 Crypto summary sent ({len(trade_actions)} trades).")

    print("\nCrypto scan done.\n")
