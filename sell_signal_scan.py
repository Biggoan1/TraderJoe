#!/usr/bin/env python3
import yfinance as yf
import pandas as pd
import numpy as np

symbols = ['DDOG', 'DASH', 'MA', 'V', 'JNJ', 'XLV', 'XLF', 'SPY']
entries = {'DDOG': 260.62, 'DASH': 173.63, 'MA': 418, 'V': 380, 'JNJ': 170, 'XLV': 160, 'XLF': 55, 'SPY': 600}

data = yf.download(symbols, period='3mo', progress=False)

results = []
for sym in symbols:
    price_col = sym
    closes = data[('Close', price_col)].dropna()
    if len(closes) < 30:
        print(f'{sym}: Insufficient data')
        continue
    current_price = closes.iloc[-1]
    entry = entries[sym]
    pl_pct = ((current_price - entry) / entry) * 100
    
    # RSI
    delta = closes.diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    current_rsi = rsi.iloc[-1]
    
    # Bollinger Bands
    sma20 = closes.rolling(window=20).mean()
    std20 = closes.rolling(window=20).std()
    upper_bb = sma20 + 2 * std20
    current_upper_bb = upper_bb.iloc[-1]
    price_at_upper = current_price >= current_upper_bb
    
    # MACD
    ema12 = closes.ewm(span=12).mean()
    ema26 = closes.ewm(span=26).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9).mean()
    macd_histogram = macd_line - signal_line
    current_hist = macd_histogram.iloc[-1]
    prev_hist = macd_histogram.iloc[-2]
    macd_death_cross = (current_hist < 0) and (current_hist < prev_hist)
    
    sell_reasons = []
    if current_rsi > 70:
        sell_reasons.append(f'RSI={current_rsi:.1f} (overbought)')
    if price_at_upper:
        sell_reasons.append(f'Price >= Upper BB (${current_upper_bb:.2f})')
    if macd_death_cross:
        sell_reasons.append('MACD death cross (histogram negative & declining)')
    
    results.append({
        'sym': sym, 'price': current_price, 'pl_pct': pl_pct,
        'rsi': current_rsi, 'upper_bb': current_upper_bb,
        'macd_death_cross': macd_death_cross,
        'sell_reasons': sell_reasons
    })

print('=' * 110)
print(f'{"Symbol":<8} {"Price":>10} {"P/L%":>8} {"RSI":>7} {"Upper BB":>10} {"MACD DC":>8} Signal')
print('-' * 110)
for r in results:
    dc = 'YES' if r['macd_death_cross'] else 'no'
    print(f'{r["sym"]:<8} ${r["price"]:>9.2f} {r["pl_pct"]:>+7.1f}% {r["rsi"]:>6.1f} ${r["upper_bb"]:>9.2f} {dc:>8} {", ".join(r["sell_reasons"]) if r["sell_reasons"] else "---"}')

print()
any_signals = any(len(r['sell_reasons']) > 0 for r in results)
if not any_signals:
    print('NO_SIGNALS: All positions green')
else:
    print('*** ACTIVE SELL SIGNALS ***')
    for r in results:
        if r['sell_reasons']:
            print(f'🔴 {r["sym"]} ${r["price"]:.2f}  P/L: {r["pl_pct"]:+.1f}%  [{", ".join(r["sell_reasons"])}]')
