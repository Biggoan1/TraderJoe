#!/usr/bin/env python3
import yfinance as yf
import pandas as pd
import numpy as np
import math
import json
import sys

symbols = ['ARB', 'AXP', 'DE', 'MA', 'PANW', 'V']

results = {}
for sym in symbols:
    try:
        df = yf.download(sym, period='5d', progress=False, auto_adjust=True)
        if df.empty:
            results[sym] = {'price': 0, 'sells': ['No data']}
            continue
        
        # Handle multi-level columns (yfinance >= 0.2.x)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        
        c = df['Close'].astype(float)
        
        if len(c) < 2:
            results[sym] = {'price': round(float(c.iloc[-1]), 2), 'sells': ['Insufficient data']}
            continue
        
        sma20 = c.rolling(20).mean()
        bb_std = c.rolling(20).std()
        bb_upper = sma20 + 2 * bb_std
        delta = c.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        ema12 = c.ewm(span=12).mean()
        ema26 = c.ewm(span=26).mean()
        macd = ema12 - ema26
        macd_sig = macd.ewm(span=9).mean()
        macd_hist = macd - macd_sig
        
        price = float(c.iloc[-1])
        rsi_val = float(rsi.iloc[-1])
        bb_u = float(bb_upper.iloc[-1])
        mac_h = float(macd_hist.iloc[-1])
        prev_mac_h = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else 0
        
        sells = []
        if not math.isnan(rsi_val) and rsi_val > 70:
            sells.append(f'RSI={rsi_val:.1f}>70')
        if not math.isnan(bb_u) and price >= bb_u:
            sells.append('Above upper BB')
        if not math.isnan(mac_h) and mac_h < 0 and mac_h < prev_mac_h:
            sells.append('MACD death cross')
        
        results[sym] = {
            'price': round(price, 2),
            'sells': sells,
            'rsi': round(rsi_val, 1) if not math.isnan(rsi_val) else None,
            'macd_hist': round(mac_h, 4) if not math.isnan(mac_h) else None
        }
    except Exception as e:
        results[sym] = {'price': 0, 'sells': [f'Error: {str(e)}']}

print(json.dumps(results, indent=2))
