#!/usr/bin/env python3
"""
GFC survival test: run the whole-market factor strategy through 2007-2010.
Data: yfinance daily 2005-2010 for the current 800-name universe + SPY (Alpaca
starts 2016). HONESTY CAVEAT baked into the report: companies that DIED in the
GFC (Lehman, WaMu, Bear Stearns, ...) are absent -> results are OPTIMISTIC.
Failing even on survivors = strong negative signal; surviving = cautious positive.
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import run, spy_baseline

OUT = "/root/hermes-trader/reports/strategy_search"
def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

uni = json.load(open(f"{OUT}/market_universe.json"))
tickers = sorted(set(uni) | {"SPY"})
log(f"downloading 2005-2010 daily bars for {len(tickers)} tickers via yfinance...")
frames_c, frames_v = [], []
CH = 100
for i in range(0, len(tickers), CH):
    chunk = tickers[i:i+CH]
    try:
        df = yf.download(chunk, start="2005-01-01", end="2010-07-01", progress=False,
                         auto_adjust=True, group_by="column", threads=True)
    except Exception as e:
        log(f"  chunk {i}: {e}"); continue
    if df is None or df.empty:
        continue
    frames_c.append(df["Close"] if "Close" in df else df)
    frames_v.append(df["Volume"] if "Volume" in df else None)
    log(f"  {min(i+CH,len(tickers))}/{len(tickers)}")
    time.sleep(1)

C = pd.concat(frames_c, axis=1)
V = pd.concat([f for f in frames_v if f is not None], axis=1)
C = C.loc[:, ~C.columns.duplicated()].dropna(axis=1, how="all")
V = V.loc[:, ~V.columns.duplicated()].reindex(columns=C.columns).fillna(0.0)
# keep symbols with real 2006 history (need 252d trailing factors by 2007)
has_hist = C.loc[:"2006-06-30"].notna().sum() > 200
C, V = C.loc[:, has_hist], V.loc[:, has_hist]
C.index, V.index = pd.to_datetime(C.index), pd.to_datetime(V.index)
log(f"matrix: {C.shape[0]} days x {C.shape[1]} symbols with pre-2006 history")

spy = spy_baseline(C, start="2007-01-01")
def fmt(name, m):
    yr = " ".join(f"{y}:{v:+.0f}%" for y, v in m["yearly"].items())
    return (f"{name:<30} cagr={m['cagr_pct']:+6.2f}% sharpe={m['sharpe']:+5.2f} "
            f"maxDD={m['max_drawdown_pct']:5.1f}%  [{yr}]")
print(fmt("SPY buy&hold 2007-2010H1", spy), flush=True)

deploy = json.load(open(f"{OUT}/factor_deploy_config.json"))
CONFIGS = [
    ("DEPLOYED cfg (m12/vg .8/.2 K25)", deploy["weights"], deploy["K"], deploy["reb_days"], deploy["liq_top"], False),
    ("DEPLOYED + kill-switch",          deploy["weights"], deploy["K"], deploy["reb_days"], deploy["liq_top"], True),
    ("mom blend K=20",                  [0.5, 0.3, 0.2, 0, 0], 20, 21, 600, False),
    ("full blend K=50 conservative",    [0.4, 0.2, 0.1, 0.2, 0.2], 50, 21, 600, False),
    ("full blend K=50 + kill-switch",   [0.4, 0.2, 0.1, 0.2, 0.2], 50, 21, 600, True),
]
results = {"SPY": spy}
LIQ = min(600, int(C.shape[1] * 0.9))
for name, w, k, reb, liq, ks in CONFIGS:
    m, s = run(C, V, w, K=k, REB=reb, LIQ_TOP=min(liq, LIQ), cost_bps=10.0,
               kill_switch=ks, start="2007-01-01")
    results[name] = m
    print(fmt(name, m), flush=True)

json.dump(results, open(f"{OUT}/gfc_test_results.json", "w"), indent=1, default=str)
log("saved -> reports/strategy_search/gfc_test_results.json")
