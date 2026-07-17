#!/usr/bin/env python3
"""
Data Analyzer: Scan last week's market data to find patterns that produce 1-2% daily returns.

Steps:
1. Pull daily OHLCV data for watchlist + popular symbols via yfinance
2. Compute indicators (RSI, MACD, BB, ADX, SMA20/50, volume)
3. Identify which symbols had 1-2% up days and what conditions preceded them
4. Identify which symbols had 1-2% down days and what conditions preceded them
5. Build a "setup profile" — what indicator values correlate with 1-2% moves
6. Output findings for strategy building

Usage:
    python strategy/data_analyzer.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuration ──
LAST_WEEK_START = "2026-07-02"
LAST_WEEK_END   = "2026-07-09"
ANALYSIS_WINDOW = "2026-06-01"  # lookback for indicator warm-up

# Broader symbol pool to scan
SYMBOL_POOL = [
    # Core ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector ETFs
    "XLF", "XLK", "XLE", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU", "SMH",
    # Momentum
    "ARKK", "SOXX", "IBB",
    # Large Cap Tech
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "TSLA", "AMZN", "INTC",
    "MRVL", "GOOGL", "GOOG", "MU", "META", "NFLX", "CRM", "ORCL",
    # Mid/Small Cap
    "PANW", "DDOG", "SNOW", "PLTR", "COIN", "MARA", "RIOT",
    # Healthcare
    "UNH", "LLY", "JNJ", "PFE", "MRNA",
    # Financials
    "JPM", "GS", "MS", "AXP", "V", "MA", "SQ",
    # Consumer/Retail
    "WMT", "COST", "TGT", "HD", "LOW",
    # Industrial
    "DE", "CAT", "GE", "RTX",
    # Energy
    "XOM", "CVX", "COP",
    # Crypto-related
    "BTC-USD", "ETH-USD",
]

def calc_indicators(df):
    """Compute all technical indicators for analysis."""
    c = df["Close"].copy()
    h = df["High"].copy()
    l = df["Low"].copy()
    v = df["Volume"].copy()

    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    bb_std = c.rolling(20).std()
    df["BB_UPPER"] = df["SMA20"] + 2 * bb_std
    df["BB_LOWER"] = df["SMA20"] - 2 * bb_std
    df["BB_WIDTH"] = (df["BB_UPPER"] - df["BB_LOWER"]) / df["SMA20"]

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

    # ADX
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
    df["VOL_RATIO"] = v / df["VOL_AVG20"]

    # Daily returns
    df["DAILY_RET"] = c.pct_change()
    df["HIGH_LOW_RANGE"] = (h - l) / c.shift(1)
    df["OPEN_CLOSE_RET"] = (c - df["Open"]) / df["Open"]

    return df


def find_big_moves(df, threshold_pct=1.0):
    """Find days where daily return >= threshold_pct."""
    if "DAILY_RET" not in df.columns:
        calc_indicators(df)
    up_days = df[df["DAILY_RET"] >= threshold_pct / 100.0]
    down_days = df[df["DAILY_RET"] <= -threshold_pct / 100.0]
    big_up = df[(df["DAILY_RET"] >= 1.0) & (df["DAILY_RET"] <= 2.0)]
    big_down = df[(df["DAILY_RET"] >= -2.0) & (df["DAILY_RET"] <= -1.0)]
    return up_days, down_days, big_up, big_down


def analyze_setup_before_move(df, day, lookback=3):
    """Analyze indicators on the day before and on the move day."""
    idx = df.index.get_loc(day)
    if idx < 1:
        return None

    move_row = df.iloc[idx]
    setup_rows = []
    for i in range(max(0, idx - lookback), idx):
        r = df.iloc[i]
        if all(pd.notna(x) for x in [r.get("RSI"), r.get("SMA20"), r.get("SMA50"),
                                       r.get("ADX"), r.get("MACD_HIST")]):
            setup_rows.append(r)

    return {
        "move_date": str(day.date()),
        "move_return": float(move_row["DAILY_RET"]),
        "open": float(move_row["Open"]),
        "close": float(move_row["Close"]),
        "high": float(move_row["High"]),
        "low": float(move_row["Low"]),
        "volume": float(move_row["Volume"]) if pd.notna(move_row["Volume"]) else 0,
        "rsi_on_move_day": float(move_row["RSI"]) if pd.notna(move_row["RSI"]) else None,
        "adx_on_move_day": float(move_row["ADX"]) if pd.notna(move_row["ADX"]) else None,
        "macd_hist_on_move_day": float(move_row["MACD_HIST"]) if pd.notna(move_row["MACD_HIST"]) else None,
        "price_vs_sma20": float(move_row["Close"] / move_row["SMA20"]) if pd.notna(move_row["SMA20"]) else None,
        "price_vs_sma50": float(move_row["Close"] / move_row["SMA50"]) if pd.notna(move_row["SMA50"]) else None,
        "above_bb": bool(move_row["Close"] > move_row["BB_UPPER"]) if pd.notna(move_row["BB_UPPER"]) else None,
        "vol_ratio": float(move_row["VOL_RATIO"]) if pd.notna(move_row["VOL_RATIO"]) else None,
        "bb_width": float(move_row["BB_WIDTH"]) if pd.notna(move_row["BB_WIDTH"]) else None,
        "setup_history": [{
            "date": str(r.name.date()),
            "rsi": float(r["RSI"]) if pd.notna(r["RSI"]) else None,
            "sma20_above_sma50": bool(r["SMA20"] > r["SMA50"]) if pd.notna(r["SMA20"]) and pd.notna(r["SMA50"]) else None,
            "macd_pos": bool(r["MACD_HIST"] > 0) if pd.notna(r["MACD_HIST"]) else None,
            "adx": float(r["ADX"]) if pd.notna(r["ADX"]) else None,
            "vol_ratio": float(r["VOL_RATIO"]) if pd.notna(r["VOL_RATIO"]) else None,
            "price_above_sma20": bool(r["Close"] > r["SMA20"]) if pd.notna(r["SMA20"]) else None,
        } for r in setup_rows],
    }


def main():
    print("=" * 70)
    print("DATA ANALYZER: Last Week Scan (July 2-9, 2026)")
    print("=" * 70)
    print()

    # ── 1. Load data for all symbols ──
    print("Loading data for all symbols...")
    all_data = {}
    errors = []
    for sym in SYMBOL_POOL:
        try:
            df = yf.download(sym, start=ANALYSIS_WINDOW, end=LAST_WEEK_END,
                             interval="1d", progress=False, auto_adjust=True)
            if df.empty:
                errors.append(sym)
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = calc_indicators(df)
            all_data[sym] = df
        except Exception as e:
            errors.append(f"{sym}: {e}")

    print(f"Loaded {len(all_data)} symbols ({len(errors)} failed)")
    if errors:
        print(f"  Failed: {', '.join(errors[:10])}")

    # ── 2. Filter to last week trading days ──
    week_mask = (all_data[list(all_data.keys())[0]].index >= pd.Timestamp(LAST_WEEK_START)) & \
                (all_data[list(all_data.keys())[0]].index <= pd.Timestamp(LAST_WEEK_END))

    week_days = sorted(all_data[list(all_data.keys())[0]].index[week_mask])
    print(f"\nLast week trading days ({len(week_days)}): {[d.date().isoformat() for d in week_days]}")

    # ── 3. Scan for 1-2% daily moves ──
    print("\n" + "=" * 70)
    print("SCANNING FOR 1-2% DAILY MOVES")
    print("=" * 70)

    big_moves = []
    for sym, df in all_data.items():
        for day in week_days:
            if day not in df.index:
                continue
            ret = df.loc[day, "DAILY_RET"]
            pct = ret * 100
            if abs(pct) >= 1.0 and abs(pct) <= 3.0:  # 1-3% range for broader scan
                analysis = analyze_setup_before_move(df, day)
                if analysis:
                    analysis["symbol"] = sym
                    analysis["type"] = "UP" if pct > 0 else "DOWN"
                    big_moves.append(analysis)

    # Sort by magnitude
    big_moves.sort(key=lambda x: abs(x["move_return"]), reverse=True)

    print(f"\nFound {len(big_moves)} days with 1-3% moves in last week:")
    for m in big_moves[:30]:
        sign = "+" if m["move_return"] > 0 else ""
        rsi_val = f"{m['rsi_on_move_day']:.1f}" if m["rsi_on_move_day"] else "N/A"
        adx_val = f"{m['adx_on_move_day']:.1f}" if m["adx_on_move_day"] else "N/A"
        vol_val = f"{m['vol_ratio']:.1f}x" if m["vol_ratio"] else "N/A"
        print(f"  {m['symbol']:6s}  {m['move_date']}  {sign}{m['move_return']*100:6.2f}%  "
              f"RSI={rsi_val:>5s}  ADX={adx_val:>5s}  Vol={vol_val}")

    # ── 4. Build "setup profile" for big up days ──
    print("\n" + "=" * 70)
    print("SETUP PROFILE ANALYSIS: What precedes 1-2% UP days?")
    print("=" * 70)

    up_days = [m for m in big_moves if m["type"] == "UP" and 1.0 <= m["move_return"] * 100 <= 2.0]
    down_days = [m for m in big_moves if m["type"] == "DOWN" and -2.0 <= m["move_return"] * 100 <= -1.0]

    if up_days:
        avg_rsi = np.nanmean([m["rsi_on_move_day"] for m in up_days if m["rsi_on_move_day"]])
        avg_adx = np.nanmean([m["adx_on_move_day"] for m in up_days if m["adx_on_move_day"]])
        avg_vol = np.nanmean([m["vol_ratio"] for m in up_days if m["vol_ratio"]])
        avg_price_vs_sma20 = np.nanmean([m["price_vs_sma20"] for m in up_days if m["price_vs_sma20"]])
        bb_above = sum(1 for m in up_days if m["above_bb"] == True) / len(up_days)
        macd_pos = sum(1 for m in up_days if m["macd_hist_on_move_day"] and m["macd_hist_on_move_day"] > 0) / len(up_days)

        print(f"\n  Average RSI on 1-2% UP days:     {avg_rsi:.1f}" if avg_rsi else "  Average RSI: N/A")
        print(f"  Average ADX:                     {avg_adx:.1f}" if avg_adx else "  Average ADX: N/A")
        print(f"  Average Volume ratio:            {avg_vol:.2f}x" if avg_vol else "  Average Volume: N/A")
        print(f"  Average Price vs SMA20:          {avg_price_vs_sma20:.3f}" if avg_price_vs_sma20 else "  Price vs SMA20: N/A")
        print(f"  % Above Upper BB:                {bb_above*100:.0f}%")
        print(f"  % MACD positive:                 {macd_pos*100:.0f}%")
    else:
        print("\n  No 1-2% UP days found in last week.")

    if down_days:
        avg_rsi_down = np.nanmean([m["rsi_on_move_day"] for m in down_days if m["rsi_on_move_day"]])
        avg_adx_down = np.nanmean([m["adx_on_move_day"] for m in down_days if m["adx_on_move_day"]])
        print(f"\n  Average RSI on 1-2% DOWN days:   {avg_rsi_down:.1f}" if avg_rsi_down else "  Average RSI: N/A")
        print(f"  Average ADX:                     {avg_adx_down:.1f}" if avg_adx_down else "  Average ADX: N/A")
    else:
        print("\n  No 1-2% DOWN days found in last week.")

    # ── 5. Check which symbols had the most opportunity ──
    print("\n" + "=" * 70)
    print("SYMBOL VOLATILITY RANKING (last week)")
    print("=" * 70)

    sym_vol = []
    for sym, df in all_data.items():
        week_data = df.loc[df.index >= pd.Timestamp(LAST_WEEK_START)]
        week_data = week_data.loc[week_data.index <= pd.Timestamp(LAST_WEEK_END)]
        if len(week_data) < 3:
            continue
        avg_daily_ret = week_data["DAILY_RET"].mean()
        max_daily_ret = week_data["DAILY_RET"].max()
        min_daily_ret = week_data["DAILY_RET"].min()
        avg_abs_ret = week_data["DAILY_RET"].abs().mean()
        sym_vol.append({
            "symbol": sym,
            "avg_daily_ret_pct": avg_daily_ret * 100,
            "max_daily_ret_pct": max_daily_ret * 100,
            "min_daily_ret_pct": min_daily_ret * 100,
            "avg_abs_daily_ret_pct": avg_abs_ret * 100,
            "days_traded": len(week_data),
        })

    sym_vol.sort(key=lambda x: x["avg_abs_daily_ret_pct"], reverse=True)

    print(f"\n  {'Symbol':<8s} {'Avg%':>6s} {'Max%':>6s} {'Min%':>6s} {'Avg|Ret|':>7s} {'Days':>5s}")
    print(f"  {'------':<8s} {'----':>6s} {'----':>6s} {'----':>6s} {'--------':>7s} {'----':>5s}")
    for s in sym_vol[:25]:
        print(f"  {s['symbol']:<8s} {s['avg_daily_ret_pct']:+6.2f} {s['max_daily_ret_pct']:+6.2f} "
              f"{s['min_daily_ret_pct']:+6.2f} {s['avg_abs_daily_ret_pct']:6.2f}% {s['days_traded']:5d}")

    # ── 6. Pattern analysis: what setups on the day BEFORE a big up day ──
    print("\n" + "=" * 70)
    print("PRE-MOVE PATTERN ANALYSIS")
    print("=" * 70)

    if up_days:
        print("\n  Common patterns on the day BEFORE a 1-2% UP move:")

        # Check setup history for commonalities
        sma20_above = sum(1 for m in up_days if m["setup_history"] and
                         m["setup_history"][-1] and m["setup_history"][-1].get("sma20_above_sma50"))
        total_with_setup = sum(1 for m in up_days if m["setup_history"])
        if total_with_setup:
            print(f"    SMA20 > SMA50 on prior day:     {sma20_above}/{total_with_setup} ({sma20_above/total_with_setup*100:.0f}%)")

        macd_pos_prior = sum(1 for m in up_days if m["setup_history"] and
                            m["setup_history"][-1] and m["setup_history"][-1].get("macd_pos"))
        if total_with_setup:
            print(f"    MACD positive on prior day:      {macd_pos_prior}/{total_with_setup} ({macd_pos_prior/total_with_setup*100:.0f}%)")

        price_above = sum(1 for m in up_days if m["setup_history"] and
                         m["setup_history"][-1] and m["setup_history"][-1].get("price_above_sma20"))
        if total_with_setup:
            print(f"    Price above SMA20 on prior day:  {price_above}/{total_with_setup} ({price_above/total_with_setup*100:.0f}%)")

        high_vol = sum(1 for m in up_days if m["setup_history"] and
                      m["setup_history"][-1] and m["setup_history"][-1].get("vol_ratio", 0) > 1.0)
        if total_with_setup:
            print(f"    Above avg volume on prior day:   {high_vol}/{total_with_setup} ({high_vol/total_with_setup*100:.0f}%)")

        avg_rsi_prior = np.nanmean([m["setup_history"][-1]["rsi"] for m in up_days
                                    if m["setup_history"] and m["setup_history"][-1] and
                                    m["setup_history"][-1].get("rsi")])
        if avg_rsi_prior:
            print(f"    Avg RSI on prior day:            {avg_rsi_prior:.1f}")

    # ── 7. Save findings ──
    output = {
        "scan_period": f"{LAST_WEEK_START} to {LAST_WEEK_END}",
        "total_symbols": len(all_data),
        "total_big_moves": len(big_moves),
        "big_up_days": len(up_days),
        "big_down_days": len(down_days),
        "top_symbols_by_volatility": sym_vol[:10],
        "big_moves_detail": big_moves[:30],
    }

    out_path = Path("reports/data_analysis_last_week.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nFull analysis saved to {out_path}")

    # ── 8. Print key findings for strategy building ──
    print("\n" + "=" * 70)
    print("KEY FINDINGS FOR STRATEGY BUILDING")
    print("=" * 70)
    if up_days:
        print(f"\n  {len(up_days)} symbols had 1-2% UP days last week")
        print(f"  Top movers:")
        for m in up_days[:5]:
            sign = "+" if m["move_return"] > 0 else ""
            print(f"    {m['symbol']:6s}: {sign}{m['move_return']*100:.2f}% ({m['move_date']})")

        # Check for patterns in top movers
        top_movers = up_days[:5]
        print(f"\n  Common traits of top movers:")
        for m in top_movers:
            setup_info = ""
            if m["setup_history"]:
                sh = m["setup_history"][-1]
                setup_info = (f"  RSI={sh.get('rsi','?'):.0f}, "
                             f"SMA20>SMA50={sh.get('sma20_above_sma50')}, "
                             f"MACD+={sh.get('macd_pos')}, "
                             f"Price>SMA20={sh.get('price_above_sma20')}")
            print(f"    {m['symbol']}: {m['move_return']*100:.1f}% {setup_info}")
    else:
        print("\n  No 1-2% UP days found — market was flat last week.")
        print("  Will use broader lookback window for next iteration.")

    # Print daily market returns for context
    print("\n" + "=" * 70)
    print("MARKET CONTEXT: Benchmark Returns Last Week")
    print("=" * 70)
    for bench in ["SPY", "QQQ", "IWM"]:
        if bench in all_data:
            df = all_data[bench]
            week_data = df[week_mask]
            print(f"\n  {bench}:")
            for day in week_data.index:
                ret = week_data.loc[day, "DAILY_RET"] * 100
                print(f"    {day.date().isoformat()}: {ret:+.2f}%")


if __name__ == "__main__":
    main()
