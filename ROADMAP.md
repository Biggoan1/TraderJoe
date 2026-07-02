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

**Status:** IN PROGRESS (Kanban-driven)

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
- **Commit:** Pending validation
- **Branch:** sprint-3/daily-digest
- **Tests:** 371 passing (23 new sector leadership and research platform tests + 348 existing)
- **Behavior change:** No
- **Feature flags enabled:** None
- **Status:** Review
- **Summary:**
  - Observational sector leadership analyzer ranks sector ETFs against SPY
  - Tracks strongest and weakest sectors across 5-day, 20-day, and 60-day windows
  - Handles missing sector data, missing benchmark data, invalid prices, and provider failures
  - Exposes sector leadership through the read-only research platform market intelligence contract
  - Adds disabled-by-default `enable_sector_leadership` flag inventory without runner integration

### Kanban Board

Sprint 7 onward uses Kanban for workflow management. See [KANBAN.md](KANBAN.md).

**Chain:** Sprint 7 → Sprint 8 → Sprint 9 → Sprint 10 → Phase 3+

| # | Card | Sprint | Status |
|---|---|---|---|
| `t_909faeab` | Sprint 7: Morning Intelligence Agent | Phase 2 | Done |
| `t_194b8638` | Sprint 8: Enhanced Daily Digest + Trade Metadata | Phase 2 | Done |
| `t_c0ab3a10` | Sprint 9: Sector Leadership Tracking | Phase 2 | Review |
| `t_5ec6406e` | Sprint 10: Market Breadth Analysis | Phase 2 | Blocked (-> S9 validation) |

**Rules:** Observational only. No trading behavior changes. Every feature behind a flag.

**Exit Criteria:** Four weeks of paper-trading data with all observational features enabled.

---

## Phase 3 — Decision Engine

**Status:** PLANNED

**Objective:** Improve trading decisions using data from Phases 1 and 2.

- Relative Strength incorporated into rankings
- Market Regime incorporated into scoring
- Sell Score replacing single-indicator exits
- Risk-Based Position Sizing
- Confidence Scoring
- Enhanced Candidate Ranking

**Rules:** One feature at a time. Measure impact before enabling next. Champion remains production.

**Exit Criteria:** Evidence of improved expectancy, drawdown, or alpha over Champion.

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

**Current:** Sprint 9 — Sector Leadership Tracking (Phase 2, Review)
**Next:** Sprint 10 — Market Breadth Analysis (Phase 2, blocked pending Sprint 9 validation)
