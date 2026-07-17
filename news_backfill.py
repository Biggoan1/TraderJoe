#!/usr/bin/env python3
"""News backfill worker: count firehose articles per (date, universe-symbol) for a
date range. Day-by-day, append to CSV after each day (resume-safe: restarts at the
last completed date + 1). Run several workers on disjoint year ranges in parallel."""
import argparse, csv, json, os, urllib.request, urllib.parse
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv("/root/hermes-trader/.env.research")
KEY, SEC = os.environ["RESEARCH_ALPACA_API_KEY"], os.environ["RESEARCH_ALPACA_SECRET_KEY"]
UNI = set(json.load(open("/root/hermes-trader/reports/strategy_search/market_universe.json")))

ap = argparse.ArgumentParser()
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

done = set()
if os.path.exists(a.out):
    with open(a.out) as f:
        for row in csv.reader(f):
            if row: done.add(row[0])

def day_counts(day):
    counts, token, pages = {}, None, 0
    while pages < 80:
        p = {"start": day + "T00:00:00Z", "end": day + "T23:59:59Z", "limit": 50}
        if token: p["page_token"] = token
        url = "https://data.alpaca.markets/v1beta1/news?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})
        for attempt in range(3):
            try:
                j = json.loads(urllib.request.urlopen(req, timeout=45).read())
                break
            except Exception:
                if attempt == 2: raise
        arts = j.get("news", [])
        for art in arts:
            for s in art.get("symbols", []):
                if s in UNI:
                    counts[s] = counts.get(s, 0) + 1
        pages += 1
        token = j.get("next_page_token")
        if not token: break
    return counts

d = datetime.strptime(a.start, "%Y-%m-%d")
end = datetime.strptime(a.end, "%Y-%m-%d")
n_days = 0
while d <= end:
    ds = d.strftime("%Y-%m-%d")
    if ds not in done:
        try:
            counts = day_counts(ds)
        except Exception as e:
            print(f"{ds}: FAILED {e}", flush=True); d += timedelta(days=1); continue
        with open(a.out, "a", newline="") as f:
            w = csv.writer(f)
            for s, n in sorted(counts.items()):
                w.writerow([ds, s, n])
            if not counts:
                w.writerow([ds, "_NONE_", 0])
        n_days += 1
        if n_days % 25 == 0:
            print(f"{ds}: done ({n_days} days this run)", flush=True)
    d += timedelta(days=1)
print(f"COMPLETE {a.start}..{a.end}", flush=True)
