# Trader Joe Trading Reference

Trader Joe is an Alpaca paper-trading system. Keep paper mode explicit and do
not claim that queued trades executed.

## Telegram Commands

- `/watch TSLA`
- `/buy TSLA`
- `/buy TSLA 1000`
- `/sell AMZN`
- `/sell AMZN 500`
- `/sell AMZN all`
- `/sell all` or `/sell-all` or `/liquidate`
- `/keep TSLA`
- `/scan`
- `/positions`
- `/report`
- `/pending-trades`
- `/trade-approve ID`
- `/trade-approve ID AMOUNT`
- `/trade-reject ID`

`/sell all` executes paper sells for every open position. `keep SYMBOL` keeps
an alerted name. A tanking alert is sent when a watched symbol drops sharply;
if no reply arrives before the 5-minute timer expires, Trader Joe auto-sells
the alerted position in paper mode.

Natural-language requests are also available through the Trader Joe Hermes
tools. Buy and sell tools usually queue approvals, but auto-approve mode is on
for routine paper trades, so eligible orders execute immediately after risk
checks. The scan job now auto-picks the strongest qualifying ETF setup and
trades it without asking during market hours. `/sell all` is a direct
paper-liquidation command. Only `/trade-approve` submits an Alpaca paper order.

## Safety Rules

- This is Alpaca paper trading only.
- A buy or sell request queues an approval; it does not submit an order unless
  auto-approve/routine paper-trade logic applies in the underlying Trader Joe
  checks.
- Never claim that a queued trade executed.
- Never submit orders through model-callable tools.
- Tell the user to use `/trade-approve ID` to submit the queued default amount.
- Tell the user to use `/trade-approve ID AMOUNT` to override the queued amount.
- Tell the user to use `/trade-reject ID` to reject a queued trade.
- Default risk per trade: **0.5% of portfolio** unless explicitly overridden.
- Buys are capped by Trader Joe's risk checks.
- Sells cannot exceed the current position value.
- `sell SYMBOL all` means queue the full current position.

## Hermes Tool Mapping

- "Watch TSLA" -> call `trader_watch_symbol`.
- "Buy TSLA 1000" -> call `trader_queue_buy` with `symbol=TSLA`, `amount=1000`.
- "Sell AMZN all" -> call `trader_queue_sell` with `symbol=AMZN`, `sell_all=true`.
- "Show my positions" -> call `trader_list_positions`.
- "Show pending trades" -> call `trader_list_pending`.
- "Send my report" -> call `trader_portfolio_report`.

## ETF Universe

Core:

- SPY, QQQ, IWM, DIA

Sector rotation:

- XLF, XLK, XLE, XLY, XLP, XLV, XLI, XLB, XLU, SMH

Defensive:

- TLT, IEF, LQD, GLD, VNQ

Momentum:

- ARKK

## Stock Watchlist

- AAPL, MSFT, NVDA, AVGO, AMD, TSLA, AMZN, INTC

ETF stays first, but these stocks are watched alongside it.
