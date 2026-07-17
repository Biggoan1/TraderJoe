#!/usr/bin/env python3
"""$100K invested 2020-01-01 -> today under the DEPLOYED rules (whole-market factor,
deployed config, SPY-200DMA kill-switch ON), on split-adjusted data. Vs SPY."""
import sys, json
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices, run, spy_baseline

OUT = "/root/hermes-trader/reports/strategy_search"
START = "2020-01-02"
CASH = 100_000.0

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

C, V = load_matrices()
cfg = json.load(open(f"{OUT}/factor_deploy_config.json"))
log(f"deployed cfg: {cfg}; window {START}..{C.index[-1].date()}")

runs = {}
for name, ks in [("DEPLOYED rules (kill-switch ON)", True), ("same, no kill-switch", False)]:
    m, s = run(C, V, cfg["weights"], K=cfg["K"], REB=cfg["reb_days"], LIQ_TOP=cfg["liq_top"],
               kill_switch=ks, start=START)
    runs[name] = (m, s / s.iloc[0] * CASH)
m50, s50 = run(C, V, [0.4,0.2,0.1,0.2,0.2], K=50, REB=21, LIQ_TOP=600, kill_switch=True, start=START)
runs["conservative K=50 + kill-switch"] = (m50, s50 / s50.iloc[0] * CASH)
spy = C["SPY"][C.index >= START].dropna()
spy_eq = spy / spy.iloc[0] * CASH
spym = spy_baseline(C, start=START)
runs["SPY buy & hold"] = (spym, spy_eq)

L = [f"# $100K invested {START[:10]} -> {C.index[-1].date()} (deployed rules, adjusted data)",
     "| Portfolio | Final value | CAGR | MaxDD | Worst year |", "|--|--|--|--|--|"]
for name, (m, eq) in runs.items():
    worst = min(m["yearly"].items(), key=lambda kv: kv[1])
    L.append(f"| {name} | **${eq.iloc[-1]:,.0f}** | {m['cagr_pct']:+.1f}% | {m['max_drawdown_pct']:.1f}% | "
             f"{worst[0]}: {worst[1]:+.0f}% |")
L += ["", "## Year-by-year, deployed rules ($ at each year-end)"]
name = "DEPLOYED rules (kill-switch ON)"
eq = runs[name][1]
L += ["| Year | Deployed $ | SPY $ |", "|--|--|--|"]
for y, g in eq.groupby(eq.index.year):
    sg = spy_eq[spy_eq.index.year == y]
    L.append(f"| {y} | ${g.iloc[-1]:,.0f} | ${sg.iloc[-1]:,.0f} |")
open(f"{OUT}/hundredk_2020.md", "w").write("\n".join(L) + "\n")
for line in L: print(line, flush=True)
