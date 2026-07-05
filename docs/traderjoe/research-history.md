# Research History

The chronological evolution of Trader Joe, phase by phase, with
commit hashes for verification. Every entry here is grounded in
`CHANGELOG.md`, `ROADMAP.md`, `STATUS.md`, and the git log.

## Phase 1 — Foundation

Baseline paper-trading runner. `trader.py` implements the six-gate
Champion scorer (later ported to `strategy/champion_scoring.py`);
`crypto_trader.py` implements Elliott Wave scanning;
`trader_cli.py` provides the JSON bridge for Hermes integration;
`telegram_approvals.py` routes approval flow.

Tag: `v0.4.0` (Phase 1 baseline).

## Phase 2 — Market Intelligence

Observational modules that inform (but do not drive) trading
decisions:

- `strategy/market_regime.py` — bullish / bearish / volatile /
  neutral classification via SPY/QQQ MA position, ATR%, ADX(14),
  MACD histogram.
- `strategy/sector_leadership.py` — ranks sector ETFs (SPDR family)
  by relative performance across 5d / 20d / 60d windows.
- `strategy/relative_strength.py` — RS vs SPY/QQQ, percentile rank,
  trend direction, composite score 0-100.
- `strategy/market_breadth.py` — how broadly a watchlist participates
  in market moves.
- `strategy/overnight_risk.py` — gap-risk analysis: prev close vs
  pre/post market, gap direction, risk score, data quality.
- `strategy/morning_intelligence.py` — Morning Intelligence Agent
  synthesises the above into a pre-market briefing.

All six modules are read-only and produce no trading signals.

Tag: `v0.10.0-phase2` (Phase 2 complete, 6/6).

## Phase 3 — Decision Engine

Champion / Challenger comparison harness plus walk-forward evaluation:

- `strategy/backtest_lab.py` (v0.11.0) — `BacktestConfig`,
  `BacktestEvent`, `DeterministicReplayClock`, `stable_hash`,
  `stable_json`.
- `strategy/data_catalog.py` (v0.12.0) — `DatasetManifest`,
  `DatasetFile`, `DataCatalog`.
- `strategy/comparison_harness.py` (v0.13.0) — Champion / Challenger
  score alignment, `DisagreementRecord`, `ScoreTable`.
- `strategy/rs_challenger.py` (v0.14.0) — Relative-Strength overlay
  Challenger, disabled by default.
- `strategy/walk_forward.py` (v0.15.0) — walk-forward pipeline,
  `WalkForwardReport`.
- `strategy/research_reports.py` (v0.16.0) — Markdown and JSON
  report writers.
- `strategy/promotion_gates.py` (v0.17.0) — `ApprovalRecord`,
  `PromotionEntry`, seven-state promotion chain,
  `STANDARD_ROLLBACK_CRITERIA`.

Tag: `v0.17.0-phase3` (Phase 3 complete, 7/7).

## Phase 4 — Learning System

Statistical and analytical layers on top of the Champion/Challenger
harness:

- `strategy/stats_engine.py` (v0.18.0) — statistical decision
  support, `StatisticalFinding`, comparison/walk-forward analyzers.
- `strategy/pattern_discovery.py` (v0.19.0) — historical pattern
  discovery.
- `strategy/feature_importance.py` (v0.20.0) — feature importance
  analysis for Champion gates.
- `strategy/weight_recommender.py` (v0.21.0) — strategy weight
  recommender.
- `strategy/learning_reports.py` (v0.22.0) — learning report
  generation.
- End-to-end validation (v0.23.0) — 2,272 tests passing at that
  point.

## Phase 5 — Research Execution

Research-only Alpaca client + LLM analyst + two-month historical
validation:

- `strategy/research_account.py` (v0.24.0) — isolated Research Alpaca
  client. Read-only; refuses to fall back to `ALPACA_*` / `APCA_*`.
  Uses `RESEARCH_ALPACA_*` namespace exclusively. Only exposes
  `fetch_bars`, `list_calendar`, `paper_account_info` — no order
  method.
- `strategy/research_analyst.py` (v0.25.0) — Local LLM Research
  Assistant. Loopback-only by default; opt-in trusted-LAN mode via
  `RESEARCH_LLM_ALLOW_REMOTE` + `RESEARCH_LLM_ALLOWED_HOSTS`. Cloud
  provider substrings refused regardless.
- `strategy/historical_validation.py` (v0.26.0) — two-month
  historical validation orchestrator wiring everything together.

Tag: `v0.26.0` (Phase 5 complete, 4/4).

Phase 5 follow-ups:

- `t_phase5_rs_live_feed` — commit `76637cc`.
- `t_phase5_champion_explanations` — commits `b806e9f`, `f1d1658`,
  `f46a548`.

## Phase 5.6 — Historical Data Warehouse

A dedicated sub-phase to make research provider-independent,
immutable, and reproducible. Full detail in
[`warehouse.md`](warehouse.md). Twelve implementation cards plus a
bridge card, all Done.

Landing chronology (from `CHANGELOG.md`, most recent last):

| Card | Commit | Module |
|---|---|---|
| `t_1c8a70da` | — | MarketDataProvider interface (`strategy/market_data_provider.py`) |
| `t_b2a75ee8` | — | Local warehouse layout (`strategy/local_warehouse.py`) |
| `t_56f319a9` | — | Data catalog extension (`strategy/data_catalog.py` phase-5.6 fields) |
| `t_6168af8e` | — | Parquet bar storage (`strategy/warehouse/parquet_io.py`) |
| `t_22210825` | — | Provider plugins (Alpaca, CSV, Parquet) |
| `t_31308fc2` | — | Import pipeline (`strategy/warehouse/import_pipeline.py`) |
| `t_cf80bf36` | — | Incremental sync (`strategy/warehouse/incremental_sync.py`) |
| `t_c32b8416` | — | Dataset versioning (`strategy/warehouse/versioning.py`) |
| `t_78ffa2b1` | — | DuckDB query layer (`strategy/warehouse/duckdb_query.py`) |
| `t_a3d29b8f` | — | Integrity validation (`strategy/warehouse/validation.py`) |
| `t_b06ec41d` | `1969df3` | Gap detection reports (`strategy/warehouse/gap_detection.py`) |
| `t_e6bf82a9` | `b3b7335` | Research cache + provider priority (`strategy/warehouse/research_cache.py`) |
| `t_b4b798af` | `81d0a9a` | HistoricalValidation warehouse wiring |

**Key discovery:** `ResearchAccountClient.fetch_bars` is provably
not called when local warehouse coverage is complete. The test suite
includes a counter-based stub asserting the counter stays at zero.

Tag: `v0.30.0-phase5.6` (Phase 5.6 complete, 12+1 cards, test suite
1811 passing).

## The 2026-07 sprint — Strategy Lab

Between the Phase 5.6 tag and the current milestone, ten cards
built the Strategy Laboratory on top of the Backtest Lab primitives.

Chronology (from `git log`):

| Commit | Card | Feature |
|---|---|---|
| `c4a0806` | `t_lab_strategy_interface` | Strategy plug-in framework — Card 1 |
| `5440e63` | `t_lab_experiment_runner` | Experiment runner — Card 2 |
| `e9a854e` | `t_lab_registry` | Strategy registry — Card 3 |
| `1aa366f` | `t_lab_leaderboard` | Leaderboard — Card 4 |
| `69177ed` | `t_lab_parameter_sweep` | Parameter sweeps — Card 5 |
| `288a48c` | `t_lab_weekend_pipeline` | Weekend Lab pipeline — Card 6 |
| `27c8925` | `t_lab_hypothesis_queue` | Hypothesis queue — Card 7 |
| `71f80dc` | `t_lab_dashboard_backend` | Dashboard backend JSON API — Card 8 |
| `28b62f1` | `t_lab_performance_metrics` | Performance metrics — Card 9 |
| `4249664` | `t_lab_multi_year_matrix` | Multi-year validation matrix — Card 10 |
| `22f954b` | `t_lab_regime_tagger` | Regime tagger |
| `08153e6` | — | AlpacaProvider defaults to SIP feed; `--feed` + `skip-if-validated` |

## The B02 investigation

**Commit `f3000d3` — "RS challenger skips overlay when base is
rejected."**

**Context.** Between Phase 5.6 and the Strategy Lab sprint, the
Research Analyst discovered a bug: `RelativeStrengthChallenger` was
applying its overlay to symbols the base Champion scorer had rejected
(`gate_count < 4`). Rejected symbols were being added to Challenger
`rankings` with an RS-only score, changing the disagreement counts.

**Post-fix analysis
(`reports/research_summaries/matrix-after-b02-2026-07-04.md`).**
Compares matrix disagreement counts pre- vs post-fix across
60d / 90d / 6mo / 1y / ytd windows:

| Window | Pre-fix | Post-fix |
|---|---:|---:|
| 60d | 61 | 0 |
| 90d | 121 | 7 |
| 1y | 635 | 323 |

Regime-conditioned means show every sign flip is negative → positive:
RS shifts from "systematic score drag" to slight positive lift in
`mid_vol` and `high_vol` regimes.

Critically: `selection_agreement_rate = 1.000` remains at every
regime × window, meaning **RS at weight 0.20 still doesn't change any
decisions**. Test suite 2,185 passing (+9). No promotion; no feature
flag change.

## The RS weight sweep

Immediately after the B02 fix, the Research Analyst produced a
weight sweep at 0.20, 0.30, 0.50, 0.75, 1.00 over the 1y matrix
(AAPL, MSFT, NVDA vs SPY, QQQ).

**Report:** `reports/research_summaries/rs-weight-sweep-post-b02-2026-07-04.md`.

**Bottom line, verbatim:** *"at every weight from 0.20 to 1.00, the
RS-overlay Champion+RS challenger produced worse realized returns
than Champion baseline. Sharpe drops from 1.11 (Champion) to as low
as 0.67 (Challenger at weight 0.75). CAGR drops from +33.2% to as
low as +16.6%."*

**Discovery:** RS never changed a single entry-selection decision
across 250 events × 5 weights; all flips are ranking-only. Best-of-
the-worst is weight 0.20.

**Verdict:** do not promote.

## The Alpaca Plus verification

**Report:** `reports/research_summaries/alpaca-plus-verification-2026-07-04.md`.

Verifies that Algo Trader Plus is active on the Research Alpaca paper
account `PA3POVULKVSB`. Runs 8 Plus-only signal probes against the
research endpoints only (never touches `ALPACA_*` / `APCA_*`).
Confirms SIP feed access, corporate-actions endpoint, news endpoint,
zero delay on SIP, historical depth back to 2016-01-04. Verdict: 8/8
Plus signals present.

## The regime pack

Between the Phase 5.6 tag and the Strategy Lab sprint, the
Research Analyst populated the `regime-pack-2016` and adjacent
datasets (`megacap-2016`, `sectors-2016`, `market-context-2016`,
`core-2016`) covering 2016 forward — enabling regime-tagged analysis
across `COVID_crash`, `COVID_recovery`, `Bull_2020_Q4`, `Bear_2022`,
`Sideways_2023H1`, `Rally_2023H2`.

## The current milestone sprint (`sprint-3/daily-digest`)

Documented in detail through the git log:

- `8ce7d65` — feat: add research run manifest
  (`t_operator_research_run_manifest`).
- `7b250c5` — chore: add offline warehouse validation runner
  (`t_operator_offline_validation_runner`).
- `90814d4` — chore: add research warehouse import command
  (`t_operator_warehouse_bootstrap`).
- `59b04f6` — chore: add research validation matrix runner
  (`t_operator_weekend_validation_matrix`).
- `6b94493` — perf: compact Research Analyst payloads
  (`t_operator_compact_analyst_payloads`).
- `33938b0` — docs: plan research dashboard backend
  (`t_dashboard_backend_plan`).
- `9f036a9` — fix: paginate `ResearchAccountClient.fetch_bars` across
  `next_page_token`.
- `65a0b9d` — feat: compact analyst payloads on by default (Task 2 /
  handoff sprint).
- `b299dfc` — feat: natural-language experiment planner (Task 3 /
  handoff sprint).
- `fcc056f` — feat: `scripts/traderjoe-research` operator entry point
  (Task 4).
- `8fdf01c` — docs: operator handoff —
  `traderjoe-research-mode.md` (Task 5).

**Current milestone commits (the four this session added):**

- `32c8024` — feat: portfolio simulator + paper-trading bridge.
- `9ddddc6` — feat: intraday/daily strategy plug-ins and paper
  daemons.
- `7bbb289` — fix: crypto paper daemon default bar loader.
- `7a24af3` — fix: JSON-serializable daemon logs (UUID
  broker_order_id).
- `bc6bf94` — docs: add Trader Joe operator skills.

## Major discoveries

1. **RS overlay is not additive on the current watchlist.** Every
   weight from 0.20 to 1.00 makes the strategy worse. Retained as a
   research artifact; do not promote.
2. **Champion's gate rejection is the primary risk filter.** Sparse
   entries save the account from whipsaws; a lower
   `min_gate_count` would trade more but likely worse.
3. **Warehouse-first is provable, not aspirational.** The test suite
   asserts the Research Alpaca client is not called when warehouse
   coverage is complete.
4. **Every strategy so far is momentum-flavoured.** RSI Mean Reversion
   and Volatility Regime Filter are the only counter-signals; the
   Sector Rotation strategy is a relative-momentum variant.
5. **Intraday coverage is the pacing constraint.** The warehouse has
   six daily datasets but zero intraday datasets. Momentum 15m and
   Opening Range Breakout cannot be exercised without warehouse
   population.
6. **UUID serialization was a latent bug.** The paper bridge and
   crypto daemon captured Alpaca `Order.id` values (which are
   `uuid.UUID` objects) into JSON payloads that didn't handle them.
   Fixed in commit `7a24af3` by stringifying at capture and adding
   `default=str` to every `json.dump`. Went undetected until the
   first successful crypto paper submission.

## Lessons learned

- **Deterministic identity primitives pay off.** `stable_hash` on
  every artifact means research runs are auditable and reproducible
  without ad-hoc bookkeeping.
- **Failure isolation matters.** Every sweep, matrix, and Lab
  pipeline isolates per-cell failures so one bad combo doesn't
  corrupt the manifest. Rolling out `isolate_failures=True` as the
  default was a small change with a large payoff.
- **Doubly-gated execute paths keep operators honest.** The env flag
  vs CLI flag split forces a two-step commitment before any paper
  order is submitted.
- **The Lab's dashboard backend was worth building even without a
  frontend.** JSON introspection over a live artifact tree is
  invaluable for debugging research runs.
- **English-parsed plans stay reproducible.** The natural-language
  planner uses regex, not an LLM. Same question → same plan.
- **Trailing whitespace hygiene compounds.** Adding
  `git diff --check` to the review flow surfaced two SKILL.md files
  with residual whitespace during the docs commit.

## Current conclusions (milestone tag)

At `v0.40.0-paper-research`:

- **Champion (v0.4.0) is the standing baseline.** Every promotion
  candidate must beat Champion on the same event stream.
- **No promotion candidate has cleared the gates.** Every strategy
  sits at `STATE_DISABLED`.
- **Every subsystem's tests pass.** 2,361 total.
- **No live-money code path exists.** `trader.py:48` and
  `crypto_trader.py:62` both carry `PAPER = True`.
- **`.env.production` is empty.** No module in the tree reads it.
- **Every safeguard is testable and tested.**

Trader Joe is now the primary operator persona, working from the
`.traderjoe/skills/` playbooks against this documented substrate.
