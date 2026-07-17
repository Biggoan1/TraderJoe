#!/usr/bin/env python3
"""Re-import the 800-name universe SPLIT+DIVIDEND ADJUSTED (Alpaca adjustment=all)
into dataset fafo-market-adj-2016. The operator CLI has no --adjustment flag, so we
call import_bars directly. Also imports SPY adjusted (needed for baselines/kill-switch)."""
import os, json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv("/root/hermes-trader/.env.research")
from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import AssetClass, BarInterval, AdjustmentMode
from strategy.warehouse.import_pipeline import import_bars, PendingDownloadsQueue

env = dict(os.environ)
from strategy.providers.alpaca import AlpacaProvider
from strategy.research_account import ResearchAccountClient, ResearchAccountConfig

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

DATASET = "fafo-market-adj-2016"
uni = json.load(open("/root/hermes-trader/reports/strategy_search/market_universe.json"))
syms = sorted(set(uni) | {"SPY"})
layout = WarehouseLayout.from_env(env); layout.create()
provider = AlpacaProvider(ResearchAccountClient(config=ResearchAccountConfig.from_env(env), env=env), feed="sip")
log(f"importing {len(syms)} symbols 2016->today, adjustment=SPLIT_DIVIDEND (alpaca 'all')")
with PendingDownloadsQueue(os.path.join(str(layout.root), "queue", f"{DATASET}.db")) as q:
    rep = import_bars(layout, provider, DATASET, symbols=syms,
                      start="2016-01-01", end=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                      asset_class=AssetClass.EQUITY, interval=BarInterval.DAILY,
                      adjustment=AdjustmentMode.SPLIT_DIVIDEND, chunk_size=50, queue=q)
log(f"ok={len(rep.symbols_ok)} empty={rep.symbols_empty} bars={rep.total_bars} files={len(rep.files)}")
log("=== ADJUSTED IMPORT COMPLETE ===")
