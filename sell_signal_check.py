import yfinance as yf
import numpy as np
import pandas as pd
import json

symbols = ['DDOG', 'DASH', 'MA', 'V', 'JNJ', 'XLV', 'XLF', 'SPY']
entries = {'DDOG': 260.62, 'DASH': 173.63, 'MA': 418.0, 'V': 380.0, 'JNJ': 170.0, 'XLV': 160.0, 'XLF': 55.0, 'SPY': 600.0}

def compute_rsi(data, period=14):
    delta = data.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def compute_bollinger(data, period=20, std_mult=2):
    sma = data.rolling(window=period).mean()
    std = data.rolling(window=period).std()
    upper = sma + std_mult * std
    return sma, upper

def compute_macd(data, fast=12, slow=26, signal=9):
    ema_fast = data.ewm(span=fast, adjust=False).mean()
    ema_slow = data.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return histogram

results = []
flagged = []
no_signals = []

for sym in symbols:
    try:
        ticker = yf.Ticker(sym)
        df = ticker.history(period='3mo', interval='1d')
        
        if df is None or df.empty or len(df) < 30:
            print('{}: insufficient data ({} rows)'.format(sym, len(df) if df is not None else 0))
            continue
        
        # Handle MultiIndex columns (newer yfinance)
        if isinstance(df.columns, pd.MultiIndex):
            close = df['Close'].dropna(axis=1, how='all')
            if len(close.columns) > 0:
                close = close.iloc[:, 0]
        else:
            close = df['Close'].dropna()
        
        price = float(close.iloc[-1])
        entry = entries[sym]
        pl_pct = ((price - entry) / entry) * 100.0
        
        rsi = compute_rsi(close)
        current_rsi = float(rsi.iloc[-1])
        
        sma, upper = compute_bollinger(close)
        current_hist = float(compute_macd(close).iloc[-1])
        prev_hist = float(compute_macd(close).iloc[-2])
        
        rsi_overbought = current_rsi > 70
        price_above_upper = price >= float(upper.iloc[-1])
        macd_death_cross = (current_hist < 0) and (current_hist < prev_hist)
        
        sell_reasons = []
        if rsi_overbought:
            sell_reasons.append('RSI {:.1f} > 70 (overbought)'.format(current_rsi))
        if price_above_upper:
            sell_reasons.append('Price ${:.2f} >= Upper BB ${:.2f}'.format(price, float(upper.iloc[-1])))
        if macd_death_cross:
            sell_reasons.append('MACD death cross (hist {:.3f} declining)'.format(current_hist))
        
        info = {
            'symbol': sym,
            'price': round(price, 2),
            'entry': entry,
            'pl_pct': round(pl_pct, 2),
            'rsi': round(current_rsi, 2),
            'upper_bb': round(float(upper.iloc[-1]), 2),
            'macd_hist': round(current_hist, 4),
            'sell_reasons': sell_reasons
        }
        results.append(info)
        
        print('--- {}: ${:.2f} P/L: {:+.1f}% | RSI: {:.1f} | BB upper: ${:.2f} | MACD hist: {:.4f} ---'.format(
            sym, price, pl_pct, current_rsi, float(upper.iloc[-1]), current_hist))
        
        if sell_reasons:
            print('  SELL SIGNAL: {}'.format(', '.join(sell_reasons)))
            flagged.append(info)
        else:
            print('  No signals')
            no_signals.append(info)
    except Exception as e:
        print('{}: ERROR - {}'.format(sym, str(e)))
        import traceback
        traceback.print_exc()

print('\n' + '=' * 60)
print('SELL SIGNALS DETECTED')
print('=' * 60)
if flagged:
    for r in flagged:
        print('  {} ${}  P/L: {}%  [{}]'.format(
            r['symbol'], r['price'], r['pl_pct'], ', '.join(r['sell_reasons'])))
else:
    print('  All positions green - no sell signals')

print('\n' + '=' * 60)
print('GREEN POSITIONS ({} total)'.format(len(no_signals)))
print('=' * 60)
for r in no_signals:
    print('  {} ${}  P/L: {}%  RSI: {}  MACD hist: {}'.format(
        r['symbol'], r['price'], r['pl_pct'], r['rsi'], r['macd_hist']))
