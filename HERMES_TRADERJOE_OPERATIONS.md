# Hermes Trader Joe Operations

Hermes Gateway owns Telegram polling. Do not run the stale
`traderjoe.service` while `hermes-gateway.service` is active because Telegram
permits only one `getUpdates` poller per bot token.

Trader Joe is registered as the named Hermes profile `traderjoe`.

```bash
hermes -p traderjoe config show
traderjoe config show
hermes-trader config show
```

`traderjoe` and `hermes-trader` both point at the same profile data:

```text
/root/.hermes/profiles/traderjoe -> /root/hermes-trader/.hermes-profile
```

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

`/sell all` executes paper sells for every open position. `keep SYMBOL` keeps an alerted name. A tanking alert is sent when a watched symbol drops sharply; if no reply arrives before the 5-minute timer expires, Trader Joe auto-sells the alerted position in paper mode.

Natural-language requests are also available through the Trader Joe Hermes
tools. Buy and sell tools usually queue approvals, but auto-approve mode is on for routine paper trades, so eligible orders execute immediately after risk checks. The scan job now auto-picks the strongest qualifying ETF setup and trades it without asking during market hours. `/sell all` is a direct paper-liquidation command. Only `/trade-approve` submits
an Alpaca paper order.

## Hermes Cron

- `traderjoe-market-scan`: weekdays every 15 minutes during U.S. market hours (`09:30–16:00 America/Detroit`)
- `traderjoe-market-closed-notice`: weekdays at `09:05 America/Detroit`; emits a notice only on major U.S. market holidays
- `traderjoe-daily-report`: weekdays at `16:15 America/Detroit`

Default risk per trade: **0.5% of portfolio** unless explicitly overridden.

ETF universe:

- *Core:* SPY, QQQ, IWM, DIA
- *Sector rotation:* XLF, XLK, XLE, XLY, XLP, XLV, XLI, XLB, XLU, SMH
- *Defensive:* TLT, IEF, LQD, GLD, VNQ
- *Momentum:* ARKK

Stock watchlist:

- AAPL, MSFT, NVDA, AVGO, AMD, TSLA, AMZN, INTC

ETF stays first, but these stocks are watched alongside it.

List jobs:

```bash
./traderjoe-agent cron list
```

## Service Status

```bash
systemctl status hermes-gateway.service
systemctl status traderjoe.service
```

Expected state:

- `hermes-gateway.service`: active and enabled
- `traderjoe.service`: inactive and disabled

`hermes profile list` may report the gateway as stopped when run without host
systemd access. Trust `systemctl status hermes-gateway.service` for the
system service state.

## Rollback

```bash
systemctl disable --now hermes-gateway.service
systemctl enable --now traderjoe.service
```

The legacy daemon understands numeric replies such as `12 y`, but Hermes uses
the explicit slash approval commands documented above.

## Telegram Token Rotation

If rotating the Telegram bot token, update both files:

- `/root/hermes-trader/.env`
- `/root/hermes-trader/.hermes-profile/.env`

Then restart Hermes Gateway:

```bash
systemctl restart hermes-gateway.service
```
