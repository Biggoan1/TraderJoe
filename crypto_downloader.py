#!/usr/bin/env python3
"""
Crypto history downloader -> warehouse.
Fetches daily crypto bars from Alpaca (/v1beta3/crypto/us/bars), converts to the
canonical Bar record, writes them under market_data/crypto/daily/<dataset>/ and
rebuilds the manifest as asset_class=crypto so the (now unlocked) simulator can
read them with --asset-class crypto.
"""
import os, json, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import Bar, AssetClass, BarInterval, AdjustmentMode
from strategy.warehouse.parquet_io import write_bars
from strategy.warehouse.import_pipeline import rebuild_manifest

ROOT = "/root/hermes-trader"
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(ROOT, ".env.research"))
KEY = os.getenv("CRYPTO_ALPACA_API_KEY") or os.getenv("RESEARCH_ALPACA_API_KEY")
SECRET = os.getenv("CRYPTO_ALPACA_SECRET_KEY") or os.getenv("RESEARCH_ALPACA_SECRET_KEY")
COINS = ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","LINK/USD","LTC/USD","DOT/USD","UNI/USD","AAVE/USD","BCH/USD","DOGE/USD"]
DATASET = "fafo-crypto-2021"
START = "2021-01-01"
END = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

def fetch(sym):
    out, token = [], None
    while True:
        params = {"symbols": sym, "timeframe": "1Day",
                  "start": START + "T00:00:00Z", "end": END + "T00:00:00Z", "limit": 10000}
        if token:
            params["page_token"] = token
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET})
        j = json.loads(urllib.request.urlopen(req, timeout=90).read())
        out += j.get("bars", {}).get(sym, [])
        token = j.get("next_page_token")
        if not token:
            break
    return out

def main():
    layout = WarehouseLayout.from_env(dict(os.environ)); layout.create()
    all_bars, per = [], {}
    for sym in COINS:
        norm = sym.replace("/", "")          # BTC/USD -> BTCUSD (no slash in path)
        raw = fetch(sym)
        bars = []
        for b in raw:
            try:
                bars.append(Bar(symbol=norm, timestamp=b["t"], open=float(b["o"]),
                                high=float(b["h"]), low=float(b["l"]), close=float(b["c"]),
                                volume=float(b["v"]), interval=BarInterval.DAILY,
                                adjustment_mode=AdjustmentMode.RAW,
                                vwap=float(b.get("vw")) if b.get("vw") is not None else None,
                                trade_count=int(b.get("n")) if b.get("n") is not None else None))
            except Exception:
                continue                     # skip any bar violating OHLC invariants
        per[norm] = (len(bars), bars[0].timestamp[:10] if bars else "-", bars[-1].timestamp[:10] if bars else "-")
        all_bars += bars
        log(f"  {norm}: {len(bars)} bars {per[norm][1]}..{per[norm][2]}")
    if not all_bars:
        log("NO BARS fetched — check keys/endpoint"); return
    written = write_bars(layout, all_bars, AssetClass.CRYPTO, dataset_id=DATASET)
    log(f"wrote {len(written)} parquet files")
    mani = rebuild_manifest(layout, DATASET, AssetClass.CRYPTO, BarInterval.DAILY,
                            provider_name="alpaca-crypto", force=True)
    log(f"manifest {DATASET}: {mani.start_date}..{mani.end_date}, {len(mani.symbols)} symbols, status={mani.validation_status}")
    log("=== CRYPTO DOWNLOAD COMPLETE ===")

if __name__ == "__main__":
    main()
