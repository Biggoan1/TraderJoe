# Trader Joe Documentation Suite

*Milestone: `v0.40.0-paper-research`*

Trader Joe is an evidence-driven research and paper-trading system built
on top of the Alpaca paper API. This directory is the canonical
reference for humans and future AI agents working on the codebase.

Every fact in these pages is grounded in the current repository state at
the milestone tag; no invented content, no aspirational features.

## Mission

Give a human operator (and the AI agents that assist them) a
paper-trading system that is:

- **Evidence-based** — every decision traces back to a deterministic
  research artifact.
- **Reproducible** — same inputs always produce the same outputs.
- **Warehouse-first** — historical bars live under version control in
  the local Parquet warehouse; providers are the fallback, not the
  source of truth.
- **Paper-only** — no code path submits real-money orders. The `PAPER
  = True` invariant in `trader.py` and `crypto_trader.py` is enforced
  by tests and by the promotion gate.
- **Isolated** — research, equity paper, crypto paper, and (future)
  production each run under their own credentials.
- **Human-in-the-loop** — no strategy auto-promotes. Every state
  transition requires an `ApprovalRecord`.

## Project status

- Champion strategy `champion-v0.4.0` — Research-only six-gate scorer.
- Challenger `rs-challenger-v0.1.0` — Ships disabled behind the
  `enable_relative_strength` feature flag.
- Portfolio Simulator, Paper Bridge, Equity Paper Daemon, Crypto Paper
  Daemon — Complete. Dry-run by default. Auto-execute doubly-gated.
- Historical Warehouse (Phase 5.6) — Complete for equities daily bars.
  Intraday, crypto, and options datasets remain empty on disk.
- Strategy Laboratory — Ten registered strategies, three preset
  experiment bundles, weekend-lab pipeline, dashboard backend, natural-
  language planner, hypothesis queue.
- Test suite — **2,361 tests passing** at the milestone commit.

Every feature flag defaults to `False`. Every paper daemon defaults to
dry-run. No `.env.production` is present or read.

## Directory guide

| Document | Contents |
|---|---|
| [`philosophy.md`](philosophy.md) | Why Trader Joe exists — evidence-over-opinion, reproducibility, human-in-the-loop, promotion-requires-evidence. |
| [`architecture.md`](architecture.md) | Every subsystem and how data flows from acquisition to (future) production. Includes Mermaid diagrams. |
| [`safety-model.md`](safety-model.md) | The full safety fence: feature flags, promotion gates, `ApprovalRecord`, `PromotionEntry`, emergency stop files, credential isolation. Explains *why* each safeguard exists. |
| [`warehouse.md`](warehouse.md) | Historical warehouse: Parquet layout, catalog, validation, versioning, DuckDB, gap detection, research cache, dataset provenance, import pipeline, incremental sync, operator bootstrap. |
| [`strategy-lab.md`](strategy-lab.md) | Strategy interface, registry, experiment runner, parameter sweeps, leaderboard, weekend lab, hypothesis queue, natural-language planner, dashboard backend, performance metrics, regime tagger, multi-year matrix, presets. |
| [`strategy-identities.md`](strategy-identities.md) | One section per strategy — Champion, Champion RS, Momentum, Momentum 15m, Opening Range Breakout, Trend, Mean Reversion, RSI Mean Reversion, Sector Rotation, Volatility Regime Filter. |
| [`research-history.md`](research-history.md) | Evolution of the project, phase-by-phase, with commit hashes and discoveries (B02 investigation, RS weight sweep, warehouse bootstrap). |
| [`operator-guide.md`](operator-guide.md) | The six operator personas (`.traderjoe/skills/`) — morning, evening, research analyst, portfolio manager, risk manager, crypto — and how they coordinate. |
| [`morning-routine.md`](morning-routine.md) | Pre-market workflow: market prep, research, paper plans, reports, approvals, safety checks. |
| [`evening-routine.md`](evening-routine.md) | End-of-day workflow: review, research, learning, planning, reports. |
| [`paper-trading.md`](paper-trading.md) | Portfolio Simulator, Paper Bridge, Equity Daemon, Crypto Daemon: dry-run vs execute, safety gates, logs, reports, state files, expected workflow. |
| [`roadmap.md`](roadmap.md) | Completed phases, current capabilities, future ideas, known limitations, open research questions, production-readiness roadmap. |

## Current architecture (30-second summary)

```
Research Alpaca API  ─┐
Providers (fallback) ─┼──►  Historical Warehouse (Parquet + DuckDB + Catalog)
Manual imports       ─┘                     │
                                            ▼
                                   Strategy Laboratory
                       (Registry ▸ Experiments ▸ Sweeps ▸ Leaderboard)
                                            │
                                            ▼
                                    Portfolio Simulator
                                    (research-only, deterministic)
                                            │
                                            ▼
                                    Paper Trading Bridge
                              (dry-run default, --execute gated)
                                            │
                                            ▼
                            Equity + Crypto Paper Daemons
                       (market-hours aware, doubly-gated auto-execute)
                                            │
                                            ▼
                             Future v1.0 Production Path
                              (empty; requires ApprovalRecord)
```

See [`architecture.md`](architecture.md) for the full diagram and
per-subsystem detail.

## For contributors

- **Never flip `PAPER = False`** in `trader.py` or `crypto_trader.py`
  without meeting every prerequisite in
  [`safety-model.md`](safety-model.md).
- **Never modify** `trader.py`, `crypto_trader.py`, or
  `strategy/runner.py` from a research card.
- **Every research module** must be importable without touching
  `.env.paper`, `.env.crypto`, or `.env.production`.
- **Follow the KANBAN work loop** — see `KANBAN.md` at the repo root.
- **When adding a strategy plug-in**, land it under `strategy/lab/`
  and let the registry auto-discover it — no registration file to
  edit.

Trader Joe's authoritative repo docs live at:

- `AGENTS.md` — quick-start entry point for agents.
- `ROADMAP.md` — the full phase/sprint plan.
- `STATUS.md` — current version and test count.
- `KANBAN.md` — the work loop.
- `CHANGELOG.md` — release-by-release history.
- `docs/agent/` — operational reference for the live paths.
- `docs/architecture/` — design docs for major features.
- `.traderjoe/skills/` — operator playbooks.
