import yfinance as yf
import pandas as pd
import numpy as np
import json

symbols = ['DDOG', 'DASH', 'MA', 'V', 'JNJ', 'XLV', 'XLF', 'SPY']
entries = {'DDOG': 260.62, 'DASH': 173.63, 'MA': 418, 'V': 380, 'JNJ': 170, 'XLV': 160, 'XLF': 55, 'SPY': 600}

results = {}

for sym in symbols:
    try:
        df = yf.download(sym, period='60d', interval='1d', progress=False)
        
        # Handle MultiIndex columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        
        if df.empty or len(df) == 0:
            results[sym] = {'error': 'no data'}
            print("%s: NO DATA" % sym)
            continue
        
        df = df.tail(40)
        close = df['Close'].astype(float)
        
        current = float(close.iloc[-1])
        entry = entries[sym]
        pl_pct = ((current - entry) / entry) * 100
        
        # RSI (14)
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        # Bollinger Bands (20, 2)
        sma20 = close.rolling(window=20).mean()
        std20 = close.rolling(window=20).std()
        upper_bb = sma20 + 2 * std20
        current_bb = float(upper_bb.iloc[-1])
        price_vs_bb = bool(current >= current_bb)
        
        # MACD (12, 26, 9)
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        macd_hist = macd_line - signal_line
        
        current_hist = float(macd_hist.iloc[-1])
        prev_hist = float(macd_hist.iloc[-2])
        death_cross = (current_hist < 0) and (current_hist < prev_hist)
        
        signals = []
        if current_rsi > 70:
            signals.append('RSI=%.1f (overbought)' % current_rsi)
        if price_vs_bb:
            signals.append('Price >= Upper BB ($%.2f)' % current_bb)
        if death_cross:
            signals.append('MACD death cross (hist=%.4f)' % current_hist)
        
        results[sym] = {
            'current': current, 'entry': entry, 'pl_pct': pl_pct,
            'rsi': current_rsi, 'bb_upper': current_bb,
            'price_vs_bb': price_vs_bb, 'death_cross': death_cross,
            'macd_hist': current_hist, 'prev_macd_hist': prev_hist,
            'signals': signals
        }
        
        status = 'SELL' if signals else 'GREEN'
        print("%s: $%.2f | P/L: %+.1f%% | RSI: %.1f | BB Upper: $%.2f | MACD Hist: %.4f | %s" % (
            sym, current, pl_pct, current_rsi, current_bb, current_hist, status))
        if signals:
            for s in signals:
                print("  WARNING: %s" % s)
    except Exception as e:
        results[sym] = {'error': str(e)}
        print("%s: ERROR - %s" % (sym, str(e)))

flagged = {k: v for k, v in results.items() if v.get('signals')}
print("\n=== FLAGGED: %d symbols ===" % len(flagged))
for sym, data in flagged.items():
    print("  %s: %s" % (sym, ", ".join(data["signals"])))

# Output JSON for cron to parse
with open('/root/hermes-trader/scripts/signal_report.json', 'w') as f:
    json.dump(results, f, default=str)
