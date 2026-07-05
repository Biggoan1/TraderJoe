# Philosophy

Trader Joe exists to make the boring, disciplined work of quantitative
paper trading tractable — to a single human operator collaborating with
AI agents. Every choice in the codebase serves one of the principles
below. When a design tension surfaces, these principles are the
tie-breaker.

## Evidence over opinion

A strategy earns capital by producing measurable evidence: bars fed
into a deterministic scorer, evaluated across held-out windows,
compared to the Champion, cross-checked in walk-forward, tracked in
paper trading, and only then presented to a human for possible
promotion. Opinions inform hypotheses; evidence promotes them.

The **Champion vs Challenger** harness
(`strategy/comparison_harness.py`) is the physical embodiment of this
principle. Every candidate must beat the standing Champion on the same
event stream, with the same bars, over the same window, before its
score-side and equity-side metrics become part of the
`ExperimentBundle`.

A hypothesis with a great story but weak numbers loses. A hypothesis
with dull numbers but great numbers wins.

## Reproducibility

Every research artifact — a `BacktestConfig`, an `ExperimentManifest`,
a `SweepManifest`, a `PortfolioSimulationResult` — carries a
`stable_hash`. Two runs of the same code against the same bars produce
the same hash. Two runs against different bars produce different
hashes, deliberately.

The Portfolio Simulator's `run_id` is `psim_` + the first twelve
characters of a SHA-256 over the strategy identity, symbols, window,
and event count. A colleague can re-run a six-month simulation from a
year ago and expect to see the same trades. If they don't, the tests
fail loudly, not silently.

`stable_parameter_hash`, `stable_hash`, `stable_json` and the
`_canonicalize` walker (`strategy/backtest_lab.py:26-46`,
`strategy/lab/strategy.py:37-52`) are the load-bearing primitives.

## Research-first

Before a single order — even a paper one — is sent, a human reviews
either a simulation report, a walk-forward report, or a paper daemon
plan. The system generates the plan; the human decides to send it. The
`traderjoe-simulate` and `traderjoe-paper-execute` commands ship as
separate CLIs on purpose: simulation is a routine operation; execution
requires a second, explicit action.

The `research-analyst` operator persona
(`.traderjoe/skills/research-analyst/SKILL.md`) is the only role
allowed to import `strategy.research_account`. Every other role — even
the `portfolio-manager` — reads from the warehouse or the paper
account, never from the Research paper account.

## Warehouse-first

`strategy/warehouse/research_cache.py` implements a three-tier
priority chain:

1. **Local warehouse** — if the requested `(symbol, interval, window)`
   is fully covered by a validated dataset, return it. No provider
   call is made. This is provable — `test_research_cache.py` asserts
   a stub `ResearchAccountClient.fetch_bars` is *not* called when
   coverage is complete.
2. **Provider plugins** — the caller-supplied ordered tuple.
3. **Manual imports** — CSV / Parquet importers at the tail of the
   chain.

`scripts/traderjoe-simulate`, `scripts/traderjoe-research`,
`scripts/research-validate-offline`, and every daemon default to
warehouse-only. The `--allow-provider-fallback` flag exists but must
be typed explicitly.

The rationale is stark: providers vanish. Vendors change schema. APIs
rate-limit. Once a dataset is validated and pinned, it must never
change out from under a research run. The warehouse guarantees that;
providers cannot.

## No hype

Test count, feature count, and "AI-powered" claims are not evidence.
The `CHANGELOG.md` records what shipped and which commit shipped it.
The `STATUS.md` records the current test count. The `ROADMAP.md`
records what remains.

The Research Analyst persona is required to refuse to promote a
strategy on the basis of aesthetic appeal, an interesting-looking
chart, or a single-window backtest. If the numbers say don't promote,
don't promote — even if the strategy is elegant.

The [`research-history.md`](research-history.md) document records
several such refusals verbatim (the RS weight sweep post-B02 being
the clearest: *"at every weight from 0.20 to 1.00, the RS-overlay
Champion+RS challenger produced worse realized returns than Champion
baseline… Do not promote."*).

## The human remains in control

There is no autonomous production trading path. There is no
autonomous approval path. Every strategy state transition
(`STATE_DISABLED → STATE_BACKTEST → STATE_WALK_FORWARD →
STATE_PAPER_TRADING → STATE_CANDIDATE → STATE_APPROVED →
STATE_PRODUCTION`) requires evidence — and, at the `_APPROVED` and
`_PRODUCTION` transitions, a human-authored `ApprovalRecord` with
approver, timestamp, flag name, scope, monitoring dashboard, and
rollback plan.

Even the paper daemons that ship in this milestone default to plan-
only. Auto-execute requires:

1. The env flag (`PAPER_AUTO_EXECUTE_EQUITIES` or
   `PAPER_AUTO_EXECUTE_CRYPTO`) set to `true` in the paper env file.
2. The `--execute-paper-orders` flag on the CLI.
3. No emergency stop file at `/etc/traderjoe/STOP_EQUITY_PAPER` or
   `/etc/traderjoe/STOP_CRYPTO_PAPER`.

Any one of those missing keeps the daemon in dry-run.

## Promotion requires evidence

`strategy/promotion_gates.py` encodes the promotion process as a
read-only state machine. Every promotion state declares the artifacts
it requires:

- `STATE_BACKTEST` — needs `experiment_manifest`, `dataset_id`.
- `STATE_WALK_FORWARD` — needs `backtest_report_id`.
- `STATE_PAPER_TRADING` — needs `walk_forward_report_id`,
  `rollback_plan`.
- `STATE_CANDIDATE` — needs `paper_trading_report_id`,
  `disagreement_summary`.
- `STATE_APPROVED` — needs `approval_record`.
- `STATE_PRODUCTION` — needs `monitoring_dashboard`, `rollback_owner`,
  `approval_record`.

The `STANDARD_ROLLBACK_CRITERIA` (`promotion_gates.py:274-324`) list
seven criteria a live strategy must not violate — worse expectancy,
worse drawdown, worse profit factor, too few trades, over-
concentration, degraded data quality, or a failed reproducibility
check. Every criterion has a numeric threshold and a comparator. No
rhetorical wiggle room.

## Paper before production

Paper trading is not a rehearsal for production — it is the current
operating environment. The system will remain in paper trading for
its full research lifetime unless and until an explicit v1.0
production readiness ApprovalRecord is written (`ROADMAP.md`
"Phase 6 — Production Readiness" section).

`docs/agent/env-isolation.md` codifies the rule: `.env.production`
must remain empty. No module in the tree reads it. Any future code
that would read it must ship in the same commit as a valid
`ApprovalRecord` naming approver, scope, monitoring dashboard, and
rollback plan — and reviewers of that PR must cite the ApprovalRecord
ID in the PR body.

## Deterministic research

The Backtest Lab is designed as a pure function of its inputs. The
`DeterministicReplayClock` (`strategy/backtest_lab.py:223`) orders
events by `(timestamp, sequence, event_type)`. Strategy adapters
never read env vars, never open sockets, never write outside their
returned data. `parameter_sweep.run_parameter_sweep` enumerates the
Cartesian product in a deterministic order and isolates per-cell
failures so one bad combo doesn't corrupt the sweep manifest.

When a research run is not deterministic — say, an LLM analyst adds
narrative in `strategy/research_analyst.py` — that non-determinism is
quarantined to a single field (`llm_narrative`) marked as such, so
the surrounding manifest and hashes stay reproducible.

---

These principles are not aspirational. They are enforced by the test
suite, by the promotion gate, by the CLI ergonomics, and by the
review process. If a proposed change compromises one of them, the
change stops.
