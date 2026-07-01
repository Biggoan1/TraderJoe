#!/usr/bin/env bash
set -euo pipefail

cd /root/hermes-trader
mkdir -p .hermes-profile/logs

source .venv/bin/activate
python3 crypto_trader.py >> .hermes-profile/logs/crypto-traderjoe-scan.log 2>&1
