#!/usr/bin/env python3
"""
Refine the regime kill-switch to cut whipsaw.
Backtest the winner once per window (cache daily returns), then sweep regime-filter
variants: MA length x hysteresis band x confirmation days. A hysteresis band + a
confirm delay reduce flip-flopping around the 200DMA line. Rank variants on the
tradeoff: recover median return without giving back downside protection.
"""
import os
from datetime import datetime, timedelta, timezone
import numpy as np
import yfinance as yf
from strategy.portfolio_simulator import PortfolioSimulatorConfig
from strategy.portfolio_simulator_cli import run_simulation

ROOT = "/root/hermes-trader"
REPORT = os.path.join(ROOT, "reports", "strategy_search", "refine.md")
SYMS = ["AAPL","AMD","AMZN","GOOGL","META","MSFT","NVDA","TSLA"]
CFG = dict(max_open_positions=6, position_size_pct=0.2, top_n_per_event=2)
WH_END = datetime(2026, 7, 4)
TRAIN = ("2025-07-01", "2026-07-04")
BEAR = ("2021-12-24", "2022-12-24")   # the worst baseline window

def log(m):
    ts = datetime.now(timezone(timedelta(hours=-4))).strftime("%H:%M:%S")
    print(f"[{ts}] {m}", flush=True)

# ---- SPY history + MAs ----
log("Downloading SPY...")
spy = yf.download("SPY", start="2015-01-01", end="2026-07-06", progress=False, auto_adjust=True)["Close"].squeeze()
spy.index = [str(d)[:10] for d in spy.index]
MAS = {n: spy.rolling(n).mean() for n in (100, 150, 200)}
SPY_DATES = list(spy.index)

def invested_state(ma_len, band, confirm):
    """State machine over full SPY history: invested unless SPY < MA*(1-band) held
    `confirm` days; re-enter when SPY > MA*(1+band) held `confirm` days."""
    ma = MAS[ma_len]
    state = {}
    invested = True
    below_run = above_run = 0
    for d in SPY_DATES:
        m = ma[d]
        if np.isnan(m):
            state[d] = True
            continue
        px = float(spy[d])
        below = px < m * (1 - band)
        above = px > m * (1 + band)
        below_run = below_run + 1 if below else 0
        above_run = above_run + 1 if above else 0
        if invested and below_run >= max(1, confirm):
            invested = False
        elif (not invested) and above_run >= max(1, confirm):
            invested = True
        state[d] = invested
    return state

def prior(state, date):
    import bisect
    i = bisect.bisect_left(SPY_DATES, date)
    return state.get(SPY_DATES[i - 1], True) if i > 0 else True

def wmetrics(eq):
    eq = np.asarray(eq, float)
    ret = (eq[-1] / eq[0] - 1) * 100
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min() * -100
    r = np.diff(eq) / eq[:-1]
    sh = (r.mean() / r.std() * (252 ** 0.5)) if r.std() > 0 else 0.0
    return ret, dd, sh

# ---- rolling windows ----
windows = []
s = datetime(2016, 7, 1)
while s + timedelta(days=365) <= WH_END:
    e = s + timedelta(days=365)
    windows.append((s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
    s += timedelta(days=91)
if TRAIN not in windows:
    windows.append(TRAIN)

# ---- run each window ONCE, cache daily equity + dates ----
cache = {}
log(f"Backtesting {len(windows)} windows once (caching returns)...")
for st, en in windows:
    try:
        res = run_simulation("volatility_regime_filter_v1", SYMS, st, en,
                             config=PortfolioSimulatorConfig(**CFG))
    except Exception as ex:
        log(f"  {st}..{en} SKIP ({type(ex).__name__})")
        continue
    dates = [ep.timestamp[:10] for ep in res.equity_curve]
    eq = np.array([ep.equity for ep in res.equity_curve], float)
    if len(eq) >= 5:
        cache[(st, en)] = (dates, eq)
log(f"cached {len(cache)} windows")

# ---- variant sweep ----
VARIANTS = [
    ("no-switch",           None, 0.00, 0),
    ("200d plain",          200,  0.00, 0),
    ("200d +2% band",       200,  0.02, 0),
    ("200d +3% band",       200,  0.03, 0),
    ("200d +2%b +2d",       200,  0.02, 2),
    ("200d +3%b +3d",       200,  0.03, 3),
    ("150d +2% band",       150,  0.02, 0),
    ("150d +2%b +2d",       150,  0.02, 2),
    ("150d +3%b +3d",       150,  0.03, 3),
    ("100d +2%b +2d",       100,  0.02, 2),
]

def apply_variant(state, dates, eq):
    r = np.diff(eq) / eq[:-1]
    if state is None:
        on = np.ones(len(r), bool)
    else:
        on = np.array([prior(state, dates[i]) for i in range(1, len(dates))])
    sr = np.where(on, r, 0.0)
    seq = np.concatenate([[eq[0]], eq[0] * np.cumprod(1 + sr)])
    return wmetrics(seq), 100.0 * on.mean()

results = []
for name, ma, band, conf in VARIANTS:
    state = None if ma is None else invested_state(ma, band, conf)
    per = {}
    for k, (dates, eq) in cache.items():
        m, inmkt = apply_variant(state, dates, eq)
        per[k] = (m, inmkt)
    oos = [per[k] for k in per if k != TRAIN]
    rets = [x[0][0] for x in oos]
    dds = [x[0][1] for x in oos]
    row = {
        "name": name,
        "med_ret": float(np.median(rets)), "mean_ret": float(np.mean(rets)),
        "worst_ret": float(min(rets)), "worst_dd": float(max(dds)),
        "med_dd": float(np.median(dds)),
        "prof": sum(1 for x in rets if x > 0), "n": len(rets),
        "bear": per.get(BEAR, ((None, None, None), None))[0],
        "now": per.get(TRAIN, ((None,), None))[0][0],
        "now_inmkt": per.get(TRAIN, (None, None))[1],
        "avg_inmkt": float(np.mean([x[1] for x in oos])),
    }
    # robust score: reward median+mean return, penalize deep worst-DD and deep worst-ret
    row["score"] = round(0.5 * row["med_ret"] + 0.5 * row["mean_ret"]
                         - 0.25 * max(0, row["worst_dd"] - 15)
                         - 0.5 * max(0, -row["worst_ret"] - 12), 2)
    results.append(row)

results.sort(key=lambda r: r["score"], reverse=True)

L = ["# Kill-Switch Refinement — variant sweep",
     f"_Winner strategy on 8 megacaps; 37 OOS rolling windows 2016-2026; hysteresis band + confirm-days to cut whipsaw_",
     f"_score = 0.5*medRet + 0.5*meanRet - 0.25*max(0,worstDD-15) - 0.5*max(0,-worstRet-12)_\n",
     "| Variant | MedRet | MeanRet | WorstRet | WorstDD | MedDD | Prof | 2022 Ret/DD | NOW Ret | InMkt | Score |",
     "|--|--|--|--|--|--|--|--|--|--|--|"]
for r in results:
    b = r["bear"]
    bear_s = f"{b[0]:+.0f}%/{b[1]:.0f}%" if b[0] is not None else "-"
    L.append(f"| {r['name']} | {r['med_ret']:+.2f}% | {r['mean_ret']:+.2f}% | {r['worst_ret']:+.2f}% | "
             f"{r['worst_dd']:.1f}% | {r['med_dd']:.1f}% | {r['prof']}/{r['n']} | {bear_s} | "
             f"{r['now']:+.1f}% | {r['avg_inmkt']:.0f}% | **{r['score']:+.2f}** |")
open(REPORT, "w").write("\n".join(L) + "\n")
log("=== DONE. Ranked variants ===")
for r in results[:5]:
    log(f"  {r['name']:<16} score={r['score']:+6.2f} medRet={r['med_ret']:+6.2f}% "
        f"worstDD={r['worst_dd']:5.1f}% worstRet={r['worst_ret']:+6.1f}% NOW={r['now']:+.1f}%")
best = results[0]
log(f"BEST: {best['name']} — medRet {best['med_ret']:+.2f}%, worstDD {best['worst_dd']:.1f}%, "
    f"worstRet {best['worst_ret']:+.1f}%, NOW {best['now']:+.1f}% (in-mkt {best['now_inmkt']:.0f}%)")
