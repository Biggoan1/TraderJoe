"""
Collect last week's market data for strategy development.
Downloads daily OHLCV + key indicators for watchlist symbols.
Saves structured JSON for analysis.
"""
import json
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys
import time

SYMBOLS = [
    # Core stocks - high momentum
    "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "AMZN", "GOOGL", "META", 
    "NFLX", "AFRM", "DASH", "DDOG", "PANW", "CRDO", "MRVL", "INTC",
    # ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLI", "XLV", "XLP", 
    "XLU", "XLY", "XLB", "ARKK", "SMH",
    # Fixed income / hedges
    "TLT", "IEF", "LQD", "GLD", "VNQ", "SPCX", "ONCY",
    # Crypto
    "BTC-USD", "ETH-USD", "SOL-USD",
]

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute key technical indicators on OHLCV data."""
    close = df['Close']
    high = df['High']
    low = df['Low']
    volume = df['Volume']
    
    # Moving averages
    df['sma_5'] = close.rolling(5).mean()
    df['sma_20'] = close.rolling(20).mean()
    df['sma_50'] = close.rolling(50).mean()
    
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema_12 = close.ewm(span=12).mean()
    ema_26 = close.ewm(span=26).mean()
    df['macd'] = ema_12 - ema_26
    df['macd_signal'] = df['macd'].ewm(span=9).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # Bollinger Bands
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    df['bb_upper'] = bb_mid + 2 * bb_std
    df['bb_lower'] = bb_mid - 2 * bb_std
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / bb_mid
    
    # ATR
    high_low = high - low
    high_close = (high - close.shift()).abs()
    low_close = (low - close.shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr_14'] = true_range.rolling(14).mean()
    
    # Gap % (overnight gap from previous close)
    df['gap_pct'] = (df['Open'] - close.shift(1)) / close.shift(1) * 100
    
    # Volume ratio (today vs 20-day avg)
    df['vol_ratio'] = volume / volume.rolling(20).mean()
    
    # 5-day momentum
    df['momentum_5d'] = (close / close.shift(5) - 1) * 100
    
    # Returns
    df['return_1d'] = close.pct_change() * 100
    df['return_5d'] = (close / close.shift(5) - 1) * 100
    df['return_20d'] = (close / close.shift(20) - 1) * 100
    
    return df


def main():
    print(f"Collecting data for {len(SYMBOLS)} symbols...")
    print(f"Symbols: {SYMBOLS}")
    
    results = {}
    errors = []
    
    # Download in batches of 10 with delays
    batch_size = 10
    delay = 2  # seconds between batches
    
    for i in range(0, len(SYMBOLS), batch_size):
        batch = SYMBOLS[i:i+batch_size]
        print(f"\nBatch {i//batch_size + 1}/{(len(SYMBOLS)-1)//batch_size + 1}: {batch}")
        
        try:
            tickers = yf.Tickers(" ".join(batch))
            time.sleep(delay)
            
            for sym in batch:
                try:
                    # Get 60 days of data (need enough for SMA50)
                    ticker = yf.Ticker(sym)
                    df = ticker.history(period="60d")
                    
                    if df.empty or len(df) < 20:
                        errors.append({"symbol": sym, "error": f"Insufficient data ({len(df)} rows)"})
                        print(f"  ⚠ {sym}: insufficient data")
                        continue
                    
                    # Keep only last 5 trading days + enough for indicators
                    df = df.tail(25).copy()
                    df = compute_indicators(df)
                    
                    # Only keep last 5 days for analysis
                    df_last5 = df.tail(5)
                    
                    results[sym] = {
                        "symbol": sym,
                        "days_analyzed": len(df_last5),
                        "latest_close": float(df_last5.iloc[-1]['Close']),
                        "latest_date": str(df_last5.iloc[-1].name.strftime("%Y-%m-%d")),
                        "daily_data": [
                            {
                                "date": str(row.name.strftime("%Y-%m-%d")),
                                "open": float(row['Open']),
                                "high": float(row['High']),
                                "low": float(row['Low']),
                                "close": float(row['Close']),
                                "volume": int(row['Volume']),
                                "rsi_14": round(float(row.get('rsi_14', 0)) if pd.notna(row.get('rsi_14')) else 0, 2),
                                "macd_hist": round(float(row.get('macd_hist', 0)) if pd.notna(row.get('macd_hist')) else 0, 4),
                                "bb_position": round(float((row['Close'] - row['bb_lower']) / (row['bb_upper'] - row['bb_lower'])) if pd.notna(row.get('bb_upper')) and row['bb_upper'] != row['bb_lower'] else 0, 4),
                                "atr_pct": round(float(row['atr_14'] / row['Close'] * 100) if pd.notna(row.get('atr_14')) else 0, 2),
                                "gap_pct": round(float(row.get('gap_pct', 0)) if pd.notna(row.get('gap_pct')) else 0, 2),
                                "vol_ratio": round(float(row.get('vol_ratio', 1)) if pd.notna(row.get('vol_ratio')) else 1, 2),
                                "momentum_5d": round(float(row.get('momentum_5d', 0)) if pd.notna(row.get('momentum_5d')) else 0, 2),
                                "return_1d": round(float(row.get('return_1d', 0)) if pd.notna(row.get('return_1d')) else 0, 2),
                                "return_5d": round(float(row.get('return_5d', 0)) if pd.notna(row.get('return_5d')) else 0, 2),
                                "sma_20_ratio": round(float(row['Close'] / row['sma_20']) if pd.notna(row.get('sma_20')) and row['sma_20'] != 0 else 0, 4),
                                "bb_width": round(float(row.get('bb_width', 0)) if pd.notna(row.get('bb_width')) else 0, 4),
                            }
                            for _, row in df_last5.iterrows()
                        ],
                        "avg_daily_return_pct": round(float(df_last5['return_1d'].mean()), 2),
                        "max_daily_return_pct": round(float(df_last5['return_1d'].max()), 2),
                        "min_daily_return_pct": round(float(df_last5['return_1d'].min()), 2),
                        "avg_daily_volume": int(df_last5['Volume'].mean()),
                        "volatility_5d": round(float(df_last5['return_1d'].std()), 2),
                    }
                    print(f"  ✓ {sym}: ${results[sym]['latest_close']:.2f}, avg return {results[sym]['avg_daily_return_pct']:+.2f}%/day, vol {results[sym]['volatility_5d']:.2f}%")
                    
                except Exception as e:
                    errors.append({"symbol": sym, "error": str(e)})
                    print(f"  ✗ {sym}: {e}")
        except Exception as e:
            print(f"  Batch error: {e}")
    
    # Save results
    output = {
        "collection_time": datetime.now().isoformat(),
        "total_symbols": len(SYMBOLS),
        "successful": len(results),
        "errors": errors,
        "symbols": results,
    }
    
    output_path = "/root/hermes-trader/docs/strategy-dev/market_data_week43.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    
    print(f"\n{'='*60}")
    print(f"Collected {len(results)}/{len(SYMBOLS)} symbols")
    print(f"Errors: {len(errors)}")
    print(f"Saved to: {output_path}")
    
    # Quick summary stats
    if results:
        returns = [s['avg_daily_return_pct'] for s in results.values()]
        print(f"\nAvg return across all symbols: {np.mean(returns):+.2f}%/day")
        print(f"Max 5-day avg return: {max(results.items(), key=lambda x: x[1]['avg_daily_return_pct'])[0]} ({max(returns):+.2f}%)")
        print(f"Min 5-day avg return: {min(results.items(), key=lambda x: x[1]['avg_daily_return_pct'])[0]} ({min(returns):+.2f}%)")

if __name__ == "__main__":
    main()
