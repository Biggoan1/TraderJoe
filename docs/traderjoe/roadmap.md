# Roadmap

The forward view at the milestone tag `v0.40.0-paper-research`.

For phase-by-phase history, see [`research-history.md`](research-history.md).
For the authoritative sprint plan, see the top-level
[`ROADMAP.md`](../../ROADMAP.md) — this document is a summary keyed to
the current state.

## Completed phases

- **Phase 1 — Foundation.** Paper-trading runner, Elliott Wave
  crypto, Telegram approval flow. Tag `v0.4.0`.
- **Phase 2 — Market Intelligence.** Regime, overnight risk, sector
  leadership, relative strength, breadth, morning intelligence. Tag
  `v0.10.0-phase2`.
- **Phase 3 — Decision Engine.** Backtest Lab, DataCatalog,
  Comparison Harness, RS Challenger, Walk-Forward, Research Reports,
  Promotion Gates. Tag `v0.17.0-phase3`.
- **Phase 4 — Learning System.** Statistical decision support,
  pattern discovery, feature importance, weight recommender,
  learning reports, end-to-end validation.
- **Phase 5 — Research Execution.** Isolated Research Alpaca client,
  Local LLM Research Assistant, two-month historical validation
  orchestrator. Tag `v0.26.0`.
- **Phase 5.6 — Historical Data Warehouse.** Twelve implementation
  cards + bridge card. Warehouse-first is provable. Tag
  `v0.30.0-phase5.6`.
- **Strategy Laboratory sprint (2026-07).** Ten cards, ten
  registered strategies, weekend lab pipeline, dashboard backend,
  natural-language planner, hypothesis queue, multi-year matrix,
  regime tagger.
- **Handoff sprint (Tasks 1-5).** Analyst payload compaction,
  NL planner, operator entry point, docs handoff.
- **Milestone sprint (`sprint-3/daily-digest`).**
  - Portfolio Simulator, Paper Bridge.
  - Five new intraday/daily strategy plug-ins.
  - Equity + Crypto paper daemons.
  - Presets, market calendar, interval requirements.
  - Systemd examples.
  - Operator skills.
  - Documentation suite (this directory).
  - Tag `v0.40.0-paper-research`.

## Current capabilities

At the milestone tag:

- **10 registered strategies** — Champion, Champion RS, Momentum,
  Momentum 15m, Opening Range Breakout, Trend, Mean Reversion, RSI
  Mean Reversion, Sector Rotation, Volatility Regime Filter.
- **3 experiment presets** — daily swing, intraday 15m, crypto 24×7.
- **Warehouse-first data access** with dataset provenance recorded on
  every research artifact.
- **Deterministic portfolio simulation** producing blotter, equity
  curve, Sharpe/Sortino/Calmar/MaxDrawdown, proposed-orders plan.
- **Paper Bridge** with symbol allowlist, per-order + aggregate
  notional caps, `.env.production` refusal, endpoint validation,
  UUID stringification.
- **Equity paper daemon** with market-hours awareness, cooldowns,
  duplicate protection, doubly-gated auto-execute.
- **Crypto paper daemon** 24/7 with the same cap/cooldown/duplicate
  regime, plus a bar loader against the paper crypto data endpoint.
- **Six operator skill playbooks** under `.traderjoe/skills/`.
- **Systemd examples** — installable plan-only for equities;
  uninstallable auto-execute templates for equity + crypto.
- **2,361-test suite** covering every research module, every daemon
  safety refusal, every strategy interval assertion.

## Future ideas

### Data

- **15-minute equity warehouse data.** `market_data/equities/minute/`
  is empty. Once the operator populates it via
  `research-import-watchlist --interval 15Min`, the intraday
  strategies (Momentum 15m, Opening Range Breakout) become
  exercisable.
- **Crypto historical warehouse.** `market_data/crypto/` is empty.
  Landing a crypto import path (`research-import-watchlist
  --asset-class crypto`) would let the crypto strategies replay
  offline.
- **Options warehouse.** `market_data/options/` is empty. Future
  design work required — Phase 5.6 doc lists options / futures as an
  "at 20+ years" architectural target.
- **Multi-provider ingestion.** The provider plugin interface
  supports Alpaca, CSV, Parquet today. Adding Polygon or Databento
  is a plugin card — no core changes required.

### Strategies

- **Volatility-normalized momentum.** `momentum / vol` variants —
  compare against `volatility_regime_filter_v1`.
- **Ensemble scorer.** Weighted combination of Champion + Trend +
  RSI Mean Reversion using historical Sharpe as weights. Test in a
  new `EnsembleScorer` strategy.
- **Sector-aware Champion.** Add a per-sector `min_gate_count`
  override so defensive sectors need fewer gates than growth
  sectors.
- **Options-flow signal.** Depends on options warehouse landing.
- **Crypto-specific momentum.** BTC/ETH/SOL momentum with volume
  filters. Crypto-24x7 preset is the current placeholder.

### Lab / operator

- **Frontend for the dashboard backend.** The JSON API
  (`strategy/lab/dashboard_backend.py`) is complete and stable but
  has no HTML client yet.
- **Automatic weekend-lab scheduling.** A systemd timer that fires
  `weekend-lab` every Saturday morning, publishing the dashboard to
  a local directory.
- **Hypothesis queue → sweep automation.** When a proposal moves to
  `STATUS_APPROVED`, automatically enqueue a parameter sweep with
  the suggested parameters. The Research Analyst reviews the
  results.
- **Comparison heatmap.** Regime × strategy heatmap over the
  multi-year matrix — visual summary of which strategy works in
  which regime.

### Safety / operations

- **`ApprovalRecord` JSON schema.** Formalize the record structure
  in `strategy/promotion_gates.py` with a JSON Schema. Enable
  linting in CI.
- **PromotionEntry ledger.** JSONL-backed ledger of every
  `PromotionEntry` state change (with the ApprovalRecord id) so the
  Research Analyst can audit history.
- **Rollback drill script.** A `scripts/rollback-drill` that
  simulates the "flip PAPER back to True" flow and records the
  drill outcome in `research_notes/`. Referenced in
  `docs/agent/env-isolation.md` but not yet implemented.
- **Bridge log rotation.** `reports/paper_bridge/` and
  `reports/paper_daemon/**/` grow unbounded. Add a rotation policy.

### Research infrastructure

- **Provenance across derived artifacts.** Every simulation report
  cites its warehouse dataset. Every leaderboard entry cites its
  bundle. But cross-artifact provenance (which leaderboard came
  from which weekend-lab run) is not indexed. A `provenance`
  endpoint on the dashboard backend would fix that.
- **Reproducibility CI.** A CI job that runs a canned simulation
  and asserts the `run_id` matches a known value. Guards against
  accidental determinism regressions in dependencies.
- **Warehouse gap-fill runner.** A `scripts/warehouse-fill-gaps`
  that runs `detect_gaps` against every validated manifest, files
  gaps as a report, and enqueues `sync_dataset` calls for missing
  windows.

### Documentation

- **CLAUDE.md.** A dedicated file summarising the project for
  future Claude Code sessions. Currently the primary entry point is
  `AGENTS.md`, which is generic.
- **Strategy authoring tutorial.** A step-by-step walk-through of
  adding a new strategy, from `strategy/lab/<slug>.py` to the
  registry to the leaderboard.
- **Failure-mode catalogue.** A summary of past incidents (B02
  investigation, UUID serialization bug) as a reference for future
  debugging.
- **Video walkthrough.** Screencast of a full weekend-lab pipeline
  invocation with commentary. Nice-to-have.

## Known limitations

At the milestone tag:

- **No live-money code path.** By design. Any live-money support
  requires Phase 6.
- **No intraday equity warehouse data.** `market_data/equities/minute/`
  is empty. Momentum 15m and Opening Range Breakout will fail to
  fetch bars until a 15-minute dataset lands.
- **No crypto warehouse data.** `market_data/crypto/` is empty. The
  crypto research preset relies on live paper data endpoint queries.
- **No production-model wiring.** `PRODUCTION_AI_MODEL` env var
  exists but no code consumes it.
- **No frontend for dashboard backend.** JSON API is stable; no HTML
  client.
- **No systemd timer for the weekend lab.** Manual invocation only.
- **No CI gate against `PAPER = False`.** The test suite asserts
  `PAPER = True` on `trader.py` and `crypto_trader.py`, but this
  check runs only when tests run.
- **`.env.production` is not password-protected.** Empty by design;
  relies on the operator not populating it.

## Open research questions

Documented in the various research summaries:

1. **Is RS ever additive on the current watchlist?** Every weight
   tested (`rs-weight-sweep-post-b02-2026-07-04.md`) says no. But
   the current watchlist is five mega-caps; RS may be additive on a
   broader universe or with sector-neutral construction.
2. **What does regime-tagged Champion look like?** Champion at
   `min_gate_count=3` vs `4` vs `5` per regime. Weekend lab does
   not sweep this by default.
3. **Does opening-range breakout survive out-of-sample?** Not yet
   testable without warehouse data. But the strategy's parameter
   space is small; expected to sweep well once data lands.
4. **What is the correlation between the daily strategies?** A
   parameter sweep by strategy across the multi-year matrix would
   inform ensemble weight priors.
5. **How does volatility_regime_filter perform in COVID_crash?** The
   filter should suppress the signal in high-vol regimes — but the
   backtest hasn't run.
6. **Should the paper daemons share state?** Both daemons track
   per-day state independently. If they should coordinate (e.g. a
   $10k daily notional cap across equity + crypto), the state model
   needs to be lifted to a shared store.

## Production readiness roadmap

The path to v1.0 production trading, from
[`ROADMAP.md`](../../ROADMAP.md) §"v1.0 Production Readiness"
(lines 1670-1774):

### Prerequisites (all must hold)

1. Every candidate flag has cleared its promotion gates.
2. Every candidate flag has an `ApprovalRecord` with approver,
   scope, monitoring dashboard, rollback plan.
3. Two-month historical validation (v0.26.0's
   `t_phase5_two_month_validation_run`) has passed on every
   candidate flag.
4. Walk-forward evidence available with `WalkForwardReport.pass=True`
   on every candidate.
5. Paper trading evidence — the equity paper daemon has run in
   auto-execute mode for at least four consecutive weeks without a
   `STANDARD_ROLLBACK_CRITERIA` alert.
6. Rollback drill executed and documented in `research_notes/`
   within the last 30 days.
7. `.env.production` populated in the same commit that flips `PAPER
   = False`.
8. Reviewers of the PR flipping `PAPER` cite the ApprovalRecord id
   in the PR body.

### Per-flag promotion criteria

From `strategy/promotion_gates.py::PROMOTION_STATES` chain:

- `STATE_BACKTEST` → provide `experiment_manifest`, `dataset_id`.
- `STATE_WALK_FORWARD` → provide `backtest_report_id`.
- `STATE_PAPER_TRADING` → provide `walk_forward_report_id`,
  `rollback_plan`.
- `STATE_CANDIDATE` → provide `paper_trading_report_id`,
  `disagreement_summary`.
- `STATE_APPROVED` → provide `approval_record`.
- `STATE_PRODUCTION` → provide `monitoring_dashboard`,
  `rollback_owner`, `approval_record`.

### Rollback triggers (v1.0)

Any single alert from `STANDARD_ROLLBACK_CRITERIA` is disqualifying.
The seven criteria are enumerated in
[`safety-model.md`](safety-model.md) §Layer 4.

### Exit criteria for the v1.0 tag

`ROADMAP.md`:
- ≥ 4 weeks of paper-trading evidence.
- ≥ 8 weeks of walk-forward evidence.
- ≥ 8 weeks of two-month historical validation evidence.
- Zero triggered rollback alerts across the evidence windows.
- Monitoring dashboard live (Grafana or equivalent).
- Rollback drill documented, dated, and repeated within 30 days.
- Every current feature flag either promoted with an
  ApprovalRecord or explicitly deferred.

### What v1.0 does NOT include

- No auto-promotion. Every promotion still requires an
  ApprovalRecord.
- No auto-flag-flipping. Every flag change still requires an
  operator commit.
- No production trading without human review of each week's
  rollback dashboard.

The safety fence is designed to remain in place after v1.0. The
transition to production tightens the fence; it does not remove it.
