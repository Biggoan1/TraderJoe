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
- Tests passing: 1559
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
- Phase 5.6 (in progress): Historical Data Warehouse — planning card `t_90ca9e6f` Done. 3 implementation cards Done (`t_1c8a70da` — MarketDataProvider interface, commit `808ec02`; `t_b2a75ee8` — Local historical warehouse layout, commit `c2173c9`; `t_56f319a9` — Data catalog extension, commit `0fd96dd`). 9 remaining implementation cards Ready. Downstream unblocked after catalog: `t_c32b8416` (data_versioning); still awaiting `t_6168af8e` (parquet_storage) for `t_e9626fc3` / `t_ababb2f7`. Non-blocking for v1.0. Storage architecture: DuckDB + Parquet + SQLite (hybrid). Design doc: `docs/architecture/phase-5-6-historical-warehouse.md`.
