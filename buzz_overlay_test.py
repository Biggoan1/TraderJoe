#!/usr/bin/env python3
"""
Frenzy-exclusion overlay test: does excluding media-frenzy names from the factor
strategy's selection help or hurt? Frenzy(T,W) = symbol had a buzz spike (>=5
articles AND ratio >= T x trailing-14d baseline) within the last W calendar days,
mask shifted +1 day (no lookahead). Window 2019-06..2026-07 (news data coverage).
Kill-switch OFF here to isolate the overlay effect.
"""
import glob, json, sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices, run

OUT = "/root/hermes-trader/reports/strategy_search"
START = "2019-06-01"
MIN_N = 5

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

# ---- buzz ratio matrix (calendar-daily) ----
frames = [pd.read_csv(f, names=["date", "symbol", "n"]) for f in sorted(glob.glob(f"{OUT}/buzz_hist/*.csv"))]
counts = pd.concat(frames)
counts = counts[counts.symbol != "_NONE_"]
counts["date"] = pd.to_datetime(counts.date)
N = counts.pivot_table(index="date", columns="symbol", values="n", aggfunc="sum").asfreq("D").fillna(0.0)
RATIO = N / N.rolling(14).mean().shift(1).clip(lower=0.5)
SPIKE = (N >= MIN_N) & (RATIO >= 2.0)          # threshold applied per-variant below

def frenzy_mask(T, W):
    sp = (N >= MIN_N) & (RATIO >= T)
    return sp.rolling(W).max().fillna(0).astype(bool).shift(1).fillna(False)

log("loading adjusted price matrices...")
C, V = load_matrices()
log(f"prices {C.shape}; news {N.shape}")

CONFIGS = [
    ("mom blend K=20",  [0.5, 0.3, 0.2, 0, 0], 20, 21, 600),
    ("DEPLOYED m12/vg K=25", [0.8, 0, 0, 0, 0.2], 25, 42, 700),
    ("full blend K=50", [0.4, 0.2, 0.1, 0.2, 0.2], 50, 21, 600),
]
VARIANTS = [("no filter", None, None), ("T=2x W=5d", 2.0, 5), ("T=3x W=5d", 3.0, 5),
            ("T=2x W=10d", 2.0, 10), ("T=3x W=10d", 3.0, 10)]

L = ["# Frenzy-Exclusion Overlay Test (2019-06..2026-07, adjusted prices, no kill-switch)",
     f"_Frenzy(T,W) = buzz spike (>= {MIN_N} articles AND ratio >= T) within last W days; mask shifted +1d._\n",
     "| Config | Variant | CAGR | Sharpe | MaxDD | ΔCAGR vs base |",
     "|--|--|--|--|--|--|"]
results = {}
for cname, w, K, reb, liq in CONFIGS:
    base_cagr = None
    for vname, T, W in VARIANTS:
        ex = frenzy_mask(T, W) if T else None
        m, _ = run(C, V, w, K=K, REB=reb, LIQ_TOP=liq, kill_switch=False, start=START, exclude=ex)
        if base_cagr is None:
            base_cagr = m["cagr_pct"]
        d = m["cagr_pct"] - base_cagr
        results[(cname, vname)] = m
        L.append(f"| {cname} | {vname} | {m['cagr_pct']:+.2f}% | {m['sharpe']:+.2f} | "
                 f"{m['max_drawdown_pct']:.1f}% | {d:+.2f}pp |")
        log(f"{cname:24} {vname:12} cagr={m['cagr_pct']:+7.2f}% sh={m['sharpe']:+.2f} dd={m['max_drawdown_pct']:4.1f}% Δ={d:+.2f}pp")

# how big is the frenzy set on average?
L += ["", "## Frenzy-set size (avg symbols excluded per day)"]
for vname, T, W in VARIANTS[1:]:
    mask = frenzy_mask(T, W)
    L.append(f"- {vname}: avg {mask.sum(axis=1).mean():.0f} names excluded")
open(f"{OUT}/buzz_overlay.md", "w").write("\n".join(L) + "\n")
json.dump({f"{c}|{v}": {k: m[k] for k in ('cagr_pct','sharpe','max_drawdown_pct')}
           for (c, v), m in results.items()}, open(f"{OUT}/buzz_overlay.json", "w"), indent=1, default=str)
log(f"report -> {OUT}/buzz_overlay.md")
