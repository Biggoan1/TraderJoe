#!/usr/bin/env python3
"""Intraday (15m) equity downloader -> warehouse, via the existing AlpacaProvider + import_bars."""
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import AssetClass, BarInterval, AdjustmentMode
from strategy.warehouse.import_pipeline import import_bars, PendingDownloadsQueue
from strategy.providers.alpaca import AlpacaProvider
from strategy.research_account import ResearchAccountClient, ResearchAccountConfig

ROOT = "/root/hermes-trader"
load_dotenv(os.path.join(ROOT, ".env.research"))
env = dict(os.environ)
DATASET = "fafo-intraday-15m"
SYMS = ["AAPL", "MSFT", "NVDA", "AMD", "META", "TSLA", "AMZN", "GOOGL"]
END = datetime.now(timezone(timedelta(hours=-4)))
START = (END - timedelta(days=180)).strftime("%Y-%m-%d")
END = END.strftime("%Y-%m-%d")

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

layout = WarehouseLayout.from_env(env); layout.create()
provider = AlpacaProvider(ResearchAccountClient(config=ResearchAccountConfig.from_env(env), env=env), feed="sip")
log(f"importing 15Min bars {START}..{END} for {len(SYMS)} symbols")
with PendingDownloadsQueue(os.path.join(str(layout.root), "queue", f"{DATASET}.db")) as q:
    rep = import_bars(layout, provider, DATASET, symbols=SYMS, start=START, end=END,
                      asset_class=AssetClass.EQUITY, interval=BarInterval.MINUTE_15,
                      adjustment=AdjustmentMode.RAW, chunk_size=8, queue=q)
log(f"import report: {rep}")
log("=== INTRADAY DOWNLOAD COMPLETE ===")
