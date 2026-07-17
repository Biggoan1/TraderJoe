#!/usr/bin/env python3
"""
Backtest the 9 new symbols we just added against the six-gate strategy.
Rules: same as trader.py / backtest.py
- 6-gate buy (need 4/6): above SMA20, SMA20>SMA50, RSI<70, MACD hist>0, ADX>15, vol>80% avg
- Sell: RSI>70, price >= upper BB, or MACD death cross (hist goes negative)
- Cooldown: 1 day (daily data proxy for 30 min)
- Position cap: 20 hard, 15 target
- Buy size: $5K per position
- Slippage: 0.1% per trade
"""

import yfinance as yf
import pandas as pd
import numpy as np

# Configuration
START_DATE = "2026-01-01"
END_DATE = "2026-07-09"
STARTING_CASH = 100_000.0
BASE_BUY_AMOUNT = 5_000.0
MAX_POSITIONS_TARGET = 6
MAX_POSITIONS_HARD = 8
SLIPPAGE_PCT = 0.001
GATES_REQUIRED = 5
MAX_DAILY_BUY = 2
MIN_HOLD_DAYS = 3

SYMBOLS = ["PANW", "DDOG", "UNH", "DASH", "AXP", "AFRM", "RTX", "DE"]


def calc_indicators(df):
    """Compute all indicators for a single symbol."""
    c = df["Close"].copy()
    h = df["High"].copy()
    l = df["Low"].copy()
    v = df["Volume"].copy()

    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    bb_std = c.rolling(20).std()
    df["BB_UPPER"] = df["SMA20"] + 2 * bb_std

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
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
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
    """Return (gates_dict, gate_count, score) or (None, 0, 0)."""
    price = row["Close"]
    sma20 = row["SMA20"]
    sma50 = row["SMA50"]
    rsi = row["RSI"]
    adx = row["ADX"]
    macd_hist = row["MACD_HIST"]
    volume = row["Volume"]
    vol_avg = row["VOL_AVG20"]

    if any(pd.isna(x) for x in [sma20, sma50, rsi, adx, macd_hist]):
        return None, 0, 0

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

    return gates, gate_count, score


def check_sell(row, prev_macd_hist=0):
    """Check sell conditions. Returns list of reasons or empty."""
    reasons = []
    if pd.notna(row["RSI"]) and row["RSI"] > 70:
        reasons.append(f"RSI>70 ({row['RSI']:.1f})")
    if pd.notna(row["BB_UPPER"]) and row["Close"] >= row["BB_UPPER"]:
        reasons.append("Above upper BB")
    macd_hist = row["MACD_HIST"]
    if pd.notna(macd_hist) and macd_hist < 0 and macd_hist < prev_macd_hist:
        reasons.append("MACD death cross")
    return reasons


# Load all data
print("Loading historical data...")
all_data = {}
for sym in SYMBOLS:
    try:
        df = yf.download(
            sym, start=START_DATE, end=END_DATE,
            interval="1d", progress=False, auto_adjust=True
        )
        if df.empty:
            print(f"  WARNING {sym}: empty data")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = calc_indicators(df)
        all_data[sym] = df
    except Exception as e:
        print(f"  ERROR {sym}: {e}")

print(f"Loaded {len(all_data)} symbols\n")
for sym in SYMBOLS:
    if sym in all_data:
        df = all_data[sym]
        print(f"  {sym}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")

print()

# Simulate
cash = STARTING_CASH
positions = {}  # sym -> {qty, entry_price, entry_date}
cooldowns = {}  # sym -> date when cooldown ends
holdings_start_date = {}  # sym -> date when position was first opened
trades = []

symbol_stats = {
    s: {"buys": 0, "sells": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    for s in SYMBOLS
}

# Find common days across all loaded symbols
common_days = None
for sym in all_data:
    if common_days is None:
        common_days = set(all_data[sym].index)
    else:
        common_days = common_days.intersection(all_data[sym].index)

if common_days is None or len(common_days) == 0:
    print("ERROR: No common days found!")
    exit(1)

common_days = sorted(common_days)
print(f"Running backtest on {len(common_days)} common trading days...\n")

for day in common_days:
    day_str = day.date().isoformat()

    # Phase 1: Check sell signals
    sells_today = []
    for sym in list(positions.keys()):
        if sym not in all_data or day not in all_data[sym].index:
            continue
        row = all_data[sym].loc[day]
        idx = all_data[sym].index.get_loc(day)
        prev_macd = 0.0
        if idx > 0:
            prev_row = all_data[sym].iloc[idx - 1]
            if pd.notna(prev_row.get("MACD_HIST")):
                prev_macd = float(prev_row["MACD_HIST"])
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
        cooldowns[sym] = day + pd.Timedelta(days=1)

        symbol_stats[sym]["sells"] += 1
        symbol_stats[sym]["total_pnl"] += pnl
        if pnl > 0:
            symbol_stats[sym]["wins"] += 1
        else:
            symbol_stats[sym]["losses"] += 1

        trades.append({
            "date": day_str, "symbol": sym, "side": "SELL",
            "price": price, "amount": market_value, "pnl": pnl,
            "reason": ", ".join(reasons),
        })
        del positions[sym]

    # Phase 2: Scan for buys
    if len(positions) >= MAX_POSITIONS_HARD or cash < BASE_BUY_AMOUNT:
        continue

    candidates = []
    for sym in SYMBOLS:
        if sym in positions or sym not in all_data:
            continue
        if day not in all_data[sym].index:
            continue
        if sym in cooldowns and day <= cooldowns[sym]:
            continue

        row = all_data[sym].loc[day]
        gates, gate_count, score = evaluate_setup(row)
        if gates is None:
            continue
        if gate_count < GATES_REQUIRED:
            continue

        # Dynamic position sizing based on score
        if score >= 8.0:
            buy_amt = 15_000.0
        elif score >= 6.0:
            buy_amt = 10_000.0
        else:
            buy_amt = 5_000.0

        candidates.append((sym, score, gate_count, row, buy_amt))

    # Sort by score descending, buy top candidates
    candidates.sort(key=lambda x: x[1], reverse=True)
    daily_buy_count = 0
    for sym, score, gate_count, row, buy_amt in candidates:
        if daily_buy_count >= MAX_DAILY_BUY:
            break
        if len(positions) >= MAX_POSITIONS_HARD:
            break
        if cash < BASE_BUY_AMOUNT:
            break

        price = float(row["Close"])
        entry_price = price * (1 + SLIPPAGE_PCT)
        qty = buy_amt / entry_price
        actual_cost = qty * entry_price

        if actual_cost > cash:
            continue

        # Check minimum holding period
        if sym in holdings_start_date and day < holdings_start_date[sym] + pd.Timedelta(days=MIN_HOLD_DAYS):
            continue

        cash -= actual_cost
        positions[sym] = {
            "qty": qty,
            "entry_price": entry_price,
            "entry_date": day_str,
        }
        holdings_start_date[sym] = day
        cooldowns[sym] = day + pd.Timedelta(days=1)
        symbol_stats[sym]["buys"] += 1
        daily_buy_count += 1

        trades.append({
            "date": day_str, "symbol": sym, "side": "BUY",
            "price": entry_price, "qty": qty, "amount": actual_cost,
            "gate_count": gate_count, "score": round(score, 2),
        })

# Calculate final unrealized P/L
final_prices = {}
for sym in SYMBOLS:
    if sym in all_data:
        final_prices[sym] = float(all_data[sym].iloc[-1]["Close"])

unrealized_pnl = 0.0
for sym, pos in positions.items():
    fp = final_prices.get(sym, pos["entry_price"])
    pnl = pos["qty"] * (fp - pos["entry_price"])
    unrealized_pnl += pnl

total_value = cash + sum(
    pos["qty"] * final_prices.get(s, pos["entry_price"])
    for s, pos in positions.items()
)
total_pnl = total_value - STARTING_CASH
realized_pnl = sum(t["pnl"] for t in trades if t["side"] == "SELL")

# Print results
print("=" * 80)
print("BACKTEST RESULTS — Six-Gate Strategy (Jan 1, 2026 → Jul 9, 2026)")
print("=" * 80)
print(f"Starting Cash:     ${STARTING_CASH:,.2f}")
print(f"Final Cash:        ${cash:,.2f}")
print(f"Open Positions:    {len(positions)}")
print(f"Total Value:       ${total_value:,.2f}")
print(f"Realized P/L:      ${realized_pnl:,.2f}")
print(f"Unrealized P/L:    ${unrealized_pnl:,.2f}")
print(f"Total P/L:         ${total_pnl:,.2f}")
print(f"Return:            {total_pnl / STARTING_CASH * 100:.2f}%")
print()

# Per-symbol breakdown
print("-" * 80)
print("PER-SYMBOL BREAKDOWN")
print("-" * 80)
print(f"{'Symbol':<8} {'Buy #':>6} {'Sell #':>7} {'Wins':>5} {'Losses':>7} {'Total P/L':>12} {'Win Rate':>10}")
print("-" * 80)
for sym in SYMBOLS:
    ss = symbol_stats[sym]
    win_rate = ss["wins"] / max(ss["sells"], 1) * 100
    print(
        f"{sym:<8} {ss['buys']:>6} {ss['sells']:>7} {ss['wins']:>5} "
        f"{ss['losses']:>7} ${ss['total_pnl']:>10,.2f} {win_rate:>9.0f}%"
    )
print()

# Buy & Hold comparison
print("-" * 80)
print("BUY & HOLD COMPARISON")
print("-" * 80)
bh_returns = []
for sym in SYMBOLS:
    if sym in all_data and len(all_data[sym]) > 0:
        first_p = float(all_data[sym].iloc[0]["Close"])
        last_p = float(all_data[sym].iloc[-1]["Close"])
        ret = (last_p - first_p) / first_p
        bh_returns.append(ret)
bh_avg = sum(bh_returns) / len(bh_returns) if bh_returns else 0
print(f"Strategy Return:        {total_pnl / STARTING_CASH * 100:.2f}%")
print(f"Buy & Hold (avg per sym): {bh_avg * 100:.2f}%")
print(f"Outperformance:         {(total_pnl / STARTING_CASH - bh_avg) * 100:.2f}%")
print()

# Trade details (last 30)
print("-" * 80)
print("RECENT TRADES (last 30)")
print("-" * 80)
for t in trades[-30:]:
    side = t["side"]
    pnl_str = f"  P/L=${t['pnl']:>9,.2f}" if side == "SELL" else ""
    info = f"{t['date']}  {t['symbol']:<6} {side}  ${t['price']:>8.2f}"
    if side == "BUY":
        info += f"  gates={t.get('gate_count', '?')}  score={t.get('score', '?')}"
    print(f"{info}{pnl_str}")
print()

# Open positions
print("-" * 80)
print("OPEN POSITIONS")
print("-" * 80)
for sym, pos in positions.items():
    fp = final_prices.get(sym, pos["entry_price"])
    pnl = pos["qty"] * (fp - pos["entry_price"])
    ret = (fp - pos["entry_price"]) / pos["entry_price"] * 100
    print(
        f"  {sym:<8} entry=${pos['entry_price']:.2f}  "
        f"current=${fp:.2f}  qty={pos['qty']:.2f}  "
        f"P/L=${pnl:,.2f} ({ret:+.1f}%)"
    )
print()

# Key metrics
total_buys = sum(s["buys"] for s in symbol_stats.values())
total_sells = sum(s["sells"] for s in symbol_stats.values())
total_wins = sum(s["wins"] for s in symbol_stats.values())
total_losses = sum(s["losses"] for s in symbol_stats.values())

print("-" * 80)
print("KEY METRICS")
print("-" * 80)
print(f"Total Buy Signals:    {total_buys}")
print(f"Total Sells:          {total_sells}")
print(f"Total Wins:           {total_wins}")
print(f"Total Losses:         {total_losses}")
print(f"Overall Win Rate:     {total_wins / max(total_sells, 1) * 100:.1f}%")
print()

# Period breakdown
print("-" * 80)
print("BUY & HOLD PERIOD BREAKDOWN")
print("-" * 80)
periods = [
    ("Jan-Mar", "2026-01-01", "2026-03-31"),
    ("Apr-Jun", "2026-04-01", "2026-06-30"),
    ("Jul", "2026-07-01", "2026-07-09"),
]
for period_name, ps, pe in periods:
    period_returns = []
    for sym in SYMBOLS:
        if sym in all_data:
            df = all_data[sym]
            mask = (df.index >= pd.Timestamp(ps)) & (df.index <= pd.Timestamp(pe))
            if mask.sum() > 1:
                first_p = float(df[mask].iloc[0]["Close"])
                last_p = float(df[mask].iloc[-1]["Close"])
                period_returns.append((last_p - first_p) / first_p)
    avg_ret = sum(period_returns) / len(period_returns) if period_returns else 0
    print(f"  {period_name:<12} B&H avg: {avg_ret:+.2f}% ({len(period_returns)} symbols)")
print()

# Signal frequency analysis
print("-" * 80)
print("SIGNAL FREQUENCY (how often each symbol qualified)")
print("-" * 80)
for sym in SYMBOLS:
    if sym not in all_data:
        continue
    df = all_data[sym]
    qualified_days = 0
    for idx, row in df.iterrows():
        gates, gate_count, score = evaluate_setup(row)
        if gates is not None and gate_count >= GATES_REQUIRED:
            qualified_days += 1
    pct = qualified_days / len(df) * 100
    print(f"  {sym:<8} qualified {qualified_days:3d}/{len(df):3d} days ({pct:.0f}%)")
print()

# Top winning trades
print("-" * 80)
print("TOP 10 WINS")
print("-" * 80)
sells = sorted([t for t in trades if t["side"] == "SELL"], key=lambda x: x["pnl"], reverse=True)
for i, t in enumerate(sells[:10]):
    print(
        f"  {i+1}. {t['symbol']:<8} {t['date']}  P/L=${t['pnl']:>10,.2f}  ({t['reason']})"
    )
print()

# Top losing trades
print("-" * 80)
print("TOP 10 LOSSES")
print("-" * 80)
for i, t in enumerate(sells[-10:]):
    print(
        f"  {i+1}. {t['symbol']:<8} {t['date']}  P/L=${t['pnl']:>10,.2f}  ({t['reason']})"
    )
print()

print("=" * 80)
