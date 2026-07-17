#!/usr/bin/env python3
"""
Overnight regime guard for the CHAMPION stock book (account PA322A4X1HC2).

Thesis (verified on SPY 2016-2026, see /srv/brain analysis 2026-07-14):
  Holding SPY overnight is a great trade in a bull regime (+17%/yr ann, worst
  night -4%) and a disaster in a bear regime (-18%/yr, worst night -10.7%).
  Gating overnight exposure by the 200/20-DMA kill-switch turned +112% -> +255%
  cumulative and lifted the overnight Sharpe 0.68 -> 1.75 while cutting the
  worst night to -4%.

Behavior:
  close (run ~15:55 ET): regime OUT -> flatten the book to cash for the night,
                         snapshotting what we sold. regime IN -> hold (no-op).
  open  (run ~09:45 ET): if we flattened last night -> reconcile back to the
                         snapshot book (buy the deltas), then clear state.

This is an OVERLAY. It does not touch trader.py. It reconciles against live
positions on restore, so anything the champion's own scanner did in between is
absorbed, not double-traded.

Safety:
  - Hard-asserts ACCOUNT == expected before any write.
  - GUARD_DRYRUN=1 (default) logs intended orders and sends nothing.
  - --force-regime IN|OUT overrides the computed regime (testing only).
  - --vol-override arms the stormy-vol trigger (default off; the +255% backtest
    was pure 200/20-DMA regime -- vol override is an untested add-on).
"""
import argparse, glob, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = "/root/hermes-trader"
ACCOUNT = "PA322A4X1HC2"                       # champion stock paper account
STATE = f"{ROOT}/reports/overnight_guard_state.json"
LOG = f"{ROOT}/.hermes-profile/logs/overnight_guard.log"
SPY_GLOB = f"{ROOT}/market_data/equities/daily/fafo-universe-2016/SPY/*.parquet"
DUST = 25.0                                    # ignore positions below $25
MIN_DELTA = 50.0                               # ignore restore deltas below $50
VOL_ANNUAL_THRESH = 0.25                       # 25% annualized 20d realized vol
DRYRUN = os.environ.get("GUARD_DRYRUN", "1") != "0"
ET = timezone(timedelta(hours=-4))

def log(m):
    line = f"[{datetime.now(ET):%Y-%m-%d %H:%M:%S}] {'DRY ' if DRYRUN else ''}{m}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

def envf(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d

E = envf(f"{ROOT}/.env")
KEY, SEC = E["ALPACA_API_KEY"], E["ALPACA_SECRET_KEY"]
TRADE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"

def api(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method,
        headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC,
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        log(f"  API {method} {path} -> {e.code}: {e.read().decode()[:200]}")
        return None

def spy_closes():
    import pandas as pd
    files = sorted(glob.glob(SPY_GLOB))
    df = pd.concat([pd.read_parquet(f, columns=["timestamp", "close"]) for f in files])
    df["d"] = pd.to_datetime(df["timestamp"].str[:10])
    s = df.drop_duplicates("d").sort_values("d").set_index("d")["close"]
    return s

def live_spy():
    r = api(DATA, "/v2/stocks/SPY/trades/latest?feed=iex")
    if r and r.get("trade"):
        return float(r["trade"]["p"])
    return None

def compute_regime(force=None, vol_override=False):
    import pandas as pd
    closes = spy_closes()
    px = live_spy()
    if px is None:
        px = float(closes.iloc[-1])
        log(f"  live SPY unavailable; using last warehouse close {px:.2f}")
    # append today's live print as the forming close
    today = pd.Timestamp(datetime.now(ET).date())
    s = closes.copy()
    s.loc[today] = px
    s = s[~s.index.duplicated(keep="last")].sort_index()
    sma200 = s.rolling(200).mean().iloc[-1]
    sma20 = s.rolling(20).mean().iloc[-1]
    ret = s.pct_change().dropna()
    vol20 = ret.iloc[-20:].std() * (252 ** 0.5)
    regime = "OUT" if (px < sma200 and px < sma20) else "IN"
    stormy = vol_override and vol20 > VOL_ANNUAL_THRESH
    log(f"  regime: SPY {px:.2f} vs 200DMA {sma200:.2f} / 20DMA {sma20:.2f} "
        f"-> {regime}; 20d ann.vol {vol20*100:.1f}%{' STORMY' if stormy else ''}")
    if force:
        log(f"  --force-regime {force} (override)")
        regime = force
    risk_off = (regime == "OUT") or stormy
    return regime, risk_off

def positions():
    return [p for p in (api(TRADE, "/v2/positions") or [])
            if abs(float(p["market_value"])) >= DUST]

def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {"mode": "HOLD"}

def save_state(st):
    if DRYRUN:
        log(f"  [would write state] {st}")
        return
    json.dump(st, open(STATE, "w"), indent=1)

def guard_account():
    a = api(TRADE, "/v2/account")
    if not a or a["account_number"] != ACCOUNT:
        log(f"SAFETY STOP: expected {ACCOUNT}, got {a and a.get('account_number')}")
        sys.exit(1)
    return a

def do_close(force, vol_override):
    a = guard_account()
    regime, risk_off = compute_regime(force, vol_override)
    st = load_state()
    if not risk_off:
        if st.get("mode") == "FLAT":
            log("  regime back IN but state FLAT -- clearing stale flag (open-run will not restore)")
            save_state({"mode": "HOLD"})
        log(f"  HOLD: regime IN, keeping overnight book (equity ${float(a['equity']):,.2f})")
        return
    pos = positions()
    if not pos:
        log("  risk-off but no positions to flatten (already cash)")
        save_state({"mode": "FLAT", "flattened_at": datetime.now(ET).isoformat(), "book": {}})
        return
    book = {p["symbol"]: round(float(p["market_value"]), 2) for p in pos}
    total = sum(book.values())
    log(f"  RISK-OFF -> flattening {len(book)} names (${total:,.0f}) to cash for the night")
    for sym, mv in sorted(book.items(), key=lambda x: -x[1]):
        if DRYRUN:
            log(f"    would CLOSE {sym:6} ${mv:>10,.2f}")
        else:
            r = api(TRADE, f"/v2/positions/{sym}", "DELETE")
            log(f"    CLOSE {sym:6} ${mv:>10,.2f} {'ok' if r is not None else 'FAILED'}")
    save_state({"mode": "FLAT", "flattened_at": datetime.now(ET).isoformat(),
                "regime": regime, "book": book})

def do_open(force, vol_override):
    a = guard_account()
    st = load_state()
    if st.get("mode") != "FLAT":
        log("  no overnight flatten to restore (mode HOLD) -- nothing to do")
        return
    book = st.get("book", {})
    if not book:
        log("  FLAT with empty book -- clearing state")
        save_state({"mode": "HOLD"})
        return
    cur = {p["symbol"]: float(p["market_value"]) for p in (api(TRADE, "/v2/positions") or [])}
    log(f"  RESTORE: rebuilding {len(book)} names (${sum(book.values()):,.0f}); "
        f"reconciling vs {len(cur)} live positions")
    for sym, tgt in sorted(book.items(), key=lambda x: -x[1]):
        delta = tgt - cur.get(sym, 0.0)
        if delta <= MIN_DELTA:
            log(f"    skip {sym:6} (already ${cur.get(sym,0.0):,.0f} of ${tgt:,.0f} target)")
            continue
        if DRYRUN:
            log(f"    would BUY {sym:6} ${delta:>10,.2f} (notional)")
        else:
            o = api(TRADE, "/v2/orders", "POST", {"symbol": sym, "side": "buy",
                    "type": "market", "time_in_force": "day", "notional": round(delta, 2)})
            log(f"    BUY {sym:6} ${delta:>10,.2f} {'id='+o['id'][:8] if o else 'FAILED'}")
    save_state({"mode": "HOLD", "restored_at": datetime.now(ET).isoformat()})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["close", "open"])
    ap.add_argument("--force-regime", choices=["IN", "OUT"], default=None)
    ap.add_argument("--vol-override", action="store_true")
    args = ap.parse_args()
    log(f"=== overnight_guard {args.phase} (dryrun={DRYRUN}) ===")
    clk = api(TRADE, "/v2/clock")
    log(f"  market clock: open={clk and clk['is_open']} now={clk and clk['timestamp'][:19]}")
    if args.phase == "close":
        do_close(args.force_regime, args.vol_override)
    else:
        do_open(args.force_regime, args.vol_override)

if __name__ == "__main__":
    main()
