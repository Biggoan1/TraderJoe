import yfinance as yf
import pandas as pd
import numpy as np

# Test with SPY first
data = yf.download('SPY', period='3mo', interval='1d', progress=False)
print("Type:", type(data))
print("Columns:", data.columns)
print("Column type:", type(data.columns))
print("Shape:", data.shape)
print("Head:\n", data.head(3))
print()

# Try to get close
if isinstance(data.columns, pd.MultiIndex):
    close = data['Close']
    print("MultiIndex path - Close type:", type(close))
    print("Close head:\n", close.head(3))
else:
    close = data['Close']
    print("SingleIndex path - Close type:", type(close))
    print("Close head:\n", close.head(3))

# Check if close is scalar or Series
print("Close value at -1:", close.iloc[-1])
print("Close value at -1 type:", type(close.iloc[-1]))
print("Float of -1:", float(close.iloc[-1]))
