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
| `t_c0ab3a10` | Sprint 9: Sector Leadership Tracking | Phase 2 | Ready |
| `t_5ec6406e` | Sprint 10: Market Breadth Analysis | Phase 2 | Blocked (-> S9) |
