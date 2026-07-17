#!/usr/bin/env python3
"""Today's P/L: champion stock acct, crypto acct, SPY, and the factor challenger's
current 25 holdings (from challenger.db) via research data API snapshots."""
import json, sqlite3, urllib.request

ROOT = "/root/hermes-trader"

def envf(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d

e = envf(f"{ROOT}/.env")
r = envf(f"{ROOT}/.env.research")

def get(url, key, sec):
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
    return json.load(urllib.request.urlopen(req, timeout=30))

PAPER = "https://paper-api.alpaca.markets"

for label, k, s in [("STOCK", e["ALPACA_API_KEY"], e["ALPACA_SECRET_KEY"]),
                    ("CRYPTO", e["CRYPTO_ALPACA_API_KEY"], e["CRYPTO_ALPACA_SECRET_KEY"])]:
    a = get(f"{PAPER}/v2/account", k, s)
    eq, last = float(a["equity"]), float(a["last_equity"])
    print(f"{label}: equity ${eq:,.2f}  prev-close ${last:,.2f}  today {((eq/last)-1)*100:+.2f}%")
    pos = get(f"{PAPER}/v2/positions", k, s)
    for p in sorted(pos, key=lambda p: -abs(float(p["market_value"]))):
        print(f"   {p['symbol']:<10} ${float(p['market_value']):>12,.2f}  today {float(p['unrealized_intraday_plpc'])*100:+.2f}%  total {float(p['unrealized_plpc'])*100:+.2f}%")

db = sqlite3.connect(f"{ROOT}/reports/strategy_search/challenger.db")
hold = json.loads(db.execute("SELECT v FROM factor_meta WHERE k='holdings'").fetchone()[0])
syms = ",".join(hold + ["SPY"])
snap = get(f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed=sip",
           r["RESEARCH_ALPACA_API_KEY"], r["RESEARCH_ALPACA_SECRET_KEY"])
chg = {}
for s, v in snap.items():
    dbar, pbar = v.get("dailyBar"), v.get("prevDailyBar")
    if dbar and pbar and pbar.get("c"):
        chg[s] = (dbar["c"] / pbar["c"] - 1) * 100
spy = chg.pop("SPY", None)
print(f"\nSPY today: {spy:+.2f}%")
if chg:
    avg = sum(chg.values()) / len(chg)
    print(f"FACTOR CHALLENGER book (equal-weight {len(chg)} names) today: {avg:+.2f}%")
    ranked = sorted(chg.items(), key=lambda kv: kv[1])
    print("  worst:", "  ".join(f"{s} {c:+.1f}%" for s, c in ranked[:5]))
    print("  best: ", "  ".join(f"{s} {c:+.1f}%" for s, c in ranked[-5:]))
