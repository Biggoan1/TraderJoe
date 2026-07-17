"""
Analyze collected market data for high-probability patterns.
Look for setups that produced 1-2% daily moves and what preceded them.
"""
import json
import numpy as np
import pandas as pd

def main():
    with open("/root/hermes-trader/docs/strategy-dev/market_data_week43.json") as f:
        data = json.load(f)
    
    symbols = data["symbols"]
    
    print("=" * 80)
    print("MARKET DATA ANALYSIS — Week 43 (July 6-10, 2026)")
    print("=" * 80)
    
    # 1. Overall performance summary
    print("\n📊 TOP PERFORMERS (5-day avg daily return)")
    perf = sorted(symbols.items(), key=lambda x: x[1]['avg_daily_return_pct'], reverse=True)
    for sym, info in perf[:15]:
        flag = ""
        if info['avg_daily_return_pct'] >= 1.0:
            flag = " 🔥"
        print(f"  {sym:10s} | avg: {info['avg_daily_return_pct']:+7.2f}%/day | max: {info['max_daily_return_pct']:+7.2f}% | min: {info['min_daily_return_pct']:+7.2f}% | vol: {info['volatility_5d']:5.2f}%{flag}")
    
    print("\n📉 WORST PERFORMERS")
    for sym, info in perf[-10:]:
        print(f"  {sym:10s} | avg: {info['avg_daily_return_pct']:+7.2f}%/day | max: {info['max_daily_return_pct']:+7.2f}% | min: {info['min_daily_return_pct']:+7.2f}% | vol: {info['volatility_5d']:5.2f}%")
    
    # 2. High-volatility symbols (potential for 1-2% moves)
    print("\n🔥 HIGH VOLATILITY SYMBOLS (std dev > 2%)")
    high_vol = sorted(symbols.items(), key=lambda x: x[1]['volatility_5d'], reverse=True)
    for sym, info in high_vol:
        if info['volatility_5d'] >= 2.0:
            print(f"  {sym:10s} | vol: {info['volatility_5d']:5.2f}% | avg return: {info['avg_daily_return_pct']:+7.2f}% | max day: {info['max_daily_return_pct']:+6.2f}%")
    
    # 3. Look at individual days for big moves
    print("\n📈 LARGEST SINGLE-DAY MOVES (any direction)")
    all_moves = []
    for sym, info in symbols.items():
        for day in info['daily_data']:
            all_moves.append({
                "symbol": sym,
                "date": day['date'],
                "return": day['return_1d'],
                "rsi": day['rsi_14'],
                "bb_pos": day['bb_position'],
                "macd_hist": day['macd_hist'],
                "gap_pct": day['gap_pct'],
                "vol_ratio": day['vol_ratio'],
                "momentum_5d": day['momentum_5d'],
                "atr_pct": day['atr_pct'],
                "sma20_ratio": day['sma_20_ratio'],
            })
    
    # Top up moves
    print("\n  Top 10 largest gains:")
    for m in sorted(all_moves, key=lambda x: x['return'], reverse=True)[:10]:
        print(f"    {m['symbol']:10s} | {m['date']} | {m['return']:+6.2f}% | RSI: {m['rsi']:5.1f} | BB pos: {m['bb_pos']:.2f} | MACD: {m['macd_hist']:+.4f} | VolRatio: {m['vol_ratio']:.1f}")
    
    # Top down moves
    print("\n  Top 10 largest drops:")
    for m in sorted(all_moves, key=lambda x: x['return'])[:10]:
        print(f"    {m['symbol']:10s} | {m['date']} | {m['return']:+6.2f}% | RSI: {m['rsi']:5.1f} | BB pos: {m['bb_pos']:.2f} | MACD: {m['macd_hist']:+.4f} | VolRatio: {m['vol_ratio']:.1f}")
    
    # 4. Crypto analysis
    print("\n💰 CRYPTO ANALYSIS")
    crypto = ['BTC-USD', 'ETH-USD', 'SOL-USD']
    for sym in crypto:
        if sym in symbols:
            info = symbols[sym]
            print(f"\n  {sym}:")
            print(f"    Latest: ${info['latest_close']:,.2f}")
            print(f"    5-day avg return: {info['avg_daily_return_pct']:+.2f}%/day")
            print(f"    Volatility: {info['volatility_5d']:.2f}%/day")
            print(f"    Max single day: {info['max_daily_return_pct']:+.2f}%")
            print(f"    Min single day: {info['min_daily_return_pct']:+.2f}%")
            for day in info['daily_data']:
                print(f"    {day['date']}: {day['return_1d']:+.2f}% | RSI: {day['rsi_14']:.1f} | BB pos: {day['bb_position']:.2f} | MACD: {day['macd_hist']:+.4f}")
    
    # 5. Pattern: What happens after big moves?
    print("\n📊 MOMENTUM ANALYSIS — What happens after big moves?")
    print("\n  If a stock goes up > 2% on day N, what happens day N+1?")
    big_up_follow_through = []
    for sym, info in symbols.items():
        for i in range(1, len(info['daily_data'])):
            if info['daily_data'][i-1]['return_1d'] > 2.0:
                next_ret = info['daily_data'][i]['return_1d']
                big_up_follow_through.append({
                    'symbol': sym,
                    'day_n_return': info['daily_data'][i-1]['return_1d'],
                    'next_day_return': next_ret,
                    'rsi_at_signal': info['daily_data'][i-1]['rsi_14'],
                })
    
    if big_up_follow_through:
        avg_next = np.mean([x['next_day_return'] for x in big_up_follow_through])
        win_rate = len([x for x in big_up_follow_through if x['next_day_return'] > 0]) / len(big_up_follow_through)
        print(f"    N={len(big_up_follow_through)} samples")
        print(f"    Next day avg return: {avg_next:+.2f}%")
        print(f"    Follow-through win rate: {win_rate:.0%}")
        for x in big_up_follow_through:
            print(f"    {x['symbol']:10s} | Day N: {x['day_n_return']:+.2f}% | Day N+1: {x['next_day_return']:+.2f}% | RSI: {x['rsi_at_signal']:.1f}")
    else:
        print("    No samples (no stocks moved up > 2% in this period)")
    
    # 6. Pattern: Oversold bounces (RSI < 30)
    print("\n  Oversold bounce test (RSI < 30 → next day?)")
    oversold_bounces = []
    for sym, info in symbols.items():
        for i in range(1, len(info['daily_data'])):
            if info['daily_data'][i-1]['rsi_14'] < 30:
                next_ret = info['daily_data'][i]['return_1d']
                oversold_bounces.append({
                    'symbol': sym,
                    'rsi_at_signal': info['daily_data'][i-1]['rsi_14'],
                    'next_day_return': next_ret,
                })
    if oversold_bounces:
        avg_next = np.mean([x['next_day_return'] for x in oversold_bounces])
        print(f"    N={len(oversold_bounces)} samples")
        print(f"    Next day avg return: {avg_next:+.2f}%")
    else:
        print("    No samples (no RSI < 30 observations)")
    
    # 7. Realistic assessment
    print("\n" + "=" * 80)
    print("🎯 REALISTIC ASSESSMENT")
    print("=" * 80)
    
    # Calculate what % of days had 1%+ moves
    days_1pct_up = len([m for m in all_moves if m['return'] >= 1.0])
    days_1pct_down = len([m for m in all_moves if m['return'] <= -1.0])
    total_days = len(all_moves)
    
    print(f"\n  Total symbol-days observed: {total_days}")
    print(f"  Days with 1%+ gain: {days_1pct_up} ({days_1pct_up/total_days:.0%})")
    print(f"  Days with 1%+ drop: {days_1pct_down} ({days_1pct_down/total_days:.0%})")
    print(f"  Days with 2%+ gain: {len([m for m in all_moves if m['return'] >= 2.0])} ({len([m for m in all_moves if m['return'] >= 2.0])/total_days:.0%})")
    
    # Best symbol for daily returns
    best_sym = max(symbols.items(), key=lambda x: abs(x[1]['avg_daily_return_pct']))[0]
    best_info = symbols[best_sym]
    print(f"\n  Most volatile symbol: {best_sym}")
    print(f"    Volatility: {best_info['volatility_5d']:.2f}%/day")
    print(f"    Avg return: {best_info['avg_daily_return_pct']:+.2f}%/day")
    
    # Crypto comparison
    if 'SOL-USD' in symbols:
        sol = symbols['SOL-USD']
        print(f"\n  SOL-USD (most volatile individual):")
        print(f"    Volatility: {sol['volatility_5d']:.2f}%/day")
        print(f"    Avg return: {sol['avg_daily_return_pct']:+.2f}%/day")
        print(f"    Max day: {sol['max_daily_return_pct']:+.2f}%")
        print(f"    Min day: {sol['min_daily_return_pct']:+.2f}%")
    
    # The hard truth about 1-2%/day
    print(f"\n  🚨 Reality check: 1-2% per day is 250-500% per year (compounded)")
    print(f"     Even the best traders in the world average 10-20% per YEAR.")
    print(f"     What we CAN do:")
    print(f"     - Build a strategy targeting 0.2-0.5% per day on average")
    print(f"     - Use high-volatility assets (crypto, momentum stocks)")
    print(f"     - Run multiple uncorrelated trades per day")
    print(f"     - Accept drawdowns as part of the system")
    
    # Save detailed analysis
    analysis_output = {
        "total_symbols": len(symbols),
        "total_days_observed": total_days,
        "days_1pct_up": days_1pct_up,
        "days_1pct_down": days_1pct_down,
        "days_2pct_up": len([m for m in all_moves if m['return'] >= 2.0]),
        "big_up_follow_through": big_up_follow_through,
        "oversold_bounces": oversold_bounces,
        "top_performers": [(x[0], x[1]) for x in perf[:10]],
        "high_vol_symbols": [(x[0], x[1]) for x in high_vol if x[1]['volatility_5d'] >= 2.0],
    }
    
    with open("/root/hermes-trader/docs/strategy-dev/analysis_output.json", "w") as f:
        json.dump(analysis_output, f, indent=2, default=str)
    
    print(f"\n  Detailed analysis saved to: /root/hermes-trader/docs/strategy-dev/analysis_output.json")

if __name__ == "__main__":
    main()
