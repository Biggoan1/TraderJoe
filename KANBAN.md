# Trader Joe — Kanban Board

> Board state is managed via Hermes Kanban (`hermes kanban`). This file documents the workflow.

## Columns

| Column | Kanban State | Description |
|---|---|---|
| Backlog | `todo` | Planned, gated by dependencies |
| Ready | `ready` | Available to claim |
| In Progress | `in_progress` | Actively being worked on |
| Review | `blocked` | Work done, awaiting Hermes validation |
| Done | `completed` | Validated and merged |

## Definition of Done

Every card must satisfy ALL of the following before moving to **Review**:

- [ ] Code implemented
- [ ] Tests added or updated
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Feature flags correct (disabled unless approved)
- [ ] Validation passed
- [ ] Focused commit created
- [ ] No unrelated changes

## Work Loop

1. Sync repository (`git status`, `git log`)
2. Sync board (`hermes kanban list`)
3. If a card is already **In Progress**, resume it
4. Otherwise, pick the highest-priority **Ready** card
5. Complete only that card
6. Run tests
7. Validate
8. Commit
9. Update docs (ROADMAP, CHANGELOG, STATUS)
10. Move card to **Review**
11. Stop and report — do NOT automatically pull another card

## Roles

- **Trader Joe**: Executes Trader Joe-specific implementation work
- **Hermes (Argus)**: Owns orchestration, board state, validation, and reporting

## Board State

Hermes is not orchestrating this work item. Current repository snapshot:

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
| `t_phase4_plan` | Phase 4: Learning System Technical Design | Phase 4 | Done |
| `t_phase4_stats_engine` | Phase 4: Statistical Decision-Support Layer | Phase 4 | Done |
| `t_phase4_pattern_discovery` | Phase 4: Historical Pattern Discovery | Phase 4 | Done |
| `t_phase4_feature_importance` | Phase 4: Feature Importance Analysis | Phase 4 | Done |
| `t_phase4_weight_recommender` | Phase 4: Strategy Weight Recommender | Phase 4 | Done |
| `t_phase4_learning_reports` | Phase 4: Learning Report Generation | Phase 4 | Done |
| `t_phase4_validation` | Phase 4: End-to-End Learning System Validation | Phase 4 | Done |
| `t_phase5_plan` | Phase 5: Research Execution Technical Design | Phase 5 | Done |
| `t_phase5_research_account_api` | Phase 5: Isolated Research Alpaca Client | Phase 5 | Done |
| `t_phase5_local_llm_research_assistant` | Phase 5: Local LLM Research Assistant | Phase 5 | Done |
| `t_phase5_two_month_validation_run` | Phase 5: Two-Month Historical Validation | Phase 5 | Done |
| `t_phase55_dashboard_plan` | Phase 5.5: Research & Learning Dashboard — Technical Design | Phase 5.5 | Backlog |
