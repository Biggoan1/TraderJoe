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

## Hermes Cron

- `traderjoe-market-scan`: weekdays every 15 minutes during U.S. market hours (`09:30-16:00 America/Detroit`)
- `traderjoe-market-closed-notice`: weekdays at `09:05 America/Detroit`; emits a notice only on major U.S. market holidays
- `traderjoe-daily-report`: weekdays at `16:15 America/Detroit`

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
the explicit slash approval commands documented in `docs/agent/trading-reference.md`.

## Telegram Token Rotation

If rotating the Telegram bot token, update both files:

- `/root/hermes-trader/.env`
- `/root/hermes-trader/.hermes-profile/.env`

Then restart Hermes Gateway:

```bash
systemctl restart hermes-gateway.service
```
