# CHANGELOG

Trader Joe release history.

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
