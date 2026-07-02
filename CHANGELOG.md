# CHANGELOG

Trader Joe release history.

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
