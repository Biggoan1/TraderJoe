# Morning Routine

The pre-market workflow. Owned primarily by the Morning Operator
persona, with Research Analyst and Portfolio Manager providing
supporting context. Every step below produces a documented artifact.

## When it runs

- **Weekday only.** The `strategy/market_calendar.py::session_status`
  helper returns `weekend` on Saturday and Sunday; no morning routine
  fires.
- **Non-holiday only.** `us_market_holidays(year)` returns the NYSE
  closed-day set (New Year, MLK, Presidents, Good Friday, Memorial,
  Juneteenth, July 4, Labor, Thanksgiving, Christmas — all with
  observance shifts). `session_status` returns `holiday` on those
  days.
- **Before 09:30 ET.** The routine's target artifacts are delivered
  before `REGULAR_SESSION_OPEN = time(9, 30)`.

Fires manually via the Morning Operator persona; scheduled via
`docs/systemd/traderjoe-paper.service` for the live scan pipeline.

## Step 1 — Market preparation

The Morning Operator reads the observational analytics modules to
build a market-regime baseline:

- **`strategy/market_regime.py`** — classifies regime as `bullish` /
  `bearish` / `volatile` / `neutral` using SPY/QQQ vs 20d MA, ATR%,
  ADX(14), MACD histogram.
- **`strategy/overnight_risk.py`** — gap analysis over previous
  close vs pre-market / after-hours. Outputs `overnight_gap_pct`,
  `gap_direction`, `significant_gap`, `risk_score` (0-100),
  `data_quality`.
- **`strategy/sector_leadership.py`** — ranks sector ETFs (SPDR
  family: `XLC/XLY/XLP/XLE/XLF/XLV/XLI/XLB/XLRE/XLK/XLU`) by
  relative performance over 5d / 20d / 60d windows.
- **`strategy/relative_strength.py`** — RS of the watchlist vs
  SPY/QQQ over configurable lookbacks.
- **`strategy/market_breadth.py`** — % of watchlist above 20d/50d
  MAs, advancers/decliners, new highs/lows.

Composite: **`strategy/morning_intelligence.py`** — the Morning
Intelligence Agent synthesises the above into a pre-market briefing.

Note from the docstring
(`strategy/morning_intelligence.py:1-5`): *"observational pre-market
briefing. Builds a morning market context report from existing
observational modules: market regime, overnight risk, and relative
strength. The report is read-only; it produces no trading signals,
places no orders, and does not alter Champion or Challenger strategy
behavior."*

## Step 2 — Watchlist scan

Runs the Champion scoring pipeline over the current watchlist:

```
python trader.py --morning
```

or

```
python trader.py --scan
```

Under the hood:

1. Fetch current prices for each watchlist symbol.
2. Compute RSI(14), MACD histogram, Bollinger Bands.
3. Run the six-gate scorer (`trader.py::evaluate_etf_setup`,
   equivalent research port at `strategy/champion_scoring.py`).
4. Filter to setups with `gate_count >= 4` — the "qualified" line.
5. Rank by score.

The watchlist itself is a merge of static ETF categories
(`trader.py:70` onwards — `core`, `sector_rotation`, `growth`,
`defensive`, `international`, `commodities`, `mega_caps`) plus the
dynamic trending watchlist in `trending_watchlist.json`.

## Step 3 — Research context (optional)

For any candidate the Morning Operator finds interesting, the
Research Analyst can produce a warehouse-first six-month simulation
snapshot on demand:

```
./scripts/traderjoe-simulate --strategy champion-v0.4.0 \
  --symbols AAPL MSFT NVDA SPY QQQ \
  --window 6mo --starting-cash 100000
```

The simulator writes:

- `reports/portfolio_simulations/<run_id>/report.md`
- `reports/portfolio_simulations/<run_id>/report.json`
- `reports/portfolio_simulations/<run_id>/trades.csv`
- `reports/portfolio_simulations/<run_id>/equity_curve.csv`
- `reports/portfolio_simulations/<run_id>/orders.json`

**Safety.** The simulator never touches a broker. `orders.json` is a
proposed-plan artifact for the Paper Bridge; it is not automatically
executed.

## Step 4 — Paper plans (optional)

If the Portfolio Manager approves a simulator-generated plan, the
Paper Bridge previews the resulting paper orders:

```
./scripts/traderjoe-paper-execute \
  --from-simulation reports/portfolio_simulations/<run_id>/orders.json \
  --dry-run
```

The bridge:

- Loads `.env.paper` (refuses `.env.production`).
- Applies caps (per-order notional, aggregate notional, symbol
  allowlist, per-order quantity).
- Writes a log to `reports/paper_bridge/paper-bridge-<timestamp>.log.json`.
- Prints accepted / rejected orders to stdout.

**Actual paper submission** requires:

```
./scripts/traderjoe-paper-execute \
  --from-simulation reports/portfolio_simulations/<run_id>/orders.json \
  --execute-paper-orders
```

Every submission line is logged with `broker_order_id` (stringified
from the SDK's UUID — see commit `7a24af3`).

## Step 5 — Morning report

The Morning Operator writes a structured Markdown report:

```
reports/morning_report_YYYY-MM-DD.md
```

Sections:

- **Regime** — result of `strategy/market_regime.py`.
- **Overnight risk** — result of `strategy/overnight_risk.py`.
- **Sector leadership** — top 3 / bottom 3 sectors from
  `strategy/sector_leadership.py`.
- **Relative strength** — watchlist RS ranking.
- **Breadth** — % above 20d/50d MAs, advancers vs decliners.
- **Watchlist scan** — qualifying setups (gate count ≥ 4), ranked by
  score.
- **Alerts** — tanking positions (>5% down from entry), overbought
  holdings (RSI > 70), breakdown candidates (MACD death cross).
- **Suggested actions** — buys to consider, sells to consider,
  positions to keep. Every suggestion cites the six-gate score,
  entry reason, and rejection reason (if applicable).

The report is a suggestion, not an execution.

## Step 6 — Approvals (human step)

Any buy in paper trading requires one of:

1. **Six-gate rule match** — Champion scorer flags a setup with
   `gate_count >= 4`.
2. **User approval** — the operator explicitly approves a pending
   order via `python trader_cli.py approve <id>` or the Telegram
   approval flow.

Pending orders sit in `trades.db` in the `pending_approvals` table.
`telegram_approvals.py` routes messages; `python trader_cli.py
pending` lists them.

**Every approved buy carries a five-minute tanking timer** — if the
position moves against by a configured threshold within five minutes
of entry, the runner exits.

## Step 7 — Safety checks

Before any auto-execute path fires, the Risk Manager or the daemon
itself verifies:

- `PAPER = True` still in `trader.py:48`.
- `.env.production` still empty.
- `/etc/traderjoe/STOP_EQUITY_PAPER` **absent** (touch to halt).
- `PAPER_AUTO_EXECUTE_EQUITIES=true` present in `.env.paper` **and**
  `--execute-paper-orders` on the CLI.
- Session status is `regular_session` (not weekend / holiday /
  pre_market / post_market).
- Notional caps under `MAX_BUY_AMOUNT=25000` (equity),
  `MAX_POSITIONS=20` (equity).
- Sector concentration under `MAX_SECTOR_EXPOSURE=0.25` (25%).
- Position risk under `RISK_PER_TRADE_DEFAULT=0.02` (2%).

Any single check failing aborts the auto-execute path with a logged
reason.

## Equity paper daemon (unattended path)

The equity paper daemon
(`strategy/paper_daemon.py`,
`scripts/traderjoe-paper-daemon`) can run this workflow on a 15-
minute schedule:

Plan-only (safe default):

```
./scripts/traderjoe-paper-daemon \
  --symbols AAPL MSFT NVDA SPY QQQ \
  --strategy momentum_15m_v1 \
  --interval 15Min
```

The daemon:

- Refuses if `HERMES_CONTEXT=production` or `PAPER` is false-y.
- Refuses outside NYSE regular session.
- Refuses if `/etc/traderjoe/STOP_EQUITY_PAPER` exists.
- Loads bars for the allowlisted symbols.
- Scores via the requested strategy.
- Applies caps (`max_per_order_notional=$2,000`,
  `max_total_daily_notional=$10,000`, `max_trades_per_day=8`,
  `cooldown_minutes=60`, `top_n_per_tick=3`).
- Writes a plan under `reports/paper_daemon/equity/plans/`.
- Writes a log under `reports/paper_daemon/equity/`.

Auto-execute (only after PAPER_AUTO_EXECUTE_EQUITIES is set):

```
./scripts/traderjoe-paper-daemon \
  --symbols AAPL MSFT NVDA SPY QQQ \
  --strategy momentum_15m_v1 \
  --interval 15Min \
  --execute-paper-orders
```

systemd:

- Plan-only timer + service — `docs/systemd/traderjoe-paper-plan.timer`
  + `traderjoe-paper-plan.service` (installable).
- Auto-execute — `docs/systemd/traderjoe-paper-daemon.service.example`
  (deliberately uninstallable; see [`safety-model.md`](safety-model.md)).

Note: at the milestone tag, `market_data/equities/minute/` is empty,
so intraday strategies will fall through the warehouse-first chain
and require `--allow-provider-fallback` (not yet implemented on the
daemon).

## Ready state (what the operator sees at 9:29 ET)

Ideally, at 09:29 ET on a trading day, the operator has:

- `reports/morning_report_YYYY-MM-DD.md` in hand.
- One or more optional simulator runs under
  `reports/portfolio_simulations/` for context.
- Zero pending approvals sitting stale in `trades.db`.
- Green light on all Risk Manager safety checks.
- Emergency stop file absent.

At 09:30 ET the equity market opens. The Morning Operator hands off
to whichever operator role is running the trading session.
