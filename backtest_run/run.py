#!/usr/bin/env python3
"""
Trader Joe — Full Backtest Engine
Strategy: multi-gate trend-following with 49-symbol universe.
Fill model: signals on close, fills on next bar open (next_open).
"""

import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

# ── Paths ────────────────────────────────────────────────────────────────
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(OUT_DIR))

# ── Parameters ───────────────────────────────────────────────────────────
START = "2024-01-01"
END = "2026-06-16"
INITIAL_CASH = 100_000.0
SLIPPAGE = 0.001  # 0.1 % per trade
MAX_POSITIONS = 20
TARGET_MAX_POSITIONS = 15
MAX_BUYS_PER_SCAN = 3
DEFAULT_BUY_AMOUNT = 5_000.0
MAX_BUY_AMOUNT = 15_000.0
COOLDOWN_DAYS = 1

# ── Universe ─────────────────────────────────────────────────────────────
ETF_UNIVERSE = {
    "core": ["SPY", "QQQ", "IWM", "DIA"],
    "sector_rotation": ["XLF", "XLK", "XLE", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU", "SMH"],
    "defensive": ["TLT", "IEF", "LQD", "GLD", "VNQ"],
    "momentum": ["ARKK"],
}
STOCK_LIST = [
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "TSLA", "AMZN", "INTC", "MRVL",
    "GOOGL", "GOOG", "MU", "BRKB", "LLY", "META", "JPM", "XOM", "JNJ",
    "V", "WMT", "COST", "MA", "ABBV", "NFLX",
]

BUCKET_PRIORITY = {"core": 4, "sector_rotation": 3, "defensive": 2, "stocks": 1, "momentum": 0}

ALL_SYMBOLS: list[str] = []
SYMBOL_BUCKET: dict[str, str] = {}
for bucket, syms in ETF_UNIVERSE.items():
    for s in syms:
        ALL_SYMBOLS.append(s)
        SYMBOL_BUCKET[s] = bucket
for s in STOCK_LIST:
    ALL_SYMBOLS.append(s)
    SYMBOL_BUCKET[s] = "stocks"

# ══════════════════════════════════════════════════════════════════════════
# Indicator helpers (vectorised)
# ══════════════════════════════════════════════════════════════════════════

def ema(series: pd.Series, span: int) -> pd.Series:
    """Standard EMA (pandas, adjust=False → Wilder-style for consistency)."""
    return series.ewm(span=span, adjust=False).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI using Wilder's smoothing (alpha=1/N, adjust=False)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx_wilder(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX (D+ D- ADX) using Wilder's smoothing.
    Returns (plus_di, minus_di, adx)."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return plus_di, minus_di, adx


def macd_hist(close: pd.Series) -> pd.Series:
    """MACD histogram (EMA12 - EMA26 - signal9)."""
    ema12 = ema(close, 12)
    ema26 = ema(close, 26)
    macd_line = ema12 - ema26
    signal = ema(macd_line, 9)
    return macd_line - signal


# ══════════════════════════════════════════════════════════════════════════
# Data download
# ══════════════════════════════════════════════════════════════════════════

def download_all(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Download daily data for all symbols. Returns dict of DataFrames."""
    print(f"[DATA] Downloading {len(symbols)} symbols ({start} → {end}) …")
    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = yf.download(sym, start=start, end=end, progress=False,
                             auto_adjust=True, threads=True)
            # Flatten multi-level columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            required = {"Open", "High", "Low", "Close", "Volume"}
            if required.issubset(set(df.columns)) and len(df) >= 55:
                data[sym] = df
            else:
                print(f"  SKIP {sym}: insufficient data ({len(df)} rows)")
        except Exception as e:
            print(f"  SKIP {sym}: {e}")
    print(f"[DATA] Got {len(data)}/{len(symbols)} symbols.")
    return data


# ══════════════════════════════════════════════════════════════════════════
# Pre-compute indicators for every symbol
# ══════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicator columns to a price DataFrame."""
    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"]
    out = df.copy()
    out["SMA20"] = c.rolling(20).mean()
    out["SMA50"] = c.rolling(50).mean()
    out["RSI"] = rsi_wilder(c, 14)
    out["ADX"] = adx_wilder(h, l, c, 14)[2]
    out["MACD_HIST"] = macd_hist(c)
    out["VOL_AVG20"] = v.rolling(20).mean()
    out["BB_UPPER"] = out["SMA20"] + 2 * c.rolling(20).std(ddof=0)  # population std
    return out


# ══════════════════════════════════════════════════════════════════════════
# Scoring & signals
# ══════════════════════════════════════════════════════════════════════════

def evaluate_row(row: pd.Series, symbol: str) -> dict | None:
    """Compute gates, score, sell signals for a single bar."""
    price = row["Close"]
    sma20 = row["SMA20"]
    sma50 = row["SMA50"]
    rsi_val = row["RSI"]
    adx_val = row["ADX"]
    macd_h = row["MACD_HIST"]
    vol = row["Volume"]
    vol_avg = row["VOL_AVG20"]
    bb_upper = row["BB_UPPER"]

    if any(pd.isna(v) for v in [sma20, sma50, rsi_val, adx_val, macd_h, vol_avg, bb_upper]):
        return None

    # MACD histogram prev for sell logic
    # (caller must pass it separately since this is row-based)

    gates = {
        "above_sma20": price > sma20,
        "sma20_above_sma50": sma20 > sma50,
        "rsi_not_overbought": rsi_val < 70,
        "macd_positive": macd_h > 0,
        "adx_strength": adx_val > 15,
        "volume_confirmation": vol > vol_avg * 0.8,
    }
    gate_count = sum(1 for g in gates.values() if g)

    score = 0.0
    score += 1.5 if gates["above_sma20"] else 0.0
    score += 1.5 if gates["sma20_above_sma50"] else 0.0
    score += 1.0 if gates["rsi_not_overbought"] else 0.0
    score += 1.5 if gates["macd_positive"] else 0.0
    score += 1.0 if gates["adx_strength"] else 0.0
    score += 0.5 if gates["volume_confirmation"] else 0.0
    score += min(max(adx_val - 15.0, 0.0) / 10.0, 1.5)
    vol_ratio = vol / vol_avg if vol_avg else 0
    score += min(max(vol_ratio - 0.8, 0.0), 1.0)

    return {
        "symbol": symbol,
        "bucket": SYMBOL_BUCKET.get(symbol, "stocks"),
        "price": price,
        "sma20": sma20,
        "sma50": sma50,
        "rsi": rsi_val,
        "adx": adx_val,
        "macd_hist": macd_h,
        "volume": vol,
        "volume_avg20": vol_avg,
        "bb_upper": bb_upper,
        "gates": gates,
        "gate_count": gate_count,
        "score": score,
        "qualified": gate_count >= 4,
    }


def check_sell_signals(row: pd.Series, prev_macd_hist: float) -> list[str]:
    """Return list of sell reasons (empty → hold)."""
    reasons = []
    if row["RSI"] > 70:
        reasons.append(f"overbought RSI ({row['RSI']:.1f})")
    if row["Close"] >= row["BB_UPPER"]:
        reasons.append(f"at/above upper BB (${row['BB_UPPER']:.2f})")
    if row["MACD_HIST"] < 0 and row["MACD_HIST"] < prev_macd_hist:
        reasons.append("MACD death cross (hist worsening)")
    return reasons


# ══════════════════════════════════════════════════════════════════════════
# Backtest engine
# ══════════════════════════════════════════════════════════════════════════

def run_backtest(data: dict[str, pd.DataFrame]):
    """Main backtest loop. Returns (trades, equity_rows, benchmark_rows)."""

    # Pre-compute indicators
    print("[IND] Computing indicators …")
    indicators: dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        indicators[sym] = compute_indicators(df)

    # Union of all dates, sorted
    all_dates = sorted(set().union(*(df.index for df in indicators.values())))
    # Ensure start_date is after warm-up (55 bars).
    # We'll skip dates where most indicators are still NaN.
    warmup_done = False
    trade_dates = []
    for d in all_dates:
        # Check if at least the first 10 symbols have valid indicators
        valid_count = 0
        for sym in list(indicators.keys())[:10]:
            row = indicators[sym].loc[[d]] if d in indicators[sym].index else None
            if row is not None and not row["SMA50"].isna().any():
                valid_count += 1
        if valid_count >= 5:
            warmup_done = True
        if warmup_done:
            trade_dates.append(d)

    print(f"[BT]  Trade dates: {trade_dates[0].date()} → {trade_dates[-1].date()}  ({len(trade_dates)} bars)")

    # ── State ──
    cash = INITIAL_CASH
    # holdings[sym] = {"shares": float, "cost_basis": float, "filled_at": date}
    holdings: dict[str, dict] = {}
    # cooldowns[sym] = date when cooldown expires (can't buy before this)
    cooldowns: dict[str, pd.Timestamp] = {}
    # Track MACD hist prev day for sell signals
    macd_hist_cache: dict[str, float] = {}
    trades: list[dict] = []
    equity_rows: list[dict] = []
    # Fees
    total_fees = 0.0

    for idx, date in enumerate(trade_dates):
        # ── Phase 1: Evaluate sells on current positions ──
        sells_today = []  # list of sym to sell
        for sym in list(holdings.keys()):
            if sym not in indicators or date not in indicators[sym].index:
                continue
            row = indicators[sym].loc[date]
            prev_macd = macd_hist_cache.get(sym, row["MACD_HIST"])
            reasons = check_sell_signals(row, prev_macd)
            if reasons:
                sells_today.append(sym)

        for sym in sells_today:
            shares = holdings[sym]["shares"]
            row = indicators[sym].loc[date]
            # Signal on close → fill on next open. We'll record the sell signal
            # and execute on next bar's open.
            # For simplicity, mark for next-open fill.
            pass  # handled below with next-open model

        # Actually, let's implement properly: record signals on close, fill next open.
        # We need a signal queue. Let's restructure:
        # On each bar close, compute signals. Fills happen at next bar open.
        # For bar i close signals → bar i+1 open fills.
        # So: signals computed on date, fills on trade_dates[idx+1] open.

        # ── Compute all signals for this bar's close ──
        buy_candidates = []
        sell_signals = {}  # sym → reasons

        # Sells
        for sym in list(holdings.keys()):
            if sym not in indicators or date not in indicators[sym].index:
                continue
            row = indicators[sym].loc[date]
            prev_macd = macd_hist_cache.get(sym, float(row["MACD_HIST"]))
            reasons = check_sell_signals(row, prev_macd)
            if reasons:
                sell_signals[sym] = reasons

        # Buys — scan all symbols not held and not on cooldown
        for sym in ALL_SYMBOLS:
            if sym in holdings:
                continue
            if sym in cooldowns and cooldowns[sym] > date:
                continue
            if sym not in indicators or date not in indicators[sym].index:
                continue
            result = evaluate_row(indicators[sym].loc[date], sym)
            if result is None:
                continue
            if result["qualified"]:
                buy_candidates.append(result)

        # Sort candidates: score desc, then ADX desc, then vol ratio desc, then bucket priority desc
        buy_candidates.sort(key=lambda x: (
            x["score"],
            x["adx"],
            x["volume"] / x["volume_avg20"] if x["volume_avg20"] else 0,
            BUCKET_PRIORITY.get(x["bucket"], 0),
        ), reverse=True)

        # ── Phase 2: Execute fills from PREVIOUS day's signals ──
        # On the first bar, nothing to fill. On subsequent bars, fill yesterday's signals.
        # We'll store pending signals.
        pass

    # ──── RESTRUCTURED: proper next-open fill model ────
    # Reset state
    cash = INITIAL_CASH
    holdings = {}
    cooldowns = {}
    macd_hist_cache = {}
    trades = []
    equity_rows = []
    total_fees = 0.0

    pending_sells = {}  # sym → (reasons, date_signal)
    pending_buys = []   # list of candidate dicts

    for i, date in enumerate(trade_dates):
        next_date = trade_dates[i + 1] if i + 1 < len(trade_dates) else None

        # ── Step A: Execute fills from previous bar's signals at current open ──
        if i > 0:  # no fills on first bar (no previous signals)
            # Get current open prices
            open_prices = {}
            for sym in ALL_SYMBOLS:
                if sym in indicators and date in indicators[sym].index:
                    open_prices[sym] = indicators[sym].loc[date, "Open"]

            # Execute sells first
            for sym, (reasons, sig_date) in list(pending_sells.items()):
                if sym not in open_prices:
                    continue
                shares = holdings.get(sym, {}).get("shares", 0)
                if shares <= 0:
                    continue
                fill_price = open_prices[sym] * (1 - SLIPPAGE)  # sell at bid (slightly worse)
                notional = shares * fill_price
                fee = notional * SLIPPAGE
                total_fees += fee
                cash += notional - fee
                cost_basis = holdings[sym]["cost_basis"]
                pl = (fill_price - cost_basis) * shares

                trades.append({
                    "date": date,
                    "symbol": sym,
                    "side": "SELL",
                    "price": round(fill_price, 4),
                    "shares": shares,
                    "notional": round(notional, 2),
                    "fee": round(fee, 2),
                    "reason": "; ".join(reasons),
                    "pl": round(pl, 2),
                    "days_held": (date - holdings[sym]["filled_at"]).days,
                })
                cooldowns[sym] = date + pd.Timedelta(days=COOLDOWN_DAYS)
                del holdings[sym]

            # Execute buys (up to MAX_BUYS_PER_SCAN)
            # Trim weakest positions if over TARGET_MAX
            current_pos_count = len(holdings)
            for candidate in pending_buys[:MAX_BUYS_PER_SCAN]:
                sym = candidate["symbol"]
                if sym in holdings or sym in cooldowns and cooldowns[sym] > date:
                    continue
                if sym not in open_prices:
                    continue

                # Check position cap
                if len(holdings) >= MAX_POSITIONS:
                    break

                # Trim weakest if over target
                if len(holdings) >= TARGET_MAX_POSITIONS:
                    # Find weakest by score (re-evaluate)
                    weakest_sym = None
                    weakest_score = float('inf')
                    for hs in holdings:
                        if hs in indicators and date in indicators[hs].index:
                            res = evaluate_row(indicators[hs].loc[date], hs)
                            if res:
                                if res["score"] < weakest_score:
                                    weakest_score = res["score"]
                                    weakest_sym = hs
                    if weakest_sym and weakest_sym != sym:
                        # Sell weakest
                        ws = holdings[weakest_sym]
                        wsh = ws["shares"]
                        if wsh > 0 and weakest_sym in open_prices:
                            w_price = open_prices[weakest_sym] * (1 - SLIPPAGE)
                            w_notional = wsh * w_price
                            w_fee = w_notional * SLIPPAGE
                            total_fees += w_fee
                            cash += w_notional - w_fee
                            w_pl = (w_price - ws["cost_basis"]) * wsh
                            trades.append({
                                "date": date,
                                "symbol": weakest_sym,
                                "side": "SELL",
                                "price": round(w_price, 4),
                                "shares": wsh,
                                "notional": round(w_notional, 2),
                                "fee": round(w_fee, 2),
                                "reason": "trimmed (weakest, over target)",
                                "pl": round(w_pl, 2),
                                "days_held": (date - ws["filled_at"]).days,
                            })
                            cooldowns[weakest_sym] = date + pd.Timedelta(days=COOLDOWN_DAYS)
                            del holdings[weakest_sym]

                # Compute buy amount
                portfolio_value = cash + sum(
                    h["shares"] * open_prices.get(hs, 0)
                    for hs, h in holdings.items() if hs in open_prices
                )
                buy_amount = min(MAX_BUY_AMOUNT, max(DEFAULT_BUY_AMOUNT, portfolio_value * 0.05))

                if cash < buy_amount * 1.0:
                    # Scale down or skip
                    if cash < DEFAULT_BUY_AMOUNT * 0.5:
                        continue
                    buy_amount = cash * 0.95  # use most of cash

                fill_price = open_prices[sym] * (1 + SLIPPAGE)  # buy at ask (slightly worse)
                shares = buy_amount / fill_price
                if shares < 1:
                    continue
                notional = shares * fill_price
                fee = notional * SLIPPAGE
                total_fees += fee
                cash -= (notional + fee)

                holdings[sym] = {
                    "shares": shares,
                    "cost_basis": fill_price,
                    "filled_at": date,
                    "score": candidate["score"],
                }
                trades.append({
                    "date": date,
                    "symbol": sym,
                    "side": "BUY",
                    "price": round(fill_price, 4),
                    "shares": shares,
                    "notional": round(notional, 2),
                    "fee": round(fee, 2),
                    "reason": f"score={candidate['score']:.2f} gates={candidate['gate_count']}/6",
                    "pl": 0,
                    "days_held": 0,
                })

        # Clear pending (they've been executed or expired)
        pending_sells = {}
        pending_buys = []

        # ── Step B: Compute signals on current bar close ──
        # Sells
        for sym in list(holdings.keys()):
            if sym not in indicators or date not in indicators[sym].index:
                continue
            row = indicators[sym].loc[date]
            prev_macd = macd_hist_cache.get(sym, float(row["MACD_HIST"]))
            reasons = check_sell_signals(row, prev_macd)
            if reasons:
                pending_sells[sym] = (reasons, date)

        # Update MACD cache
        for sym in list(holdings.keys()):
            if sym in indicators and date in indicators[sym].index:
                macd_hist_cache[sym] = float(indicators[sym].loc[date, "MACD_HIST"])

        # Buys
        for sym in ALL_SYMBOLS:
            if sym in holdings:
                continue
            if sym in cooldowns and cooldowns[sym] > date:
                continue
            if sym not in indicators or date not in indicators[sym].index:
                continue
            result = evaluate_row(indicators[sym].loc[date], sym)
            if result is None:
                continue
            if result["qualified"]:
                pending_buys.append(result)

        # Sort buy candidates
        pending_buys.sort(key=lambda x: (
            x["score"],
            x["adx"],
            x["volume"] / x["volume_avg20"] if x["volume_avg20"] else 0,
            BUCKET_PRIORITY.get(x["bucket"], 0),
        ), reverse=True)

        # ── Step C: Record daily equity ──
        # Use close prices for equity calculation
        close_prices = {}
        for sym in ALL_SYMBOLS:
            if sym in indicators and date in indicators[sym].index:
                close_prices[sym] = indicators[sym].loc[date, "Close"]

        exposure = sum(
            h["shares"] * close_prices.get(hs, 0)
            for hs, h in holdings.items() if hs in close_prices
        )
        equity = cash + exposure
        equity_rows.append({
            "date": date,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "exposure": round(exposure, 2),
            "positions": len(holdings),
        })

    return trades, equity_rows, total_fees


# ══════════════════════════════════════════════════════════════════════════
# Benchmark: equal-weight buy-and-hold
# ══════════════════════════════════════════════════════════════════════════

def compute_benchmark(data: dict[str, pd.DataFrame], trade_dates: list) -> pd.DataFrame:
    """Equal-weight buy-and-hold benchmark across all available symbols."""
    # Each symbol gets equal notional allocation on the first valid date
    valid_syms = [s for s in data.keys() if trade_dates[0] in data[s].index]
    n = len(valid_syms)
    if n == 0:
        return pd.DataFrame()

    weight = 1.0 / n
    daily_equity = []
    for date in trade_dates:
        total_val = 0.0
        for sym in valid_syms:
            if date in data[sym].index:
                price = data[sym].loc[date, "Close"]
                # Assume we bought at first-date price, shares = weight * INITIAL_CASH / first_price
                first_price = data[sym].loc[trade_dates[0], "Close"]
                shares = (weight * INITIAL_CASH) / first_price
                total_val += shares * price
        daily_equity.append({"date": date, "equity": round(total_val, 2)})

    return pd.DataFrame(daily_equity)


# ══════════════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════════════

def compute_metrics(trades: list[dict], equity_df: pd.DataFrame,
                    benchmark_df: pd.DataFrame) -> dict:
    """Compute all requested performance metrics."""

    # ── Strategy returns ──
    equity_df = equity_df.copy()
    equity_df["date"] = pd.to_datetime(equity_df["date"])
    equity_df = equity_df.set_index("date").sort_index()
    daily_returns = equity_df["equity"].pct_change().dropna()

    total_return = (equity_df["equity"].iloc[-1] / equity_df["equity"].iloc[0]) - 1

    n_days = (equity_df.index[-1] - equity_df.index[0]).days
    n_years = n_days / 365.25
    ann_return = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1

    # Max drawdown
    cummax = equity_df["equity"].cummax()
    drawdown = (equity_df["equity"] - cummax) / cummax
    max_dd = drawdown.min()

    # Sharpe ratio (daily, sample std N-1, sqrt(252), rf=0)
    sharpe = daily_returns.mean() / daily_returns.std(ddof=1) * np.sqrt(252) if daily_returns.std(ddof=1) > 0 else 0

    # ── Trade-level metrics ──
    # Match BUY/SELL pairs by symbol (FIFO)
    open_positions: dict[str, dict] = {}
    round_trips = []
    for t in trades:
        sym = t["symbol"]
        if t["side"] == "BUY":
            if sym in open_positions:
                # Add to existing position (average cost)
                old = open_positions[sym]
                total_shares = old["shares"] + t["shares"]
                avg_cost = (old["shares"] * old["price"] + t["shares"] * t["price"]) / total_shares
                open_positions[sym] = {
                    "shares": total_shares,
                    "price": avg_cost,
                    "filled_at": old["filled_at"],
                    "total_notional": old["total_notional"] + t["notional"],
                }
            else:
                open_positions[sym] = {
                    "shares": t["shares"],
                    "price": t["price"],
                    "filled_at": t["date"],
                    "total_notional": t["notional"],
                }
        elif t["side"] == "SELL" and sym in open_positions:
            op = open_positions[sym]
            sell_shares = min(t["shares"], op["shares"])
            pl = t.get("pl", (t["price"] - op["price"]) * sell_shares)
            days = (pd.to_datetime(t["date"]) - pd.to_datetime(op["filled_at"])).days
            round_trips.append({
                "symbol": sym,
                "pl": pl,
                "days_held": days,
                "buy_date": op["filled_at"],
                "sell_date": t["date"],
            })
            op["shares"] -= sell_shares
            if op["shares"] <= 0.001:
                del open_positions[sym]

    total_trades = len(trades)
    n_round_trips = len(round_trips)
    wins = [r for r in round_trips if r["pl"] > 0]
    losses = [r for r in round_trips if r["pl"] <= 0]
    hit_rate = len(wins) / n_round_trips if n_round_trips > 0 else 0

    sum_wins = sum(r["pl"] for r in wins)
    sum_losses = abs(sum(r["pl"] for r in losses))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else float('inf')

    avg_pl = np.mean([r["pl"] for r in round_trips]) if round_trips else 0
    best_trade = max((r["pl"] for r in round_trips), default=0)
    worst_trade = min((r["pl"] for r in round_trips), default=0)
    avg_days = np.mean([r["days_held"] for r in round_trips]) if round_trips else 0

    # Turnover
    total_notional = sum(t["notional"] for t in trades)
    mean_equity = equity_df["equity"].mean()
    turnover = total_notional / mean_equity if mean_equity > 0 else 0

    # ── Benchmark metrics ──
    if len(benchmark_df) > 0:
        benchmark_df = benchmark_df.copy()
        benchmark_df["date"] = pd.to_datetime(benchmark_df["date"])
        benchmark_df = benchmark_df.set_index("date").sort_index()
        bench_total_return = (benchmark_df["equity"].iloc[-1] / benchmark_df["equity"].iloc[0]) - 1
        bench_daily = benchmark_df["equity"].pct_change().dropna()
        bench_ann = (1 + bench_total_return) ** (1 / max(n_years, 0.01)) - 1
        bench_cummax = benchmark_df["equity"].cummax()
        bench_dd = ((benchmark_df["equity"] - bench_cummax) / bench_cummax).min()
        bench_sharpe = bench_daily.mean() / bench_daily.std(ddof=1) * np.sqrt(252) if bench_daily.std(ddof=1) > 0 else 0
    else:
        bench_total_return = bench_ann = bench_dd = bench_sharpe = 0

    # Total fees
    total_fees = sum(t.get("fee", 0) for t in trades)

    return {
        "strategy_total_return": round(total_return * 100, 2),
        "benchmark_total_return": round(bench_total_return * 100, 2),
        "strategy_annualized_return": round(ann_return * 100, 2),
        "benchmark_annualized_return": round(bench_ann * 100, 2),
        "strategy_max_drawdown": round(max_dd * 100, 2),
        "benchmark_max_drawdown": round(bench_dd * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "benchmark_sharpe_ratio": round(bench_sharpe, 3),
        "total_trades": total_trades,
        "round_trips": n_round_trips,
        "hit_rate": round(hit_rate * 100, 2),
        "profit_factor": round(profit_factor, 3),
        "avg_trade_pnl": round(avg_pl, 2),
        "best_trade": round(best_trade, 2),
        "worst_trade": round(worst_trade, 2),
        "avg_days_in_trade": round(avg_days, 1),
        "turnover": round(turnover, 3),
        "total_fees_paid": round(total_fees, 2),
        "final_equity": round(equity_df["equity"].iloc[-1], 2),
        "initial_capital": INITIAL_CASH,
        "trade_days": len(equity_df),
        "start_date": str(equity_df.index[0].date()),
        "end_date": str(equity_df.index[-1].date()),
    }


# ══════════════════════════════════════════════════════════════════════════
# Report generation
# ══════════════════════════════════════════════════════════════════════════

def generate_report(metrics: dict) -> str:
    """Generate a human-readable markdown report."""
    m = metrics
    lines = [
        "# Trader Joe Backtest Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Period:** {m['start_date']} → {m['end_date']} ({m['trade_days']} trading days)",
        f"**Initial Capital:** ${m['initial_capital']:,.0f}",
        "",
        "## Performance Summary",
        "",
        f"| Metric | Strategy | Benchmark |",
        f"|--------|----------|-----------|",
        f"| Total Return | **{m['strategy_total_return']:+.2f}%** | {m['benchmark_total_return']:+.2f}% |",
        f"| Annualized Return | **{m['strategy_annualized_return']:+.2f}%** | {m['benchmark_annualized_return']:+.2f}% |",
        f"| Max Drawdown | **{m['strategy_max_drawdown']:.2f}%** | {m['benchmark_max_drawdown']:.2f}% |",
        f"| Sharpe Ratio | **{m['sharpe_ratio']:.3f}** | {m['benchmark_sharpe_ratio']:.3f} |",
        "",
        "## Trade Statistics",
        "",
        f"- **Total Trades:** {m['total_trades']}",
        f"- **Round Trips:** {m['round_trips']}",
        f"- **Hit Rate:** {m['hit_rate']:.1f}%",
        f"- **Profit Factor:** {m['profit_factor']:.3f}",
        f"- **Avg Trade P&L:** ${m['avg_trade_pnl']:,.2f}",
        f"- **Best Trade:** ${m['best_trade']:,.2f}",
        f"- **Worst Trade:** ${m['worst_trade']:,.2f}",
        f"- **Avg Days in Trade:** {m['avg_days_in_trade']:.1f}",
        "",
        "## Portfolio Metrics",
        "",
        f"- **Final Equity:** ${m['final_equity']:,.2f}",
        f"- **Total Fees Paid:** ${m['total_fees_paid']:,.2f}",
        f"- **Turnover (× mean equity):** {m['turnover']:.3f}×",
        "",
        "## Strategy Description",
        "",
        "Trend-following multi-gate system with 49-symbol universe (4 core ETFs, 10 sector ETFs, 5 defensive ETFs, 1 momentum ETF, 24 stocks).",
        "",
        "**Buy Gates (need 4 of 6):**",
        "1. Price > SMA(20)",
        "2. SMA(20) > SMA(50)",
        "3. RSI(14) < 70",
        "4. MACD Histogram > 0",
        "5. ADX(14) > 15",
        "6. Volume > 20-day avg × 0.8",
        "",
        "**Sell Signals (any):** RSI > 70, Price ≥ Upper Bollinger Band, MACD histogram worsening negative.",
        "",
        "**Position Limits:** Max 20 positions, trim weakest above 15. Buy size: min($15k, max($5k, 5% portfolio)). Up to 3 buys per scan.",
        "",
        "**Slippage:** 0.1% per trade. Fill model: signals on close, fills on next open.",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  Trader Joe — Backtest Engine")
    print("=" * 60)

    # Download data
    data = download_all(ALL_SYMBOLS, START, END)

    if not data:
        print("[ERROR] No data downloaded. Aborting.")
        sys.exit(1)

    # Determine trade dates
    all_dates = sorted(set().union(*(df.index for df in data.values())))
    warmup_done = False
    trade_dates = []
    for d in all_dates:
        valid_count = sum(
            1 for sym in list(data.keys())[:10]
            if d in data[sym].index
        )
        if valid_count >= 5:
            warmup_done = True
        if warmup_done:
            trade_dates.append(d)

    # Run backtest
    print("[BT] Running backtest …")
    trades, equity_rows, total_fees = run_backtest(data)

    # Save trades
    trades_df = pd.DataFrame(trades)
    trades_csv = os.path.join(OUT_DIR, "trades.csv")
    if len(trades_df) > 0:
        trades_df.to_csv(trades_csv, index=False)
    else:
        pd.DataFrame(columns=["date", "symbol", "side", "price", "shares", "notional", "fee", "reason", "pl", "days_held"]).to_csv(trades_csv, index=False)
    print(f"[OUT] trades.csv ({len(trades_df)} trades)")

    # Save equity curve
    equity_df = pd.DataFrame(equity_rows)
    equity_csv = os.path.join(OUT_DIR, "equity.csv")
    equity_df.to_csv(equity_csv, index=False)
    print(f"[OUT] equity.csv ({len(equity_df)} rows)")

    # Compute benchmark
    print("[BM] Computing benchmark …")
    benchmark_df = compute_benchmark(data, trade_dates)
    bench_csv = os.path.join(OUT_DIR, "benchmark_equity.csv")
    benchmark_df.to_csv(bench_csv, index=False)
    print(f"[OUT] benchmark_equity.csv ({len(benchmark_df)} rows)")

    # Compute metrics
    print("[MET] Computing metrics …")
    metrics = compute_metrics(trades, equity_df, benchmark_df)

    # Save summary.json
    summary_json = os.path.join(OUT_DIR, "summary.json")
    with open(summary_json, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"[OUT] summary.json")

    # Save report
    report_md = os.path.join(OUT_DIR, "report.md")
    with open(report_md, "w") as f:
        f.write(generate_report(metrics))
    print(f"[OUT] report.md")

    # Print summary
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    for k, v in metrics.items():
        print(f"  {k:30s}: {v}")
    print("=" * 60)

    return metrics


if __name__ == "__main__":
    main()
