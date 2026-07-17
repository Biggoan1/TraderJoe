#!/usr/bin/env python3
"""
Regime kill-switch validation.
Overlay: hold the winner's book only when SPY > its 200-day MA; otherwise flat to
cash (0 daily return). Signal uses the PRIOR trading day's close vs its 200DMA
(no lookahead). Re-runs all rolling windows baseline-vs-switched to prove the
switch tames the 2022 bear drawdown while keeping bull-market upside.
"""
import os
from datetime import datetime, timedelta, timezone
import numpy as np
import yfinance as yf
from strategy.portfolio_simulator import PortfolioSimulatorConfig
from strategy.portfolio_simulator_cli import run_simulation

ROOT = "/root/hermes-trader"
REPORT = os.path.join(ROOT, "reports", "strategy_search", "kill_switch.md")
SYMS = ["AAPL","AMD","AMZN","GOOGL","META","MSFT","NVDA","TSLA"]
CFG = dict(max_open_positions=6, position_size_pct=0.2, top_n_per_event=2)
WH_END = datetime(2026, 7, 4)
TRAIN = ("2025-07-01", "2026-07-04")

def log(m):
    ts = datetime.now(timezone(timedelta(hours=-4))).strftime("%H:%M:%S")
    print(f"[{ts}] {m}", flush=True)

# ---- SPY 200DMA regime signal (prior-day, no lookahead) ----
log("Downloading SPY for 200DMA regime signal...")
spy = yf.download("SPY", start="2015-06-01", end="2026-07-06", progress=False, auto_adjust=True)["Close"].squeeze()
spy.index = [str(d)[:10] for d in spy.index]
sma200 = spy.rolling(200).mean()
regime_on = {}          # date -> bool (invested that day's close?)
for d in spy.index:
    v, s = float(spy[d]), float(sma200[d]) if not np.isnan(sma200[d]) else None
    regime_on[d] = True if s is None else (v > s)
spy_dates = list(spy.index)

def prior_signal(date):
    """regime as of the last trading day strictly before `date` (tradeable)."""
    import bisect
    i = bisect.bisect_left(spy_dates, date)
    if i == 0:
        return True
    return regime_on.get(spy_dates[i - 1], True)

def metrics(eq):
    eq = np.asarray(eq, float)
    ret = (eq[-1] / eq[0] - 1) * 100
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min() * -100
    r = np.diff(eq) / eq[:-1]
    sharpe = (r.mean() / r.std() * (252 ** 0.5)) if r.std() > 0 else 0.0
    return ret, dd, sharpe

def run_window(start, end):
    cfg = PortfolioSimulatorConfig(**CFG)
    res = run_simulation("volatility_regime_filter_v1", SYMS, start, end, config=cfg)
    dates = [ep.timestamp[:10] for ep in res.equity_curve]
    eq = np.array([ep.equity for ep in res.equity_curve], float)
    if len(eq) < 5:
        return None
    base = metrics(eq)
    # apply overlay to daily returns
    r = np.diff(eq) / eq[:-1]
    on = np.array([prior_signal(dates[i]) for i in range(1, len(dates))])
    sr = np.where(on, r, 0.0)
    seq = np.concatenate([[eq[0]], eq[0] * np.cumprod(1 + sr)])
    sw = metrics(seq)
    pct_in = 100.0 * on.mean()
    return {"start": start, "end": end, "base": base, "sw": sw, "pct_in": pct_in}

# ---- windows: rolling 1y quarterly + training ----
windows = []
s = datetime(2016, 7, 1)
while s + timedelta(days=365) <= WH_END:
    e = s + timedelta(days=365)
    windows.append((s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
    s += timedelta(days=91)
if TRAIN not in windows:
    windows.append(TRAIN)

rows = []
for st, en in windows:
    try:
        r = run_window(st, en)
    except Exception as ex:
        log(f"  {st}..{en} SKIP ({type(ex).__name__}: {str(ex)[:60]})")
        continue
    if not r:
        continue
    rows.append(r)
    b, w = r["base"], r["sw"]
    log(f"  {st}..{en}  base ret={b[0]:+7.2f}% dd={b[1]:5.2f}%  ->  switch ret={w[0]:+7.2f}% "
        f"dd={w[1]:5.2f}% sharpe={w[2]:+5.2f}  in-mkt {r['pct_in']:.0f}%")

# ---- report ----
def stats(key_ret, key_dd):
    rr = [r[key_ret][0] for r in rows if (r["start"], r["end"]) != TRAIN]
    dd = [r[key_dd][1] for r in rows if (r["start"], r["end"]) != TRAIN]
    return rr, dd
brr, bdd = stats("base", "base")
wrr, wdd = stats("sw", "sw")
bears = [r for r in rows if r["start"] >= "2021-06-01" and r["end"] <= "2023-06-30"]
analogue = next((r for r in rows if r["start"] == "2022-12-23"), None)

L = ["# Regime Kill-Switch Validation (SPY > 200DMA, else cash)",
     f"_Winner: volatility_regime_filter_v1 on {','.join(SYMS)} — {CFG}_",
     f"_Generated {datetime.now(timezone(timedelta(hours=-4))).isoformat()}; {len(rows)} windows_\n",
     "## Out-of-sample summary (37 rolling windows, excl. training)",
     f"| | Baseline | With kill-switch |",
     f"|--|--|--|",
     f"| Median return | {np.median(brr):+.2f}% | {np.median(wrr):+.2f}% |",
     f"| Mean return | {np.mean(brr):+.2f}% | {np.mean(wrr):+.2f}% |",
     f"| Worst return | {min(brr):+.2f}% | {min(wrr):+.2f}% |",
     f"| Worst drawdown | {max(bdd):.2f}% | {max(wdd):.2f}% |",
     f"| Median drawdown | {np.median(bdd):.2f}% | {np.median(wdd):.2f}% |",
     f"| Profitable windows | {sum(1 for x in brr if x>0)}/{len(brr)} | {sum(1 for x in wrr if x>0)}/{len(wrr)} |",
     ""]
L += ["## 💀→🛡️ The 2022 bear (the windows that killed the baseline)",
      "| Window | Base Ret | Base DD | Switch Ret | Switch DD | In-market |",
      "|--|--|--|--|--|--|"]
for r in bears:
    L.append(f"| {r['start']}..{r['end']} | {r['base'][0]:+.2f}% | {r['base'][1]:.2f}% | "
             f"**{r['sw'][0]:+.2f}%** | **{r['sw'][1]:.2f}%** | {r['pct_in']:.0f}% |")
if analogue:
    a = analogue
    L += ["", "## Regime analogue to now (2022-12..2023-12, bull)",
          f"- Baseline: ret {a['base'][0]:+.2f}%, dd {a['base'][1]:.2f}%",
          f"- Kill-switch: ret {a['sw'][0]:+.2f}%, dd {a['sw'][1]:.2f}%, in-market {a['pct_in']:.0f}% "
          f"(switch should barely trigger in a bull — upside preserved)"]
tr = next((r for r in rows if (r["start"], r["end"]) == TRAIN), None)
if tr:
    L += ["", "## Training / 'now' window",
          f"- Baseline: ret {tr['base'][0]:+.2f}%, dd {tr['base'][1]:.2f}%",
          f"- Kill-switch: ret {tr['sw'][0]:+.2f}%, dd {tr['sw'][1]:.2f}%, in-market {tr['pct_in']:.0f}%"]
L += ["", "## All windows", "| Window | Base Ret | Base DD | Switch Ret | Switch DD | Sharpe | In-mkt |",
      "|--|--|--|--|--|--|--|"]
for r in rows:
    tag = " (now)" if (r["start"], r["end"]) == TRAIN else ""
    L.append(f"| {r['start']}..{r['end']}{tag} | {r['base'][0]:+.2f}% | {r['base'][1]:.2f}% | "
             f"{r['sw'][0]:+.2f}% | {r['sw'][1]:.2f}% | {r['sw'][2]:+.2f} | {r['pct_in']:.0f}% |")
open(REPORT, "w").write("\n".join(L) + "\n")
log(f"=== DONE. Report -> {REPORT} ===")
log(f"WORST DD: baseline {max(bdd):.1f}%  ->  kill-switch {max(wdd):.1f}%")
log(f"WORST RET: baseline {min(brr):+.1f}%  ->  kill-switch {min(wrr):+.1f}%")
if tr:
    log(f"NOW window: baseline {tr['base'][0]:+.1f}%  ->  switch {tr['sw'][0]:+.1f}% "
        f"(in-market {tr['pct_in']:.0f}%)")
