#!/usr/bin/env python3
"""
Market Screener — scans a broad universe of tradeable symbols for momentum/trend
setups and maintains a separate trending watchlist.

Runs before market open to surface new opportunities outside the user's manual
watchlist. Uses the same 4-of-6 gate evaluation as trader.py.

Output: trending_watchlist.json  (top candidates with scores)
        screener_log.txt         (human-readable summary)
"""

import os
import json
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from strategy.relative_strength import RelativeStrengthCalculator

ET = ZoneInfo("America/New_York")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db")
TRENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trending_watchlist.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener_log.txt")
COOLDOWN_MINUTES = 30

# Broad universe: top stocks by market cap + liquid names + sector ETFs
# We screen this every cycle and keep the best scoring ones
MARKET_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "AMZN", "TSLA", "AMD",
    "AVGO", "ORCL", "CRM", "ADBE", "NFLX", "INTC", "QCOM", "TXN", "AMAT",
    "MU", "MRVL", "ADI", "LRCX", "KLAC", "SNPS", "CDNS", "NXPI", "MRVL",
    "CRDO", "AMBA", "ALAB", "ONTO", "COHR", "LITE",
    # Big banks / financials
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW", "USB", "PNC",
    "V", "MA", "AXP", "PYPL", "COIN", "SOFI", "AFRM",
    # Healthcare / pharma
    "LLY", "JNJ", "UNH", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
    "BMY", "AMGN", "ISRG", "VRTX", "REGN", "BIIB", "ZTS", "HCA",
    # Consumer / retail
    "WMT", "COST", "HD", "NKE", "MCD", "SBUX", "TGT", "LOW", "TJX",
    "EL", "DG", "ROST", "LULU", "GPS", "ANF",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "DVN", "MPC", "VLO",
    # Industrials
    "CAT", "DE", "HON", "UPS", "BA", "GE", "LMT", "RTX", "UNP",
    "MMM", "ITW", "EMR", "APH", "ETN",
    # Communication / media
    "DIS", "CMCSA", "NFLX", "PARA", "WMG", "TMUS", "CHTR", "T", "VZ",
    # Crypto proxy
    "MSTR", "COIN", "MARA", "RIOT", "CLSK",
    # Semiconductor ETFs (already in watchlist but worth screening)
    "SMH", "SOXX", "XLK",
    # Broad ETFs
    "SPY", "QQQ", "IWM", "DIA", "VGT", "XLF", "XLE", "XLI", "XLP",
    "XLV", "XLU", "XLB", "XLY", "ARKK", "TQQQ", "SQQQ", "SPXL",
    # Emerging momentum
    "PLTR", "SNOW", "DDOG", "NET", "CRWD", "ZS", "MDB", "MDB",
    "PANW", "SNOW", "ESTC", "MDB", "S", "NET", "DDOG", "BILL",
    "SHOP", "SQ", "UBER", "LYFT", "ABNB", "DASH", "RBLX", "U",
    # Materials / gold
    "NEM", "GOLD", "FCX", "SCCO", "TECK", "AA",
    "GLD", "SLV", "IAU",
    # Bonds (low priority but included)
    "TLT", "IEF", "LQD", "HYG", "VNQ",
]

# Filter: exclude symbols already on the user's manual watchlist from
# trending output (we still evaluate them for scoring consistency)
def get_manual_watchlist():
    """Load the user's manual watchlist from the DB."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.execute(
            "SELECT symbol FROM watchlist ORDER BY symbol"
        ).fetchall()
        conn.close()
        return {row[0] for row in cur}
    except Exception:
        return set()


def evaluate_setup(symbol):
    """Run the 4-of-6 gate evaluation on a single symbol.
    
    Same logic as trader.py evaluate_etf_setup() — returns dict with
    all indicators, gates, score, and qualified flag.
    """
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="6mo", interval="1d", auto_adjust=True)
    except Exception:
        return None

    if data.empty or len(data) < 55:
        return None

    # Flatten MultiIndex columns
    if getattr(data.columns, "nlevels", 1) > 1:
        data.columns = data.columns.get_level_values(0)

    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(data.columns)):
        return None

    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    # Technicals
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    bb_std = close.rolling(20).std()
    bb_lower = sma20 - (2 * bb_std)
    bb_upper = sma20 + (2 * bb_std)

    # RSI (14-day)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))

    # ADX (14-period)
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
    adx = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal

    # Volume
    volume_avg20 = volume.rolling(20).mean()

    # Get last values
    last_row = 0
    values = [
        float(close.iloc[-1]),
        float(sma20.iloc[-1]),
        float(sma50.iloc[-1]),
        float(bb_lower.iloc[-1]),
        float(bb_upper.iloc[-1]),
        float(rsi.iloc[-1]),
        float(adx.iloc[-1]),
        float(macd_hist.iloc[-1]),
        float(macd_hist.iloc[-2]),
        float(volume.iloc[-1]),
        float(volume_avg20.iloc[-1]),
    ]

    if any(math.isnan(v) for v in values):
        return None

    price, sma20_v, sma50_v, bb_lower_v, bb_upper_v = values[:5]
    rsi_v, adx_v = values[5], values[6]
    macd_hist_v, macd_hist_prev = values[7], values[8]
    volume_v, volume_avg_v = values[9], values[10]

    # 4-of-6 gates
    gates = {
        "above_sma20": price > sma20_v,
        "sma20_above_sma50": sma20_v > sma50_v,
        "rsi_not_overbought": rsi_v < 70,
        "macd_positive": macd_hist_v > 0,
        "adx_strength": adx_v > 15,
        "volume_confirmation": volume_v > volume_avg_v * 0.8,
    }

    gate_count = sum(1 for v in gates.values() if v)

    # Score
    score = 0.0
    score += 1.5 if gates["above_sma20"] else 0.0
    score += 1.5 if gates["sma20_above_sma50"] else 0.0
    score += 1.0 if gates["rsi_not_overbought"] else 0.0
    score += 1.5 if gates["macd_positive"] else 0.0
    score += 1.0 if gates["adx_strength"] else 0.0
    score += 0.5 if gates["volume_confirmation"] else 0.0
    score += min(max(adx_v - 15.0, 0.0) / 10.0, 1.5)
    score += min(max((volume_v / volume_avg_v) - 0.8, 0.0), 1.0)

    # Price change over last 5 days (momentum indicator)
    if len(close) >= 7:
        price_5d_ago = float(close.iloc[-7])
        pct_change_5d = ((price - price_5d_ago) / price_5d_ago) * 100
    else:
        pct_change_5d = 0.0

    # Avg daily volume ($ value)
    avg_daily_vol = volume_avg_v * price

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "sma20": round(sma20_v, 2),
        "sma50": round(sma50_v, 2),
        "rsi": round(rsi_v, 1),
        "adx": round(adx_v, 1),
        "macd_hist": round(macd_hist_v, 4),
        "bb_lower": round(bb_lower_v, 2),
        "bb_upper": round(bb_upper_v, 2),
        "volume": int(volume_v),
        "volume_avg20": int(volume_avg_v),
        "avg_daily_vol_usd": round(avg_daily_vol, 0),
        "pct_change_5d": round(pct_change_5d, 2),
        "gates": gates,
        "gate_count": gate_count,
        "score": round(score, 2),
        "qualified": gate_count >= 4,
    }


def get_existing_cooldowns():
    """Symbols currently on cooldown (recently sold)."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
        rows = conn.execute(
            "SELECT symbol FROM trade_cooldowns WHERE cooldown_until > ?",
            (cutoff,)
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def current_positions():
    """Current position symbols."""
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM pending_approvals WHERE side='BUY' AND status IN ('APPROVED','FILLED') ORDER BY symbol"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def run_screener():
    """Run the full market screen and output results."""
    now = datetime.now(ET)
    timestamp = now.isoformat()

    manual_watchlist = get_manual_watchlist()
    cooldowns = get_existing_cooldowns()
    positions = current_positions()

    # Deduplicate universe
    universe = list(dict.fromkeys(MARKET_UNIVERSE))
    
    results = []
    failed = []

    print(f"=== Market Screener: {timestamp} ===")
    print(f"Universe: {len(universe)} symbols")

    for symbol in universe:
        try:
            result = evaluate_setup(symbol)
            if result is None:
                failed.append(symbol)
                continue
            
            # Tag whether it's new (not on manual watchlist)
            result["on_manual_watchlist"] = symbol in manual_watchlist
            result["owned"] = symbol in positions
            result["on_cooldown"] = symbol in cooldowns
            results.append(result)
        except Exception as e:
            failed.append(f"{symbol} ({e})")
    
    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    # Qualified setups (4+ gates)
    qualified = [r for r in results if r["qualified"]]
    
    # Top trending (not on manual watchlist, qualified)
    trending = [r for r in qualified if not r["on_manual_watchlist"] and not r["owned"] and not r["on_cooldown"]]
    
    # Relative Strength analysis (observational only — does NOT influence scoring)
    # Calculate RS for qualified and trending symbols
    rs_symbols = [r["symbol"] for r in qualified] or [r["symbol"] for r in results[:20]]
    rs_results = {}
    if rs_symbols:
        try:
            calc = RelativeStrengthCalculator()
            for rs_result in calc.calculate_watchlist_rs(rs_symbols):
                rs_results[rs_result.symbol] = rs_result.to_dict()
            print(f"RS analysis complete for {len(rs_results)} symbols (observational only)")
        except Exception as e:
            print(f"RS analysis skipped: {e}")

    # Add RS data to each result (observational only)
    for r in results:
        sym = r["symbol"]
        if sym in rs_results:
            r["rs_data"] = rs_results[sym]
        else:
            r["rs_data"] = None
    
    # Save trending watchlist
    trending_data = {
        "timestamp": timestamp,
        "total_scanned": len(results),
        "qualified_count": len(qualified),
        "trending_count": len(trending),
        "top_qualified": qualified[:20],
        "trending": trending[:15],
        "failed_symbols": failed[:20],
    }

    with open(TRENDING_FILE, "w") as f:
        json.dump(trending_data, f, indent=2)

    # Write human-readable log
    with open(LOG_FILE, "w") as f:
        f.write(f"Market Screener — {timestamp}\n")
        f.write(f"{'='*60}\n\n")
        f.write(f"Scanned: {len(results)}/{len(universe)} symbols\n")
        f.write(f"Qualified (4+ gates): {len(qualified)}\n")
        f.write(f"New trending candidates: {len(trending)}\n\n")

        if trending:
            f.write("TOP TRENDING (new opportunities):\n")
            f.write(f"{'-'*60}\n")
            for r in trending[:10]:
                gate_str = "+".join(k[:2] for k, v in r["gates"].items() if v)
                rs_line = ""
                if r.get("rs_data"):
                    rs = r["rs_data"]
                    score = rs.get("rs_score", 0)
                    trend = rs.get("trend_direction", "stable")
                    rs_line = f"  RS={score:.0f}({trend})"
                f.write(f"  {r['symbol']:8s} ${r['price']:>10,.2f}  score={r['score']:5.1f}  gates={r['gate_count']}/6  RSI={r['rsi']:5.1f}  ADX={r['adx']:5.1f}  5d={r['pct_change_5d']:+.1f}%  [{gate_str}]{rs_line}\n")
    
        f.write(f"\nALL QUALIFIED:\n")
        f.write(f"{'-'*60}\n")
        for r in qualified[:20]:
            wl_tag = " (watched)" if r["on_manual_watchlist"] else (" (owned)" if r["owned"] else " NEW")
            gate_str = "+".join(k[:2] for k, v in r["gates"].items() if v)
            rs_line = ""
            if r.get("rs_data"):
                rs = r["rs_data"]
                score = rs.get("rs_score", 0)
                trend = rs.get("trend_direction", "stable")
                rs_vs_spy = rs.get("rs_vs_benchmark", {}).get("SPY", {})
                spy_5d = rs_vs_spy.get("5d", 0)
                spy_20d = rs_vs_spy.get("20d", 0)
                rs_line = f"  RS={score:.0f}({trend})  SPY_5d={spy_5d:+.1f}%  SPY_20d={spy_20d:+.1f}%"
            f.write(f"  {r['symbol']:8s} ${r['price']:>10,.2f}  score={r['score']:5.1f}  gates={r['gate_count']}/6  RSI={r['rsi']:5.1f}  ADX={r['adx']:5.1f}  5d={r['pct_change_5d']:+.1f}%  [{gate_str}]{wl_tag}{rs_line}\n")

        if failed:
            f.write(f"\nFAILED ({len(failed)} symbols):\n")
            for s in failed[:10]:
                f.write(f"  {s}\n")

    # Print summary to stdout
    print(f"Qualified: {len(qualified)}, Trending: {len(trending)}")
    for r in trending[:5]:
        print(f"  {r['symbol']:8s} ${r['price']:>10,.2f}  score={r['score']}  gates={r['gate_count']}/6")

    return trending_data


if __name__ == "__main__":
    run_screener()
