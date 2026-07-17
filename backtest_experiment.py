#!/usr/bin/env python3
"""
Systematic strategy experiment runner.
Tests multiple configurations and reports which is closest to target.
Target: 1-2% daily return on $100K = $1,000-$2,000/day.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from itertools import product
from dataclasses import dataclass, field
from typing import List

SYMBOLS = ["PANW", "DDOG", "UNH", "DASH", "AXP", "AFRM", "RTX", "DE"]

@dataclass
class Config:
    name: str
    gates_required: int = 4
    max_positions: int = 8
    max_daily_buys: int = 3
    min_hold_days: int = 1
    score_threshold: float = 5.0  # minimum score to buy
    buy_base: float = 5000.0
    score_8_size: float = 15000.0
    score_6_size: float = 10000.0
    sell_rsi: float = 70.0
    sell_macd_cross: bool = True  # use MACD death cross
    sell_bb: bool = True  # use upper Bollinger
    slippage: float = 0.001

    # Dynamic: aggressive sell on bigger losses
    max_loss_pct: float = 5.0  # stop loss %
    take_profit_pct: float = 3.0  # take profit %

    # How to size based on score
    def buy_amount(self, score: float) -> float:
        if score >= 8.0:
            return self.score_8_size
        elif score >= 6.0:
            return self.score_6_size
        elif score >= 7.0:
            return self.score_8_size * 0.8  # intermediate
        return self.buy_base

CONFIGS: List[Config] = [
    # Baseline - current improved version
    Config("baseline_5gate", gates_required=5, max_positions=8, max_daily_buys=2,
           min_hold_days=3, score_threshold=5.0,
           score_8_size=15000, score_6_size=10000, buy_base=5000,
           sell_rsi=70, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=5, take_profit_pct=3),

    # Try 1: Aggressive scoring - score >= 7 only, bigger sizes
    Config("tight_7gate", gates_required=5, max_positions=6, max_daily_buys=2,
           min_hold_days=3, score_threshold=7.0,
           score_8_size=20000, score_6_size=15000, buy_base=10000,
           sell_rsi=70, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=3, take_profit_pct=4),

    # Try 2: Very aggressive - fewer positions, bigger size, tighter stops
    Config("tight_7p5gate", gates_required=5, max_positions=5, max_daily_buys=2,
           min_hold_days=3, score_threshold=7.5,
           score_8_size=25000, score_6_size=18000, buy_base=12000,
           sell_rsi=68, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=2.5, take_profit_pct=4),

    # Try 3: Mean reversion play - buy dips below SMA20, sell on RSI bounce
    Config("mean_rev", gates_required=6, max_positions=4, max_daily_buys=2,
           min_hold_days=5, score_threshold=7.0,
           score_8_size=25000, score_6_size=20000, buy_base=15000,
           sell_rsi=72, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=3, take_profit_pct=5),

    # Try 4: Trend follow - only buy when price well above SMA20
    Config("trend_follow", gates_required=5, max_positions=6, max_daily_buys=2,
           min_hold_days=3, score_threshold=6.5,
           score_8_size=20000, score_6_size=15000, buy_base=10000,
           sell_rsi=70, sell_macd_cross=True, sell_bb=False,  # no BB sell
           max_loss_pct=4, take_profit_pct=5),

    # Try 5: Ultra-tight scalping
    Config("ultra_tight", gates_required=5, max_positions=4, max_daily_buys=3,
           min_hold_days=1, score_threshold=7.0,
           score_8_size=25000, score_6_size=18000, buy_base=12000,
           sell_rsi=65, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=2, take_profit_pct=3),

    # Try 6: Conservative but large positions
    Config("conservative_large", gates_required=6, max_positions=4, max_daily_buys=1,
           min_hold_days=5, score_threshold=7.0,
           score_8_size=30000, score_6_size=25000, buy_base=20000,
           sell_rsi=70, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=4, take_profit_pct=5),

    # Try 7: Aggressive + RSI oversold buy (buy when RSI dips 40-55)
    Config("rsi_oscillation", gates_required=5, max_positions=5, max_daily_buys=2,
           min_hold_days=2, score_threshold=6.0,
           score_8_size=20000, score_6_size=15000, buy_base=10000,
           sell_rsi=70, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=3, take_profit_pct=4),

    # Try 8: Combine trend + volume breakout
    Config("volume_breakout", gates_required=5, max_positions=5, max_daily_buys=2,
           min_hold_days=3, score_threshold=6.5,
           score_8_size=22000, score_6_size=16000, buy_base=12000,
           sell_rsi=70, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=3.5, take_profit_pct=4.5),

    # Try 9: Max aggressive - highest sizes, tightest stops, most trades
    Config("max_aggressive", gates_required=5, max_positions=6, max_daily_buys=4,
           min_hold_days=1, score_threshold=6.0,
           score_8_size=30000, score_6_size=20000, buy_base=15000,
           sell_rsi=65, sell_macd_cross=True, sell_bb=True,
           max_loss_pct=2, take_profit_pct=3),
]


def calc_indicators(df):
    c = df["Close"].copy()
    h = df["High"].copy()
    l = df["Low"].copy()
    v = df["Volume"].copy()

    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    bb_std = c.rolling(20).std()
    df["BB_UPPER"] = df["SMA20"] + 2 * bb_std

    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

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

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    df["MACD"] = macd
    df["MACD_SIG"] = macd.ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = macd - df["MACD_SIG"]

    df["VOL_AVG20"] = v.rolling(20).mean()
    df["VOL_RATIO"] = v / df["VOL_AVG20"]

    return df


def evaluate_setup(row):
    price = row["Close"]
    sma20 = row["SMA20"]
    sma50 = row["SMA50"]
    rsi = row["RSI"]
    adx = row["ADX"]
    macd_hist = row["MACD_HIST"]
    volume = row["Volume"]
    vol_avg = row["VOL_AVG20"]

    if any(pd.isna(x) for x in [sma20, sma50, rsi, adx, macd_hist]):
        return None, 0, 0.0

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


def check_sell_conditions(row, config, prev_macd_hist=0):
    reasons = []
    rsi_val = row["RSI"]
    if pd.notna(rsi_val) and rsi_val > config.sell_rsi:
        reasons.append(f"RSI>{config.sell_rsi:.0f}")

    if config.sell_bb and pd.notna(row["BB_UPPER"]) and row["Close"] >= row["BB_UPPER"]:
        reasons.append("Above upper BB")

    if config.sell_macd_cross:
        mac_h = row["MACD_HIST"]
        if pd.notna(mac_h) and mac_h < 0 and mac_h < prev_macd_hist:
            reasons.append("MACD death cross")

    return reasons


def run_backtest(config, all_data, START_DATE="2026-01-01", END_DATE="2026-07-09"):
    cash = 100_000.0
    positions = {}
    cooldowns = {}
    holdings_start_date = {}
    trades = []

    # Build common days
    common_days = None
    for sym in all_data:
        if common_days is None:
            common_days = set(all_data[sym].index)
        else:
            common_days = common_days.intersection(all_data[sym].index)
    common_days = sorted(common_days)

    daily_buy_tracker = {}  # day -> count

    for day in common_days:
        day_str = day.date().isoformat()
        daily_buy_tracker[day] = 0

        # --- SELL PHASE ---
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

            reasons = check_sell_conditions(row, config, prev_macd)

            # Check stop loss / take profit
            pos = positions[sym]
            pnl_pct = (row["Close"] / pos["entry_price"] - 1) * 100
            if pnl_pct <= -config.max_loss_pct:
                reasons.append(f"Stop loss {pnl_pct:.1f}%")
            if pnl_pct >= config.take_profit_pct:
                reasons.append(f"Take profit {pnl_pct:.1f}%")

            if reasons:
                pos = positions[sym]
                market_value = pos["qty"] * row["Close"] * (1 - config.slippage)
                cost_basis = pos["qty"] * pos["entry_price"]
                pnl = market_value - cost_basis
                cash += market_value

                symbol_stats_local[sym]["sells"] += 1
                symbol_stats_local[sym]["total_pnl"] += pnl
                if pnl > 0:
                    symbol_stats_local[sym]["wins"] += 1
                else:
                    symbol_stats_local[sym]["losses"] += 1

                trades.append({"date": day_str, "symbol": sym, "side": "SELL",
                               "price": row["Close"], "pnl": pnl, "reason": ", ".join(reasons)})
                del positions[sym]

        # --- BUY PHASE ---
        if len(positions) >= config.max_positions or cash < config.buy_base:
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
            result = evaluate_setup(row)
            if result[0] is None:
                continue
            gates, gate_count, score = result
            if gate_count < config.gates_required:
                continue
            if score < config.score_threshold:
                continue

            # Additional: for trend_follow, require price well above SMA20
            if config.name == "trend_follow" and row["Close"] < row["SMA20"] * 1.02:
                continue

            # Additional: for mean_rev, require price near/below SMA20
            if config.name == "mean_rev" and row["Close"] > row["SMA20"] * 1.03:
                continue

            buy_amt = config.buy_amount(score)
            candidates.append((sym, score, gate_count, row, buy_amt))

        candidates.sort(key=lambda x: x[1], reverse=True)

        for sym, score, gate_count, row, buy_amt in candidates:
            if daily_buy_tracker[day] >= config.max_daily_buys:
                break
            if len(positions) >= config.max_positions:
                break
            if cash < config.buy_base:
                break

            price = float(row["Close"])
            entry_price = price * (1 + config.slippage)
            qty = buy_amt / entry_price
            actual_cost = qty * entry_price

            if actual_cost > cash:
                continue

            # Min hold period check
            if sym in holdings_start_date and day < holdings_start_date[sym] + pd.Timedelta(days=config.min_hold_days):
                continue

            cash -= actual_cost
            positions[sym] = {"qty": qty, "entry_price": entry_price, "entry_date": day_str}
            holdings_start_date[sym] = day
            cooldowns[sym] = day + pd.Timedelta(days=1)
            symbol_stats_local[sym]["buys"] += 1
            daily_buy_tracker[day] += 1

            trades.append({"date": day_str, "symbol": sym, "side": "BUY",
                           "price": entry_price, "qty": qty, "amount": actual_cost,
                           "score": round(score, 2), "size": round(buy_amt)})

    # Final unrealized P/L
    total_value = cash
    for sym, pos in positions.items():
        if sym in all_data and all_data[sym].index[-1] >= common_days[-1]:
            fp = float(all_data[sym].iloc[-1]["Close"])
        else:
            fp = pos["entry_price"]
        total_value += pos["qty"] * fp

    total_pnl = total_value - 100_000.0
    total_trades = len(trades)
    sell_trades = [t for t in trades if t["side"] == "SELL"]
    wins = sum(1 for t in sell_trades if t["pnl"] > 0)
    losses = len(sell_trades) - wins
    avg_daily_pnl = total_pnl / len(common_days)
    avg_daily_pct = avg_daily_pnl / 100_000 * 100

    return {
        "name": config.name,
        "total_pnl": total_pnl,
        "total_return": total_pnl / 100_000 * 100,
        "avg_daily_pnl": avg_daily_pnl,
        "avg_daily_pct": avg_daily_pct,
        "total_trades": total_trades,
        "total_buys": sum(1 for t in trades if t["side"] == "BUY"),
        "total_sells": len(sell_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(sell_trades) * 100 if sell_trades else 0,
        "final_value": total_value,
        "open_positions": len(positions),
        "max_daily_pnl": max((t["pnl"] for t in sell_trades), default=0),
        "max_daily_loss": min((t["pnl"] for t in sell_trades), default=0),
        "symbol_stats": {k: {**v, "total_pnl": round(v["total_pnl"], 2)}
                         for k, v in symbol_stats_local.items()},
        "avg_daily_pnl_str": f"${avg_daily_pnl:+,.0f}",
        "avg_daily_pct_str": f"{avg_daily_pct:+.2f}%",
    }


def main():
    print("=" * 80)
    print("STRATEGY EXPERIMENT RUNNER")
    print("Testing multiple configs on:", SYMBOLS)
    print("=" * 80)

    # Load data once
    print("\nLoading data...")
    all_data = {}
    for sym in SYMBOLS:
        try:
            df = yf.download(sym, start="2026-01-01", end="2026-07-09",
                             interval="1d", progress=False, auto_adjust=True)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = calc_indicators(df)
            all_data[sym] = df
        except Exception as e:
            print(f"  ERROR {sym}: {e}")

    print(f"Loaded {len(all_data)} symbols\n")

    results = []
    for i, config in enumerate(CONFIGS):
        print(f"[{i+1}/{len(CONFIGS)}] Running {config.name}...")
        global symbol_stats_local
        symbol_stats_local = {s: {"buys": 0, "sells": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
                              for s in SYMBOLS}
        result = run_backtest(config, all_data)
        results.append(result)
        print(f"  P/L: ${result['total_pnl']:>+12,.0f} ({result['total_return']:+.2f}%)  "
              f"| Avg/day: {result['avg_daily_pct_str']:>8}  "
              f"| Trades: {result['total_trades']}  "
              f"| Win: {result['win_rate']:.0f}%")

    # Sort by avg daily %
    results.sort(key=lambda r: r["avg_daily_pct"], reverse=True)

    print("\n" + "=" * 100)
    print("RESULTS RANKED BY AVERAGE DAILY RETURN")
    print("=" * 100)
    print(f"{'Rank':>4} {'Strategy':<25} {'Total P/L':>14} {'Return':>8} {'Avg $/day':>12} {'Avg %/day':>10} "
          f"{'Trades':>6} {'Wins':>5} {'Losses':>6} {'Win%':>6}")
    print("-" * 100)
    for rank, r in enumerate(results, 1):
        marker = "  >>> TARGET ZONE <<<" if 1000 <= r["avg_daily_pnl"] <= 2000 else ""
        print(f"{rank:>4} {r['name']:<25} ${r['total_pnl']:>+12,.0f} "
              f"{r['total_return']:>+7.2f}% {r['avg_daily_pnl_str']:>12} {r['avg_daily_pct_str']:>10} "
              f"{r['total_trades']:>6} {r['wins']:>5} {r['losses']:>6} {r['win_rate']:>5.0f}% {marker}")

    # Top 3 detailed
    print("\n" + "=" * 100)
    print("DETAILED ANALYSIS - TOP 3")
    print("=" * 100)
    for rank in range(min(3, len(results))):
        r = results[rank]
        print(f"\n{'#' * 4} {r['name'].upper()} {'#' * 4}")
        print(f"  Total P/L:          ${r['total_pnl']:>+12,.0f} ({r['total_return']:+.2f}%)")
        print(f"  Avg Daily P/L:      {r['avg_daily_pnl_str']:>12}")
        print(f"  Avg Daily %:        {r['avg_daily_pct_str']:>12}")
        print(f"  Total Trades:       {r['total_trades']} ({r['total_buys']} buys, {r['total_sells']} sells)")
        print(f"  Win/Loss:           {r['wins']} / {r['losses']} ({r['win_rate']:.0f}% win rate)")
        print(f"  Open Positions:     {r['open_positions']}")
        print(f"  Biggest Win:        ${r['max_daily_pnl']:>+10,.0f}")
        print(f"  Biggest Loss:       ${r['max_daily_loss']:>10,.0f}")
        print(f"\n  Per-Symbol Breakdown:")
        print(f"  {'Symbol':<8} {'Buys':>5} {'Sells':>6} {'W':>3} {'L':>3} {'Win%':>5} {'Total P/L':>12}")
        print(f"  {'-'*8} {'-'*5} {'-'*6} {'-'*3} {'-'*3} {'-'*5} {'-'*12}")
        ss = r['symbol_stats']
        for sym in sorted(ss, key=lambda s: ss[s]["total_pnl"], reverse=True):
            st = ss[sym]
            wr = st["wins"] / (st["wins"] + st["losses"]) * 100 if (st["wins"] + st["losses"]) > 0 else 0
            print(f"  {sym:<8} {st['buys']:>5} {st['sells']:>6} {st['wins']:>3} {st['losses']:>3} "
                  f"{wr:>4.0f}% ${st['total_pnl']:>+10,.0f}")

    print("\n" + "=" * 100)
    print("TARGET ANALYSIS")
    print("=" * 100)
    print(f"Target: $1,000-$2,000/day (1-2% on $100K)")
    for rank, r in enumerate(results, 1):
        in_target = 1000 <= r["avg_daily_pnl"] <= 2000
        status = "✅ IN TARGET" if in_target else f"❌ {r['avg_daily_pct_str']} vs target 1-2%/day"
        print(f"  #{rank} {r['name']:<25} -> {status}")

    if any(1000 <= r["avg_daily_pnl"] <= 2000 for r in results):
        best = results[0]
        print(f"\n🎯 BEST: {best['name']} -> {best['avg_daily_pct_str']} avg/day")
    else:
        best = results[0]
        print(f"\n⚠️ No strategy hits 1-2%/day target.")
        print(f"Closest is {best['name']} at {best['avg_daily_pct_str']} avg/day")
        print(f"Gap to target: {1000 - best['avg_daily_pnl']:.0f} $/day")
        print(f"\nTo reach 1-2%/day, you'd need: leverage, options, or higher-volatility instruments.")


if __name__ == "__main__":
    main()
