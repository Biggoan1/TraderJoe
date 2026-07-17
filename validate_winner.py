#!/usr/bin/env python3
"""
Walk-forward / out-of-sample validation of the strategy-search winner, plus a
regime-analogue test: find the past 1-year window whose market regime (SPY
return/vol/drawdown + VIX) most resembles the training window ("now"), and check
how the winner performs there — a true out-of-sample test in a similar regime.
"""
import json, subprocess, os, re
from datetime import datetime, timedelta, timezone
import numpy as np
import yfinance as yf

ROOT = "/root/hermes-trader"
OUT = os.path.join(ROOT, "reports", "strategy_search")
REPORT = os.path.join(OUT, "validation.md")

WINNER = {"strategy": "volatility_regime_filter_v1",
          "symbols": ["AAPL","AMD","AMZN","GOOGL","META","MSFT","NVDA","TSLA"],
          "max_open_positions": 6, "position_size_pct": 0.2, "top_n_per_event": 2}
WH_END = datetime(2026, 7, 4)
TRAIN = ("2025-07-01", "2026-07-04")     # the window the winner was selected on = "now"

def log(m):
    ts = datetime.now(timezone(timedelta(hours=-4))).strftime("%H:%M:%S")
    print(f"[{ts}] {m}", flush=True)

def score(start, end):
    cmd = [f"{ROOT}/.venv/bin/python", "-m", "strategy.portfolio_simulator_main",
           "--strategy", WINNER["strategy"], "--symbols", *WINNER["symbols"],
           "--start", start, "--end", end,
           "--max-open-positions", str(WINNER["max_open_positions"]),
           "--position-size-pct", str(WINNER["position_size_pct"]),
           "--top-n-per-event", str(WINNER["top_n_per_event"]),
           "--json", "--no-write"]
    env = dict(os.environ, HERMES_CONTEXT="simulator")
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    out = p.stdout.strip()
    if not out:
        return {"error": "nodata"}
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return {"error": "parse"}
        d = json.loads(m.group(0))
    return d.get("metrics", d)

# ---- rolling 1y windows, quarterly step ----
windows = []
s = datetime(2016, 7, 1)
while s + timedelta(days=365) <= WH_END:
    e = s + timedelta(days=365)
    windows.append((s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
    s += timedelta(days=91)
if TRAIN not in windows:
    windows.append(TRAIN)

# ---- regime fingerprints from SPY + VIX ----
log("Downloading SPY + ^VIX for regime fingerprints...")
def series(tkr):
    df = yf.download(tkr, start="2016-01-01", end="2026-07-06", progress=False, auto_adjust=True)
    c = df["Close"]
    return c.squeeze()
spy = series("SPY")
try:
    vix = series("^VIX")
except Exception:
    vix = None

def fingerprint(start, end):
    px = spy.loc[start:end].dropna().values.astype(float).flatten()
    if len(px) < 50:
        return None
    ret = (px[-1] / px[0] - 1) * 100
    dr = np.diff(px) / px[:-1]
    vol = dr.std() * (252 ** 0.5) * 100
    peak = np.maximum.accumulate(px)
    dd = ((px - peak) / peak).min() * 100
    vx = float("nan")
    if vix is not None:
        v = vix.loc[start:end].dropna().values.astype(float).flatten()
        if len(v):
            vx = float(v.mean())
    return {"spy_ret": ret, "spy_vol": vol, "spy_dd": dd, "vix": vx}

# ---- run all windows ----
rows = []
for (st, en) in windows:
    fp = fingerprint(st, en)
    m = score(st, en)
    rows.append({"start": st, "end": en, "fp": fp, "m": m})
    if "error" in m or not fp:
        log(f"  {st}..{en}  SKIP ({m.get('error','no-regime')})")
    else:
        log(f"  {st}..{en}  ret={m['total_return_pct']:+6.2f}% dd={m['max_drawdown_pct']:5.2f}% "
            f"sharpe={m['sharpe']:+5.2f} | SPY {fp['spy_ret']:+6.1f}% vol{fp['spy_vol']:4.1f} vix{fp['vix']:4.1f}")

valid = [r for r in rows if r["fp"] and "error" not in r["m"]]

# ---- regime similarity to "now" (TRAIN) ----
train_row = next((r for r in rows if (r["start"], r["end"]) == TRAIN and r["fp"]), None)
feat_keys = ["spy_ret", "spy_vol", "spy_dd", "vix"]
def vec(fp):
    return np.array([fp[k] for k in feat_keys], float)
if train_row and valid:
    mat = np.array([vec(r["fp"]) for r in valid])
    mu, sd = mat.mean(0), mat.std(0)
    sd[sd == 0] = 1.0
    tz = (vec(train_row["fp"]) - mu) / sd
    for r in valid:
        z = (vec(r["fp"]) - mu) / sd
        r["dist"] = float(np.linalg.norm(z - tz))
    # analogue = closest regime that does NOT overlap the training window
    non_overlap = [r for r in valid if r["end"] < "2025-01-01"]
    analogue = min(non_overlap, key=lambda r: r["dist"]) if non_overlap else None
else:
    analogue = None

# ---- verdict stats over OOS windows (exclude the training window itself) ----
oos = [r for r in valid if (r["start"], r["end"]) != TRAIN]
rets = [r["m"]["total_return_pct"] for r in oos]
dds = [r["m"]["max_drawdown_pct"] for r in oos]
shs = [r["m"]["sharpe"] for r in oos]

lines = ["# Winner Validation — walk-forward + regime analogue",
         f"_Strategy: **{WINNER['strategy']}** on {','.join(WINNER['symbols'])}, "
         f"pos={WINNER['max_open_positions']} size={WINNER['position_size_pct']} top={WINNER['top_n_per_event']}_",
         f"_Generated {datetime.now(timezone(timedelta(hours=-4))).isoformat()}_\n",
         f"In-sample (training / 'now') window {TRAIN[0]}..{TRAIN[1]}: "
         f"ret={train_row['m']['total_return_pct']:+.2f}% dd={train_row['m']['max_drawdown_pct']:.2f}% "
         f"sharpe={train_row['m']['sharpe']:+.2f}\n" if train_row else "",
         "## Out-of-sample summary (all rolling windows except the training one)",
         f"- Windows with data: **{len(oos)}**",
         f"- Return: median **{np.median(rets):+.2f}%**, mean {np.mean(rets):+.2f}%, "
         f"range {min(rets):+.2f}%..{max(rets):+.2f}%" if rets else "- no OOS data",
         f"- Profitable windows: **{sum(1 for x in rets if x>0)}/{len(rets)}** "
         f"({100*sum(1 for x in rets if x>0)/len(rets):.0f}%)" if rets else "",
         f"- Max drawdown: median {np.median(dds):.2f}%, worst {max(dds):.2f}%" if dds else "",
         f"- Sharpe: median **{np.median(shs):+.2f}**, negative in {sum(1 for x in shs if x<0)} windows" if shs else "",
         ""]
if analogue:
    a = analogue
    lines += ["## 🔮 Regime analogue to *now*",
              f"Closest non-overlapping regime to {TRAIN[0]}..{TRAIN[1]}: "
              f"**{a['start']}..{a['end']}** (distance {a['dist']:.2f}).",
              f"- That regime: SPY {a['fp']['spy_ret']:+.1f}%, vol {a['fp']['spy_vol']:.1f}, "
              f"drawdown {a['fp']['spy_dd']:.1f}%, VIX {a['fp']['vix']:.1f}",
              f"- **Winner there: ret={a['m']['total_return_pct']:+.2f}% "
              f"dd={a['m']['max_drawdown_pct']:.2f}% sharpe={a['m']['sharpe']:+.2f} "
              f"trades={a['m']['trade_count']}**", ""]
lines += ["## All windows (chronological)",
          "| Window | Strat Ret% | DD% | Sharpe | Trades | SPY Ret% | SPY Vol | VIX | Dist→now |",
          "|--|--|--|--|--|--|--|--|--|"]
for r in valid:
    m, fp = r["m"], r["fp"]
    tag = " **(now)**" if (r["start"], r["end"]) == TRAIN else ""
    lines.append(f"| {r['start']}..{r['end']}{tag} | {m['total_return_pct']:+.2f} | "
                 f"{m['max_drawdown_pct']:.2f} | {m['sharpe']:+.2f} | {m['trade_count']} | "
                 f"{fp['spy_ret']:+.1f} | {fp['spy_vol']:.1f} | {fp['vix']:.1f} | {r.get('dist',0):.2f} |")
open(REPORT, "w").write("\n".join(lines) + "\n")
log(f"=== DONE. {len(oos)} OOS windows. Report -> {REPORT} ===")
if rets:
    log(f"OOS median ret {np.median(rets):+.2f}%, profitable {sum(1 for x in rets if x>0)}/{len(rets)}, "
        f"median sharpe {np.median(shs):+.2f}")
if analogue:
    log(f"ANALOGUE {analogue['start']}..{analogue['end']}: winner ret "
        f"{analogue['m']['total_return_pct']:+.2f}% sharpe {analogue['m']['sharpe']:+.2f}")
