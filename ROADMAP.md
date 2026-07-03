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
| `t_phase3_champion_challenger` | Phase 3: Champion/Challenger Comparison Harness | Phase 3 | Done |
| `t_phase3_rs_challenger` | Phase 3: Relative Strength Challenger Overlay | Phase 3 | Done |
| `t_phase3_walk_forward` | Phase 3: Walk-Forward Evaluation Pipeline | Phase 3 | Done |
| `t_phase3_reports` | Phase 3: Research Report Generation | Phase 3 | Done |
| `t_phase3_promotion_gates` | Phase 3: Feature Promotion Gates | Phase 3 | Done |

**Rules:** Observational only. No trading behavior changes. Every feature behind a flag.

**Exit Criteria:** Four weeks of paper-trading data with all observational features enabled.

---

## Phase 3 — Decision Engine

**Status:** COMPLETE (research platform in place; no decision-engine behavior enabled)

**Objective:** Transform Trader Joe from an observational intelligence platform
into an evidence-driven research platform before any decision-engine behavior is
enabled.

**Exit state (milestone `v0.17.0-phase3`):**
- All eight Phase 3 Kanban cards Done (one planning card + seven implementation cards)
- 656 tests passing
- No trading behavior, buy/sell logic, runner behavior, or feature flags changed
- Historical validation paper account remains documentation-only
- Research stack: `strategy/backtest_lab.py`, `strategy/data_catalog.py`,
  `strategy/comparison_harness.py`, `strategy/rs_challenger.py`,
  `strategy/walk_forward.py`, `strategy/research_reports.py`,
  `strategy/promotion_gates.py`
- Champion remains production; RS Challenger exists as a disabled-by-default overlay
  awaiting explicit human approval to advance beyond the `disabled` promotion state

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

#### Validation Paper Account (future data source)
- A separate Alpaca paper account exists as a dedicated Backtest Lab / historical replay data source for research use only.
- Isolation rules (must hold before any integration is added):
  - Must remain separate from the live account and from the normal paper-trading account.
  - Must never be used by the live runner (`trader.py`, `crypto_trader.py`, `strategy/runner.py`, scheduler, Telegram approval path, `trader_cli.py`).
  - Credentials must not be shared with production or the active paper-trading account.
  - Permitted uses only: historical replay, walk-forward validation, Champion/Challenger comparison, and strategy evaluation.
  - Referred to as validation, replay, or research — never as "training."
- No account integration is implemented yet. Adding SDK calls, credentials, env plumbing, or runner wiring is out of scope for any card whose scope does not explicitly require it (currently: none).
- Expected consumers when integration lands: `t_phase3_walk_forward`, and any card that explicitly scopes ingestion into `strategy/data_catalog.py`.

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
- **Status:** Done
- **Scope:** Run Champion and Challenger evaluators side-by-side in replay without order placement.
- **Definition of Done:** Same input stream produces comparable score tables and disagreement records.
- **Validation:** Tests prove identical inputs, deterministic outputs, and no order-path imports/calls.
- **Implementation note:** `strategy/comparison_harness.py` implements a read-only `ComparisonHarness` with a `ComparisonEvaluator` protocol satisfied by the existing `NoOpStrategyAdapter`. Per-event `ScoreTable` and `ScoreRow` align Champion and Challenger scores, ranks, selection, score deltas, and rank deltas. `DisagreementRecord` classifies divergences as `ranking_only`, `entry_selection`, `score_delta`, or `data_unavailable`. `ChampionChallengerRunMetadata.run_id` is derived from a deterministic hash of champion/challenger ids, dataset id, event signature, seed, and threshold; `stable_hash` on the full comparison ignores `generated_at`. No Relative Strength Challenger logic, order-path imports, or feature-flag mutation.
- **Future data source:** A separate Alpaca paper account exists as a dedicated Backtest Lab / historical replay data source for research use only (see "Validation Paper Account" under Data Requirements). It is not integrated in this card and no code path in the harness reads from it. Integration is deferred to a card whose scope explicitly requires it (candidate: `t_phase3_walk_forward`).
- **Validation outcome:** 500 tests passing; harness confirmed read-only, deterministic (`stable_hash` and `run_id` independent of `generated_at`), and observational; no `alpaca`, `place_order`, `submit_order`, `TradingClient`, or credential/env references; validation paper account remains documentation-only; feature flags remain all disabled; commit `1b341ef` plus policy addendum `a0fa765`.

#### `t_phase3_rs_challenger` — Relative Strength Challenger Overlay
- **Status:** Done
- **Scope:** Implement disabled-by-default Relative Strength score overlay for challenger evaluation only.
- **Definition of Done:** Overlay produces score deltas and explanations but cannot affect live trading decisions.
- **Validation:** Tests for score composition, missing RS data, disabled flag defaults, and Champion parity when disabled.
- **Implementation note:** `strategy/rs_challenger.py` adds `RelativeStrengthChallenger`, a read-only wrapper around any `ComparisonEvaluator`. The overlay is gated by `FeatureFlags.enable_relative_strength` (default `False`, global singleton untouched). When disabled the wrapper is a passthrough — scores, rankings, and explanations mirror the base evaluator so the comparison harness sees zero disagreements. When enabled the wrapper computes `score += weight * (rs - neutral) / range`, clipped to `[neutral - range, neutral + range]`, recomputes rankings by new score desc (tie-break by symbol asc), and emits per-symbol explanations. Missing or malformed RS values are captured as warnings with the base score preserved. RS data comes from a caller-supplied `RelativeStrengthProvider`; the `rs_provider_from_map` helper builds a deterministic provider from a `{timestamp: {symbol: rs}}` snapshot suitable for the Data Catalog. No order-path imports, no live trading impact, no broker credentials, no `yfinance` dependency.
- **Validation outcome:** 532 tests passing; overlay confirmed disabled by default; global feature flags remain all disabled; Champion parity verified when disabled (harness records zero disagreements); enabled overlay produces the expected score contribution and rerank; determinism verified across repeat evaluations and across `generated_at` differences; no `alpaca`, `yfinance`, `TradingClient`, `api_key`, or order-path references; commit `4569bce`.

#### `t_phase3_walk_forward` — Walk-Forward Evaluation Pipeline
- **Status:** Done
- **Scope:** Add time-ordered in-sample / out-of-sample splits and out-of-sample comparison reports.
- **Definition of Done:** Pipeline runs configured splits and aggregates metrics without future-data leakage.
- **Validation:** Tests for split boundaries, leakage prevention, and reproducible split manifests.
- **Implementation note:** `strategy/walk_forward.py` adds `WalkForwardSplit`, `WalkForwardSchedule`, `generate_walk_forward_schedule`, `WalkForwardSplitResult`, `WalkForwardReport`, and `WalkForwardPipeline`. Split constructor enforces `out_of_sample_start > in_sample_end`; schedule constructor enforces strictly time-ordered splits by `out_of_sample_start`. `WalkForwardPipeline.run` groups events by OOS window date lookup, forwards only OOS events to the `ComparisonHarness` (in-sample events are counted for reporting but never reach the harness), and aggregates disagreement counts across splits. `report_id` is derived from `schedule.stable_hash + champion_id + challenger_id + dataset_id + seed + event_count + score_delta_threshold`; `WalkForwardReport.stable_hash` strips `generated_at` from the top level and from every nested comparison metadata for reproducibility. Uses `in-sample` / `out-of-sample` terminology throughout (never "training"). No order-path imports, no live trading impact, no broker credentials.
- **Validation outcome:** 567 tests passing; leakage prevention verified (in-sample events never reach the harness); split boundaries strictly non-overlapping (OOS start > IS end); determinism verified (report_id and stable_hash independent of `generated_at`); no `alpaca`, `yfinance`, `TradingClient`, `api_key`, or order-path references; global feature flags remain all disabled; commit `01089d3`.

#### `t_phase3_reports` — Research Report Generation
- **Status:** Done
- **Scope:** Generate Markdown and JSON reports for backtests, walk-forward runs, daily comparisons, and disagreements.
- **Definition of Done:** Reports include metrics, artifacts, data-quality notes, and reproducibility metadata.
- **Validation:** Snapshot tests for report structure and JSON schema tests for machine-readable outputs.
- **Implementation note:** `strategy/research_reports.py` adds `ResearchReport`, `ResearchReportPaths`, `render_comparison_report` (for `ChampionChallengerComparison`), and `render_walk_forward_report` (for `WalkForwardReport`). Each report bundle carries a Markdown body, a JSON payload with summary + daily-summary + source dict, a flattened deterministically-sorted disagreement list, and a manifest with report id, source id, source hash, disagreement counts, and `generated_at`. `report_id` is derived from the source `stable_hash`, so identical source objects always produce the same report id. `ResearchReport.stable_hash()` excludes `generated_at`; the walk-forward payload strips nested `generated_at` fields at every comparison metadata level. `ResearchReport.write(output_dir)` persists four files under `<output_dir>/<report_id>/`: `report.md`, `report.json`, `disagreements.json`, `manifest.json`. No order-path imports, no live trading impact, no broker credentials, no `yfinance` dependency.
- **Validation outcome:** 599 tests passing; report_id and stable_hash independent of `generated_at`; byte-identical `to_json`, `to_markdown`, `disagreements_json`, and `manifest_json` across repeat renders; manifest carries source id + source hash; `write` produces all four files, is idempotent, and lands under `<output_dir>/<report_id>/`; no `alpaca`, `yfinance`, `TradingClient`, `api_key`, or order-path references; global feature flags remain all disabled; commit `a2f1940`.

#### `t_phase3_promotion_gates` — Feature Promotion Gates
- **Status:** Done
- **Scope:** Encode promotion-state documentation, approval records, and rollback checks.
- **Definition of Done:** Promotion reports can prove current state and required evidence for each transition.
- **Validation:** Tests for gate completeness, missing approval blocks, rollback trigger detection, and disabled defaults.
- **Implementation note:** `strategy/promotion_gates.py` encodes the seven promotion states (`disabled → backtest → walk_forward → paper_trading → candidate → approved → production`) plus `REQUIRED_EVIDENCE_PER_STATE`, `ApprovalRecord`, `RollbackCriterion`, `RollbackAlert`, `PromotionEntry`, and `PromotionReport`. `evaluate_promotion` returns the current state, next state, required evidence, missing evidence, approvals, and rollback alerts against a caller-supplied metrics dict; missing metrics never look like a pass and are surfaced as warnings. `STANDARD_ROLLBACK_CRITERIA` mirrors the ROADMAP "Failure / Rollback Criteria" list. `PromotionReport.stable_hash` excludes `generated_at`, and `report_id` is derived from a deterministic hash of flag, current/target state, evidence keys, approval count, metric keys, and criterion names. Module never mutates `strategy.config.FeatureFlags`, never imports order paths, and never wires the historical validation paper account.
- **Validation outcome:** 656 tests passing; default `PromotionEntry` reflects the disabled flag; `report_id` and `stable_hash` independent of `generated_at`; standard rollback criteria trigger under worst-case metrics and pass under safe metrics; missing metrics surface as warnings rather than passes; approved/production states without any `ApprovalRecord` emit a warning; no `alpaca`, `yfinance`, `TradingClient`, `api_key`, or order-path references; module never mutates global feature flags; commit `623b046`.

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

**Status:** DESIGN COMPLETE

**Objective:** Turn the Phase 3 research platform into a
recommendations-only Learning System that surfaces measurable evidence
for human review. Phase 4 must not enable feature flags, mutate
production config, place orders, or advance promotion state
automatically.

- Statistical Decision Support
- Historical Pattern Discovery
- Performance breakdowns (by regime, strength, sector, score)
- Feature Importance Analysis
- Strategy Weight Recommendations
- Learning-System Reports

**Rules:** Recommendations only. Never modify production automatically.
Every recommendation carries a confidence label, a sample-size figure,
and a reproducibility hash.

**Exit Criteria:** Every recommendation is backed by measurable
historical evidence and can be replayed against a fixed Phase 3
artifact set to produce byte-identical outputs.

### Planning Card: Learning System Technical Design
- **Card:** `t_phase4_plan`
- **Status:** Done
- **Scope:** Planning only — no learning code, no feature flags enabled, no
  trading behavior changed.
- **Design outcome:** Build a read-only Learning System that consumes
  Phase 3 artifacts (`ChampionChallengerComparison`,
  `WalkForwardReport`, `ResearchReport`, `PromotionReport`) plus the
  existing `trades_history.db` and market-intelligence modules, and
  produces machine-readable recommendation artifacts routed to
  `PromotionEntry.evidence` for human review.
- **First implementation card:** `t_phase4_stats_engine` — Statistical
  Decision-Support Layer.

### Learning System Architecture

The Learning System is a read-only analysis subsystem. It ingests
Phase 3 evidence and produces recommendation artifacts. It never
executes trades, mutates paper-trading state, enables feature flags,
or automatically advances promotion state.

#### Components
- **Statistical Decision Support:** Computes descriptive statistics,
  confidence intervals, effect sizes, and sample-size checks over
  paper-trading logs, Champion / Challenger comparisons, and
  walk-forward reports. Distinguishes hypothesis-level findings from
  validated findings.
- **Historical Pattern Discovery:** Identifies co-occurring conditions
  in market regime, sector leadership, market breadth, and relative
  strength history that correlate with trade outcomes. Every pattern is
  labeled as a hypothesis until validated on an out-of-sample window.
- **Feature Importance Analysis:** Correlates score components,
  disagreement kinds, and market-context features with realized
  outcomes across the same event windows the harness evaluated.
  Produces ranked feature-importance scores with methodology metadata.
- **Strategy Weight Recommender:** Suggests adjusted weights for the
  Champion's scoring components (e.g., `SELL_SCORE_FACTOR_WEIGHTS`)
  based on feature-importance output. Recommendations are records —
  the module never writes to `strategy/config.py`.
- **Learning Reports:** Aggregates statistics, patterns, feature
  importance, and weight recommendations into Markdown + JSON reports
  that share the Phase 3 report layout (`report_id`, `manifest`,
  `stable_hash`, `report.md` / `report.json` /
  `disagreements.json` / `manifest.json`).
- **Validation Harness:** End-to-end smoke test that runs the full
  Learning System against a frozen Phase 3 artifact set and asserts
  determinism, feature-flag isolation, and absence of any order-path
  side effects.

#### Interfaces
- `EvidenceIntake`: pulls Phase 3 artifacts by report id or from a
  supplied directory; never fetches remote data.
- `StatisticalFinding`: dataclass with metric name, effect size,
  confidence interval, sample size, methodology, and label
  (`hypothesis` / `validated`).
- `PatternHypothesis`: dataclass with pattern description,
  co-occurring context, supporting evidence ids, sample size, and
  confidence label.
- `FeatureImportanceScore`: dataclass with feature name, importance
  score, method, sample size, and evidence ids.
- `WeightRecommendation`: dataclass with target config key, current
  value, proposed value, rationale, feature-importance references, and
  required promotion state before adoption.
- `LearningReport`: dataclass mirroring `ResearchReport` — carries
  Markdown, JSON payload, flattened recommendations list, and
  reproducibility manifest; writes to `<output_dir>/<report_id>/`.
- `RecommendationEnvelope`: routing struct that packages a
  `WeightRecommendation` (or other recommendation) into
  `PromotionEntry.evidence["weight_recommendation"]` (or a similar
  namespaced key) so the promotion gate machinery can require it as
  evidence for the next transition.

#### Data Flow
1. Operator supplies (or the report generator points at) a frozen
   Phase 3 artifact directory plus a `trades_history.db` snapshot.
2. `EvidenceIntake` loads
   `ChampionChallengerComparison` + `WalkForwardReport` + relevant
   `ResearchReport` bundles + trade logs.
3. Statistical Decision Support computes metrics and produces
   `StatisticalFinding` records with confidence labels.
4. Historical Pattern Discovery correlates market-intelligence
   context (Phase 2 modules) with trade outcomes and produces
   `PatternHypothesis` records.
5. Feature Importance Analysis ranks features and produces
   `FeatureImportanceScore` records.
6. Strategy Weight Recommender consumes feature-importance output
   and produces `WeightRecommendation` records with a required
   promotion state (never `production` directly).
7. Learning Reports aggregate all outputs into a Markdown + JSON
   report bundle, writing to `reports/learning/<report_id>/` by
   default.
8. Operators (humans) review the report, decide whether to record
   evidence on the relevant `PromotionEntry`, and — if promoting —
   record an `ApprovalRecord`. The Learning System itself never
   performs steps 8.

#### Storage
- Inputs: read-only artifact tree under `reports/backtests/…` and the
  existing `trades_history.db`. No new writes to input paths.
- Outputs: `reports/learning/<report_id>/report.md`,
  `report.json`, `recommendations.json`, `manifest.json`.
- Recommendation records may also be referenced from `PromotionEntry`
  evidence dicts, but the entries themselves are human-authored data
  files or operations records, never generated automatically.

#### Configuration
- All Learning System settings live in explicit config parameters
  passed to the analysis functions or dataclasses — no reads from
  `strategy/config.py` beyond `FeatureFlags` (used only to verify
  disabled state).
- No production trading config may be modified by a Learning System
  run.
- No feature flag may be enabled as a side effect of a Learning
  System run.

### Hard Safety Rules

These rules apply to every Phase 4 card. A card that cannot satisfy
all of them is out of scope and must be re-plotted before work
begins.

1. **Recommendations only.** Every output is a labeled recommendation
   record with confidence, sample size, methodology, and evidence ids.
2. **No automatic production changes.** No module may write to
   `strategy/config.py`, mutate the `FeatureFlags` singleton, or
   change any file under `.hermes-profile/`.
3. **No feature flags enabled.** Modules may read
   `FeatureFlags.all_disabled` to assert isolation, but never
   `enable`/`disable` any flag.
4. **No buy/sell logic changed.** Modules may not import `trader.py`,
   `crypto_trader.py`, `strategy/runner.py`, `trader_cli.py`,
   `telegram_approvals.py`, or any scheduler / cron entry.
5. **No broker / order-path integration.** No `alpaca`,
   `TradingClient`, `place_order`, `submit_order`, `yfinance`,
   `api_key`, or credential references. Historical validation paper
   account remains documentation-only (see the "Validation Paper
   Account" subsection under Phase 3 Data Requirements).
6. **Human approval required.** Recommendations may advance
   promotion state only via `PromotionEntry.evidence` plus a
   human-authored `ApprovalRecord`. The Learning System never
   constructs an `ApprovalRecord` autonomously.
7. **Deterministic.** Given a fixed input artifact set, every module
   produces byte-identical outputs. Reports carry a `stable_hash`
   independent of `generated_at`.
8. **Terminology.** Validation, replay, and research. Never
   "training".

### Kanban Breakdown

Each Phase 4 card must be independently testable and reversible. No
card may enable behavior-changing feature flags or write to production
config.

#### `t_phase4_plan` — Learning System Technical Design
- **Status:** Done
- **Scope:** Planning only. No code. Establishes architecture, safety
  rules, and card breakdown for Phase 4.
- **Definition of Done:** ROADMAP Phase 4 section expanded with
  architecture, interfaces, data flow, storage, safety rules, and
  Kanban breakdown; KANBAN.md reflects the six implementation cards.

#### `t_phase4_stats_engine` — Statistical Decision-Support Layer
- **Status:** Done
- **Scope:** Add read-only descriptive statistics, confidence
  intervals, effect-size estimates, and sample-size checks over the
  trades log and Phase 3 comparison outputs. Produce
  `StatisticalFinding` records labeled `hypothesis` or `validated`.
- **Definition of Done:** Deterministic findings for a fixed input
  artifact set; JSON schema tests for `StatisticalFinding`; no
  order-path imports; no flag mutations.
- **Validation:** Tests for correct effect-size math on known
  fixtures, confidence-interval boundary behavior, sample-size floor
  enforcement, and reproducibility.
- **Implementation note:** `strategy/stats_engine.py` adds
  `StatisticalFinding`, `analyze_comparison`, and
  `analyze_walk_forward` plus numeric helpers (`normal_mean_ci`,
  `wilson_proportion_ci`, `cohens_d_one_sample`,
  `cohens_d_two_sample`) and `findings_stable_hash`.  Findings are
  labeled `validated` iff their sample size meets
  `STATS_SAMPLE_SIZE_FLOOR` (default 30); otherwise `hypothesis`.
  Analysis returns findings in fixed metric order per input type:
  `score_delta_mean → rank_delta_mean → disagreement_rate →
  selection_agreement_rate` for a comparison, and
  `wf_score_delta_mean → wf_disagreement_rate` aggregated across all
  splits' OOS comparisons for a walk-forward report.  Evidence ids
  reference the source `run_id` values so downstream reports can trace
  every finding back to a reproducible artifact.  Uses the
  normal-approximation z-table for mean CIs and Wilson score intervals
  for proportion CIs (supported confidence levels: 0.90, 0.95, 0.99).
  Never imports `strategy.config`, never reads or mutates the
  `FeatureFlags` singleton, and never references any order-path
  module.
- **Validation outcome:** 715 tests passing; math verified against
  known-input fixtures (mean CI, Wilson CI, Cohen's d); determinism
  verified (repeat analysis byte-identical, `findings_stable_hash`
  order-independent); sample-size floor enforced (below-floor →
  `hypothesis`, at/above → `validated`); no `alpaca` / `yfinance` /
  `TradingClient` / `api_key` / order-path references; module never
  imports `strategy.config` and never mutates global feature flags;
  commit `7005254`.

#### `t_phase4_pattern_discovery` — Historical Pattern Discovery
- **Status:** Done
- **Scope:** Identify co-occurring conditions across regime, sector
  leadership, market breadth, and relative strength history that
  correlate with trade outcomes. Emit `PatternHypothesis` records —
  never validated without an explicit out-of-sample check.
- **Definition of Done:** Discovered patterns come with supporting
  evidence ids, sample sizes, and confidence labels; all patterns
  default to `hypothesis`.
- **Validation:** Tests for hypothesis vs validated labeling, minimum
  sample-size gate, deterministic pattern ordering, and evidence-id
  provenance.
- **Implementation note:** `strategy/pattern_discovery.py` adds
  `PatternObservation`, `PatternHypothesis`, `discover_patterns`,
  `validate_patterns_out_of_sample`, and `hypotheses_stable_hash`.
  `discover_patterns` groups observations by sorted feature-key
  tuples, computes win rate (Wilson CI), mean return (normal-approx
  CI), and Cohen's d, and emits records with `LABEL_HYPOTHESIS` and
  supporting evidence trade ids.  Groups below the sample-size floor
  (`PATTERN_MIN_SAMPLE_SIZE = 10` default) are skipped.
  `validate_patterns_out_of_sample` promotes to `LABEL_VALIDATED`
  only when the OOS group meets the sample floor, the OOS mean-return
  direction matches, and the OOS win-rate direction matches;
  otherwise the record stays `LABEL_HYPOTHESIS` with a detail note.
  Reuses the stats-engine helpers (`normal_mean_ci`,
  `wilson_proportion_ci`, `cohens_d_one_sample`).  Never imports
  `strategy.config`; never references any order-path module.
- **Validation outcome:** 762 tests passing; all discovered patterns
  default to `LABEL_HYPOTHESIS`; OOS validation only promotes on
  matching return direction + matching win-rate direction + OOS
  sample meeting floor; OOS insufficient sample and directional
  mismatch keep the record as `LABEL_HYPOTHESIS` with detail notes;
  determinism verified (shuffled input yields identical output;
  `hypotheses_stable_hash` order-independent); global feature flags
  remain all disabled; commit `3711453`.

#### `t_phase4_feature_importance` — Feature Importance Analysis
- **Status:** Done
- **Scope:** Correlate score components, disagreement kinds, and
  market-context features with realized outcomes across the harness
  event windows. Produce ranked `FeatureImportanceScore` records.
- **Definition of Done:** Scores are deterministic, method metadata is
  recorded, and low-sample features are flagged rather than silently
  reported.
- **Validation:** Tests for methodology metadata, sample-size flagging,
  deterministic ordering, and rejection of look-ahead inputs (features
  computed after the outcome window).
- **Implementation note:** `strategy/feature_importance.py` adds
  `FeatureObservation` (with `feature_timestamp` and
  `outcome_timestamp` bookkeeping and a `has_lookahead()` guard),
  `FeatureImportanceScore` (with `method`, sample size, Fisher-z
  Pearson CI, label, and a `flagged` boolean carrying `low_sample`
  / `zero_variance` / `ci_spans_zero` reasons), and
  `analyze_feature_importance` (Pearson r + Fisher z CI, sorted by
  `|score|` descending with feature-name tiebreak). Look-ahead is
  rejected by default with a `ValueError`; callers can pre-clean via
  `filter_lookahead_observations` and pass `reject_lookahead=False`.
  Reuses stats-engine confidence-level plumbing.  Never imports
  `strategy.config`; never references any order-path module.
- **Validation outcome:** 814 tests passing; `pearson_r` returns 1.0
  for perfect positive linear inputs; Fisher CI symmetric at r=0;
  look-ahead observations rejected by default (`ValueError`);
  `filter_lookahead_observations` correctly partitions;
  deterministic ranking (perfectly correlated feature ranks above
  uncorrelated feature; passing the same list in a different order
  yields identical output); low-sample features flagged with
  `low_sample`; `importance_stable_hash` order-independent; global
  feature flags remain all disabled; commit `22259ad`.

#### `t_phase4_weight_recommender` — Strategy Weight Recommender
- **Status:** Done
- **Scope:** Suggest adjusted weights for Champion scoring components
  (e.g., `SELL_SCORE_FACTOR_WEIGHTS`) based on feature-importance
  output. Emit `WeightRecommendation` records that name the target
  config key, current value, proposed value, rationale, and required
  promotion state.
- **Definition of Done:** Module never writes to `strategy/config.py`;
  recommendations always include a `required_promotion_state` no
  higher than `paper_trading`; unit tests prove config immutability.
- **Validation:** Tests for config-write refusal, `RecommendationEnvelope`
  round-trip through `PromotionEntry.evidence`, and refusal to
  recommend `production` directly.
- **Implementation note:** `strategy/weight_recommender.py` adds
  `WeightRecommendation`, `RecommendationEnvelope`,
  `recommend_weights`, `envelope_for`, and
  `recommendations_stable_hash`.  `MAX_ALLOWED_PROMOTION_STATE` is
  `STATE_PAPER_TRADING`; `FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER`
  covers `candidate` / `approved` / `production` and is enforced both
  at the `recommend_weights` entry point and by the
  `WeightRecommendation.__post_init__` validator.  Recommendations are
  skipped for feature scores that are hypothesis-labeled, flagged
  `low_sample` / `zero_variance` / `ci_spans_zero`, below the
  configurable `min_feature_score`, or absent from the caller's
  `current_weights` dict.  `envelope_for` builds a
  `RecommendationEnvelope` whose `apply_to_entry` returns a new
  `PromotionEntry` (never mutates the input) with a serialized
  recommendation added under a configurable `evidence_key`.  Module
  never imports `strategy.config`; unit tests read
  `strategy/config.py` bytes before and after a recommender run and
  assert byte-identical content.
- **Validation outcome:** 867 tests passing; forbidden promotion
  states (`candidate` / `approved` / `production`) rejected at both
  entry points; recommendation math verified (10 → 12.0 at +20%);
  `apply_to_entry` proven immutable (original entry evidence
  unchanged after routing); `strategy/config.py` bytes verified
  byte-identical before and after a recommender run; determinism +
  order-independent hash verified; global feature flags remain all
  disabled; commit `a04d46d`.

#### `t_phase4_learning_reports` — Learning Report Generation
- **Status:** Done
- **Scope:** Aggregate `StatisticalFinding`, `PatternHypothesis`,
  `FeatureImportanceScore`, and `WeightRecommendation` records into a
  `LearningReport` bundle. Mirror the Phase 3 `ResearchReport` layout
  (`report_id`, manifest, `stable_hash`, four artifact files).
- **Definition of Done:** Bundle writes four files under
  `reports/learning/<report_id>/`; `stable_hash` independent of
  `generated_at`; report id derived from source stable hash.
- **Validation:** Snapshot tests for Markdown structure; JSON schema
  tests; determinism tests; idempotent write.
- **Implementation note:** `strategy/learning_reports.py` adds
  `LearningReport`, `LearningReportPaths`, and
  `render_learning_report`.  `report_id` is derived from a
  deterministic hash of the four source hashes
  (`findings_hash + hypotheses_hash + importance_hash +
  recommendations_hash`).  `LearningReport.stable_hash` excludes
  `generated_at`.  `write(output_dir)` persists four files under
  `<output_dir>/<report_id>/`: `report.md`, `report.json`,
  `recommendations.json`, `manifest.json`.  Default output tree is
  `reports/learning/`.  Markdown renders sections for statistical
  findings, pattern hypotheses, feature importance, weight
  recommendations, optional recommendation envelopes, and a
  reproducibility footer.  Never imports `strategy.config`; never
  references any order-path module.
- **Validation outcome:** 893 tests passing; `stable_hash` and
  `report_id` independent of `generated_at`; repeat render
  byte-identical across `to_json`, `to_markdown`,
  `recommendations_json`, and `manifest_json`; `write` persists all
  four files, is idempotent, and lands under
  `<output_dir>/<report_id>/`; manifest carries all four source
  hashes; global feature flags remain all disabled; commit
  `878c50e`.

#### `t_phase4_validation` — End-to-End Learning System Validation
- **Status:** Review
- **Scope:** Wire the Learning System end-to-end against a frozen
  Phase 3 artifact set (test fixtures under `tests/fixtures/`) and
  assert determinism, feature-flag isolation, no order-path imports,
  and no writes outside the configured `reports/learning/` root.
- **Definition of Done:** A single test target replays the full
  pipeline twice and asserts byte-identical outputs; a separate test
  target proves flag isolation and forbidden-import bans.
- **Validation:** Tests must cover: byte-identical reruns, empty input
  handling, missing-input handling, and refusal to write outside the
  configured output root.
- **Implementation note:** `strategy/learning_pipeline.py` adds
  `LearningPipeline` (frozen dataclass) with an immutable
  `output_root` and a `run(...)` method that renders + writes.  A
  defensive path guard raises `LearningPipelineError` if the resolved
  report directory escapes the configured root.
  `tests/test_learning_system_e2e.py` exercises the full pipeline
  against a frozen fixture set, asserts byte-identical files across
  two independent pipeline runs sharing the same `generated_at`,
  handles empty and partial inputs, verifies that no files land
  outside `output_root` (including through a symlinked root),
  confirms the global `FeatureFlags` singleton stays disabled,
  confirms `strategy/config.py` bytes are unchanged, scans every
  Phase 4 module for source-level `alpaca` / `place_order` /
  `submit_order` / `TradingClient` / `api_key` / `yfinance`
  references, asserts import-time exclusion of `trader_cli`,
  `trader`, `crypto_trader`, and `telegram_approvals`, and audits
  terminology to catch any stray "training" references outside the
  policy sentence.

### Dependencies

- `t_phase4_stats_engine` depends only on Phase 3 artifacts (available).
- `t_phase4_pattern_discovery` depends only on Phase 2 intelligence
  modules (available).
- `t_phase4_feature_importance` depends on `t_phase4_stats_engine`.
- `t_phase4_weight_recommender` depends on `t_phase4_feature_importance`.
- `t_phase4_learning_reports` depends on all four analysis cards.
- `t_phase4_validation` depends on all five implementation cards.

### Architectural Risks

- **Overfitting.** Feature importance and weight recommendations may
  fit noise. Mitigation: require walk-forward evidence before any
  weight recommendation targets `paper_trading` or above.
- **Small-sample findings.** Statistical findings may look strong on
  short windows. Mitigation: hard sample-size floors enforced by the
  stats engine.
- **Metric gaming.** Recommendations optimizing one metric may degrade
  drawdown or concentration. Mitigation: the weight recommender must
  cite the rollback criteria in `strategy.promotion_gates` and refuse
  to recommend changes that would trigger a standard rollback.
- **Silent config drift.** A future contributor may wire a
  recommendation directly into `strategy/config.py`. Mitigation: the
  weight recommender's tests must prove config immutability via
  file-content assertions.
- **Look-ahead bias.** Features computed after the outcome window may
  leak into importance analysis. Mitigation: feature-importance tests
  explicitly reject post-outcome features.
- **Artifact sprawl.** Learning reports may accumulate. Mitigation:
  `stable_hash`-derived `report_id`s de-duplicate reruns.

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
