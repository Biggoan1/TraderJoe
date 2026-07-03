# CHANGELOG

Trader Joe release history.

---

## v0.14.0 — Phase 3: Relative Strength Challenger Overlay

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Review
**Card:** `t_phase3_rs_challenger`

### Added
- `strategy/rs_challenger.py` — read-only, disabled-by-default RS
  overlay for Champion/Challenger comparison
- `RelativeStrengthChallenger` wraps any `ComparisonEvaluator` and
  applies an additive score contribution
  `weight * (rs - neutral) / range` when
  `FeatureFlags.enable_relative_strength` is `True`
- `RelativeStrengthProvider` protocol for caller-supplied RS lookups
  keyed by symbol and event; providers return `None` when RS data is
  unavailable
- `rs_provider_from_map` builds a deterministic provider from a
  `{timestamp: {symbol: rs}}` snapshot (defensively copied) suitable for
  the Data Catalog and unit tests
- `RS_CHALLENGER_STRATEGY_ID` / `RS_CHALLENGER_FLAG_NAME` /
  `DEFAULT_RS_OVERLAY_WEIGHT` / `DEFAULT_RS_NEUTRAL_SCORE` /
  `DEFAULT_RS_SCORE_RANGE` constants

### Behavior
- Disabled by default — global `FeatureFlags` singleton is never
  mutated; enablement is done per-instance by passing a local
  `FeatureFlags(enable_relative_strength=True)` to the constructor
- When disabled the wrapper is Champion-parity: scores, rankings, and
  explanations are byte-identical to the base evaluator (only
  `strategy_id` differs); the harness records zero disagreements
- When enabled: scores get an additive contribution, rankings are
  recomputed by new score desc with symbol-asc tie-break, per-symbol
  explanations note the base and RS contribution
- RS values outside `[neutral - range, neutral + range]` are clamped
- Missing RS values keep the base score and add a single aggregated
  warning; provider exceptions are captured per-symbol as warnings
- Symbols with a base score but no base ranking are rescored but stay
  unranked

### Tests
- `tests/test_rs_challenger.py` — 32 tests covering:
  - Constructor rejects negative weight, non-positive range, empty
    `strategy_id`; defaults match module constants; conforms to
    `ComparisonEvaluator` protocol
  - `is_enabled` reflects the flag; global singleton stays disabled
  - Champion parity when disabled: identical scores, rankings, and
    explanations; harness records zero disagreements; result is a deep
    copy of the base evaluation
  - Symmetric contribution formula for both positive and negative RS
    deltas
  - Neutral RS produces zero contribution
  - Out-of-range RS is clamped
  - Custom weight scales contribution linearly
  - Re-rank by new scores desc; symbol-asc tie-break
  - Explanations include base note, RS score, and signed contribution
  - Symbols in base scores but not base rankings stay unranked
  - Harness surfaces `ranking_only` and `score_delta` disagreements
    correctly when enabled
  - Missing RS symbol keeps base score with an aggregated warning
  - Empty snapshot marks every symbol
  - Provider exception recorded as a warning; base score preserved
  - Missing-RS symbol with preserved rank/score triggers no harness
    disagreement; `data_unavailable` remains reserved for one-sided
    symbols
  - `rs_provider_from_map` returns values for known keys, `None` for
    unknown timestamp/symbol, defensively copies its input
  - Repeat runs produce identical evaluations; harness `stable_hash` is
    independent of `generated_at`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Importing `strategy.rs_challenger` does not pull in `trader_cli`,
    `trader`, `crypto_trader`, or `telegram_approvals`
  - Global feature flags remain `all_disabled` after a run
- 532 passing total (0 failures)

### Notes
- Overlay is behavior-neutral: production Champion path is unchanged;
  the runner (`strategy/runner.py`), scheduler, Telegram approval flow,
  CLI, and plugin behavior are untouched
- No feature flags are enabled globally; local `FeatureFlags` instances
  in tests do not mutate the singleton
- RS data source is caller-supplied — no `yfinance` calls, no HTTP,
  no broker credentials, no historical validation paper account wiring
- Walk-forward pipeline (`t_phase3_walk_forward`), reports
  (`t_phase3_reports`), and promotion gates (`t_phase3_promotion_gates`)
  remain in Backlog

---

## v0.13.0 — Phase 3: Champion/Challenger Comparison Harness

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_champion_challenger`
**Commit:** `1b341ef`, plus policy addendum `a0fa765` and validation board update

### Validation
- 500 tests passing (0 failing)
- Working tree clean prior to validation commit
- Harness confirmed read-only and deterministic (verified `stable_hash`
  and `run_id` independent of `generated_at`; repeated runs produce
  byte-identical `to_dict()` output)
- No references to `alpaca`, `place_order`, `submit_order`, or
  `TradingClient` in `strategy/comparison_harness.py`
- No credentials, env vars, SDK calls, or account wiring added
- Historical validation paper account remains documentation-only (no
  code path in the harness reads from it)
- Terminology audit passed: docs refer to validation, replay, or
  research — no "training" usage
- Feature flags remain `all_disabled` after harness runs; runner,
  scheduler, Telegram, CLI, and plugin behavior unchanged
- Card `t_phase3_champion_challenger` moved to Done
- Card `t_phase3_rs_challenger` unblocked and moved to Ready

### Added
- `strategy/comparison_harness.py` — read-only Champion/Challenger comparison
  harness built on top of the Backtest Lab foundation
- `ComparisonEvaluator` runtime protocol satisfied by the existing
  `NoOpStrategyAdapter`
- `ScoreRow` and `ScoreTable` per-event alignment of Champion and
  Challenger scores, ranks, selection status, score deltas, and rank
  deltas
- `DisagreementRecord` classifying divergences as `ranking_only`,
  `entry_selection`, `score_delta`, or `data_unavailable`
- `ChampionChallengerRunMetadata` with a deterministic `run_id` derived
  from champion/challenger ids, dataset id, event signature, seed, and
  threshold
- `ChampionChallengerComparison` container with `to_dict`, `to_json`,
  `stable_hash` (ignores `generated_at`), and
  `disagreements_by_kind()`
- Score-delta threshold controls score-only disagreement sensitivity
- Rows sorted by symbol; disagreements sorted by
  `(event_timestamp, event_type, symbol, kind)` for deterministic output
- `CHAMPION_ROLE` / `CHALLENGER_ROLE` aliases reuse
  `strategy.config.CHAMPION_NAME` / `CHALLENGER_NAME`

### Changed
- `strategy/data_catalog.py` — dropped unused `stable_json` import
  (non-blocking lint noted during validation of `t_phase3_data_catalog`)

### Tests
- `tests/test_comparison_harness.py` — 27 tests covering:
  - `ScoreRow`, `ScoreTable`, and `DisagreementRecord` serialization
  - Rejection of unknown disagreement kinds
  - Role aliases (`CHAMPION_ROLE`, `CHALLENGER_ROLE`)
  - `NoOpStrategyAdapter` satisfies `ComparisonEvaluator`
  - Harness rejects negative thresholds and identical strategy ids
  - No-op run produces empty tables and no disagreements
  - Identical evaluations produce no disagreements
  - Explicit classification tests for `ranking_only`, `entry_selection`,
    `score_delta`, and `data_unavailable`
  - Score-delta threshold suppresses under-threshold differences
  - Disagreements sorted deterministically across multiple events
  - Score-table rows sorted alphabetically by symbol
  - Score tables capture event type, sequence, and per-side warnings
  - Run id derived from inputs; `stable_hash` independent of
    `generated_at`; run id changes with dataset id
  - `to_json` produces byte-identical output across runs (excluding
    `generated_at`)
  - Full result is JSON-serializable
  - `disagreements_by_kind()` partitions records across known kinds
  - Module source has no `alpaca` / `place_order` / `submit_order` /
    `TradingClient` references
  - Feature flags remain `all_disabled` after harness runs
  - Importing the harness module does not pull in `trader_cli`, `trader`,
    `crypto_trader`, or `telegram_approvals`
- 500 passing total (0 failures)

### Notes
- Harness foundation only — no Relative Strength Challenger implementation
- Runner, scheduler, Telegram, CLI, plugin, and order-path behavior
  unchanged
- No feature flags enabled; `strategy/config.py` untouched
- Relative Strength Challenger (`t_phase3_rs_challenger`) and
  Walk-Forward Pipeline (`t_phase3_walk_forward`) remain in Backlog
- A separate Alpaca paper account is documented as a **future**
  validation data source (see ROADMAP "Validation Paper Account"). This
  card does not integrate it — no credentials, SDK calls, env plumbing,
  or runner wiring were added. Isolation rules: must remain separate
  from the live and normal paper accounts, must never be used by the
  live runner, must not share credentials with production, and is
  reserved for historical replay, walk-forward validation, and
  Champion/Challenger comparison only. Referred to as validation,
  replay, or research — never "training."

---

## v0.12.0 — Phase 3: Research Data Catalog

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_data_catalog`
**Commit:** `0410aa2`, plus validation board update

### Validation
- 473 tests passing (0 failing)
- Working tree clean prior to validation commit
- Confirmed catalog is read-only (no file/manifest mutation on validate)
- Confirmed catalog is deterministic (sorted listings, stable manifest hashes)
- Confirmed reproducibility metadata is stable across repeated calls
- Confirmed `stable_hash` is independent of `imported_at`
- Confirmed no references to `alpaca`, `place_order`, `submit_order`, or
  `TradingClient` in `strategy/data_catalog.py`
- Confirmed feature flags remain `all_disabled` after catalog operations
- No trading behavior, buy/sell logic, runner behavior, or feature flags
  changed
- Card `t_phase3_data_catalog` moved to Done
- Card `t_phase3_champion_challenger` unblocked and moved to Ready

### Added
- `strategy/data_catalog.py` — read-only Research Data Catalog
- `DatasetFile`, `DatasetManifest`, and `DatasetValidationResult` dataclasses
  with structural validation and stable JSON serialization
- `DataCatalog.from_directory()` loads immutable manifests from
  `research_data/manifests/` and rejects duplicate dataset ids
- `DataCatalog.list()` / `get()` / `has()` for read-only lookup with kind
  filtering across `historical_bars`, `benchmark`, `paper_log`, and
  `research_context`
- `DataCatalog.validate()` verifies file existence, size, SHA-256, and
  CSV / JSON schema against the manifest and never mutates the dataset
- `DataCatalog.validate_all()` produces one result per registered dataset
- `DataCatalog.checksum_file()` recomputes the on-disk SHA-256 for a
  referenced file
- `DataCatalog.reproducibility_metadata()` returns manifest hash, kind,
  source, imported timestamp, and per-file checksums for run manifests
- `build_dataset_manifest()` operator helper computes checksums offline
- `sha256_file()` streaming hash utility
- Manifest `stable_hash()` ignores `imported_at` for deterministic run ids

### Tests
- `tests/test_data_catalog.py` — 45 tests covering:
  - Dataset kind constants
  - `DatasetFile` roundtrip, tuple coercion, and validation errors
  - `DatasetManifest` roundtrip, deterministic hashing, validation errors,
    and duplicate-file-path rejection
  - `build_dataset_manifest` computes real checksums and rejects missing files
  - `sha256_file` matches `hashlib` for identical bytes
  - Catalog loading (sorted, filtered, missing dir, duplicate id rejection)
  - Validation success, missing file, checksum mismatch, size mismatch,
    CSV schema mismatch, JSON schema success, JSON schema missing key,
    unspecified-schema warning, and `validate_all` result ordering
  - `validate` and `validate_all` do not mutate manifest or data bytes
  - `checksum_file` matches on-disk digest and rejects unregistered paths
  - `reproducibility_metadata` contains manifest hash, kind, source,
    imported timestamp, symbols/benchmarks, and file checksums; is
    deterministic across calls
  - Observational-only guarantees: no `alpaca`, `place_order`, or
    `TradingClient` references, and feature flags remain all disabled
- 473 passing total (0 failures)

### Notes
- Registry is strictly read-only — no dataset files or manifests are
  written by the catalog itself
- Operator helper `build_dataset_manifest` is offline and not called by
  catalog reads
- No live brokerage calls, order placement, buy/sell logic, runner
  behavior, or feature flags changed
- `research_data/` remains untracked in this commit; manifests are
  produced offline before being checked in

---

## v0.11.0 — Phase 3: Backtest Lab Foundation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 968b5f5, plus validation board update

### Added
- `strategy/backtest_lab.py` — Backtest Lab foundation data contracts
- Deterministic `BacktestConfig` with stable hashing and validation
- `BacktestRunMetadata`, `BacktestArtifactPaths`, and `BacktestRunManifest`
  for reproducible run identity and artifact locations
- `BacktestStrategyResult` and `BacktestReport` shells for future
  Champion/Challenger result reporting
- `BacktestEvent`, `DeterministicReplayClock`, `StrategyEvaluation`, and
  `NoOpStrategyAdapter` fixtures for deterministic replay scaffolding
- Stable JSON/hash helpers for deterministic experiment metadata
- Empty report factory for future dry-run backtest workflows

### Tests
- `tests/test_backtest_lab.py` — 27 tests covering:
  - Stable JSON and hash determinism
  - Config serialization and validation errors
  - Run metadata and timestamp-independent run ids
  - Artifact path generation
  - Manifest serialization and stable hashes
  - Deterministic replay event ordering
  - No-op strategy adapter evaluation
  - Strategy result and report serialization
  - Markdown report shell output
  - Observational-only guarantees
- 428 passing total (0 failures)

### Notes
- Foundation only — no historical replay engine yet
- No Relative Strength Challenger implementation
- No live brokerage calls, order placement, buy/sell logic, runner behavior, or feature flags changed
- Card `t_phase3_backtest_lab` validated and moved to Done
- Card `t_phase3_data_catalog` unblocked and moved to Ready

---

## v0.10.0 — Sprint 10: Market Breadth Analysis

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 5bcbf83

### Added
- `strategy/market_breadth.py` — observational market breadth tracker
- Watchlist participation analysis across 20-day and 50-day moving averages
- Advancer, decliner, unchanged, new-high, and new-low counts
- Aggregate breadth score and breadth regime classification
- `BreadthSymbolObservation` and `MarketBreadthReport` structured outputs with JSON-ready serialization
- Market breadth summaries exposed through the read-only research platform
- Disabled-by-default `enable_market_breadth` feature flag inventory

### Tests
- `tests/test_market_breadth.py` — 27 tests covering:
  - Moving average, period return, latest close, percentage, and A/D ratio helpers
  - New high and new low edge cases
  - Breadth score and regime classification
  - Report/result serialization
  - Missing data, invalid close, insufficient history, and provider failures
  - Mocked price provider behavior
  - Observational-only guarantees
- `tests/test_research_platform.py` — 3 new tests plus snapshot coverage updates covering:
  - Market breadth data contract defaults
  - Snapshot JSON compatibility
  - Mocked research platform market breadth handoff
  - Failure fallback behavior
- 401 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, runner behavior, or enabled feature flags changed
- Card `t_5ec6406e` validated and moved to Done
- Phase 2 Market Intelligence implementation completed

---

## v0.9.0 — Sprint 9: Sector Leadership Tracking

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 465faeb

### Added
- `strategy/sector_leadership.py` — observational sector leadership tracker
- Sector ETF leadership ranking against SPY across 5-day, 20-day, and 60-day windows
- `SectorLeadershipResult` and `SectorLeadershipReport` structured outputs with JSON-ready serialization
- Strongest/weakest sector summaries exposed through the read-only research platform
- Disabled-by-default `enable_sector_leadership` feature flag inventory

### Tests
- `tests/test_sector_leadership.py` — 20 tests covering:
  - Period return calculations
  - Leadership scoring and trend classification
  - Report/result serialization
  - Missing sector data and missing benchmark handling
  - Invalid/zero price handling
  - Mocked price provider behavior
  - Observational-only guarantees
- `tests/test_research_platform.py` — 3 new tests plus snapshot coverage updates covering:
  - Sector leadership data contract defaults
  - Snapshot JSON compatibility
  - Mocked research platform sector leadership handoff
  - Failure fallback behavior
- 371 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, runner behavior, or enabled feature flags changed
- Card `t_c0ab3a10` validated and moved to Done
- Card `t_5ec6406e` unblocked and moved to Ready

---

## v0.8.0 — Sprint 8: Enhanced Daily Digest + Trade Metadata

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** c60f807

### Added
- `DigestMetadataSummary` for observational trade metadata aggregation
- Daily Digest metadata sections for symbols, exit reasons, market context,
  active flags, entry scores, custom metadata, and RS snapshots
- Optional `metadata` payloads on `TradeLogger.log_trade_entry()` and
  `TradeLogger.log_trade_exit()`
- SQLite migration for nullable `trade_metadata_entry` and
  `trade_metadata_exit` columns
- JSONL export parsing for RS snapshots and custom metadata payloads
- `rs_data` passthrough in `DailyDigestService.generate_and_deliver()`

### Changed
- Digest stats now preserve the requested report date
- Trade details render both legacy digest keys and TradeLogger row keys

### Tests
- 9 new/updated tests covering:
  - Metadata summary aggregation
  - Malformed metadata handling
  - Digest metadata and RS snapshot rendering
  - TradeLogger metadata storage and export
  - Existing database migration
  - Service RS data passthrough
- 348 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, or feature flags changed
- Card `t_194b8638` validated and moved to Done

---

## v0.7.0 — Sprint 7: Morning Intelligence Agent

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** c1ed907

### Added
- `strategy/morning_intelligence.py` — Observational pre-market briefing agent
- Composes existing market regime, overnight risk, and relative strength data
- `MorningIntelligenceReport` structured output with JSON serialization
- `MorningIntelligenceRenderer` Markdown output for review/delivery workflows
- Provider injection for deterministic tests and future scheduler integration
- Feature flag recording: `enable_morning_intelligence` remains disabled by default

### Tests
- `tests/test_morning_intelligence.py` — 19 tests covering:
  - Report serialization
  - Markdown rendering
  - Mocked provider aggregation
  - Feature flag recording
  - Empty watchlist and provider failure handling
  - Observational-only guarantees
- 339 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No scheduler, Telegram, buy/sell, ranking, or strategy behavior changes
- Card `t_909faeab` validated and moved to Done

---

## v0.6.0 — Sprint 6: Overnight Risk Engine

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest

### Added
- `strategy/overnight_risk.py` — Overnight gap risk analysis engine
- Measures overnight market risk by comparing previous close to
  pre-market/after-hours prices
- Purely observational — produces NO trading signals, places NO trades,
  and does NOT influence strategy scoring
- Per-symbol observations: previous_close, current_price, overnight_gap_pct,
  gap_direction, significant_gap, risk_score, data_quality
- Aggregate report: symbols_analyzed, significant_gaps, avg_risk_score,
  max_gap_up, max_gap_down
- Gap calculation helpers: calculate_gap_pct, determine_gap_direction,
  is_significant_gap, compute_risk_score
- PriceFetcher with yfinance integration and graceful error handling
- Feature flag: `enable_overnight_risk_engine` (disabled by default)
- Runner stubs in `_run_challenger` and `_challenger_sell` (observational only)

### Changed
- Removed dead imports (`pandas`, `OVERNIGHT_RISK_THRESHOLD`,
  `OVERNIGHT_EVAL_START_HOUR`, `OVERNIGHT_EVAL_START_MINUTE`) from
  `strategy/overnight_risk.py`
- Documented threshold design: engine uses `DEFAULT_GAP_THRESHOLD` (2.0 %)
  as the natural unit; config risk-score threshold reserved for Phase 3

### Tests
- `tests/test_overnight_risk.py` — 78 tests covering:
  - OvernightGapObservation and OvernightRiskReport dataclasses
  - Gap calculation helpers (calculate_gap_pct, determine_gap_direction,
    is_significant_gap, compute_risk_score)
  - PriceFetcher (mocked yfinance)
  - OvernightRiskEngine (assess, assess_symbol, get_summary)
  - get_engine convenience function
  - Feature flag integration
  - Observational-only guarantees (no trading methods, no signals)
  - Edge cases (missing data, zero/invalid close, extreme gaps, single price)
- 320 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- Gap threshold default is wired through `strategy.config`
- Timing/risk-score constants defined for future scheduler integration
- Engine can be wired into Phase 3 Decision Engine as a sell signal source

---

## v0.5.0 — Sprint 5: Market Regime Classification

**Date:** 2026-07-02
**Commit:** b785a35
**Branch:** sprint-3/daily-digest

### Added
- `strategy/market_regime.py` — Market regime classification engine
- Classifies market as bullish/bearish/volatile/neutral
- SPY/QQQ analysis: price vs MA, ATR volatility, ADX trend strength, MACD momentum
- 8 signals across 2 benchmarks with weighted scoring
- 43 new tests for indicators, signals, and classification

### Tests
- 151 passing (0 failures)

### Notes
- Observational only — no impact on trading decisions
- Feature flag: `enable_market_regime` (disabled by default)
- Classification uses 20-day and 50-day MAs, 14-day ATR/ADX
- Foundation for Phase 3 Decision Engine regime-based scoring

---

## v0.4.0 — Sprint 4: Relative Strength Analysis

**Date:** 2026-07-02
**Commit:** 74dc06c
**Branch:** sprint-3/daily-digest

### Added
- `strategy/relative_strength.py` — Relative strength comparison against SPY/QQQ benchmark
- Relative strength analysis in daily digest
- Tests for relative strength calculations

### Tests
- 108 passing (0 failures)

### Notes
- Observational only — no impact on trading decisions
- Compares candidate performance against market benchmarks
- Foundation for Phase 3 Decision Engine

---

## v0.3.0 — Sprint 3: Daily Performance Digest

**Date:** 2026-07-01
**Commit:** 49f935f
**Branch:** sprint-3/daily-digest

### Added
- `strategy/daily_digest.py` — Full digest pipeline (Builder, Renderer, Saver, Service)
- `strategy/trade_logger.py` — `get_day_trades(date)` method
- `scripts/generate_daily_digest.py` — Standalone CLI with `--date` and `--dry-run`
- `tests/test_daily_digest.py` — 23 tests

### Changed
- Daily digest now includes relative performance metrics

### Tests
- 70 passing (0 failures)

### Notes
- Zero impact on trading decisions
- Pure observability feature
- Digests saved to `reports/` directory

---

## v0.2.0 — Sprint 2: Historical Trade Logger

**Date:** 2026-07-01
**Commit:** 70790f9
**Branch:** sprint-2/historical-statistics

### Added
- Persistent trade history logging
- Historical statistics calculation
- Performance metrics tracking (win rate, profit factor, drawdown)
- Database schema for trade records

### Tests
- 47 passing (0 failures)

### Notes
- Trades logged with entry/exit details, indicators, and timestamps
- Foundation for all future performance analysis

---

## v0.1.0 — Sprint 1: Feature Flag and Champion/Challenger

**Date:** 2026-07-01
**Commit:** 26eca28
**Branch:** sprint-1/feature-flag-framework

### Added
- Feature flag system with runtime enable/disable
- Champion/Challenger strategy architecture
- `strategy/config.py` — Feature flag configuration
- `strategy/runner.py` — Strategy runner with flag awareness
- Initial test suite

### Changed
- Strategy selection now respects feature flags
- Champion strategy remains production default

### Tests
- 4 passing (0 failures)

### Notes
- All flags disabled by default
- Champion strategy unchanged
- Foundation for all future incremental improvements

---

## v0.0.1 — Initial Release

**Date:** 2026-07-01
**Commit:** 194bf1a
**Branch:** main

### Added
- Paper trading system with Alpaca integration
- Technical indicator scanning (RSI, Bollinger Bands, MACD)
- Watchlist management
- Telegram integration for approvals and alerts
- Market screener with candidate ranking

### Notes
- Initial paper trading system
- Day-trading focused
- Six-gate buy rules, single-indicator exits
