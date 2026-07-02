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

Run `hermes kanban list` for live state.
