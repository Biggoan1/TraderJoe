#!/usr/bin/env python3
"""
LIVE paper executor: whole-market factor strategy + FAST RE-ENTRY crash rule,
trading research paper account PA3POVULKVSB (John's call 2026-07-13 -- this
account also carries the data subscription; research-only doctrine amended to
allow this one strategy).

Rules mirror crash_protect_test.py / the backtest overlay:
  - regime OUT iff SPY < 200DMA AND SPY < 20DMA at last close (warehouse data,
    refreshed nightly at 18:00); otherwise IN
  - naked book = top-K by the deployed factor config, re-ranked every REB
    trading days; the rebalance clock keeps ticking while OUT
  - IN: hold the book equal-weight; OUT: 100% cash
  - trades ONLY on rebalance day, regime flip, or first run -- weights drift
    freely in between, same as the backtest

Scheduled 9:35 ET weekdays (market open -> immediate fills). Run after hours,
orders queue for the next open (used for the initial seed).
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
import pandas as pd

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices
from factor_challenger import current_topk

ROOT = "/root/hermes-trader"
OUT = f"{ROOT}/reports/strategy_search"
STATE = f"{OUT}/fastre_state.json"
ACCOUNT = "PA3POVULKVSB"
MIN_DELTA = 100.0          # ignore drift below this on reconcile days
INVEST_FRAC = 0.995        # leave a sliver of cash for rounding

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)

def envf(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d

R = envf(f"{ROOT}/.env.research")
KEY, SEC = R["RESEARCH_ALPACA_API_KEY"], R["RESEARCH_ALPACA_SECRET_KEY"]
BASE = "https://paper-api.alpaca.markets"

def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method,
        headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC,
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        log(f"  API {method} {path} -> {e.code}: {e.read().decode()[:200]}")
        return None

acct = api("/v2/account")
if not acct or acct["account_number"] != ACCOUNT:
    log(f"SAFETY STOP: expected {ACCOUNT}, got {acct and acct.get('account_number')}")
    sys.exit(1)
equity = float(acct["equity"])
log(f"account {ACCOUNT} equity ${equity:,.2f}")

cfg = json.load(open(f"{OUT}/factor_deploy_config.json"))
C, V = load_matrices()
last = C.index[-1]
spy = C["SPY"].dropna()
below_both = bool(spy.iloc[-1] < spy.rolling(200).mean().iloc[-1]
                  and spy.iloc[-1] < spy.rolling(20).mean().iloc[-1])
regime = "OUT" if below_both else "IN"
log(f"data through {last.date()}; regime {regime} (SPY {spy.iloc[-1]:.2f} vs "
    f"200DMA {spy.rolling(200).mean().iloc[-1]:.2f} / 20DMA {spy.rolling(20).mean().iloc[-1]:.2f})")

st = json.load(open(STATE)) if os.path.exists(STATE) else None
first_run = st is None
elapsed = int((C.index > pd.Timestamp(st["last_reb"])).sum()) if st else 0
need_reb = first_run or elapsed >= cfg["reb_days"]
if need_reb:
    book = current_topk(C, V, cfg)
    st = {"last_reb": str(last.date()), "book": book, "regime": regime}
    log(f"REBALANCE: new top-{cfg['K']}: {', '.join(book)}")
else:
    book = st["book"]
    log(f"book unchanged ({elapsed}/{cfg['reb_days']} trading days since {st['last_reb']})")

flip = (not first_run) and st.get("regime") != regime
if not (need_reb or flip):
    st["regime"] = regime
    json.dump(st, open(STATE, "w"))
    log("no action (mid-cycle, regime unchanged)")
    sys.exit(0)

targets = {} if regime == "OUT" else {s: equity * INVEST_FRAC / cfg["K"] for s in book}
pos = {p["symbol"]: float(p["market_value"]) for p in (api("/v2/positions") or [])}
sells, buys = [], []
for sym, mv in pos.items():
    tgt = targets.get(sym, 0.0)
    if tgt == 0.0:
        sells.append((sym, None))                    # close whole position
    elif mv - tgt > MIN_DELTA:
        sells.append((sym, mv - tgt))
for sym, tgt in targets.items():
    if tgt - pos.get(sym, 0.0) > MIN_DELTA:
        buys.append((sym, tgt - pos.get(sym, 0.0)))

clock = api("/v2/clock")
log(f"market open: {clock['is_open']}; placing {len(sells)} sells, {len(buys)} buys")

DATA = "https://data.alpaca.markets"

def live_px(sym):
    for feed in ("sip", "iex"):
        req = urllib.request.Request(f"{DATA}/v2/stocks/{sym}/trades/latest?feed={feed}",
            headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=20))
            if r and r.get("trade"):
                return float(r["trade"]["p"])
        except urllib.error.HTTPError:
            continue
    return None

def submit(sym, side, amount):
    # Marketable LIMIT (whole shares -- limit orders can't be notional). Anchored to
    # the live print with a 0.5% cap so a gapped/running name won't fill at the top;
    # falls back to the last warehouse close if live data is unavailable.
    px = live_px(sym) or float(C[sym].dropna().iloc[-1])
    limit = round(px * 1.005, 2) if side == "buy" else round(px * 0.995, 2)
    q = int(amount // px)
    if q < 1:
        log(f"  skip {side} {sym} ${amount:,.0f} (<1 share at ${px:.2f})")
        return
    o = api("/v2/orders", "POST", {"symbol": sym, "side": side, "type": "limit",
                                   "limit_price": f"{limit:.2f}", "time_in_force": "day",
                                   "qty": str(q)})
    if o:
        log(f"  {side:4} {sym:6} {q}sh @ lmt {limit:.2f} (${amount:,.0f}) id={o['id'][:8]}")

for sym, amt in sells:
    if amt is None:
        r = api(f"/v2/positions/{sym}", "DELETE")
        log(f"  CLOSE {sym} {'ok' if r is not None else 'FAILED'}")
    else:
        submit(sym, "sell", amt)
if sells and clock["is_open"]:
    time.sleep(10)                                   # let sells fill before buying
for sym, amt in buys:
    submit(sym, "buy", amt)

st["regime"] = regime
json.dump(st, open(STATE, "w"))
log(f"done: regime {regime}, book {len(book)} names")
