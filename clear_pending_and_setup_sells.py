#!/usr/bin/env python3
"""Set up sell alerts for AMD and INTC."""
import sys
sys.path.insert(0, '/root/hermes-trader')

from telegram_approvals import get_pending_sell_alert, queue_sell_alert
import yfinance as yf

symbols = ['AMD', 'INTC']

print("=== Setting up sell alerts ===")

for symbol in symbols:
    t = yf.Ticker(symbol)
    
    # Get price via history since fast_info is unreliable
    df = t.history(period='1d')
    if df.empty:
        print(f"  {symbol}: Could not get price")
        continue
    
    current = df['Close'].iloc[-1]
    
    # Check if alert already exists
    existing = get_pending_sell_alert(symbol)
    if existing:
        print(f"  {symbol}: Already has alert #{existing['approval_id']}")
        continue
    
    # Queue sell alert
    approval_id, status = queue_sell_alert(symbol, current, 0)
    print(f"  {symbol}: Alert queued (#{approval_id}, status={status})")

print("\nDone!")
