#!/usr/bin/env python3
"""Quick market check for positions and watchlist."""
import yfinance as yf

# Current positions with significant size
positions = ['AMD', 'INTC', 'IEF', 'LLY', 'XLV', 'ARKK', 'IWM', 'XLF', 'TLT']
# Broader market
indices = ['SPY', 'QQQ', 'IWM', 'DIA']
# Hot watchlist names
watchlist = ['NVDA', 'TSLA', 'AAPL', 'MSFT', 'META', 'AMZN', 'SMH', 'XLK', 'ARKK', 'MRVL', 'MU', 'AVGO', 'ONCY', 'GOOG']

all_tickers = list(dict.fromkeys(indices + positions + watchlist))

print("=== MARKET PULSE ===\n")

for ticker in all_tickers:
    t = yf.Ticker(ticker)
    info = t.fast_info
    current = info.lastPrice if hasattr(info, 'lastPrice') else None
    prev = info.prevLastPrice if hasattr(info, 'prevLastPrice') else None
    
    if current and prev:
        chg = ((current - prev) / prev) * 100
        print(f"{ticker:8s} ${current:9.2f}  ({chg:+.2f}%)")
    else:
        try:
            hist = t.history(period='1d')
            if not hist.empty:
                print(f"{ticker:8s} ${hist['Close'].iloc[-1]:9.2f}  (no prev)")
        except:
            print(f"{ticker:8s} N/A")
