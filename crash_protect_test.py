#!/usr/bin/env python3
"""
Crash-protection redesign shootout (2026-07-13, standing pre-real-money item).
Variants applied as EXPOSURE OVERLAYS on the deployed factor strategy's naked
daily returns (prev-day signal, no lookahead; switch/scale trading costs NOT
modeled -- same simplification as the Saturday kill-switch math):
  naked            : always 100% invested (current deployed stance)
  switch200        : binary SPY>200DMA (the variant we dropped Saturday)
  fast re-entry    : out ONLY when SPY < 200DMA AND SPY < 20DMA -- slow exit,
                     quick re-entry (a V-rebound reclaims the 20DMA early)
  vol-target 20/15 : exposure = min(1, target / SPY 20d realized vol) -- never
                     fully exits, scales down in storms
Windows: modern 2020-01-02..now (adjusted warehouse) + GFC 2007-01..2010-06
(yfinance survivors, cached to parquet -- OPTIMISTIC, dead names absent).
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices, run

OUT = "/root/hermes-trader/reports/strategy_search"
def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

cfg = json.load(open(f"{OUT}/factor_deploy_config.json"))

def build_overlays(spy, idx):
    ma200 = spy.rolling(200).mean()
    ma20 = spy.rolling(20).mean()
    vol = spy.pct_change().rolling(20).std() * np.sqrt(252)
    ex = {
        "naked (deployed)": pd.Series(1.0, index=spy.index),
        "switch 200DMA (dropped Sat)": (spy > ma200).astype(float),
        "FAST RE-ENTRY (<200 AND <20)": (~((spy < ma200) & (spy < ma20))).astype(float),
        "VOL-TARGET 20%": (0.20 / vol).clip(upper=1.0),
        "VOL-TARGET 15%": (0.15 / vol).clip(upper=1.0),
    }
    return {k: v.shift(1).reindex(idx).ffill().fillna(1.0) for k, v in ex.items()}

def metrics(eq):
    r = eq.pct_change().dropna()
    yrs = len(eq) / 252
    return {"total": (eq.iloc[-1]/eq.iloc[0]-1)*100,
            "cagr": ((eq.iloc[-1]/eq.iloc[0])**(1/yrs)-1)*100,
            "sharpe": r.mean()/r.std()*np.sqrt(252) if r.std() > 0 else 0.0,
            "maxdd": ((eq/eq.cummax())-1).min()*-100,
            "yearly": {int(y): (g.iloc[-1]/g.iloc[0]-1)*100 for y, g in eq.groupby(eq.index.year)}}

def shootout(C, V, start, label, liq_top):
    m, s = run(C, V, cfg["weights"], K=cfg["K"], REB=cfg["reb_days"],
               LIQ_TOP=liq_top, kill_switch=False, start=start)
    rets = s.pct_change().fillna(0.0)
    spy = C["SPY"].dropna()
    rows = {}
    for name, expo in build_overlays(spy, rets.index).items():
        eq = (1 + expo * rets).cumprod() * 100_000
        mm = metrics(eq)
        mm["avg_expo"] = float(expo.mean()) * 100
        rows[name] = (mm, eq)
        log(f"{label:8} {name:30} final=${eq.iloc[-1]:>12,.0f} maxDD={mm['maxdd']:.1f}% avgExpo={mm['avg_expo']:.0f}%")
    spy_eq = (spy[spy.index >= start] / spy[spy.index >= start].iloc[0] * 100_000)
    rows["SPY buy&hold"] = (metrics(spy_eq) | {"avg_expo": 100.0}, spy_eq)
    return rows

# ---------- modern window ----------
log("loading adjusted warehouse matrices...")
C, V = load_matrices()
modern = shootout(C, V, "2020-01-02", "MODERN", cfg["liq_top"])

# ---------- GFC window (yfinance survivors, cached) ----------
CACHE_C, CACHE_V = f"{OUT}/gfc_prices.parquet", f"{OUT}/gfc_volumes.parquet"
if os.path.exists(CACHE_C):
    log("GFC cache hit")
    Cg, Vg = pd.read_parquet(CACHE_C), pd.read_parquet(CACHE_V)
else:
    import yfinance as yf
    uni = json.load(open(f"{OUT}/market_universe.json"))
    tickers = sorted(set(uni) | {"SPY"})
    log(f"downloading 2005-2010 for {len(tickers)} tickers via yfinance...")
    fc, fv = [], []
    for i in range(0, len(tickers), 100):
        try:
            df = yf.download(tickers[i:i+100], start="2005-01-01", end="2010-07-01",
                             progress=False, auto_adjust=True, group_by="column", threads=True)
        except Exception as e:
            log(f"  chunk {i}: {e}"); continue
        if df is None or df.empty: continue
        fc.append(df["Close"] if "Close" in df else df)
        fv.append(df["Volume"] if "Volume" in df else None)
        log(f"  {min(i+100,len(tickers))}/{len(tickers)}"); time.sleep(1)
    Cg = pd.concat(fc, axis=1); Vg = pd.concat([f for f in fv if f is not None], axis=1)
    Cg = Cg.loc[:, ~Cg.columns.duplicated()].dropna(axis=1, how="all")
    Vg = Vg.loc[:, ~Vg.columns.duplicated()].reindex(columns=Cg.columns).fillna(0.0)
    has_hist = Cg.loc[:"2006-06-30"].notna().sum() > 200
    Cg, Vg = Cg.loc[:, has_hist], Vg.loc[:, has_hist]
    Cg.index, Vg.index = pd.to_datetime(Cg.index), pd.to_datetime(Vg.index)
    Cg.to_parquet(CACHE_C); Vg.to_parquet(CACHE_V)
    log(f"cached GFC matrix {Cg.shape} -> parquet")
gfc = shootout(Cg, Vg, "2007-01-03", "GFC", min(cfg["liq_top"], int(Cg.shape[1]*0.9)))

# ---------- report ----------
L = ["# Crash-Protection Shootout — fast re-entry vs vol-targeting",
     f"_{datetime.now(timezone(timedelta(hours=-4))).date()} — overlays on deployed cfg "
     f"`{json.dumps(cfg)}`, prev-day signals, switch costs not modeled. "
     "GFC leg is survivor-only (optimistic)._\n",
     "## Modern window: $100K on 2020-01-02 -> today",
     "| Variant | Final | CAGR | MaxDD | 2020 | 2022 | Avg expo |", "|--|--|--|--|--|--|--|"]
for name, (m, eq) in modern.items():
    L.append(f"| {name} | **${eq.iloc[-1]:,.0f}** | {m['cagr']:+.1f}% | {m['maxdd']:.1f}% | "
             f"{m['yearly'].get(2020,0):+.1f}% | {m['yearly'].get(2022,0):+.1f}% | {m['avg_expo']:.0f}% |")
L += ["", "## GFC window: 2007-01 -> 2010-06 (survivor-only)",
      "| Variant | Total | MaxDD | 2008 | Avg expo |", "|--|--|--|--|--|"]
for name, (m, eq) in gfc.items():
    L.append(f"| {name} | {m['total']:+.1f}% | {m['maxdd']:.1f}% | "
             f"{m['yearly'].get(2008,0):+.1f}% | {m['avg_expo']:.0f}% |")
open(f"{OUT}/crash_protect.md", "w").write("\n".join(L) + "\n")
json.dump({w: {n: {k: m[k] for k in ("total","cagr","sharpe","maxdd","yearly","avg_expo")}
               for n, (m, _) in rows.items()}
           for w, rows in [("modern", modern), ("gfc", gfc)]},
          open(f"{OUT}/crash_protect.json", "w"), indent=1, default=str)
log(f"report -> {OUT}/crash_protect.md")
