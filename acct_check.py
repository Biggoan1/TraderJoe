#!/usr/bin/env python3
"""Verify the research-key paper account: number, equity, open positions/orders."""
import json, urllib.request

def envf(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d

r = envf("/root/hermes-trader/.env.research")
K, S = r["RESEARCH_ALPACA_API_KEY"], r["RESEARCH_ALPACA_SECRET_KEY"]
BASE = r.get("RESEARCH_ALPACA_ENDPOINT", "https://paper-api.alpaca.markets").rstrip("/")

def get(path):
    req = urllib.request.Request(BASE + path, headers={"APCA-API-KEY-ID": K, "APCA-API-SECRET-KEY": S})
    return json.load(urllib.request.urlopen(req, timeout=30))

a = get("/v2/account")
print(f"endpoint {BASE}")
print(f"account_number={a['account_number']} status={a['status']} equity=${float(a['equity']):,.2f} cash=${float(a['cash']):,.2f}")
pos = get("/v2/positions")
print(f"open positions: {len(pos)}")
for p in pos:
    print(f"  {p['symbol']} {p['qty']} ${float(p['market_value']):,.2f}")
orders = get("/v2/orders?status=open")
print(f"open orders: {len(orders)}")
for o in orders[:10]:
    print(f"  {o['symbol']} {o['side']} {o.get('qty') or o.get('notional')}")
