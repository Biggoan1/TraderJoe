# Market Screener

Automatically scans a broad universe of approximately 170 tradeable symbols
(top stocks by market cap, sector leaders, and ETFs) and maintains a separate
**trending watchlist** ranked by technical score.

## Files

- `/root/hermes-trader/market_screener.py` - Main screener script
- `/root/hermes-trader/trending_watchlist.json` - Output: top candidates with full analysis
- `/root/hermes-trader/screener_log.txt` - Human-readable summary
- `/root/hermes-trader/traderjoe_screener.sh` - Bash wrapper for cron, also in `.hermes-profile/scripts/`

## CLI Commands

```bash
# View current trending watchlist
cd /root/hermes-trader && .venv/bin/python trader_cli.py trending

# Run screener now and save to trending_watchlist.json
cd /root/hermes-trader && .venv/bin/python trader_cli.py screener

# Run screener directly
cd /root/hermes-trader && .venv/bin/python market_screener.py
```

## Cron Job

Job `Market Screener` runs at **8:00 AM ET weekdays** (1.5 hours before market
open), scans the universe, and delivers top trending candidates via Telegram.

## How It Works

1. Scans approximately 170 symbols across mega-cap tech, banks, healthcare,
   consumer, energy, industrials, semiconductors, and ETFs.
2. Applies the same 4-of-6 gate evaluation as `trader.py`:
   - Price above SMA20
   - SMA20 above SMA50
   - RSI < 70 (not overbought)
   - MACD histogram positive
   - ADX > 15 (trend strength)
   - Volume > 80% of 20-day average
3. Scores each setup on a 0-10 scale, higher is better.
4. Separates results:
   - `top_qualified` - all qualified (4+ gates), sorted by score
   - `trending` - qualified symbols not on manual watchlist, not owned, and not on cooldown
5. Excludes symbols already on the user's manual watchlist, current positions,
   and cooldown.

## Using Trending Data

When the user asks about the market or wants new ideas:

1. Check `trending_watchlist.json` with `trader_cli.py trending`.
2. Present top candidates with score, price, gates, RSI, and ADX.
3. If the user approves, add to watchlist with `trader_watch_symbol`.
4. Run full analysis with `trader_queue_buy` for detailed evaluation.

## Auto-Add Workflow

When the screener finds strong candidates (score >= 7.5, 5+ gates):

1. Present the top 5 to the user with key metrics.
2. Ask whether they want to add any to the watchlist.
3. Add approved symbols and run the buy analysis scan.

## Pitfalls

- Screener takes approximately 120 seconds for 166 symbols; schedule early enough before market open.
- Three delisted symbols (GPS, PARA, SQ) fail on yfinance; these are expected and logged.
- Pre-market vs after-hours: screener works at any time but prices may differ from market open.
- Do not auto-add to watchlist without user approval; keep manual watchlist under user control.
- Screener universe is static; update `MARKET_UNIVERSE` in the script to add or remove symbols.
