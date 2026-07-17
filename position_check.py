#!/usr/bin/env python3
"""Check monitored positions for sell signals using RSI, Bollinger Bands, and MACD."""
import yfinance as yf
import pandas as pd
import sys

# Monitored symbols with entry prices
SYMBOLS = {
    'DDOG': 260.62,
    'DASH': 173.63,
    'MA': 418.00,
    'V': 380.00,
    'JNJ': 170.00,
    'XLV': 160.00,
    'XLF': 55.00,
    'SPY': 600.00,
}

signals = []

for sym, entry_price in SYMBOLS.items():
    try:
        df = yf.download(sym, period='6mo', interval='1d', progress=False)
        
        # Handle yfinance MultiIndex columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        
        if df.empty or len(df) < 30:
            print(f"SKIP {sym}: insufficient data ({len(df)} bars)")
            continue
        
        # Use last 30 bars for indicators
        tail = df.tail(30).copy()
        
        # Current price
        current_price = tail['Close'].iloc[-1]
        
        # P/L
        pl_pct = ((current_price - entry_price) / entry_price) * 100
        
        # RSI(14)
        delta = tail['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        # Bollinger Bands (20, 2)
        sma20 = tail['Close'].rolling(window=20).mean()
        bb_std = tail['Close'].rolling(window=20).std()
        upper_bb = sma20 + 2 * bb_std
        current_upper_bb = upper_bb.iloc[-1]
        
        # MACD (12, 26, 9)
        ema12 = tail['Close'].ewm(span=12, adjust=False).mean()
        ema26 = tail['Close'].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - signal_line
        
        current_macd = macd_hist.iloc[-1]
        prev_macd = macd_hist.iloc[-2]
        
        # Check sell conditions
        sell_reasons = []
        
        # 1. RSI > 70
        if current_rsi > 70:
            sell_reasons.append(f"RSI overbought ({current_rsi:.1f})")
        
        # 2. Price >= Upper BB
        if current_price >= current_upper_bb:
            sell_reasons.append("Price at/above Upper BB ($" + f"{current_upper_bb:.2f}" + ")")
        
        # 3. MACD death cross: histogram negative AND declining
        if current_macd < 0 and current_macd < prev_macd:
            sell_reasons.append(f"MACD death cross (hist: {current_macd:.4f}, declining)")
        
        signal_text = ", ".join(sell_reasons) if sell_reasons else "NONE"
        
        if sell_reasons:
            signals.append(f"RED {sym}: ${current_price:.2f} | Entry: ${entry_price:.2f} | P/L: {pl_pct:+.2f}% | RSI: {current_rsi:.1f} | BB Upper: ${current_upper_bb:.2f} | MACD: {current_macd:.4f} | {signal_text}")
        else:
            print(f"GREEN {sym}: ${current_price:.2f} | Entry: ${entry_price:.2f} | P/L: {pl_pct:+.2f}% | RSI: {current_rsi:.1f} | BB Upper: ${current_upper_bb:.2f} | MACD: {current_macd:.4f} | NONE")
            
    except Exception as e:
        print(f"ERROR {sym}: {e}", file=sys.stderr)

# Print flagged symbols
print("---FLAGGED---")
if signals:
    for s in signals:
        print(s)
else:
    print("NO_SIGNALS")
