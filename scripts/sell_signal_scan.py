#!/usr/bin/env python3
"""Sell signal scan for Trade Joe positions."""
import yfinance as yf
import numpy as np
import pandas as pd

symbols = {
    'DDOG': 260.62,
    'DASH': 173.63,
    'MA': 418.0,
    'V': 380.0,
    'JNJ': 170.0,
    'XLV': 160.0,
    'XLF': 55.0,
    'SPY': 600.0,
}

def calc_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_bollinger(prices, period=20, std_dev=2):
    sma = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return sma, upper, lower

def calc_macd(prices, fast=12, slow=26, signal=9):
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

signals_found = False
report_lines = []

for sym, entry_price in symbols.items():
    try:
        t = yf.Ticker(sym)
        df = t.history(period='3mo')
        
        if df.empty or len(df) < 26:
            report_lines.append(f'? {sym} — insufficient data')
            continue
        
        close = df['Close']
        current_price = close.iloc[-1]
        pl_pct = ((current_price - entry_price) / entry_price) * 100
        
        rsi = calc_rsi(close)
        current_rsi = rsi.iloc[-1]
        
        sma, upper_band, lower_band = calc_bollinger(close)
        current_upper = upper_band.iloc[-1]
        current_sma = sma.iloc[-1]
        price_above_upper = current_price >= current_upper
        
        macd_line, signal_line, histogram = calc_macd(close)
        current_hist = histogram.iloc[-1]
        prev_hist = histogram.iloc[-2]
        death_cross = (current_hist < 0) and (current_hist < prev_hist)
        
        sell_reasons = []
        if current_rsi > 70:
            sell_reasons.append(f'RSI {current_rsi:.1f} > 70 (overbought)')
        if price_above_upper:
            sell_reasons.append(f'Price ${current_price:.2f} >= Upper BB ${current_upper:.2f}')
        if death_cross:
            sell_reasons.append(f'MACD death cross (hist {current_hist:.4f} < 0, declining)')
        
        if sell_reasons:
            signals_found = True
            line = f"\U0001f534 {sym} ${current_price:.2f}  P/L: {pl_pct:+.1f}%  {' | '.join(sell_reasons)}"
            report_lines.append(line)
        else:
            line = f"\U0001f7e2 {sym} ${current_price:.2f}  P/L: {pl_pct:+.1f}%  RSI: {current_rsi:.1f}  SMA(20): ${current_sma:.2f}  Upper BB: ${current_upper:.2f}"
            report_lines.append(line)
    
    except Exception as e:
        report_lines.append(f'? {sym} — Error: {e}')

print('='*80)
print('TRADE JOE — POSITION SELL SIGNAL SCAN')
print(f'Timestamp: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S ET")}')
print('='*80)
for line in report_lines:
    print(line)
print('='*80)
if not signals_found:
    print('NO_SIGNALS: All positions green')
else:
    count = sum(1 for l in report_lines if '\U0001f534' in l)
    print(f'\n{count} position(s) with sell signals — reply "sell" to queue, "sell all" to sell all flagged, "hold" to stay put.')
