# Trader Joe Agent Guide

This file is the lightweight entry point for agents working in
`/root/hermes-trader`. Keep it small. Do not paste long-lived reference
material here; put that material in `docs/agent/` or the existing dedicated
project docs and link to it from this index.

## Loading Policy

Read only the documents needed for the current task.

- Trading commands, approval flow, watchlists, risk rules, or portfolio tools:
  read `docs/agent/trading-reference.md`.
- Hermes Gateway, Telegram polling, cron jobs, service status, rollback, or
  token rotation: read `docs/agent/operations.md`.
- Environment files (`.env.paper` / `.env.crypto` / `.env.research` /
  `.env.production`), credential isolation, or the pre-live safeguards
  around `PAPER = True`: read `docs/agent/env-isolation.md`.
- Hermes Agent plugin migration or integration architecture: read
  `docs/agent/hermes-integration.md`.
- Sprint planning, feature flags, roadmap, version, or current project state:
  read `ROADMAP.md` and `STATUS.md`.
- Release history or previous sprint contents: read `CHANGELOG.md`.
- Market screener behavior, trending watchlist, or screener cron: read
  `docs/agent/market-screener.md`.

If a task spans multiple areas, load the minimum set of documents that covers
the request. Avoid bulk-reading all Markdown files.

## Current Project Shape

Trader Joe is an Alpaca paper-trading system integrated with Hermes Agent and
Telegram. The active profile is `traderjoe`, with profile data at
`/root/hermes-trader/.hermes-profile`.

Important runtime files:

- `trader.py` - stock trading scan and strategy logic.
- `crypto_trader.py` - crypto Elliott Wave scan logic.
- `telegram_approvals.py` - approval and Telegram workflow logic.
- `trader_cli.py` - JSON bridge used by Hermes plugin tools.
- `.hermes-profile/plugins/traderjoe/` - Hermes plugin registration, schemas,
  and tool handlers.
- `strategy/` - feature-flagged strategy infrastructure and observational
  market intelligence modules.
- `tests/` - project tests.

## Engineering Rules

- Keep Champion behavior functional.
- Keep feature flags disabled unless explicitly approved.
- Every sprint should be independently testable and reversible.
- Do not mix multiple behavior-changing trading features in one sprint.
- Preserve paper-trading safety boundaries.
- Prefer small, focused edits that match existing module boundaries.

## Verification

For code changes, run the narrowest meaningful tests first, then broaden when
the change touches shared strategy, persistence, or user-facing workflows.

Common commands:

```bash
.venv/bin/python -m pytest
.venv/bin/python trader_cli.py positions
.venv/bin/python trader_cli.py pending
```

Use `.venv/bin/python` from `/root/hermes-trader` unless a task says otherwise.

## Documentation Maintenance

When changing operational behavior, update the relevant file under
`docs/agent/`. When changing roadmap, sprint status, or release state, update
`ROADMAP.md`, `STATUS.md`, and `CHANGELOG.md` as appropriate.

`AGENTS.md` must stay under 10 KB; the target size is 5-8 KB.
