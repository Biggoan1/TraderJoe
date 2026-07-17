import yfinance as yf
import pandas as pd
import numpy as np

def compute_all(symbol, entry_price):
    data = yf.download(symbol, period='3mo', interval='1d', progress=False)
    if data.empty:
        return None
    
    # With MultiIndex columns, use tuple key to get Series
    close = data[('Close', symbol)]
    
    if len(close) < 30:
        return None
    
    # RSI(14)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    
    # Bollinger Bands (20,2)
    sma20 = close.rolling(window=20).mean()
    std20 = close.rolling(window=20).std()
    upper_bb = sma20 + 2 * std20
    
    # MACD (12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line
    
    price = float(close.iloc[-1])
    rsi_val = float(rsi.iloc[-1])
    upper = float(upper_bb.iloc[-1])
    macd_now = float(macd_hist.iloc[-1])
    macd_prev = float(macd_hist.iloc[-2])
    
    return price, rsi_val, upper, macd_now, macd_prev

symbols = {
    'DDOG': 260.62, 'DASH': 173.63, 'MA': 418.00, 'V': 380.00,
    'JNJ': 170.00, 'XLV': 160.00, 'XLF': 55.00, 'SPY': 600.00,
}

print("=" * 80)
print("TRADE JOE - POSITION SELL SIGNAL CHECK")
print("=" * 80)

has_signals = False

for sym, entry in symbols.items():
    try:
        result = compute_all(sym, entry)
        if result is None:
            print(f"\n{sym}: Entry ${entry:.2f} — insufficient/no data")
            continue
        
        price, rsi, upper_bb, macd_n, macd_p = result
        pnl = ((price - entry)/entry)*100
        signals = []
        
        if rsi > 70:
            signals.append(f"RSI overbought ({rsi:.1f})")
        if price >= upper_bb:
            signals.append(f"Price >= Upper BB ({price:.2f} >= {upper_bb:.2f})")
        if macd_n < 0 and macd_n < macd_p:
            signals.append("MACD death cross (histogram negative & declining)")
        
        pnl_symbol = "🟢" if pnl >= 0 else "🔴"
        print(f"\n{sym}: {pnl_symbol} ${price:.2f} | Entry: ${entry:.2f} | P/L: {pnl:+.2f}%")
        print(f"  RSI: {rsi:.1f} | Upper BB: {upper_bb:.2f} | MACD Hist: {macd_n:.3f} (prev: {macd_p:.3f})")
        
        if signals:
            has_signals = True
            for s in signals:
                print(f"  🔴 SELL SIGNAL: {s}")
        else:
            print(f"  ✅ No sell signals")
            
    except Exception as e:
        print(f"\n{sym}: Error — {e}")

print("\n" + "=" * 80)
if has_signals:
    print("ACTIVE SELL SIGNALS DETECTED")
else:
    print("NO_SIGNALS: All positions green")
print("=" * 80)
