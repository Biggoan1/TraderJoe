#!/usr/bin/env bash
set -euo pipefail

cd /root/hermes-trader
mkdir -p .hermes-profile/logs

source .venv/bin/activate
python3 market_screener.py >> .hermes-profile/logs/market-screener.log 2>&1
