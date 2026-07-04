# Trader Joe — Status

## Version
- Current: v0.17.0
- Champion: v0.4.0
- Challenger: rs-challenger-v0.1.0 (disabled by default)

## Sprint
- Current: none (Phase 5 complete)
- Last completed: Phase 5 Two-Month Historical Validation (`t_phase5_two_month_validation_run`)
- Workflow: Kanban (see KANBAN.md)

## Repository
- Branch: sprint-3/daily-digest
- Commit: latest HEAD (Research Alpaca client validation)
- Tags: v0.4.0 (Phase 1 baseline), v0.10.0-phase2 (Phase 2), v0.17.0-phase3 (Phase 3)
- Status: Research Alpaca client validated + env isolation + launchers + model config; Local LLM Research Assistant ready

## Testing
- Tests passing: 1811
- Tests failing: 0
- Last run: 2026-07-03

## Feature Flags
- All disabled: true
- Enabled flags: none

## Trading
- Production mode: Champion
- Paper trading: active

## Database
- Schema version: 1
- Trade log: enabled

## Outstanding
- TODOs: 12

## Phase
- Current: 5.6 (Historical Data Warehouse) — first implementation card in Review
- Phase 1: complete
- Phase 2: complete (6/6 complete)
- Phase 3: complete (7/7 complete)
- Phase 4: complete (7/7 complete)
- Phase 5: complete (4/4 complete)
- Phase 5 follow-ups (Done): `t_phase5_rs_live_feed` (76637cc), `t_phase5_champion_explanations` (b806e9f + f1d1658 + f46a548)
- Phase 5.5 (planned): Research & Learning Dashboard — 1 planning card in Backlog
- Phase 5.6 (**complete**, tag `v0.30.0-phase5.6`): Historical Data Warehouse — planning card `t_90ca9e6f` Done. **All 12 implementation cards + bridge card Done** (`t_b4b798af` — HistoricalValidation warehouse wiring, commit `81d0a9a`).  Read + write + validation + cache + wiring path fully complete.  Warehouse-first `WarehouseReader` with provider priority + version pinning; ``ResearchAccountClient.fetch_bars`` provably not called when local coverage is complete.  Non-blocking for v1.0. Storage architecture: DuckDB + Parquet + SQLite (hybrid). Design doc: `docs/architecture/phase-5-6-historical-warehouse.md`.  **Remaining before first live warehouse-backed 60-day replay: operator populates the warehouse via ``import_bars`` against the Research Alpaca account.**
