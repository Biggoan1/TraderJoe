#!/usr/bin/env python3
"""
Whole-market cross-sectional factor lab.
Universe: rule-based (top-800 by liquidity, no hand-picking). Each rebalance the
ENTIRE universe is re-ranked by price/volume "growth" factors computed point-in-time;
hold the top-K equal-weight. Costs charged on turnover. Optional SPY>200DMA kill-switch.

Factors (all from market data, computed at each rebalance date, cross-sectional z-scores):
  mom12  : 12-month return skipping the most recent month (classic 12-1 momentum)
  mom6   : 6-month return
  mom3   : 3-month return
  lowvol : negative 3-month daily-return volatility (prefer steadier movers)
  volgro : dollar-volume growth (3m avg vs 12m avg) — "is money flowing in?"
Composite = weighted sum of z-scores. Eligibility at date: price>$5 AND trailing
63d median dollar volume in the top LIQ_TOP of the universe (point-in-time, so a
name that was illiquid in 2016 can't be picked in 2016).
"""
import os, sys, json, glob
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

ROOT = "/root/hermes-trader"
# SPLIT+DIVIDEND-ADJUSTED dataset (2026-07-12): the raw dataset has split cliffs
# (NVDA 2024 10:1 showed as a fake -90% day). Factor math needs adjusted prices.
DATASET = "fafo-market-adj-2016"
DDIR = f"{ROOT}/market_data/equities/daily/{DATASET}"
OUT = f"{ROOT}/reports/strategy_search"

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

def load_matrices():
    closes, vols = {}, {}
    symdirs = sorted(os.listdir(DDIR))
    # restrict to the current rule-based universe (excludes delisted-from-universe
    # ETP dirs still on disk); SPY handled separately below
    uni_path = f"{OUT}/market_universe.json"
    if os.path.exists(uni_path):
        uni = set(json.load(open(uni_path)))
        symdirs = [s for s in symdirs if s in uni]
    for sym in symdirs:
        files = sorted(glob.glob(f"{DDIR}/{sym}/*.parquet"))
        if not files:
            continue
        df = pd.concat([pd.read_parquet(f, columns=["timestamp", "close", "volume"]) for f in files])
        idx = pd.to_datetime(df["timestamp"].str[:10])
        closes[sym] = pd.Series(df["close"].values, index=idx)
        vols[sym] = pd.Series(df["volume"].values, index=idx)
    C = pd.DataFrame(closes).sort_index()
    V = pd.DataFrame(vols).sort_index()
    C = C[~C.index.duplicated()]
    V = V[~V.index.duplicated()]
    if "SPY" not in C.columns:   # SPY is ARCA-listed, excluded by the exchange screen
        files = sorted(glob.glob(f"{ROOT}/market_data/equities/daily/fafo-universe-2016/SPY/*.parquet"))
        df = pd.concat([pd.read_parquet(f, columns=["timestamp", "close", "volume"]) for f in files])
        idx = pd.to_datetime(df["timestamp"].str[:10])
        spy_c = pd.Series(df["close"].values, index=idx)
        C = C.join(spy_c.rename("SPY"), how="left")
        V["SPY"] = 0.0
    return C, V

def zscore(row):
    m, s = row.mean(), row.std()
    return (row - m) / s if s and s > 0 else row * 0.0

def run(C, V, weights, K=20, REB=21, LIQ_TOP=600, cost_bps=10.0, kill_switch=False,
        start="2017-06-01", exclude=None):
    """exclude: optional calendar-daily DataFrame[date x symbol] of bools; names
    True as of the rebalance date are barred from selection (frenzy filter etc.)."""
    px = C
    ret1 = px.pct_change()
    dollar = (px * V)
    mom12 = px.shift(21) / px.shift(252) - 1
    mom6 = px / px.shift(126) - 1
    mom3 = px / px.shift(63) - 1
    vol3 = ret1.rolling(63).std()
    volgro = dollar.rolling(63).mean() / dollar.rolling(252).mean() - 1
    liq = dollar.rolling(63).median()

    spy = px["SPY"] if "SPY" in px else None
    spy_ok = (spy > spy.rolling(200).mean()) if spy is not None else None

    dates = px.index[px.index >= start]
    reb_dates = dates[::REB]
    equity, eq = [], 1.0
    hold = pd.Index([])
    daily = []
    prev_d = None
    weights_map = dict(zip(["mom12", "mom6", "mom3", "lowvol", "volgro"], weights))
    for i, d in enumerate(dates):
        if prev_d is not None:
            r = ret1.loc[d, hold].mean() if len(hold) else 0.0
            if kill_switch and spy_ok is not None and not bool(spy_ok.loc[:d].iloc[-2]):
                r = 0.0
            if not np.isfinite(r):
                r = 0.0
            eq *= (1 + r)
        if d in reb_dates.values or prev_d is None:
            # point-in-time eligibility
            lq = liq.loc[:d].iloc[-1]
            price_ok = px.loc[d] > 5.0
            elig = lq.rank(ascending=False) <= LIQ_TOP
            elig &= price_ok
            elig &= mom12.loc[d].notna()
            uni = elig[elig].index
            if exclude is not None:
                exh = exclude.loc[:d]
                if len(exh):
                    exset = exh.iloc[-1]
                    uni = uni[~uni.isin(exset[exset].index)]
            if len(uni) >= K * 2:
                score = (weights_map["mom12"] * zscore(mom12.loc[d, uni])
                         + weights_map["mom6"] * zscore(mom6.loc[d, uni])
                         + weights_map["mom3"] * zscore(mom3.loc[d, uni])
                         - weights_map["lowvol"] * zscore(vol3.loc[d, uni])
                         + weights_map["volgro"] * zscore(volgro.loc[d, uni]))
                new_hold = score.nlargest(K).index
                turn = 1.0 - (len(hold.intersection(new_hold)) / K if len(hold) else 0.0)
                eq *= (1 - 2 * turn * cost_bps / 1e4)   # sell old + buy new legs
                hold = new_hold
        daily.append((d, eq))
        prev_d = d
    s = pd.Series(dict(daily))
    r = s.pct_change().dropna()
    yrs = len(s) / 252
    out = {
        "total_return_pct": (s.iloc[-1] / s.iloc[0] - 1) * 100,
        "cagr_pct": ((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100,
        "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0,
        "max_drawdown_pct": ((s / s.cummax()) - 1).min() * -100,
        "yearly": {str(y): (g.iloc[-1] / g.iloc[0] - 1) * 100 for y, g in s.groupby(s.index.year)},
    }
    return out, s

def spy_baseline(C, start="2017-06-01"):
    s = C["SPY"][C.index >= start].dropna()
    r = s.pct_change().dropna()
    yrs = len(s) / 252
    return {"total_return_pct": (s.iloc[-1]/s.iloc[0]-1)*100,
            "cagr_pct": ((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100,
            "sharpe": r.mean()/r.std()*np.sqrt(252),
            "max_drawdown_pct": ((s/s.cummax())-1).min()*-100,
            "yearly": {str(y): (g.iloc[-1]/g.iloc[0]-1)*100 for y,g in s.groupby(s.index.year)}}

def fmt(name, m):
    yr = " ".join(f"{y}:{v:+.0f}%" for y, v in m["yearly"].items())
    return (f"{name:<28} cagr={m['cagr_pct']:+6.2f}% sharpe={m['sharpe']:+5.2f} "
            f"maxDD={m['max_drawdown_pct']:5.1f}%  [{yr}]")

if __name__ == "__main__":
    log("loading price/volume matrices...")
    C, V = load_matrices()
    log(f"matrix: {C.shape[0]} days x {C.shape[1]} symbols")
    base = spy_baseline(C)
    print(fmt("SPY buy&hold", base), flush=True)
    CONFIGS = [
        ("mom12 classic",        [1.0, 0.0, 0.0, 0.0, 0.0], 20, False),
        ("mom blend",            [0.5, 0.3, 0.2, 0.0, 0.0], 20, False),
        ("mom+lowvol",           [0.5, 0.3, 0.0, 0.3, 0.0], 20, False),
        ("mom+volgrowth",        [0.5, 0.2, 0.0, 0.0, 0.3], 20, False),
        ("full growth blend",    [0.4, 0.2, 0.1, 0.2, 0.2], 20, False),
        ("full blend K=50",      [0.4, 0.2, 0.1, 0.2, 0.2], 50, False),
        ("full blend +killswitch",[0.4, 0.2, 0.1, 0.2, 0.2], 20, True),
        ("mom blend +killswitch",[0.5, 0.3, 0.2, 0.0, 0.0], 20, True),
    ]
    results = []
    for name, w, k, ks in CONFIGS:
        m, s = run(C, V, w, K=k, kill_switch=ks)
        results.append((name, m))
        print(fmt(name, m), flush=True)
    json.dump({n: m for n, m in results} | {"SPY": base},
              open(f"{OUT}/factor_lab_results.json", "w"), indent=1, default=str)
    log("saved -> reports/strategy_search/factor_lab_results.json")
