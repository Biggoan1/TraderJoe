#!/usr/bin/env python3
"""Warehouse diagnostics after the 18:00 sync: last-row coverage, duplicate
timestamps, and a spot-check of NVDA/AAPL tails in fafo-market-adj-2016."""
import glob, json, sys
import pandas as pd

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices

C, V = load_matrices()
print(f"matrix {C.shape}; last 5 dates and non-NaN counts:")
tail = C.tail(5)
for d, row in tail.iterrows():
    print(f"  {d.date()}  non-NaN {row.notna().sum()}/{C.shape[1]}")
print("index duplicated:", int(C.index.duplicated().sum()))

for sym in ["NVDA", "AAPL", "CIFR", "SPY"]:
    files = sorted(glob.glob(f"/root/hermes-trader/market_data/equities/daily/fafo-market-adj-2016/{sym}/*.parquet"))
    if not files:
        print(f"{sym}: no files"); continue
    df = pd.concat([pd.read_parquet(f, columns=["timestamp", "close"]) for f in files[-2:]])
    dups = df["timestamp"].duplicated().sum()
    print(f"{sym}: last file {files[-1].split('/')[-1]}, {len(df)} rows in last 2 buckets, dup timestamps={dups}")
    print(df.tail(6).to_string(index=False))
