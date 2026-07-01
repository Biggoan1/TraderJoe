#!/usr/bin/env python3
"""
Backtest: Apply current six-gate rules to historical data.

Simulates the bot's trading logic over a date range using daily OHLCV data
from yfinance. Compares backtest results against actual paper account.

Rules applied:
- 6-gate buy system (need 4/6): above SMA20, SMA20 > SMA50, RSI < 70, MACD histogram > 0, ADX > 15, volume > 80% avg
- Auto-sell: RSI > 70, price >= upper Bollinger Band, or MACD death cross
- 30-min cooldown (treated as 1-day in daily data)
- Position cap: 15 target, 20 hard cap
- Auto-trim weakest position when over 15
- Buy size: $5K per position (5% of $100K)
- Slippage: 0.1% per trade
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys

# ── Configuration ──
START_DATE = "2026-05-29"
END_DATE   = "2026-06-15"
STARTING_CASH = 100_000.0
BUY_AMOUNT    = 5_000.0
MAX_POSITIONS = 20
TARGET_POSITIONS = 15
SLIPPAGE_PCT = 0.001  # 0.1% per trade
GATES_REQUIRED = 4

# Watchlist (from trader.py)
WATCHLIST = [
    # Core ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector rotation
    "XLF", "XLK", "XLE", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU", "SMH",
    # Defensive
    "TLT", "IEF", "LQD", "GLD", "VNQ",
    # Momentum
    "ARKK",
    # Stocks
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "TSLA", "AMZN", "INTC",
    "MRVL", "GOOGL", "GOOG", "MU", "BRKB", "LLY", "META", "JPM",
    "XOM", "JNJ", "V", "WMT", "COST", "MA", "ABBV", "NFLX",
]

# Bucket priority (higher = preferred)
BUCKET_PRIORITY = {
    "core": 4, "sector_rotation": 3, "defensive": 2, "stocks": 1, "momentum": 0,
}

ETF_BUCKETS = {
    "core": {"SPY", "QQQ", "IWM", "DIA"},
    "sector_rotation": {"XLF", "XLK", "XLE", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU", "SMH"},
    "defensive": {"TLT", "IEF", "LQD", "GLD", "VNQ"},
    "momentum": {"ARKK"},
}

def get_bucket(sym):
    for bucket, syms in ETF_BUCKETS.items():
        if sym in syms:
            return bucket
    return "stocks"

def calc_indicators(df):
    """Compute all indicators for evaluate_etf_setup."""
    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"]
    
    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    bb_std = c.rolling(20).std()
    df["BB_UPPER"] = df["SMA20"] + 2 * bb_std
    df["BB_LOWER"] = df["SMA20"] - 2 * bb_std
    
    # RSI (Wilder's 14)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    
    # ADX (14)
    up = h.diff()
    dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    
    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    df["MACD"] = macd
    df["MACD_SIG"] = macd.ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = macd - df["MACD_SIG"]
    
    df["VOL_AVG20"] = v.rolling(20).mean()
    
    return df

def evaluate_setup(row):
    """Return gate dict and score for a single row (daily bar)."""
    price = row["Close"]
    sma20 = row["SMA20"]
    sma50 = row["SMA50"]
    bb_upper = row["BB_UPPER"]
    rsi = row["RSI"]
    adx = row["ADX"]
    macd_hist = row["MACD_HIST"]
    volume = row["Volume"]
    vol_avg = row["VOL_AVG20"]
    
    # Need valid data
    if pd.isna(sma20) or pd.isna(sma50) or pd.isna(rsi) or pd.isna(adx) or pd.isna(macd_hist):
        return None, 0.0
    
    gates = {
        "above_sma20": price > sma20,
        "sma20_above_sma50": sma20 > sma50,
        "rsi_not_overbought": rsi < 70,
        "macd_positive": macd_hist > 0,
        "adx_strength": adx > 15,
        "volume_confirmation": volume > vol_avg * 0.8,
    }
    
    gate_count = sum(1 for v in gates.values() if v)
    
    score = 0.0
    score += 1.5 if gates["above_sma20"] else 0.0
    score += 1.5 if gates["sma20_above_sma50"] else 0.0
    score += 1.0 if gates["rsi_not_overbought"] else 0.0
    score += 1.5 if gates["macd_positive"] else 0.0
    score += 1.0 if gates["adx_strength"] else 0.0
    score += 0.5 if gates["volume_confirmation"] else 0.0
    score += min(max(adx - 15.0, 0.0) / 10.0, 1.5)
    score += min(max((volume / vol_avg - 0.8) if vol_avg else 0, 0.0), 1.0)
    
    return gates, score

def check_sell(row, prev_macd_hist=0):
    """Check sell conditions. Returns list of reasons or empty."""
    reasons = []
    if pd.notna(row["RSI"]) and row["RSI"] > 70:
        reasons.append(f"RSI overbought ({row['RSI']:.1f})")
    if pd.notna(row["BB_UPPER"]) and row["Close"] >= row["BB_UPPER"]:
        reasons.append(f"Price at/above upper BB (${row['BB_UPPER']:.2f})")
    macd_hist = row["MACD_HIST"]
    if pd.notna(macd_hist) and macd_hist < 0 and macd_hist < prev_macd_hist:
        reasons.append("MACD death cross")
    return reasons

# ── Load all data ──
print("Loading historical data...")
all_data = {}
for sym in WATCHLIST:
    try:
        df = yf.download(sym, start="2026-03-01", end="2026-06-16", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = calc_indicators(df)
        all_data[sym] = df
    except Exception as e:
        print(f"  Failed to load {sym}: {e}")

print(f"Loaded {len(all_data)} symbols")

# Get market days in range
market_days = set()
for df in all_data.values():
    mask = (df.index >= pd.Timestamp(START_DATE)) & (df.index <= pd.Timestamp(END_DATE))
    market_days.update(df.index[mask])
market_days = sorted(market_days)
print(f"Market days: {len(market_days)} ({market_days[0].date()} → {market_days[-1].date()})")

# ── Simulate ──
cash = STARTING_CASH
positions = {}  # sym -> {"qty": float, "entry_price": float, "score": float}
cooldowns = {}  # sym -> date when sold
trades = []     # list of {"date", "sym", "side", "price", "qty", "amount", "reason"}
daily_snapshots = []

print("\n" + "="*70)
print("BACKTEST START")
print("="*70)

for day_idx, day in enumerate(market_days):
    day_str = day.date().isoformat()
    
    # ── Phase 1: Check sell signals ──
    sells_today = []
    for sym in list(positions.keys()):
        if sym not in all_data:
            continue
        df = all_data[sym]
        if day not in df.index:
            continue
        row = df.loc[day]
        
        # Get previous MACD hist for death cross detection
        prev_macd = 0.0
        prev_day_idx = day_idx - 1
        if prev_day_idx >= 0:
            prev_day = market_days[prev_day_idx]
            if prev_day in df.index:
                pm = df.loc[prev_day, "MACD_HIST"]
                if pd.notna(pm):
                    prev_macd = float(pm)
        
        reasons = check_sell(row, prev_macd_hist=prev_macd)
        if reasons:
            sells_today.append((sym, reasons, float(row["Close"])))
    
    # Execute sells
    for sym, reasons, price in sells_today:
        pos = positions[sym]
        market_value = pos["qty"] * price * (1 - SLIPPAGE_PCT)
        cost_basis = pos["qty"] * pos["entry_price"]
        pnl = market_value - cost_basis
        cash += market_value
        trades.append({
            "date": day_str,
            "symbol": sym,
            "side": "SELL",
            "price": price,
            "qty": pos["qty"],
            "amount": market_value,
            "pnl": pnl,
            "reason": ", ".join(reasons),
        })
        cooldowns[sym] = day
        del positions[sym]
    
    # ── Phase 2: Scan for buys ──
    current_pos_count = len(positions)
    
    # Trim if over target
    if current_pos_count >= TARGET_POSITIONS and len(sells_today) == 0:
        # Find weakest position
        weakest_sym = None
        weakest_score = float('inf')
        for sym, pos in positions.items():
            if sym not in all_data or day not in all_data[sym].index:
                continue
            row = all_data[sym].loc[day]
            if pd.isna(row.get("SMA20", np.nan)):
                continue
            _, score = evaluate_setup(row)
            # Adjust for P/L
            pl = pos["qty"] * (float(row["Close"]) - pos["entry_price"])
            adjusted = score + (pl / 1000)
            if adjusted < weakest_score:
                weakest_score = adjusted
                weakest_sym = sym
        
        if weakest_sym and current_pos_count >= TARGET_POSITIONS:
            sell_price = float(all_data[weakest_sym].loc[day, "Close"])
            pos = positions[weakest_sym]
            mv = pos["qty"] * sell_price * (1 - SLIPPAGE_PCT)
            cost = pos["qty"] * pos["entry_price"]
            pnl = mv - cost
            cash += mv
            trades.append({
                "date": day_str,
                "symbol": weakest_sym,
                "side": "TRIM",
                "price": sell_price,
                "qty": pos["qty"],
                "amount": mv,
                "pnl": pnl,
                "reason": f"Position cap ({current_pos_count} >= {TARGET_POSITIONS})",
            })
            cooldowns[weakest_sym] = day
            del positions[weakest_sym]
            current_pos_count -= 1
    
    # Block buys if at hard cap
    if current_pos_count >= MAX_POSITIONS:
        pass  # No more buys
    else:
        # Evaluate candidates
        candidates = []
        for sym in WATCHLIST:
            if sym in positions:
                continue
            if sym in cooldowns and cooldowns[sym] >= day:  # On cooldown (same day or later)
                continue
            if sym not in all_data or day not in all_data[sym].index:
                continue
            
            row = all_data[sym].loc[day]
            gates, score = evaluate_setup(row)
            
            if gates is None:
                continue
            
            gate_count = sum(1 for v in gates.values() if v)
            if gate_count < GATES_REQUIRED:
                continue
            
            bucket = get_bucket(sym)
            candidates.append({
                "symbol": sym,
                "score": score,
                "gate_count": gate_count,
                "bucket": bucket,
                "price": float(row["Close"]),
                "adx": float(row["ADX"]),
                "volume": float(row["Volume"]),
                "vol_avg": float(row["VOL_AVG20"]),
            })
        
        # Sort by score (highest first), then ADX, then volume ratio, then bucket priority
        candidates.sort(key=lambda x: (
            x["score"],
            x["adx"],
            x["volume"] / x["vol_avg"] if x["vol_avg"] else 0,
            BUCKET_PRIORITY.get(x["bucket"], 0),
        ), reverse=True)
        
        # Buy up to 1 per day (conservative daily backtest)
        # Or buy while we have room and cash
        buys_today = 0
        for cand in candidates:
            if current_pos_count >= MAX_POSITIONS:
                break
            if cash < BUY_AMOUNT * 1.1:  # Need 10% buffer
                break
            
            buy_price = cand["price"] * (1 + SLIPPAGE_PCT)
            qty = BUY_AMOUNT / buy_price
            cash -= BUY_AMOUNT
            positions[cand["symbol"]] = {
                "qty": qty,
                "entry_price": buy_price,
                "score": cand["score"],
            }
            trades.append({
                "date": day_str,
                "symbol": cand["symbol"],
                "side": "BUY",
                "price": buy_price,
                "qty": qty,
                "amount": BUY_AMOUNT,
                "pnl": 0,
                "reason": f"Score={cand['score']:.2f}, Gates={cand['gate_count']}/6",
            })
            current_pos_count += 1
            buys_today += 1
        
        # Clear cooldowns that are now stale (older than today)
        for sym in list(cooldowns.keys()):
            if cooldowns[sym] < day:
                del cooldowns[sym]
    
    # ── Daily snapshot ──
    portfolio_val = cash
    for sym, pos in positions.items():
        if sym in all_data and day in all_data[sym].index:
            portfolio_val += pos["qty"] * float(all_data[sym].loc[day, "Close"])
        else:
            portfolio_val += pos["qty"] * pos["entry_price"]  # fallback
    
    daily_snapshots.append({
        "date": day_str,
        "cash": cash,
        "portfolio_value": portfolio_val,
        "positions": len(positions),
        "buys": sum(1 for t in trades if t["date"] == day_str and t["side"] == "BUY"),
        "sells": sum(1 for t in trades if t["date"] == day_str and t["side"] in ("SELL", "TRIM")),
    })

# ── Results ──
print("\n" + "="*70)
print("BACKTEST RESULTS")
print("="*70)

final_snapshot = daily_snapshots[-1]
initial_snapshot = daily_snapshots[0]

print(f"\nPeriod: {START_DATE} → {END_DATE} ({len(market_days)} market days)")
print(f"Starting: ${STARTING_CASH:,.2f}")
print(f"Ending:   ${final_snapshot['portfolio_value']:,.2f}")
pnl_total = final_snapshot['portfolio_value'] - STARTING_CASH
pnl_pct = (pnl_total / STARTING_CASH) * 100
print(f"P&L:      ${pnl_total:+,.2f} ({pnl_pct:+.2f}%)")

# Trade stats
buys = [t for t in trades if t["side"] == "BUY"]
sells = [t for t in trades if t["side"] in ("SELL", "TRIM")]
closed_trades = [t for t in trades if t["side"] in ("SELL", "TRIM") and t["pnl"] != 0]

print(f"\nTrade Activity:")
print(f"  Total buys:    {len(buys)}")
print(f"  Total sells:   {len(sells)}")
print(f"  Avg buy size:  ${np.mean([t['amount'] for t in buys]):,.2f}" if buys else "  Avg buy size:  N/A")

if closed_trades:
    pnls = [t["pnl"] for t in closed_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    print(f"\nClosed Trade P&L:")
    print(f"  Win rate:        {len(wins)}/{len(closed_trades)} ({len(wins)/len(closed_trades)*100:.1f}%)")
    print(f"  Avg win:         ${np.mean(wins):,.2f}" if wins else "  Avg win:         N/A")
    print(f"  Avg loss:        ${np.mean(losses):,.2f}" if losses else "  Avg loss:        N/A")
    print(f"  Total P&L:       ${sum(pnls):+,.2f}")
    print(f"  Best trade:      ${max(pnls):+,.2f}")
    print(f"  Worst trade:     ${min(pnls):+,.2f}")
    
    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    pf = gross_profit / gross_loss if gross_loss else float('inf')
    print(f"  Profit factor:   {pf:.2f}")

# Position stats
print(f"\nPosition Management:")
print(f"  Max positions held: {max(s['positions'] for s in daily_snapshots)}")
print(f"  Avg positions held: {np.mean([s['positions'] for s in daily_snapshots]):.1f}")
print(f"  Current positions:  {final_snapshot['positions']}")

# Equity curve (key dates)
print(f"\nEquity Curve (sampled):")
for snap in daily_snapshots[::3]:  # every 3rd day
    marker = "  "
    day_trades = sum(1 for t in trades if t["date"] == snap["date"])
    if day_trades:
        marker = "🔥"
    print(f"  {marker} {snap['date']}: ${snap['portfolio_value']:>12,.2f}  ({snap['positions']} pos, {snap['buys']}B/{snap['sells']}S)")

# Current holdings
if positions:
    print(f"\nCurrent Holdings:")
    for sym, pos in sorted(positions.items(), key=lambda x: x[1].get("score", 0), reverse=True):
        if sym in all_data and market_days[-1] in all_data[sym].index:
            cur_price = float(all_data[sym].loc[market_days[-1], "Close"])
            cur_val = pos["qty"] * cur_price
            cur_pl = cur_val - (pos["qty"] * pos["entry_price"])
        else:
            cur_val = pos["qty"] * pos["entry_price"]
            cur_pl = 0
        print(f"  {sym:6}  ${cur_val:>10,.2f}  P/L: ${cur_pl:+,.2f}")

# Compare with actual
print(f"\n" + "="*70)
print("COMPARISON")
print("="*70)
print(f"\n  Backtest ending value: ${final_snapshot['portfolio_value']:,.2f}")
print(f"  P&L:                   ${pnl_total:+,.2f} ({pnl_pct:+.2f}%)")
print(f"\n  → This is what the SAME $100K would have been worth")
print(f"    if current rules had been in place from the start.")
print(f"    (No actual account comparison — paper account had different rules)")

# Trade log
print(f"\n" + "="*70)
print(f"TRADE LOG ({len(trades)} trades)")
print("="*70)
for t in trades:
    side_emoji = "🟢" if t["side"] == "BUY" else "🔴"
    pnl_str = f"  P/L: ${t['pnl']:+,.2f}" if t["pnl"] != 0 else ""
    print(f"  {side_emoji} {t['date']} {t['side']:4} {t['symbol']:6} @{t['price']:.2f} x{t['qty']:.2f} = ${t['amount']:>10,.2f}{pnl_str}")
    print(f"      {t['reason']}")
