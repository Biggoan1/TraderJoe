# Trader Joe — Development Roadmap

This document is the authoritative source for project state and future direction.

It must remain accurate at all times.

---

## Phase 1 — Foundation

**Status:** COMPLETE

**Objective:** Establish core infrastructure before making any trading decisions.

### Sprint 1: Feature Flag and Champion/Challenger Framework
- **Date:** 2026-07-01
- **Commit:** 26eca28
- **Branch:** sprint-1/feature-flag-framework
- **Tests:** 4 passing
- **Behavior change:** No
- **Feature flags enabled:** None
- **Summary:**
  - Feature flag system with runtime enable/disable
  - Champion/Challenger strategy architecture
  - All flags disabled by default

### Sprint 2: Historical Trade Logger
- **Date:** 2026-07-01
- **Commit:** 70790f9
- **Branch:** sprint-2/historical-statistics
- **Tests:** 47 passing
- **Behavior change:** No
- **Feature flags enabled:** None
- **Summary:**
  - Persistent trade history logging
  - Historical statistics calculation
  - Performance metrics tracking

### Sprint 3: Daily Performance Digest
- **Date:** 2026-07-01
- **Commit:** 49f935f
- **Branch:** sprint-3/daily-digest
- **Tests:** 70 passing
- **Behavior change:** No
- **Feature flags enabled:** None
- **Summary:**
  - Full digest pipeline (Builder, Renderer, Saver, Service)
  - `get_day_trades(date)` method on TradeLogger
  - Standalone CLI with `--date` and `--dry-run`
  - 23 digest-specific tests added

### Sprint 4: Relative Strength Analysis
- **Date:** 2026-07-02
- **Commit:** 74dc06c
- **Branch:** sprint-3/daily-digest
- **Tests:** 108 passing
- **Behavior change:** No
- **Feature flags enabled:** None
- **Summary:**
  - Relative strength comparison against benchmark (SPY/QQQ)
  - Observational analysis only — no impact on rankings
  - `strategy/relative_strength.py` module
  - Tests for relative strength calculations

**Phase 1 Exit Criteria Met:** Core infrastructure in place. Observational data collection active. No trading behavior altered.

---

## Phase 2 — Market Intelligence

**Status:** COMPLETE (Kanban-driven implementation)

**Objective:** Increase situational awareness without affecting trading decisions.

### Sprint 5: Market Regime Classification ✅
- **Date:** 2026-07-02
- **Commit:** b785a35
- **Branch:** sprint-3/daily-digest
- **Tests:** 151 passing
- **Behavior change:** No
- **Feature flags enabled:** None
- **Summary:**
  - Market regime classification: bullish/bearish/volatile/neutral
  - Uses SPY/QQQ indicators: MA position, ATR, ADX, MACD
  - Weighted scoring across 8 signals (4 per benchmark)
  - 43 new tests covering indicators, signals, and classification

### Sprint 6: Overnight Risk Engine ✅
- **Date:** 2026-07-02
- **Commit:** 2e6bf02
- **Branch:** sprint-3/daily-digest
- **Tests:** 320 passing (78 overnight risk + 242 existing)
- **Behavior change:** No
- **Feature flags enabled:** None
- **Summary:**
  - Overnight gap risk analysis: previous close vs pre-market/after-hours
  - Per-symbol risk scoring with aggregate report
  - Purely observational — no trading signals, no behavior change
  - 78 comprehensive tests covering all engine components
  - Config-backed gap threshold with mocked PriceFetcher coverage
  - Runner stubs added (observational only)

### Sprint 7: Morning Intelligence Agent
- **Date:** 2026-07-02
- **Commit:** c1ed907
- **Branch:** sprint-3/daily-digest
- **Tests:** 339 passing (19 morning intelligence + 320 existing)
- **Behavior change:** No
- **Feature flags enabled:** None
- **Status:** Done
- **Summary:**
  - Pre-market context report from market regime, overnight risk, and relative strength
  - Structured JSON-ready report plus Markdown renderer
  - Provider injection for deterministic tests and future scheduler integration
  - Purely observational — no trading signals, no behavior change

### Sprint 8: Enhanced Daily Digest + Trade Metadata
- **Date:** 2026-07-02
- **Commit:** c60f807
- **Branch:** sprint-3/daily-digest
- **Tests:** 348 passing (9 new/updated digest and metadata tests + 339 existing)
- **Behavior change:** No
- **Feature flags enabled:** None
- **Status:** Done
- **Summary:**
  - Daily digest now summarizes observational trade metadata
  - TradeLogger captures optional entry/exit metadata payloads
  - SQLite migration adds nullable metadata columns for existing databases
  - Digest renders exit reasons, market context, flags, entry scores, and RS snapshots
  - Digest date now respects the requested report date

### Sprint 9: Sector Leadership Tracking
- **Date:** 2026-07-02
- **Commit:** 465faeb
- **Branch:** sprint-3/daily-digest
- **Tests:** 371 passing (23 new sector leadership and research platform tests + 348 existing)
- **Behavior change:** No
- **Feature flags enabled:** None
- **Status:** Done
- **Summary:**
  - Observational sector leadership analyzer ranks sector ETFs against SPY
  - Tracks strongest and weakest sectors across 5-day, 20-day, and 60-day windows
  - Handles missing sector data, missing benchmark data, invalid prices, and provider failures
  - Exposes sector leadership through the read-only research platform market intelligence contract
  - Adds disabled-by-default `enable_sector_leadership` flag inventory without runner integration

### Sprint 10: Market Breadth Analysis
- **Date:** 2026-07-02
- **Commit:** 5bcbf83
- **Branch:** sprint-3/daily-digest
- **Tests:** 401 passing (30 new/updated market breadth and research platform tests + 371 existing)
- **Behavior change:** No
- **Feature flags enabled:** None
- **Status:** Done
- **Summary:**
  - Observational market breadth analyzer tracks watchlist participation
  - Measures percent above 20-day and 50-day moving averages
  - Tracks advancers, decliners, unchanged symbols, new highs, and new lows
  - Produces aggregate breadth score and breadth regime
  - Exposes market breadth through the read-only research platform market intelligence contract
  - Adds disabled-by-default `enable_market_breadth` flag inventory without runner integration

### Kanban Board

Sprint 7 onward uses Kanban for workflow management. See [KANBAN.md](KANBAN.md).

**Chain:** Sprint 7 → Sprint 8 → Sprint 9 → Sprint 10 → Phase 3+

| # | Card | Sprint | Status |
|---|---|---|---|
| `t_909faeab` | Sprint 7: Morning Intelligence Agent | Phase 2 | Done |
| `t_194b8638` | Sprint 8: Enhanced Daily Digest + Trade Metadata | Phase 2 | Done |
| `t_c0ab3a10` | Sprint 9: Sector Leadership Tracking | Phase 2 | Done |
| `t_5ec6406e` | Sprint 10: Market Breadth Analysis | Phase 2 | Done |
| `t_phase3_plan` | Phase 3: Decision Engine Technical Design | Phase 3 | Done |
| `t_phase3_backtest_lab` | Phase 3: Backtest Lab Foundation | Phase 3 | Done |
| `t_phase3_data_catalog` | Phase 3: Research Data Catalog | Phase 3 | Done |
| `t_phase3_champion_challenger` | Phase 3: Champion/Challenger Comparison Harness | Phase 3 | Ready |
| `t_phase3_rs_challenger` | Phase 3: Relative Strength Challenger Overlay | Phase 3 | Backlog |
| `t_phase3_walk_forward` | Phase 3: Walk-Forward Evaluation Pipeline | Phase 3 | Backlog |
| `t_phase3_reports` | Phase 3: Research Report Generation | Phase 3 | Backlog |
| `t_phase3_promotion_gates` | Phase 3: Feature Promotion Gates | Phase 3 | Backlog |

**Rules:** Observational only. No trading behavior changes. Every feature behind a flag.

**Exit Criteria:** Four weeks of paper-trading data with all observational features enabled.

---

## Phase 3 — Decision Engine

**Status:** DESIGN COMPLETE

**Objective:** Transform Trader Joe from an observational intelligence platform
into an evidence-driven research platform before any decision-engine behavior is
enabled.

- Relative Strength incorporated into rankings
- Market Regime incorporated into scoring
- Sell Score replacing single-indicator exits
- Risk-Based Position Sizing
- Confidence Scoring
- Enhanced Candidate Ranking

**Rules:** One feature at a time. Measure impact before enabling next. Champion remains production.

**Exit Criteria:** Evidence of improved expectancy, drawdown, or alpha over Champion.

### Planning Card: Decision Engine Technical Design
- **Card:** `t_phase3_plan`
- **Status:** Done
- **Scope:** Planning only — no strategy logic changes, no feature flags enabled.
- **Design outcome:** Build research infrastructure first; evaluate strategy changes only after reproducible backtests and walk-forward comparisons exist.
- **First candidate strategy feature:** Relative Strength candidate-ranking overlay.
- **First implementation card:** `t_phase3_backtest_lab` — Backtest Lab Foundation.

### Backtest Lab Architecture

The Backtest Lab is a read-only research subsystem. It replays historical
market context and strategy inputs, compares versioned strategy variants, and
generates reproducible evidence packages. It must not place orders, mutate
paper-trading state, or enable feature flags.

#### Components
- **Dataset Registry:** Versioned index of historical market data, benchmark data, paper-trading logs, watchlists, research notes, and experiment inputs.
- **Replay Engine:** Deterministic event loop that replays market snapshots, candidate lists, and strategy inputs in timestamp order.
- **Strategy Adapter Layer:** Stable interface wrapping Champion and Challenger evaluators without importing order-placement behavior.
- **Execution Simulator:** Deterministic fill/slippage/fee model for hypothetical outcomes. Assumptions are configuration-driven and recorded with each run.
- **Experiment Runner:** Coordinates dataset, strategy version, config, random seed, and output paths.
- **Metrics Engine:** Calculates performance, risk, stability, concentration, and statistical comparison metrics.
- **Report Generator:** Writes human-readable Markdown and machine-readable JSON reports.
- **Artifact Store:** Stores run manifests, configs, inputs, metrics, reports, and hashes needed to reproduce results.

#### Interfaces
- `DatasetProvider`: returns historical bars, benchmark series, watchlist snapshots, and closed trade context by time range.
- `ReplayClock`: yields deterministic timestamps and event batches.
- `StrategyEvaluator`: accepts a replay event and returns scores, rankings, and explanations only.
- `ExecutionModel`: converts hypothetical entries/exits into simulated fills using recorded assumptions.
- `MetricsCalculator`: accepts Champion and Challenger ledgers and returns comparable metrics.
- `ReportWriter`: persists experiment outputs under a stable run id.

These are design interfaces. They are not implementation commitments until the
corresponding Kanban card is active.

#### Data Flow
1. User selects dataset version, strategy versions, config, and date range.
2. Experiment Runner creates a run manifest with git commit, tag state, config hash, dataset hash, and random seed.
3. Dataset Provider loads immutable historical inputs.
4. Replay Engine emits timestamped market/candidate events.
5. Champion and Challenger Strategy Evaluators score the same event stream.
6. Execution Simulator produces hypothetical ledgers using identical fill assumptions.
7. Metrics Engine compares ledgers and score/ranking differences.
8. Report Generator writes summary, detailed trades, disagreement reports, and reproducibility metadata.
9. Artifact Store preserves the full run package.

#### Storage
- `research_data/` for immutable imported datasets and manifests.
- `reports/backtests/` for human-readable reports.
- `reports/backtests/artifacts/` for JSON manifests, metrics, score deltas, and ledgers.
- Existing `trades_history.db` remains the source for paper-trading logs.
- Research Notebook entries may reference backtest run ids but must not be required to reproduce a run.

#### Configuration
- All backtest settings live in explicit config files or command arguments recorded in the run manifest.
- Required config fields: dataset id, date range, symbols/watchlist source, benchmark set, strategy ids, fee model, slippage model, execution timing assumptions, seed, and output directory.
- No production trading config may be modified by a backtest run.
- No feature flag may be enabled as a side effect of a backtest run.

### Champion / Challenger Framework

#### Participants
- **Champion:** Current production strategy behavior, including existing ranking and buy/sell decisions as the reference path.
- **Relative Strength Challenger:** Champion plus Relative Strength ranking overlay, evaluated as scores/rankings first and behavior-changing only after approval.

#### Side-By-Side Execution
- Both evaluators receive identical replay events, candidate lists, market context, and execution assumptions.
- Champion output is the baseline ledger and score/ranking table.
- Challenger output includes the same fields plus Relative Strength score contribution and explanation.
- Challenger must be able to run in shadow mode during paper trading without placing orders.

#### Score Comparison
- Record raw Champion score, Relative Strength component score, final Challenger score, rank delta, and selected/not-selected status.
- Store per-symbol score explanations for every evaluated candidate.
- Highlight large rank changes and threshold crossings in disagreement reports.

#### Trade Comparison
- Compare would-enter, would-skip, would-size, and would-exit differences.
- Classify disagreements as ranking-only, entry-selection, exit-timing, position-sizing, or data-unavailable.
- No disagreement may affect live or paper orders until the approval gate is satisfied.

#### Daily Reports
- Daily summary of Champion vs Challenger candidate rankings.
- Hypothetical Challenger trades, skipped Champion trades, and shared trades.
- Daily P/L, drawdown, exposure, concentration, and regime context.
- Data-quality warnings for missing Relative Strength, regime, sector, or breadth context.

#### Disagreement Reports
- Every trade/candidate disagreement includes timestamp, symbol, Champion decision, Challenger decision, score delta, contributing factors, market regime, sector leadership, and breadth context.
- Reports group disagreements by outcome and reason so the team can identify systematic value or harm.

#### Performance Metrics
- Expectancy, win rate, average return, average winner, average loser, profit factor, drawdown, Sharpe, trade count, exposure, turnover, concentration, and regime breakdown.
- Metrics must be calculated identically for Champion and Challenger.

### Feature Flag Promotion Process

No feature flag may be enabled without explicit human approval. Promotion is a
state transition for a specific feature and strategy version, not a general
permission to change trading behavior.

| State | Meaning | Required Gate |
|---|---|---|
| Disabled | Feature code exists but has no behavior impact | Default state; tests prove flag defaults off |
| Backtest | Feature is evaluated on historical replay only | Approved experiment manifest and reproducible dataset |
| Walk Forward | Feature is evaluated on time-ordered out-of-sample periods | Backtest report accepted; no look-ahead findings |
| Paper Trading | Feature runs in shadow or paper comparison mode | Walk-forward report accepted; rollback plan documented |
| Candidate | Feature has sufficient paper-trading evidence for review | Candidate report includes metrics, failures, and disagreements |
| Approved | Human explicitly approves a bounded rollout | Approval names flag, scope, owner, monitoring, and rollback |
| Production | Feature affects production behavior under approved flag | Monitoring confirms no rollback condition is active |

Every transition requires a dated report, git commit, dataset/run ids, and
explicit approval recorded in project documentation or an approved operations
record.

### Success Metrics

Promotion criteria must be measured against Champion over the same replay or
paper-trading window. Numeric thresholds are intentionally not hard-coded here;
they must be set in the active experiment plan and justified by sample size.

- Sharpe or risk-adjusted return improves versus Champion.
- Max drawdown is lower or not materially worse than Champion.
- Win rate improves or remains stable without weakening average winner/loser ratio.
- Profit factor improves versus Champion.
- Average return and expectancy per trade improve after costs.
- Trade frequency remains sufficient and does not degrade opportunity coverage.
- Risk-adjusted return improvement is not concentrated in one symbol, sector, or regime.
- Statistical significance or confidence interval analysis supports the observed improvement.
- Results persist across backtest, walk-forward, and paper-trading windows.

### Failure / Rollback Criteria

Rollback criteria are objective and must be evaluated from recorded metrics.

- Challenger expectancy is lower than Champion for the approved evaluation window.
- Challenger max drawdown exceeds the approved limit or is worse than Champion beyond the active experiment threshold.
- Challenger profit factor is lower than Champion beyond the active experiment threshold.
- Challenger trade frequency falls below the active experiment minimum sample requirement.
- Challenger concentration exceeds approved symbol, sector, or regime limits.
- Data-quality failures affect more than the approved missing-data tolerance.
- Reproducibility check fails: same run id/config/dataset cannot regenerate the same outputs.
- Any order-path behavior changes outside the approved feature flag and scope.
- Any required report, artifact, or approval record is missing.

If any rollback condition is met during an approved rollout, disable the feature
flag and return to Champion behavior. The rollback action must be documented
with metrics and commit/run references.

### Data Requirements

#### Historical Market Data
- OHLCV bars for all candidate symbols at the granularity used by Champion.
- Corporate action adjustment policy recorded with every dataset.
- Data source, import time, symbol universe, and checksum recorded in dataset manifests.

#### Benchmark Data
- SPY and QQQ data for Relative Strength, market regime, and benchmark comparisons.
- Sector ETF data for sector leadership context.
- Breadth universe data for market breadth context.

#### Paper Trading Logs
- Closed trades, open trades, entry/exit metadata, scores, market context, and timestamps from `trades_history.db`.
- Paper-trading comparison snapshots from Champion and Challenger shadow runs.
- Daily digest outputs as human-readable supporting context.

#### Research Notebook Integration
- Notebook entries reference run ids, hypothesis ids, and evidence summaries.
- Notebook conclusions must distinguish hypotheses from validated findings.
- Notebook data is supporting context, not the primary reproducibility source.

#### Experiment Metadata
- Git commit, tag, branch, strategy id, feature flag state, config hash, dataset id, run id, seed, runtime, and operator.
- Approval state and gate transition history.
- Known limitations and data-quality notes.

#### Version Tracking
- Strategy adapters are versioned independently from datasets.
- Reports must identify Champion version and Challenger version.
- Dataset changes require new dataset ids and cannot silently replace prior inputs.

### Kanban Breakdown

Each Phase 3 card must be independently testable and reversible. No card may
enable behavior-changing feature flags unless its scope explicitly includes an
approved production rollout.

#### `t_phase3_backtest_lab` — Backtest Lab Foundation
- **Status:** Done
- **Scope:** Build run manifests, deterministic replay skeleton, artifact paths, and no-op strategy adapter fixtures.
- **Definition of Done:** Reproducible dry-run backtest creates manifest, deterministic event order, and empty report artifacts.
- **Validation:** Unit tests for manifest hashing, replay ordering, config serialization, and no production side effects.
- **Implementation note:** Foundation data contracts are implemented and validated for deterministic config, run metadata, artifact paths, replay event ordering, no-op strategy adapter fixtures, strategy result shells, run manifests, and report serialization. Full historical replay is intentionally deferred.

#### `t_phase3_data_catalog` — Research Data Catalog
- **Status:** Done
- **Scope:** Define dataset manifests for historical bars, benchmarks, paper logs, and research context.
- **Definition of Done:** Dataset registry can list, validate, and checksum local datasets without mutating them.
- **Validation:** Tests for missing data, checksum mismatch, schema validation, and reproducibility metadata.
- **Implementation note:** `strategy/data_catalog.py` implements `DatasetFile`, `DatasetManifest`, `DatasetValidationResult`, and a read-only `DataCatalog` loaded from `research_data/manifests/`. Validation covers missing files, size mismatch, SHA-256 mismatch, and CSV/JSON schema checks. Reproducibility metadata surfaces manifest hash, imported timestamp, and per-file checksums. Operator helper `build_dataset_manifest` computes checksums offline. No production data was imported and no feature flags were enabled.
- **Validation outcome:** 473 tests passing; catalog confirmed read-only, deterministic, and observational; feature flags remain all disabled; commit `0410aa2`.

#### `t_phase3_champion_challenger` — Champion/Challenger Comparison Harness
- **Status:** Backlog
- **Scope:** Run Champion and Challenger evaluators side-by-side in replay without order placement.
- **Definition of Done:** Same input stream produces comparable score tables and disagreement records.
- **Validation:** Tests prove identical inputs, deterministic outputs, and no order-path imports/calls.

#### `t_phase3_rs_challenger` — Relative Strength Challenger Overlay
- **Status:** Backlog
- **Scope:** Implement disabled-by-default Relative Strength score overlay for challenger evaluation only.
- **Definition of Done:** Overlay produces score deltas and explanations but cannot affect live trading decisions.
- **Validation:** Tests for score composition, missing RS data, disabled flag defaults, and Champion parity when disabled.

#### `t_phase3_walk_forward` — Walk-Forward Evaluation Pipeline
- **Status:** Backlog
- **Scope:** Add time-ordered train/evaluate splits and out-of-sample comparison reports.
- **Definition of Done:** Pipeline runs configured splits and aggregates metrics without future-data leakage.
- **Validation:** Tests for split boundaries, leakage prevention, and reproducible split manifests.

#### `t_phase3_reports` — Research Report Generation
- **Status:** Backlog
- **Scope:** Generate Markdown and JSON reports for backtests, walk-forward runs, daily comparisons, and disagreements.
- **Definition of Done:** Reports include metrics, artifacts, data-quality notes, and reproducibility metadata.
- **Validation:** Snapshot tests for report structure and JSON schema tests for machine-readable outputs.

#### `t_phase3_promotion_gates` — Feature Promotion Gates
- **Status:** Backlog
- **Scope:** Encode promotion-state documentation, approval records, and rollback checks.
- **Definition of Done:** Promotion reports can prove current state and required evidence for each transition.
- **Validation:** Tests for gate completeness, missing approval blocks, rollback trigger detection, and disabled defaults.

### Architectural Risks

- **Look-ahead bias:** Historical replay may accidentally use future data unless event timestamps and split boundaries are strict.
- **Data survivorship bias:** Current watchlists may omit symbols that failed or fell out of the universe.
- **Non-reproducible data sources:** External price APIs can revise history or fail; dataset snapshots and checksums are required.
- **Strategy/order coupling:** Existing strategy code may mix scoring with execution; adapters must avoid importing order paths.
- **Overfitting:** Relative Strength parameters may look good in-sample and fail out-of-sample.
- **Small sample size:** Paper-trading data may be insufficient for statistically meaningful promotion.
- **Metric gaming:** Optimizing one metric can degrade drawdown, concentration, or trade frequency.
- **Operational ambiguity:** Approval and rollback ownership must be explicit before any behavior-changing rollout.
- **Artifact sprawl:** Backtests can create many reports; run ids, retention rules, and manifests must keep results navigable.
- **False confidence from paper fills:** Paper execution may differ from real fills; slippage and cost assumptions must be explicit.

---

## Phase 4 — Learning System

**Status:** PLANNED

**Objective:** Continuous improvement through data analysis.

- Statistical Decision Support
- Historical Pattern Discovery
- Performance breakdowns (by regime, strength, sector, score)
- Feature Importance Analysis
- Strategy Weight Recommendations

**Rules:** Recommendations only. Never modify production automatically.

**Exit Criteria:** Every recommendation backed by measurable historical evidence.

---

## Phase 5 — Research Platform

**Status:** PLANNED

**Objective:** Laboratory for strategy development.

- Backtesting Framework
- Walk-Forward Analysis
- Parameter Optimization
- Monte Carlo Simulation
- Champion/Challenger Analytics
- Strategy Comparison Dashboard
- Performance Attribution
- Risk Analysis

**Exit Criteria:** Strategies validated on historical and forward-looking data before promotion.

---

## Phase 6 — Production Readiness

**Status:** PLANNED

**Objective:** Prepare for live trading.

- Stable Champion with positive expectancy
- Controlled maximum drawdown
- Full observability and logging
- Automated recovery and alerting
- Deployment and disaster recovery procedures

**Promotion Criteria:** Sustained performance over a statistically meaningful sample.

---

## Engineering Principles

Every sprint must:
- Be independently testable and reversible
- Be committed and pushed separately
- Leave Champion strategy functional
- Leave feature flags disabled unless approved

Never implement multiple behavior-changing features in a single sprint.

## Current Sprint

**Completed:** Sprint 10 — Market Breadth Analysis (Phase 2)
**Next:** Phase 3 planning
