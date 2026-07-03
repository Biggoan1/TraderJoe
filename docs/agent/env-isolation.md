# Environment File Isolation and Production Safeguards

Trader Joe operates across four independent trading contexts. Each
context has its own credentials, its own env file, and its own strict
isolation guarantees. Sharing credentials — even accidentally — breaks
the risk model that lets research and paper trading coexist safely with
future live trading.

**None of the four env files are tracked in git.** Only `.env.example`,
the placeholder template, is tracked.

---

## The four env files

### `.env.paper` — normal paper trading

- **Used by:** `trader.py` (equities paper trading) and any script that
  drives the live runner in paper mode.
- **Credentials:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
  `ALPACA_ENDPOINT` (paper endpoint), plus `OPENAI_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` for approvals.
- **Alpaca account:** the normal paper account. `PAPER = True` in
  `trader.py` gates every `TradingClient(..., paper=PAPER)`
  construction — do not change it without following the Production
  Safeguards below.
- **Do NOT** put research or production credentials in this file.

### `.env.crypto` — crypto paper trading / crypto testing

- **Used by:** `crypto_trader.py`.
- **Credentials:** `CRYPTO_ALPACA_API_KEY`, `CRYPTO_ALPACA_SECRET_KEY`
  (optionally falling back to `ALPACA_*` per current code), plus
  Telegram + OpenAI keys.
- **Alpaca account:** the dedicated crypto testing paper account.
  `PAPER = True` in `crypto_trader.py` gates every
  `TradingClient(..., paper=PAPER)` construction.
- **Do NOT** put equities paper, research, or production credentials
  in this file.

### `.env.research` — historical validation / replay / Backtest Lab

- **Used by:** `strategy/research_account.py` (the isolated Research
  Alpaca client) and, in the future, `t_phase5_two_month_validation_run`.
- **Credentials:** `RESEARCH_ALPACA_API_KEY`,
  `RESEARCH_ALPACA_SECRET_KEY`, `RESEARCH_ALPACA_ENDPOINT`.
- **Alpaca account:** the dedicated Research paper account —
  distinct from the normal paper account. Research is
  read-only: `ResearchAccountClient` exposes only `fetch_bars`,
  `list_calendar`, and `paper_account_info`. No order-placement
  method exists on it.
- **Hard rules:**
  - `strategy/research_account.py` refuses to fall back to
    `ALPACA_*` / `APCA_*` env values — a Research run that does not
    have `RESEARCH_ALPACA_*` set will error out rather than reuse
    the paper account's credentials.
  - No Phase 1-4 module may import `strategy.research_account`.
  - The live runner (`trader.py`, `crypto_trader.py`,
    `strategy/runner.py`, `trader_cli.py`, `telegram_approvals.py`,
    any scheduler) must NEVER read `RESEARCH_ALPACA_*` or import
    the Research client.

### `.env.production` — future live trading (real money)

- **Used by:** nothing today. The codebase does not run against a
  production account and does not read a production env file. This
  file exists only as a placeholder in the isolation model.
- **Credentials:** none yet. **`.env.production` MUST remain empty
  until an explicit production ApprovalRecord authorises live
  trading.** See `strategy/promotion_gates.py` (`STATE_APPROVED`
  and `STATE_PRODUCTION`) and the "v1.0 Production Readiness"
  section in `ROADMAP.md` for the promotion gate that must be
  cleared before any credentials are ever written here.
- **Production means real money.** Real orders, real fills, real
  capital at risk. Every safeguard elsewhere in this repo is
  designed to keep that boundary clear.
- **Do NOT** copy credentials from any other `.env.*` file into
  `.env.production`.

---

## Isolation rules — all four contexts

These invariants hold across every phase of the codebase:

1. **Credentials are never shared across accounts.** Paper, crypto,
   research, and production each hold their own Alpaca account with
   its own key pair.
2. **Research credentials never reach the live runner.**
   `strategy/research_account.py` is the only module that reads
   `RESEARCH_ALPACA_*`, and it must not be imported by any live
   runner, scheduler, Telegram approval path, CLI entry, or plugin.
3. **Paper and crypto credentials never reach research paths.** The
   Research client refuses to fall back to `ALPACA_*` / `APCA_*` so
   a misconfigured environment fails loudly.
4. **Production credentials do not exist yet.** No module reads
   `.env.production`. Any future code that would use it must ship
   inside an approved promotion transition with an
   `ApprovalRecord` naming the approver, scope, monitoring
   dashboard, and rollback plan.

---

## Production Safeguards

Trader Joe does not currently run against a live production account.
The guard rail today is a hardcoded module constant:

- `trader.py`: `PAPER = True` (line ~25)
- `crypto_trader.py`: `PAPER = True` (line ~39)

Both are passed as `paper=PAPER` to the Alpaca `TradingClient`.

### Before flipping `PAPER` to `False`

All of the following must be satisfied:

1. An `ApprovalRecord` (see `strategy/promotion_gates.py`) exists in
   an operations record that names the approver, dated approval,
   flag scope, monitoring dashboard, and rollback plan.
2. The relevant `PromotionEntry` sits at
   `STATE_APPROVED` or `STATE_PRODUCTION` with no triggered rollback
   alerts across `STANDARD_ROLLBACK_CRITERIA`.
3. Walk-forward evidence, paper-trading evidence, and (once
   `t_phase5_two_month_validation_run` lands) the two-month
   historical validation evidence all pass the promotion criteria in
   ROADMAP §"v1.0 Production Readiness".
4. `.env.production` has been populated with the approved
   credentials and is loaded by the same commit that flips
   `PAPER = False`.
5. A rollback drill has been executed and documented within the
   last 30 days.

The current codebase does not enforce these programmatically because
no live-mode code path exists yet. Reviewers of any PR that touches
`PAPER =` on either file MUST verify the checklist above and cite the
approving `ApprovalRecord` id in the PR body.

### For automation and CI

- CI runs must not have `.env.production` present. If a future CI
  step needs credentials, it must load `.env.paper`, `.env.crypto`,
  or `.env.research` explicitly and never fall through to
  `.env.production`.
- Any future orchestrator that resolves an env file by name should
  refuse `.env.production` unless a `HERMES_PRODUCTION_APPROVAL_ID`
  env var is set and matches a recorded `ApprovalRecord`.

---

## `.gitignore` invariants

The following must always hold:

- `.env` and every `.env.*` variant are ignored.
- `.env.example` (the placeholder template) is tracked as an
  explicit `!.env.example` whitelist entry.

If you edit `.gitignore`, keep the whitelist entry in place and
verify with:

```
git check-ignore -v .env .env.paper .env.crypto .env.research .env.production .env.example
```

`.env.example` must not appear as ignored; every other listed file
must.

---

## What lives in `.env.example`

`.env.example` documents the union of every namespace the codebase
knows how to read. Every value is a placeholder. Contributors copy
the sections they need into the appropriate `.env.<context>` file
and populate them locally.
