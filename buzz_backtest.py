#!/usr/bin/env python3
"""
Buzz-factor backtest (2019-2026) on SPLIT-ADJUSTED prices.
Signal: per-symbol daily article count vs trailing 14d baseline (buzz ratio).
Entry: NEXT trading day's close after the buzz day (no lookahead, misses same-day pop
on purpose). Outcomes: 1d/5d/21d forward returns MINUS the universe equal-weight mean
(excess). Cuts: buzz deciles, up-buzz vs down-buzz (sign of buzz-day return), yearly
stability. Plus a tradeable sim: top-10 up-buzz names daily, 5d overlapping tranches,
10bps/side.
"""
import os, glob, json
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

ROOT = "/root/hermes-trader"
OUT = f"{ROOT}/reports/strategy_search"
ADJ = f"{ROOT}/market_data/equities/daily/fafo-market-adj-2016"
MIN_N, BASE_DAYS, RATIO_MIN = 5, 14, 2.0

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

# ---- news counts ----
frames = []
for f in sorted(glob.glob(f"{OUT}/buzz_hist/*.csv")):
    frames.append(pd.read_csv(f, names=["date", "symbol", "n"]))
counts = pd.concat(frames)
counts = counts[counts.symbol != "_NONE_"]
counts["date"] = pd.to_datetime(counts.date)
N = counts.pivot_table(index="date", columns="symbol", values="n", aggfunc="sum")
N = N.asfreq("D").fillna(0.0)   # calendar-daily, missing = 0 articles
log(f"news matrix: {N.shape[0]} days x {N.shape[1]} symbols, {int(N.values.sum())} articles")

# ---- adjusted prices ----
closes = {}
uni = set(json.load(open(f"{OUT}/market_universe.json")))
for sym in sorted(os.listdir(ADJ)):
    if sym not in uni and sym != "SPY":
        continue
    fs = sorted(glob.glob(f"{ADJ}/{sym}/*.parquet"))
    if not fs: continue
    df = pd.concat([pd.read_parquet(x, columns=["timestamp", "close"]) for x in fs])
    closes[sym] = pd.Series(df["close"].values, index=pd.to_datetime(df["timestamp"].str[:10]))
C = pd.DataFrame(closes).sort_index()
C = C[~C.index.duplicated()]
log(f"price matrix (ADJUSTED): {C.shape[0]} days x {C.shape[1]} symbols")
R1 = C.pct_change()
tdays = C.index

# buzz baseline: trailing 14 calendar days mean (shifted, excludes today)
BASE = N.rolling(BASE_DAYS).mean().shift(1)
RATIO = N / BASE.clip(lower=0.5)

# map calendar buzz day -> next trading day (entry)
def next_tday(d):
    i = tdays.searchsorted(d, side="right")
    return tdays[i] if i < len(tdays) else None

uni_mean = {h: R1.mean(axis=1).rolling(h).sum().shift(-h) for h in (1, 5, 21)}  # fwd uni mean per window
fwd = {h: C.pct_change(h).shift(-h) for h in (1, 5, 21)}                        # fwd sym returns

events = []
for d in N.index:
    if d < pd.Timestamp("2019-02-01"):
        continue
    row_n, row_r = N.loc[d], RATIO.loc[d]
    hits = row_n[(row_n >= MIN_N) & (row_r >= RATIO_MIN)].index
    if not len(hits): continue
    e = next_tday(d)
    if e is None or e not in fwd[21].index: continue
    for s in hits:
        if s not in C.columns or pd.isna(C.loc[e, s]): continue
        signday_ret = R1.loc[e, s] if d not in tdays else R1.loc[d, s] if s in R1.columns else np.nan
        rec = {"date": d, "entry": e, "symbol": s, "n": row_n[s], "ratio": row_r[s],
               "sign": np.sign(signday_ret) if pd.notna(signday_ret) else 0}
        ok = True
        for h in (1, 5, 21):
            v = fwd[h].loc[e, s]
            u = uni_mean[h].loc[e] if e in uni_mean[h].index else np.nan
            if pd.isna(v) or pd.isna(u) or abs(v) > 3: ok = False; break
            rec[f"ex{h}"] = (v - u) * 100
        if ok: events.append(rec)
E = pd.DataFrame(events)
log(f"{len(E)} buzz events (n>={MIN_N}, ratio>={RATIO_MIN})")

L = ["# Buzz Factor Backtest (2019-2026, split-adjusted prices)",
     f"_Events: article count >= {MIN_N} AND ratio >= {RATIO_MIN}x trailing {BASE_DAYS}d baseline. "
     f"Entry = next trading day close (no lookahead). Excess vs universe mean. {len(E)} events._\n"]
def block(title, df):
    rows = [f"## {title}", "| Cut | Events | ex1d | ex5d | ex21d | 5d win% |", "|--|--|--|--|--|--|"]
    for name, g in df:
        if len(g) < 30: continue
        rows.append(f"| {name} | {len(g)} | {g.ex1.mean() if 'ex1' in g else g['ex1'].mean():+.2f}% | "
                    f"{g['ex5'].mean():+.2f}% | {g['ex21'].mean():+.2f}% | {(g['ex5']>0).mean()*100:.0f}% |")
    return rows
E["decile"] = pd.qcut(E["ratio"], 5, labels=["Q1(low)","Q2","Q3","Q4","Q5(high)"], duplicates="drop")
L += block("All events by buzz quintile", E.groupby("decile", observed=True))
L += block("Up-buzz vs down-buzz (buzz-day return sign)",
           E.assign(k=np.where(E["sign"]>0,"UP day",np.where(E["sign"]<0,"DOWN day","flat"))).groupby("k"))
L += block("Yearly (all events)", E.groupby(E["date"].dt.year))
L += block("Yearly — UP-buzz only", E[E["sign"]>0].groupby(E[E["sign"]>0]["date"].dt.year))

# ---- tradeable sim: top-10 up-buzz daily, hold 5d in overlapping tranches ----
log("running tradeable sim...")
cost = 10/1e4
tranche_rets = {}   # entry tday -> portfolio 5d return net
byday = E[(E["sign"] > 0)].groupby("entry")
for e, g in byday:
    top = g.sort_values("ratio", ascending=False).head(10)
    gross = (fwd[5].loc[e, top.symbol].mean())
    if pd.notna(gross):
        tranche_rets[e] = gross - 2 * cost
tr = pd.Series(tranche_rets).sort_index()
# 1/5 of capital per daily tranche → daily portfolio return approx = mean of last 5 tranches' 1d slice
eq, dates_l = 1.0, []
vals = []
tr5 = (1 + tr) ** (1/5) - 1   # spread each tranche's 5d return over 5 days (approx)
roll = tr5.rolling(5, min_periods=1).mean()
for d, r in roll.items():
    eq *= (1 + (r if pd.notna(r) else 0)); vals.append((d, eq))
s = pd.Series(dict(vals))
yrs = len(s)/252
r = s.pct_change().dropna()
spy = C["SPY"][C.index >= s.index[0]].dropna()
spy_cagr = ((spy.iloc[-1]/spy.iloc[0])**(1/(len(spy)/252))-1)*100
L += ["", "## Tradeable sim: top-10 up-buzz daily, 5d hold, 10bps/side",
      f"- Active on {len(tr)} of ~{len(tdays)} trading days; CAGR **{((s.iloc[-1])**(1/yrs)-1)*100:+.2f}%**, "
      f"Sharpe {r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0:+.2f}, "
      f"maxDD {((s/s.cummax())-1).min()*-100:.1f}%  (SPY same period CAGR {spy_cagr:+.2f}%)"]
open(f"{OUT}/buzz_backtest.md", "w").write("\n".join(L) + "\n")
E.to_csv(f"{OUT}/buzz_events.csv", index=False)
log(f"report -> {OUT}/buzz_backtest.md")
for line in L[3:20]: print(line, flush=True)
