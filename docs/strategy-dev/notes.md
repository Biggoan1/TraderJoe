# Strategy Development Notes

## Goal
Build a strategy that can reliably generate 1-2% daily returns from market data.

## Process
1. Analyze last week's market data → identify patterns
2. Design strategy based on findings
3. Backtest and iterate
4. Update this file after each step

---

## Sprint 7 — Pattern Discovery & Strategy Development
**Started:** July 11, 2026

### Phase 1: Data Collection
**Status:** In progress

#### Symbols under analysis
- **Watchlist (core):** NVDA, TSLA, AMD, AAPL, MSFT, AMZN, GOOGL, META, NFLX, AFRM, DASH, DDOG, PANW, CRDO, MRVL, INTC, ARKK, SMH
- **ETFs (regime):** SPY, QQQ, IWM, DIA, XLK, XLF, XLE, XLI, XLV, XLP, XLU, XLG, XLY, XLB
- **Fixed Income (hedge):** TLT, IEF, LQD, GLD, VNQ, SPCX, ONCY
- **Crypto:** BTC, ETH, SOL

#### Data parameters
- Timeframe: 1-day candles
- Lookback: last 5 trading days (July 6-10, 2026)
- Indicators computed: RSI(14), MACD, BB(20,2), SMA(20,50,200), ATR(14), VWAP, volume ratio, gap %, momentum(5d)

### Phase 2: Pattern Analysis
**Status:** Pending

### Phase 3: Strategy Design
**Status:** Pending

### Phase 4: Backtesting
**Status:** Pending

### Phase 5: Validation
**Status:** Pending

---

## Lessons Learned
- Downloading 49+ symbols simultaneously causes timeout — must batch
- Need to be conservative with data windows
