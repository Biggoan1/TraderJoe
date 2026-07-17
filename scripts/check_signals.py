import yfinance as yf
import pandas as pd
import numpy as np

symbols = ['DDOG', 'DASH', 'MA', 'V', 'JNJ', 'XLV', 'XLF', 'SPY']
entries = {'DDOG': 260.62, 'DASH': 173.63, 'MA': 418.0, 'V': 380.0, 'JNJ': 170.0, 'XLV': 160.0, 'XLF': 55.0, 'SPY': 600.0}

results = []

for sym in symbols:
    try:
        df = yf.download(sym, period='6mo', interval='1d', progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if df.empty or len(df) < 25:
            results.append({'symbol': sym, 'error': 'insufficient data'})
            continue
        
        df = df.tail(30)
        close = df['Close']
        price = close.iloc[-1]
        entry = entries.get(sym, price)
        pl_pct = ((price - entry) / entry) * 100
        
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        upper_bb = sma20 + 2 * std20
        current_upper_bb = upper_bb.iloc[-1]
        
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        macd_hist = macd_line - signal_line
        current_macd_hist = macd_hist.iloc[-1]
        prev_macd_hist = macd_hist.iloc[-2]
        
        signals = []
        if current_rsi > 70:
            signals.append(f'RSI {current_rsi:.1f} > 70 (overbought)')
        if price >= current_upper_bb:
            signals.append(f'Price >= Upper BB (${current_upper_bb:.2f})')
        if current_macd_hist < 0 and current_macd_hist < prev_macd_hist:
            signals.append(f'MACD death cross (hist={current_macd_hist:.4f})')
        
        results.append({
            'symbol': sym, 'price': price, 'entry': entry, 'pl_pct': pl_pct,
            'rsi': current_rsi, 'upper_bb': current_upper_bb,
            'macd_hist': current_macd_hist, 'prev_macd_hist': prev_macd_hist,
            'signals': signals
        })
    except Exception as e:
        results.append({'symbol': sym, 'error': str(e)})

for r in results:
    sym = r['symbol']
    if 'error' in r:
        print(f'{sym}: ERROR - {r["error"]}')
        continue
    sig_text = ' | '.join(r['signals']) if r['signals'] else 'NONE'
    flag = 'RED' if r['signals'] else 'GREEN'
    print(f'{flag} {sym}: ${r["price"]:.2f} | Entry: ${r["entry"]:.2f} | P/L: {r["pl_pct"]:+.2f}% | RSI: {r["rsi"]:.1f} | BB Upper: ${r["upper_bb"]:.2f} | MACD: {r["macd_hist"]:.4f} | {sig_text}')

print('---FLAGGED---')
for r in results:
    if 'error' not in r and r['signals']:
        sigs = ' | '.join(r['signals'])
        print(f'{r["symbol"]}|{r["price"]:.2f}|{r["pl_pct"]:+.2f}%|{sigs}')
