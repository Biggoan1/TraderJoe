import yfinance as yf
import pandas as pd
import numpy as np

symbols = ['DDOG', 'DASH', 'MA', 'V', 'JNJ', 'XLV', 'XLF', 'SPY']
entries = {'DDOG': 260.62, 'DASH': 173.63, 'MA': 418, 'V': 380, 'JNJ': 170, 'XLV': 160, 'XLF': 55, 'SPY': 600}

data = yf.download(symbols, period='1y', threads=True, group_by='ticker', progress=False)

for sym in symbols:
    df = data[sym].dropna()
    close = df['Close']
    current_price = close.iloc[-1]
    entry = entries.get(sym, current_price)
    pl_pct = ((current_price - entry) / entry) * 100

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper_bb = sma20 + 2 * std20

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9).mean()
    macd_hist = macd_line - signal_line

    current_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
    current_upper_bb = float(upper_bb.iloc[-1]) if not pd.isna(upper_bb.iloc[-1]) else None
    current_macd_hist = float(macd_hist.iloc[-1]) if not pd.isna(macd_hist.iloc[-1]) else None
    prev_macd_hist = float(macd_hist.iloc[-2]) if len(macd_hist) > 2 and not pd.isna(macd_hist.iloc[-2]) else None

    signals = []
    reasons = []

    if current_rsi and current_rsi > 70:
        signals.append(True)
        reasons.append('RSI=%.1f (overbought >70)' % current_rsi)

    if current_upper_bb and current_price >= current_upper_bb:
        signals.append(True)
        reasons.append('Price $%.2f >= Upper BB $%.2f' % (current_price, current_upper_bb))

    if current_macd_hist is not None and prev_macd_hist is not None:
        if current_macd_hist < 0 and current_macd_hist < prev_macd_hist:
            signals.append(True)
            reasons.append('MACD death cross (hist=%.4f < prev=%.4f)' % (current_macd_hist, prev_macd_hist))

    flag = 'RED' if signals else 'GREEN'
    signal_str = ' | '.join(reasons) if signals else 'none'
    print('%s %s  Price=$%.2f  Entry=$%.2f  P/L=%.2f%%  RSI=%.1f  UpperBB=$%.2f  MACD=%.4f  => %s' % (
        flag, sym, current_price, entry, pl_pct,
        current_rsi if current_rsi else 0,
        current_upper_bb if current_upper_bb else 0,
        current_macd_hist if current_macd_hist else 0,
        signal_str))
