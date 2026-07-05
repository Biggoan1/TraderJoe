# Evening Routine

The end-of-day workflow. Owned primarily by the Evening Operator
persona, with support from Portfolio Manager and Research Analyst.
Every step below produces a documented artifact.

## When it runs

- **After 16:00 ET.**
  `strategy/market_calendar.py::REGULAR_SESSION_CLOSE = time(16, 0)`.
- **Same day.** The digest reports on the trading day just completed.
- **Weekday only.** Weekend "learning" work is deferred to the
  Research Analyst's weekend lab (see below).

## Step 1 — Position review

Close-of-day snapshot of the current paper account:

```
python trader_cli.py positions
```

Returns JSON with open positions, unrealized P&L, and current market
value. The Evening Operator cross-references against the account's
day-start snapshot to identify:

- Positions closed today (with exit reason).
- Positions still open (with unrealized P&L trend).
- Positions carrying elevated risk (`stop-loss triggered`, drawdown
  >5%, RSI > 70).

## Step 2 — Trade journal

`strategy/trade_logger.py` records every closed trade in `trades.db`
with a canonical exit reason:

- `RSI overbought` — RSI(14) > 70 at close.
- `MACD death cross` — MACD line crosses below signal line.
- `upper Bollinger` — close hit upper BB (2σ).
- `six-gate failure` — Champion scorer no longer qualifies the
  setup.
- `user decision` — operator manually closed.
- `tanking timer` — 5-minute tanking trigger fired.

The Evening Operator reviews each exit reason against the trade's
`entry_score` (the six-gate score at entry) and writes any anomalies
into `research_notes/YYYY-MM-DD.md`. An anomaly is any position where:

- Exit reason ≠ entry hypothesis (e.g. entered on breakout momentum
  but exited on mean-reversion signal).
- Realised P&L is more than 2σ from the strategy's historical mean
  P&L per hold.
- Hold duration is more than 2× the strategy's historical average.

## Step 3 — Daily digest

```
python scripts/generate_daily_digest.py
```

or with a specific date:

```
python scripts/generate_daily_digest.py --date 2026-07-04
```

The digest (`strategy/daily_digest.py`):

- Pulls today's trades from `trades.db`.
- Computes daily metrics: total P&L, win rate, avg winner, avg
  loser, profit factor, best trade, worst trade.
- Lists open positions with unrealized P&L.
- Includes a feature-flag snapshot (`FeatureFlags.enabled_flags`).
- Sends the summary to Telegram (via `TELEGRAM_BOT_TOKEN` +
  `TELEGRAM_CHAT_ID`).
- Saves Markdown to `~/hermes-trader/reports/daily_digest_YYYY-MM-DD.md`.

`--dry-run` prints the digest without sending Telegram or writing a
file.

## Step 4 — Portfolio-level review

The Portfolio Manager sanity-checks against the day-end state:

- Drawdown from account peak — target ≤ 5%.
- Single-position exposure — target ≤ 25% of equity.
- Sector concentration — target ≤ 50%.
- Correlation of top 3 positions — target ≤ 0.8.
- Cash reserve — target ≥ 20% for opportunistic entries.

Portfolio-level anomalies trigger escalation to the Risk Manager.

## Step 5 — Learning (deferred by default)

Real learning work — sweeps, walk-forward, matrix runs — happens on
the weekend or on demand, not every evening. But the Evening Operator
can *file* hypotheses into the queue for later review:

```python
# Via the HypothesisQueue API
from strategy.lab.hypothesis_queue import HypothesisQueue, ExperimentProposal

queue = HypothesisQueue()
proposal = ExperimentProposal(
    title="Test lower min_gate_count in Bear regimes",
    strategy_name="champion",
    suggested_parameters={"min_gate_count": 3},
    motivation="Champion produced no entries during 2026-Q1 whipsaw",
    confidence=0.4,
    source="evening-operator",
)
queue.propose(proposal)
```

The queue is JSONL-backed under `reports/hypothesis_queue/`. Same
identity (title + strategy + params) → same proposal id → idempotent.

Weekend lab (Saturday):

```
./scripts/weekend-lab
```

or

```
python -m strategy.lab.weekend_lab
```

Full pipeline: warehouse import → strategy matrix → parameter sweeps
→ leaderboard → HTML dashboard under
`reports/weekend_lab/<run_id>/`.

## Step 6 — Planning tomorrow's brief

The Evening Operator prepares carry-over notes for the Morning
Operator:

- Open positions and their exit criteria.
- Overnight risk flags (positions with elevated overnight gap risk).
- Pending approvals still sitting in `trades.db`.
- Watchlist adjustments (adds/removes based on today's activity).
- Any Research Analyst evidence packages waiting for review.

The notes are written to `research_notes/tomorrow-YYYY-MM-DD.md`
(same directory the trade journal lands in). The Morning Operator
reads that file as part of Step 1 of the morning routine.

## Step 7 — Safety and hygiene

Before signing off, the Evening Operator verifies:

- Test suite still green — `pytest -q` should return `2361 passed`
  at the milestone tag.
- `git status` — no unintended local changes to `trader.py`,
  `crypto_trader.py`, or `strategy/runner.py`.
- Log rotation — daemon logs under `reports/paper_daemon/` and
  bridge logs under `reports/paper_bridge/` are not eating disk.
- Telegram bot still responsive to `/status`.
- Emergency stop files at `/etc/traderjoe/STOP_*` still **absent**
  (or intentionally present).

## Reports the Evening Operator produces

- `reports/daily_digest_YYYY-MM-DD.md` — the primary artifact.
- `research_notes/YYYY-MM-DD.md` — trade journal anomalies.
- `research_notes/tomorrow-YYYY-MM-DD.md` — carry-over notes.
- Optional: hypothesis queue entries under
  `reports/hypothesy_queue/*.jsonl` (typo in path due to
  legacy convention — see `strategy/lab/hypothesis_queue.py`).

Actual path: `reports/hypothesis_queue/*.jsonl`.

## Weekly cadence

- **Saturday morning:** Research Analyst runs the weekend lab
  (`./scripts/weekend-lab`) with the default strategies and sweeps.
  Publishes HTML dashboard under `reports/weekend_lab/<run_id>/`.
- **Sunday evening:** Research Analyst reviews the weekend results
  and files hypotheses for the coming week.
- **Weekday evenings:** Evening Operator runs the daily digest and
  files anomalies. Learning work is deferred to the weekend.

## Monthly cadence

- **First Monday of the month:** Research Analyst runs
  `scripts/research-run-matrix` across `("60d","90d","6mo","1y","ytd")`
  windows to produce a `MatrixSummary`. Publishes under
  `reports/research_summaries/matrix-<date>-summary.md`.
- **Mid-month:** Risk Manager audits `.env.production` emptiness,
  `PAPER = True` on both live-runner constants, feature flag
  defaults, and gitignore invariants.

## Quiet mode

If the system is intentionally quiet — no positions, no research
runs, no daemon activity — the Evening Operator still produces:

- `reports/daily_digest_YYYY-MM-DD.md` — recording zero trades.
- A brief `research_notes/YYYY-MM-DD.md` line noting quiet day.

Consistency of the audit trail matters more than volume of output.

## What the Evening Operator never does

- Never places or approves orders after the close.
- Never enables feature flags.
- Never advances `PromotionEntry` states.
- Never modifies `trader.py`, `crypto_trader.py`, or
  `strategy/runner.py`.
- Never runs an auto-execute daemon in production mode.
- Never touches `.env.production`.

Every "never" is enforced by the safety layers in
[`safety-model.md`](safety-model.md).
