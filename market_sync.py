#!/usr/bin/env python3
"""Daily incremental sync: append fresh Alpaca bars to fafo-market-2016 (and
fafo-crypto-2021 stays separate). Works because the dataset is unvalidated."""
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv("/root/hermes-trader/.env.research")
from strategy.local_warehouse import WarehouseLayout
from strategy.warehouse.incremental_sync import sync_all, SyncPolicy
from strategy.providers.alpaca import AlpacaProvider
from strategy.research_account import ResearchAccountClient, ResearchAccountConfig

env = dict(os.environ)
layout = WarehouseLayout.from_env(env)
provider = AlpacaProvider(ResearchAccountClient(config=ResearchAccountConfig.from_env(env), env=env), feed="sip")
reports = sync_all(layout, ["fafo-market-2016", "fafo-market-adj-2016"], SyncPolicy(providers=(provider,)))
for r in reports:
    print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] sync {r.dataset_id}: "
          f"appended={getattr(r,'bars_appended',getattr(r,'rows_appended','?'))} "
          f"warnings={getattr(r,'warnings',())}", flush=True)
