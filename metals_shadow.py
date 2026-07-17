#!/usr/bin/env python3
"""Metals sleeve SHADOW — virtual buy-and-hold GLD/SLV/GDX, quarterly rebalance.

No broker orders. Recomputes the equity curve from inception each run (idempotent)
from the fafo-metals-2016 warehouse and writes challenger.db table metals_shadow.

Why a shadow and not a paper account: the 2026-07-17 backtest showed metals' value
is buy-and-hold + DIVERSIFICATION, not active trading (best active strat captured
<1/3 of just holding GLD); platinum/palladium excluded (both ~-85%). John is capped
at 3 Alpaca paper accounts, so this rides the existing shadow infra (challenger.db)
instead of consuming one — its curve + correlation vs the equity books show on the
dashboard, and it can be funded for real later if it earns its keep.
"""
import glob
import sqlite3

import pandas as pd

BASE = "/root/hermes-trader"
DB = f"{BASE}/reports/strategy_search/challenger.db"
SLEEVE = ["GLD", "SLV", "GDX"]     # monetary metals + miners; PGMs excluded
INCEPTION = "2026-07-10"           # matches the factor-challenger race baseline
START_CASH = 100_000.0
REBALANCE_EVERY = 63               # ~quarterly in trading days


def closes(sym):
    fs = sorted(glob.glob(f"{BASE}/market_data/equities/daily/fafo-metals-2016/{sym}/*.parquet"))
    if not fs:
        raise SystemExit(f"no warehouse data for {sym} — import fafo-metals-2016 first")
    df = pd.concat([pd.read_parquet(f) for f in fs]).sort_values("timestamp")
    df["d"] = df["timestamp"].str[:10]
    return df.groupby("d")["close"].last()


def main():
    px = pd.DataFrame({s: closes(s) for s in SLEEVE}).sort_index().dropna()
    px = px[px.index >= INCEPTION]
    if len(px) < 1:
        print("no metals data at/after inception"); return

    shares = {}
    rows = []
    for i, d in enumerate(px.index):
        if i == 0 or i % REBALANCE_EVERY == 0:
            eq = START_CASH if i == 0 else sum(shares[s] * px[s].iloc[i] for s in SLEEVE)
            for s in SLEEVE:
                shares[s] = (eq / len(SLEEVE)) / px[s].iloc[i]
        equity = sum(shares[s] * px[s].iloc[i] for s in SLEEVE)
        rows.append((d, round(equity, 2)))

    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS metals_shadow (date TEXT PRIMARY KEY, equity REAL)")
    con.execute("DELETE FROM metals_shadow")
    con.executemany("INSERT INTO metals_shadow (date, equity) VALUES (?, ?)", rows)
    con.commit()
    con.close()
    print(f"metals_shadow: {len(rows)} days {rows[0][0]}..{rows[-1][0]}, "
          f"inception ${START_CASH:,.0f} -> latest ${rows[-1][1]:,.0f} "
          f"({(rows[-1][1]/START_CASH - 1) * 100:+.2f}%)")


if __name__ == "__main__":
    main()
