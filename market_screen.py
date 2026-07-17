#!/usr/bin/env python3
"""Whole-market liquidity screen: rank ALL tradable major-exchange US common stocks
by recent median dollar volume (no human picking), keep top N liquid names."""
import json, os, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from statistics import median
from dotenv import load_dotenv

load_dotenv("/root/hermes-trader/.env.research")
KEY, SEC = os.environ["RESEARCH_ALPACA_API_KEY"], os.environ["RESEARCH_ALPACA_SECRET_KEY"]
TOP_N = 800
MIN_PRICE = 5.0

assets = json.load(open("/tmp/assets.json"))
# rule-based universe: tradable, major exchange, OPERATING COMPANIES ONLY.
# Exclude funds/ETPs/leveraged products by name markers (ADRs/foreign shares are
# real companies and stay). REITs ("... Trust, Inc.") stay — only fund-trusts go.
import re
FUND_RE = re.compile(
    r"(\bETF\b|\bETN\b|\bFund\b|\bIndex\b|iShares|Vanguard|SPDR|ProShares|Direxion|"
    r"GraniteShares|Global X|YieldMax|Defiance|Tradr|Roundhill|Leveraged|"
    r"\b[123](\.5)?[xX]\b|Ultra(Pro|Short)?\s|Daily (Long|Short|Bull|Bear|Target)|"
    r"Bull\b|\bBear\b|Trust, Series|Invesco QQQ|Preferred Stock)", re.I)
syms = sorted({a["symbol"] for a in assets
               if a["tradable"] and a["exchange"] in ("NYSE", "NASDAQ", "AMEX")
               and a["symbol"].isalpha() and len(a["symbol"]) <= 5
               and not FUND_RE.search(a.get("name") or "")})
print(f"screening {len(syms)} symbols...", flush=True)

end = datetime.now(timezone.utc)
start = (end - timedelta(days=45)).strftime("%Y-%m-%d")
rows = {}
CH = 200
for i in range(0, len(syms), CH):
    chunk = syms[i:i+CH]
    params = {"symbols": ",".join(chunk), "timeframe": "1Day",
              "start": start, "limit": 10000, "adjustment": "raw", "feed": "sip"}
    url = "https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})
        j = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        print(f"  chunk {i}: ERR {e}", flush=True)
        continue
    for s, bars in (j.get("bars") or {}).items():
        if not bars or len(bars) < 15:
            continue
        last = bars[-1]["c"]
        if last < MIN_PRICE:
            continue
        dv = median(b["c"] * b["v"] for b in bars)
        rows[s] = (dv, last)
    if (i // CH) % 10 == 0:
        print(f"  {i}/{len(syms)} screened, kept {len(rows)}", flush=True)

ranked = sorted(rows.items(), key=lambda kv: kv[1][0], reverse=True)[:TOP_N]
out = [s for s, _ in ranked]
json.dump(out, open("/root/hermes-trader/reports/strategy_search/market_universe.json", "w"))
print(f"kept top {len(out)} by median dollar volume (price>${MIN_PRICE})")
print("top 15:", out[:15])
print("ranks 390-410:", out[390:410])
print("bottom 15:", out[-15:])
