# CHANGELOG

Trader Joe release history.

---

## v0.20.0 — Phase 4: Feature Importance Analysis

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Review
**Card:** `t_phase4_feature_importance`

### Added
- `strategy/feature_importance.py` — per-feature Pearson-r importance
  scoring with Fisher z-transform confidence intervals
- `FeatureObservation` frozen dataclass with feature/outcome
  timestamps and a `has_lookahead()` guard
- `FeatureImportanceScore` frozen dataclass with method,
  sample size, CI, label, `flagged` boolean, and structured flag
  reasons (`low_sample`, `zero_variance`, `ci_spans_zero`)
- `pearson_r(x, y)` and `fisher_z_ci(r, n, confidence_level)`
  helpers (clip to `(-1, 1)` to keep `atanh` finite; return
  `(r, r)` when `n < 4`)
- `filter_lookahead_observations` partitions inputs into kept and
  dropped
- `analyze_feature_importance(observations, feature_names, ...)`:
  Pearson r + Fisher z CI per feature, ranked by `|score|`
  descending with feature-name asc tiebreak, rejects look-ahead by
  default with a `ValueError`
- `importance_stable_hash` order-independent deterministic hash

### Tests
- `tests/test_feature_importance.py` — 52 tests covering:
  - `FeatureObservation` roundtrip and validation errors;
    `has_lookahead` at strictly-before, exact-equal, and after
    timestamps
  - `FeatureImportanceScore` roundtrip; `is_significant` at CI
    boundary conditions; every validation error
  - `pearson_r`: perfect positive/negative correlation, zero
    variance, empty and single-value inputs, length-mismatch error
  - `fisher_z_ci`: symmetric around zero at r=0, widens with higher
    confidence, shrinks with larger samples, bounded at r=±1,
    flat interval for n<4
  - `filter_lookahead_observations` partitions correctly on empty
    and mixed input
  - `analyze_feature_importance`: perfect positive/negative
    correlation returns r=±1 with `LABEL_VALIDATED`; low sample
    flagged `low_sample`; constant feature flagged
    `zero_variance`; uncorrelated data flagged `ci_spans_zero`;
    ranking by `|score|`; missing feature yields zero-sample record;
    look-ahead rejection by default; `reject_lookahead=False` skips
    the guard; duplicate feature names deduplicated; deterministic
    output; confidence-level propagation; unsupported confidence
    rejected; negative floor rejected
  - `importance_stable_hash` deterministic and order-independent
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references; terminology
    check; module never mutates global feature flags; never imports
    `strategy.config`; import-time exclusion of `trader_cli`,
    `trader`, `crypto_trader`, `telegram_approvals`
- 814 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- Follow-up cards remain in Backlog: `t_phase4_weight_recommender`,
  `t_phase4_learning_reports`, `t_phase4_validation`

---

## v0.19.0 — Phase 4: Historical Pattern Discovery

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_pattern_discovery`
**Commit:** `3711453`, plus validation board update

### Validation
- 762 tests passing (0 failing)
- Working tree clean prior to validation commit
- All discovered patterns default to `LABEL_HYPOTHESIS`
- OOS validation only promotes to `LABEL_VALIDATED` when the OOS
  group meets the sample-size floor, the mean-return direction
  matches, and the win-rate direction matches
- OOS insufficient sample surfaces as `LABEL_HYPOTHESIS` with an
  `insufficient OOS sample` detail note
- OOS return-direction mismatch surfaces as `LABEL_HYPOTHESIS` with a
  `return direction mismatch` detail note
- Determinism verified: shuffled observation input yields identical
  discovery output; `hypotheses_stable_hash` order-independent
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/pattern_discovery.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_pattern_discovery` moved to Done
- Card `t_phase4_feature_importance` unblocked and moved to Ready

### Added
- `strategy/pattern_discovery.py` — read-only pattern discovery over
  bucketed market-context and realized-outcome trade observations
- `PatternObservation` frozen dataclass with trade id, entry/exit
  timestamps, context dict, outcome dict, and metadata; rejects
  empty required fields and out-of-order exit timestamps
- `PatternHypothesis` frozen dataclass with sample size, win rate +
  Wilson CI, mean return + normal-approx CI, Cohen's d one-sample
  effect size, `hypothesis` / `validated` label, and sorted
  supporting evidence ids
- `discover_patterns` groups observations by sorted feature-key
  tuples, computes descriptive statistics, and emits hypotheses
  labeled `LABEL_HYPOTHESIS` for each feature combination meeting
  `PATTERN_MIN_SAMPLE_SIZE` (default 10)
- `validate_patterns_out_of_sample` promotes to `LABEL_VALIDATED`
  only when the OOS group meets the sample floor, the mean-return
  direction matches, and the win-rate direction matches; otherwise
  re-emits with `LABEL_HYPOTHESIS` and a detail note explaining the
  gap
- `hypotheses_stable_hash` deterministic order-independent hash

### Tests
- `tests/test_pattern_discovery.py` — 47 tests covering:
  - `PatternObservation`: roundtrip, context/outcome helpers, and all
    validation errors
  - `PatternHypothesis`: roundtrip, `features()` helper,
    `is_significant_return` at CI boundaries, all validation errors
    including mismatched key/value lengths
  - `discover_patterns`: every emitted hypothesis defaults to
    `LABEL_HYPOTHESIS`; per-feature-combination groups produced; win
    rate / mean return / CI math on constant-value fixtures; below-
    floor groups skipped; evidence ids sorted and reference source
    trade ids; deterministic ordering; single-key grouping; empty
    feature-key tuple skipped; observations missing context or
    outcome silently dropped; negative floor and unsupported
    confidence rejected; confidence level propagates for all
    supported levels
  - `validate_patterns_out_of_sample`: promotion to
    `LABEL_VALIDATED` on directional match; stays `LABEL_HYPOTHESIS`
    on return-direction flip; stays `LABEL_HYPOTHESIS` on OOS-sample
    shortfall; win-rate direction mismatch stays `LABEL_HYPOTHESIS`;
    pattern id preserved across validation; negative floor rejected
  - `hypotheses_stable_hash` deterministic and order-independent
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references; terminology
    check; module never mutates global feature flags; never imports
    `strategy.config`; import-time exclusion of `trader_cli`,
    `trader`, `crypto_trader`, `telegram_approvals`
- 762 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- The `validated` label indicates only that the pattern's directional
  effect held on a disjoint observation window; it is **not** a
  promotion signal and does not by itself justify advancing a feature
  past `disabled`
- Follow-up cards remain in Backlog:
  `t_phase4_feature_importance`, `t_phase4_weight_recommender`,
  `t_phase4_learning_reports`, `t_phase4_validation`

---

## v0.18.0 — Phase 4: Statistical Decision-Support Layer

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_stats_engine`
**Commit:** `7005254`, plus validation board update

### Validation
- 715 tests passing (0 failing)
- Working tree clean prior to validation commit
- Math verified against known-input fixtures: `normal_mean_ci` for
  values `[1..5]` (mean 3.0, CI [1.6141, 4.3859] at 95%);
  `wilson_proportion_ci` correctly handles all-failure and all-success
  boundaries; `cohens_d_one_sample` on the same fixture returns
  1.8974 as expected
- Determinism verified: repeat `analyze_comparison` produces
  byte-identical `to_dict` output; `findings_stable_hash` is
  order-independent
- Sample-size floor enforced: below-floor findings labeled
  `hypothesis`; at/above-floor findings labeled `validated`
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/stats_engine.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_stats_engine` moved to Done
- Card `t_phase4_pattern_discovery` unblocked and moved to Ready

### Added
- `strategy/stats_engine.py` — read-only descriptive-statistics layer
  over Phase 3 comparison artifacts
- `StatisticalFinding` frozen dataclass with metric, effect size,
  confidence-interval bounds, sample size, methodology,
  `hypothesis` / `validated` label, confidence level, evidence ids,
  and detail
- Deterministic numeric helpers: `normal_mean_ci` (normal-approx mean
  CI), `wilson_proportion_ci` (Wilson score proportion CI),
  `cohens_d_one_sample`, `cohens_d_two_sample`
- Supported confidence levels: 0.90, 0.95, 0.99
  (`SUPPORTED_CONFIDENCE_LEVELS`)
- Sample-size floor `STATS_SAMPLE_SIZE_FLOOR = 30` gates the
  `validated` label; smaller samples fall through to `hypothesis`
- `analyze_comparison(comparison)` returns findings in fixed order:
  `score_delta_mean → rank_delta_mean → disagreement_rate →
  selection_agreement_rate`
- `analyze_walk_forward(wf_report)` returns aggregate findings
  (`wf_score_delta_mean` + `wf_disagreement_rate`) across all splits'
  OOS comparisons — never mixes in-sample data
- Evidence ids on every finding reference the source `run_id` values
  so downstream reports can trace back to reproducible artifacts
- `findings_stable_hash` produces an order-independent deterministic
  hash across a sequence of findings

### Tests
- `tests/test_stats_engine.py` — 59 tests covering:
  - `normal_mean_ci`: empty input, single-value, known mean/stdev,
    CI widens at higher confidence, rejects unsupported confidence
  - `wilson_proportion_ci`: zero trials, all success (CI < 1), all
    failure (CI > 0), half-success centred near 0.5, boundaries
    clipped to [0, 1], invalid inputs rejected
  - `cohens_d_one_sample` / `cohens_d_two_sample`: known effect sizes,
    zero-variance and empty-sample degenerate cases, symmetry
  - `SUPPORTED_CONFIDENCE_LEVELS` exposes 0.90 / 0.95 / 0.99
  - `StatisticalFinding`: roundtrip, `is_significant` at all CI
    boundary configurations (excludes-zero, spans-zero, touches-zero,
    negative-only), evidence-ids tuple coercion, and every
    constructor validation error
  - `analyze_comparison`: fixed metric ordering; label defaults to
    `hypothesis` for small samples; `validated` label triggered when
    sample meets floor; score delta finding surfaces a positive
    effect and a CI excluding zero; disagreement rate + selection
    agreement rate finding math on a two-symbol fixture; empty
    comparison yields zero-sample findings; evidence ids reference the
    comparison run id; confidence-level propagation; custom
    sample-size floor; negative floor rejected
  - `analyze_walk_forward`: fixed metric ordering; evidence ids
    include the walk-forward report id plus every split's comparison
    run id; score delta sample count matches OOS row count;
    disagreement rate = 1.0 for the fixture; labels default to
    hypothesis below floor; negative floor rejected
  - Determinism: repeat analysis produces identical findings;
    `findings_stable_hash` deterministic and order-independent
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references; terminology
    check; module never mutates global feature flags; import-time
    exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`; source-level assertion that the module does
    not import `strategy.config`
- 715 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- The `validated` label indicates only that the underlying sample
  meets `STATS_SAMPLE_SIZE_FLOOR` — it is **not** a promotion signal
  and does not by itself justify advancing a feature past `disabled`
- Follow-up cards remain in Backlog: `t_phase4_pattern_discovery`,
  `t_phase4_feature_importance`, `t_phase4_weight_recommender`,
  `t_phase4_learning_reports`, `t_phase4_validation`

---

## v0.17.0 — Phase 3: Feature Promotion Gates

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_promotion_gates`
**Commit:** `623b046`, plus validation board update

### Validation
- 656 tests passing (0 failing)
- Working tree clean prior to validation commit
- Default `PromotionEntry` reflects the disabled flag; the promotion
  machinery cannot silently claim more progress than the code state
- `report_id` and `stable_hash` independent of `generated_at`
- Standard rollback criteria trigger under worst-case metrics and pass
  under safe metrics
- Missing metrics surface as warnings rather than looking like passes
- Approved / production states without any `ApprovalRecord` emit a
  warning
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/promotion_gates.py`
- Global feature flags remain `all_disabled` after evaluation
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_promotion_gates` moved to Done
- **Phase 3 is complete — 7 / 7 cards Done**

### Added
- `strategy/promotion_gates.py` — read-only encoding of the Phase 3
  feature-flag promotion process
- Ordered promotion states:
  `disabled → backtest → walk_forward → paper_trading → candidate →
  approved → production`
- `REQUIRED_EVIDENCE_PER_STATE` maps each target state to the required
  evidence keys pulled from the ROADMAP "Feature Flag Promotion Process"
  table; `APPROVAL_EVIDENCE_KEY` is a virtual key satisfied by any
  `ApprovalRecord` on the entry
- `ApprovalRecord` frozen dataclass with mandatory approver, date,
  flag name, scope, monitoring, and rollback-plan fields
- `RollbackCriterion` frozen dataclass with `lt` / `le` / `gt` / `ge`
  / `eq` comparators; missing metrics never look like a pass
- `RollbackAlert` frozen dataclass carries `triggered`,
  `data_available`, and a human-readable detail
- `STANDARD_ROLLBACK_CRITERIA` mirrors the ROADMAP "Failure /
  Rollback Criteria" list (expectancy delta, drawdown delta, profit
  factor delta, trade frequency, concentration, data quality,
  reproducibility)
- `PromotionEntry` mutable dataclass carries the current state (default
  `disabled`), recorded evidence dict, approvals list, and free-form
  notes; validates flag_name and refuses approvals that name a
  different flag
- `evaluate_promotion(entry, metrics, rollback_criteria, generated_at)`
  returns a `PromotionReport` with current state, next state, required
  evidence, missing evidence, approval dicts, per-criterion rollback
  alerts, warnings, and `generated_at`
- `PromotionReport.stable_hash` excludes `generated_at`; `report_id`
  derived from a deterministic hash of flag, current/target state,
  evidence keys, approval count, metric keys, and criterion names
- `PromotionReport.to_markdown` renders `# Promotion Report`,
  `## Current State`, `## Required Evidence for Next Transition`
  (marked ✓/✗), `## Approvals`, `## Rollback Checks` (marked `!`/`·`),
  and an optional `## Warnings` section
- `next_state` / `state_index` / `is_terminal_state` helpers and a
  `triggered_alerts` convenience for filtering serialized alerts

### Tests
- `tests/test_promotion_gates.py` — 57 tests covering:
  - Promotion state ordering; `state_index`; `next_state` progression;
    terminal state; rejection of unknown states
  - `REQUIRED_EVIDENCE_PER_STATE` covers every state; disabled requires
    no evidence; approved and production require the approval-record
    virtual key
  - `ApprovalRecord` roundtrip and validation errors for every required
    field
  - `RollbackCriterion` for every comparator; missing metric → non-
    triggered alert with `data_available=False`; invalid comparator
    rejected at construction; empty name rejected; standard criteria
    inventory present
  - `PromotionEntry` defaults to disabled, rejects unknown states,
    rejects mismatched approval flag names, roundtrips through
    `to_dict`
  - `evaluate_promotion`:
    - Disabled entry lists backtest requirements without emitting a
      missing-evidence warning
    - Backtest entry with all evidence flags no missing keys
    - Candidate entry without approval flags approval_record missing
    - Approved state without any ApprovalRecord emits a warning
    - Terminal (Production) has no next state and no required evidence
    - Standard rollback criteria trigger correctly with worst-case
      metrics
    - Standard rollback criteria pass with safe metrics
    - Missing metrics produce warnings and never look like a pass
    - Custom criteria override the standard set
    - `report_id` is deterministic across `generated_at`
    - `report_id` changes when the evidence set changes
  - `PromotionReport`:
    - `stable_hash` excludes `generated_at`
    - `to_json` schema covers every documented key
    - Markdown contains all required sections and the flag name
    - Missing evidence marked `✗`; satisfied evidence marked `✓`
  - Observational-only:
    - No `alpaca`, `place_order`, `submit_order`, `TradingClient`,
      `api_key`, or `yfinance` references
    - Terminology check: only the one explanatory sentence containing
      `training` (inside quotes) is present
    - `evaluate_promotion` never mutates global feature flags
    - Import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
      `telegram_approvals`
    - Default `PromotionEntry` reflects the disabled flag default so
      the promotion machinery cannot silently claim more progress than
      the code state supports
- 656 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior are untouched
- No feature flags enabled; `strategy/config.py` untouched
- No credentials, env plumbing, or historical validation paper account
  wiring
- Approvals are data artifacts — this module never constructs one
  autonomously; callers build them from repository documentation or
  operations records before passing them in
- Phase 3 backlog is now empty pending Hermes validation of this card

---

## v0.16.0 — Phase 3: Research Report Generation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_reports`
**Commit:** `a2f1940`, plus validation board update

### Validation
- 599 tests passing (0 failing)
- Working tree clean prior to validation commit
- `report_id` and `stable_hash` independent of `generated_at` for both
  comparison and walk-forward reports
- Repeat renders with the same `generated_at` produce byte-identical
  `to_json`, `to_markdown`, `disagreements_json`, and `manifest_json`
- Manifest carries source id + source hash for reproducibility
- `write` persists all four files (`report.md`, `report.json`,
  `disagreements.json`, `manifest.json`), is idempotent, and lands
  under `<output_dir>/<report_id>/`
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/research_reports.py`
- Global feature flags remain `all_disabled` after render + write
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_reports` moved to Done
- Card `t_phase3_promotion_gates` unblocked and moved to Ready

### Added
- `strategy/research_reports.py` — read-only research report renderers
- `ResearchReport` bundle carrying Markdown + JSON payload + flattened
  disagreements + reproducibility manifest
- `ResearchReportPaths` with deterministic file layout:
  `<output_dir>/<report_id>/report.md`,
  `<output_dir>/<report_id>/report.json`,
  `<output_dir>/<report_id>/disagreements.json`,
  `<output_dir>/<report_id>/manifest.json`
- `render_comparison_report` renders any
  `ChampionChallengerComparison`; produces daily-summary rows keyed by
  event timestamp with per-kind disagreement counts, and groups
  disagreements by kind for the Markdown body
- `render_walk_forward_report` renders any `WalkForwardReport`;
  produces per-split rows with IS/OOS windows and per-kind
  disagreement counts, and annotates disagreements with `split_id` /
  `split_index`
- `report_id` derived from the source `stable_hash`, so identical
  source objects always yield the same `report_id`
- `ResearchReport.stable_hash()` excludes `generated_at`; walk-forward
  payload strips nested `generated_at` fields for reproducibility
- `ResearchReport.write(output_dir)` persists all four files via
  `pathlib`; parent directory is created if missing and is idempotent

### Tests
- `tests/test_research_reports.py` — 32 tests covering:
  - `ResearchReport` rejects unknown kinds
  - `KNOWN_REPORT_KINDS` covers comparison and walk-forward
  - `ResearchReportPaths` derives all four artifact paths from
    `output_dir + report_id`
  - Comparison report: `rr_` prefix, custom title, JSON payload
    schema, disagreement counts match records, disagreements sorted
    deterministically, daily summary row per event, manifest carries
    reproducibility metadata, Markdown contains all required sections,
    Markdown flags the no-events case, data-quality notes include
    warnings
  - Walk-forward report: `rr_` prefix, JSON schema, disagreements
    carry split metadata, disagreements sorted by
    (split_index, timestamp, event_type, symbol, kind), Markdown key
    sections, no-splits case flagged, manifest carries source id +
    hashes, payload strips nested `generated_at` at every level
  - Determinism: `stable_hash` and `report_id` independent of
    `generated_at` for both comparison and walk-forward; repeat renders
    produce byte-identical `to_json`, `to_markdown`,
    `disagreements_json`, and `manifest_json`
  - Write: persists all four files; is idempotent; places files under
    `<output_dir>/<report_id>/`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Terminology check: only the one explanatory sentence containing
    `training` (inside quotes) is present
  - Import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`
  - Global feature flags remain `all_disabled` after render + write
- 599 passing total (0 failures)

### Notes
- Renderers are behavior-neutral: production Champion path is
  unchanged; runner, scheduler, Telegram, CLI, and plugin behavior are
  untouched
- No feature flags enabled; `strategy/config.py` untouched
- No `yfinance`, HTTP, or broker credentials touched
- Historical validation paper account remains documentation-only
- Promotion gates (`t_phase3_promotion_gates`) remain in Backlog

---

## v0.15.0 — Phase 3: Walk-Forward Evaluation Pipeline

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_walk_forward`
**Commit:** `01089d3`, plus validation board update

### Validation
- 567 tests passing (0 failing)
- Working tree clean prior to validation commit
- Leakage prevention verified: in-sample events never reach the
  harness (only OOS events populate `ComparisonHarness.run`)
- Split boundaries strictly non-overlapping (`out_of_sample_start >
  in_sample_end`) enforced at construction time
- Determinism verified: `report_id` and `stable_hash` independent of
  `generated_at`, including nested comparison metadata
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/walk_forward.py`
- Global feature flags remain `all_disabled` after pipeline runs
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_walk_forward` moved to Done
- Card `t_phase3_reports` unblocked and moved to Ready

### Added
- `strategy/walk_forward.py` — read-only walk-forward orchestration for
  the Champion/Challenger comparison harness
- `WalkForwardSplit` frozen dataclass with in-sample / out-of-sample
  windows; constructor enforces `out_of_sample_start` strictly after
  `in_sample_end`
- `WalkForwardSchedule` frozen dataclass with deterministic
  `stable_hash` and enforced strictly time-ordered splits
- `generate_walk_forward_schedule` builds splits from calendar
  parameters (start/end date, in-sample days, out-of-sample days,
  step days); returns an empty tuple when the window does not fit
- `WalkForwardSplitResult` captures per-split event counts, dropped
  in-sample count, harness comparison output, and disagreement counts
  by kind
- `WalkForwardReport` aggregates per-split results, totals disagreements
  by kind across splits, and records unassigned events; `stable_hash`
  strips `generated_at` from the report and from every nested
  comparison metadata
- `WalkForwardPipeline` groups events by an OOS-date lookup and
  forwards only OOS events to a fresh `ComparisonHarness` per split;
  in-sample events are counted for reporting but never reach the
  harness
- `report_id` derived from schedule hash + champion/challenger ids +
  dataset id + seed + event count + score-delta threshold
- Uses `in-sample` / `out-of-sample` terminology; never "training"

### Tests
- `tests/test_walk_forward.py` — 35 tests covering:
  - `WalkForwardSplit` roundtrip, `contains_in_sample` /
    `contains_out_of_sample`, and every constructor validation error
    including the strictly-after-IS rule
  - Schedule generator: basic layout, no overlap within a split,
    time-ordered splits, end-date boundary, empty schedule when window
    too small, determinism, and every parameter validation error
  - `WalkForwardSchedule.stable_hash` determinism and sensitivity to
    config changes; rejection of out-of-order splits at construction
  - Pipeline leakage: in-sample events counted as `dropped_event_count`
    and never reach the harness; events outside every window recorded
    as `total_unassigned_events` with a warning
  - Per-split aggregation: `ranking_only` and `score_delta` counts
    partition correctly across splits; totals sum across splits;
    `total_out_of_sample_events` matches OOS events
  - `report_id` stable across `generated_at`; changes with dataset id;
    `stable_hash` independent of `generated_at` at every nesting level
  - Report serialization roundtrips via `to_json`
  - Pipeline construction rejects negative threshold and exposes
    champion/challenger ids
  - RS Challenger integration: disabled overlay yields zero
    disagreements; global feature flags remain `all_disabled`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Terminology check: only the one explanatory sentence containing
    `training` (inside quotes) is present; no other `training` usage
  - Import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`
  - Global feature flags remain `all_disabled` after pipeline runs
- 567 passing total (0 failures)

### Changed
- ROADMAP `t_phase3_walk_forward` scope phrasing updated from
  "train/evaluate splits" to "in-sample / out-of-sample splits" for
  consistency with the terminology rule.

### Notes
- Pipeline is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior are untouched
- No feature flags enabled globally; RS Challenger integration uses a
  locally-scoped `FeatureFlags` instance
- No `yfinance`, HTTP, or broker credentials touched
- Historical validation paper account remains documentation-only
- Reports (`t_phase3_reports`) and promotion gates
  (`t_phase3_promotion_gates`) remain in Backlog

---

## v0.14.0 — Phase 3: Relative Strength Challenger Overlay

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_rs_challenger`
**Commit:** `4569bce`, plus validation board update

### Validation
- 532 tests passing (0 failing)
- Working tree clean prior to validation commit
- Overlay confirmed disabled by default; global `FeatureFlags`
  singleton remains `all_disabled` after harness runs
- Champion parity verified when disabled: identical scores, rankings,
  and explanations; harness records zero disagreements
- Enabled overlay produces the expected additive contribution and
  rerank (verified against
  `weight * (rs - neutral) / range = 0.20 * ±0.4/0.8`)
- Determinism verified across repeat evaluations and across
  `generated_at` differences (harness `stable_hash` stable)
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in `strategy/rs_challenger.py`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_rs_challenger` moved to Done
- Card `t_phase3_walk_forward` unblocked and moved to Ready

### Added
- `strategy/rs_challenger.py` — read-only, disabled-by-default RS
  overlay for Champion/Challenger comparison
- `RelativeStrengthChallenger` wraps any `ComparisonEvaluator` and
  applies an additive score contribution
  `weight * (rs - neutral) / range` when
  `FeatureFlags.enable_relative_strength` is `True`
- `RelativeStrengthProvider` protocol for caller-supplied RS lookups
  keyed by symbol and event; providers return `None` when RS data is
  unavailable
- `rs_provider_from_map` builds a deterministic provider from a
  `{timestamp: {symbol: rs}}` snapshot (defensively copied) suitable for
  the Data Catalog and unit tests
- `RS_CHALLENGER_STRATEGY_ID` / `RS_CHALLENGER_FLAG_NAME` /
  `DEFAULT_RS_OVERLAY_WEIGHT` / `DEFAULT_RS_NEUTRAL_SCORE` /
  `DEFAULT_RS_SCORE_RANGE` constants

### Behavior
- Disabled by default — global `FeatureFlags` singleton is never
  mutated; enablement is done per-instance by passing a local
  `FeatureFlags(enable_relative_strength=True)` to the constructor
- When disabled the wrapper is Champion-parity: scores, rankings, and
  explanations are byte-identical to the base evaluator (only
  `strategy_id` differs); the harness records zero disagreements
- When enabled: scores get an additive contribution, rankings are
  recomputed by new score desc with symbol-asc tie-break, per-symbol
  explanations note the base and RS contribution
- RS values outside `[neutral - range, neutral + range]` are clamped
- Missing RS values keep the base score and add a single aggregated
  warning; provider exceptions are captured per-symbol as warnings
- Symbols with a base score but no base ranking are rescored but stay
  unranked

### Tests
- `tests/test_rs_challenger.py` — 32 tests covering:
  - Constructor rejects negative weight, non-positive range, empty
    `strategy_id`; defaults match module constants; conforms to
    `ComparisonEvaluator` protocol
  - `is_enabled` reflects the flag; global singleton stays disabled
  - Champion parity when disabled: identical scores, rankings, and
    explanations; harness records zero disagreements; result is a deep
    copy of the base evaluation
  - Symmetric contribution formula for both positive and negative RS
    deltas
  - Neutral RS produces zero contribution
  - Out-of-range RS is clamped
  - Custom weight scales contribution linearly
  - Re-rank by new scores desc; symbol-asc tie-break
  - Explanations include base note, RS score, and signed contribution
  - Symbols in base scores but not base rankings stay unranked
  - Harness surfaces `ranking_only` and `score_delta` disagreements
    correctly when enabled
  - Missing RS symbol keeps base score with an aggregated warning
  - Empty snapshot marks every symbol
  - Provider exception recorded as a warning; base score preserved
  - Missing-RS symbol with preserved rank/score triggers no harness
    disagreement; `data_unavailable` remains reserved for one-sided
    symbols
  - `rs_provider_from_map` returns values for known keys, `None` for
    unknown timestamp/symbol, defensively copies its input
  - Repeat runs produce identical evaluations; harness `stable_hash` is
    independent of `generated_at`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Importing `strategy.rs_challenger` does not pull in `trader_cli`,
    `trader`, `crypto_trader`, or `telegram_approvals`
  - Global feature flags remain `all_disabled` after a run
- 532 passing total (0 failures)

### Notes
- Overlay is behavior-neutral: production Champion path is unchanged;
  the runner (`strategy/runner.py`), scheduler, Telegram approval flow,
  CLI, and plugin behavior are untouched
- No feature flags are enabled globally; local `FeatureFlags` instances
  in tests do not mutate the singleton
- RS data source is caller-supplied — no `yfinance` calls, no HTTP,
  no broker credentials, no historical validation paper account wiring
- Walk-forward pipeline (`t_phase3_walk_forward`), reports
  (`t_phase3_reports`), and promotion gates (`t_phase3_promotion_gates`)
  remain in Backlog

---

## v0.13.0 — Phase 3: Champion/Challenger Comparison Harness

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_champion_challenger`
**Commit:** `1b341ef`, plus policy addendum `a0fa765` and validation board update

### Validation
- 500 tests passing (0 failing)
- Working tree clean prior to validation commit
- Harness confirmed read-only and deterministic (verified `stable_hash`
  and `run_id` independent of `generated_at`; repeated runs produce
  byte-identical `to_dict()` output)
- No references to `alpaca`, `place_order`, `submit_order`, or
  `TradingClient` in `strategy/comparison_harness.py`
- No credentials, env vars, SDK calls, or account wiring added
- Historical validation paper account remains documentation-only (no
  code path in the harness reads from it)
- Terminology audit passed: docs refer to validation, replay, or
  research — no "training" usage
- Feature flags remain `all_disabled` after harness runs; runner,
  scheduler, Telegram, CLI, and plugin behavior unchanged
- Card `t_phase3_champion_challenger` moved to Done
- Card `t_phase3_rs_challenger` unblocked and moved to Ready

### Added
- `strategy/comparison_harness.py` — read-only Champion/Challenger comparison
  harness built on top of the Backtest Lab foundation
- `ComparisonEvaluator` runtime protocol satisfied by the existing
  `NoOpStrategyAdapter`
- `ScoreRow` and `ScoreTable` per-event alignment of Champion and
  Challenger scores, ranks, selection status, score deltas, and rank
  deltas
- `DisagreementRecord` classifying divergences as `ranking_only`,
  `entry_selection`, `score_delta`, or `data_unavailable`
- `ChampionChallengerRunMetadata` with a deterministic `run_id` derived
  from champion/challenger ids, dataset id, event signature, seed, and
  threshold
- `ChampionChallengerComparison` container with `to_dict`, `to_json`,
  `stable_hash` (ignores `generated_at`), and
  `disagreements_by_kind()`
- Score-delta threshold controls score-only disagreement sensitivity
- Rows sorted by symbol; disagreements sorted by
  `(event_timestamp, event_type, symbol, kind)` for deterministic output
- `CHAMPION_ROLE` / `CHALLENGER_ROLE` aliases reuse
  `strategy.config.CHAMPION_NAME` / `CHALLENGER_NAME`

### Changed
- `strategy/data_catalog.py` — dropped unused `stable_json` import
  (non-blocking lint noted during validation of `t_phase3_data_catalog`)

### Tests
- `tests/test_comparison_harness.py` — 27 tests covering:
  - `ScoreRow`, `ScoreTable`, and `DisagreementRecord` serialization
  - Rejection of unknown disagreement kinds
  - Role aliases (`CHAMPION_ROLE`, `CHALLENGER_ROLE`)
  - `NoOpStrategyAdapter` satisfies `ComparisonEvaluator`
  - Harness rejects negative thresholds and identical strategy ids
  - No-op run produces empty tables and no disagreements
  - Identical evaluations produce no disagreements
  - Explicit classification tests for `ranking_only`, `entry_selection`,
    `score_delta`, and `data_unavailable`
  - Score-delta threshold suppresses under-threshold differences
  - Disagreements sorted deterministically across multiple events
  - Score-table rows sorted alphabetically by symbol
  - Score tables capture event type, sequence, and per-side warnings
  - Run id derived from inputs; `stable_hash` independent of
    `generated_at`; run id changes with dataset id
  - `to_json` produces byte-identical output across runs (excluding
    `generated_at`)
  - Full result is JSON-serializable
  - `disagreements_by_kind()` partitions records across known kinds
  - Module source has no `alpaca` / `place_order` / `submit_order` /
    `TradingClient` references
  - Feature flags remain `all_disabled` after harness runs
  - Importing the harness module does not pull in `trader_cli`, `trader`,
    `crypto_trader`, or `telegram_approvals`
- 500 passing total (0 failures)

### Notes
- Harness foundation only — no Relative Strength Challenger implementation
- Runner, scheduler, Telegram, CLI, plugin, and order-path behavior
  unchanged
- No feature flags enabled; `strategy/config.py` untouched
- Relative Strength Challenger (`t_phase3_rs_challenger`) and
  Walk-Forward Pipeline (`t_phase3_walk_forward`) remain in Backlog
- A separate Alpaca paper account is documented as a **future**
  validation data source (see ROADMAP "Validation Paper Account"). This
  card does not integrate it — no credentials, SDK calls, env plumbing,
  or runner wiring were added. Isolation rules: must remain separate
  from the live and normal paper accounts, must never be used by the
  live runner, must not share credentials with production, and is
  reserved for historical replay, walk-forward validation, and
  Champion/Challenger comparison only. Referred to as validation,
  replay, or research — never "training."

---

## v0.12.0 — Phase 3: Research Data Catalog

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_data_catalog`
**Commit:** `0410aa2`, plus validation board update

### Validation
- 473 tests passing (0 failing)
- Working tree clean prior to validation commit
- Confirmed catalog is read-only (no file/manifest mutation on validate)
- Confirmed catalog is deterministic (sorted listings, stable manifest hashes)
- Confirmed reproducibility metadata is stable across repeated calls
- Confirmed `stable_hash` is independent of `imported_at`
- Confirmed no references to `alpaca`, `place_order`, `submit_order`, or
  `TradingClient` in `strategy/data_catalog.py`
- Confirmed feature flags remain `all_disabled` after catalog operations
- No trading behavior, buy/sell logic, runner behavior, or feature flags
  changed
- Card `t_phase3_data_catalog` moved to Done
- Card `t_phase3_champion_challenger` unblocked and moved to Ready

### Added
- `strategy/data_catalog.py` — read-only Research Data Catalog
- `DatasetFile`, `DatasetManifest`, and `DatasetValidationResult` dataclasses
  with structural validation and stable JSON serialization
- `DataCatalog.from_directory()` loads immutable manifests from
  `research_data/manifests/` and rejects duplicate dataset ids
- `DataCatalog.list()` / `get()` / `has()` for read-only lookup with kind
  filtering across `historical_bars`, `benchmark`, `paper_log`, and
  `research_context`
- `DataCatalog.validate()` verifies file existence, size, SHA-256, and
  CSV / JSON schema against the manifest and never mutates the dataset
- `DataCatalog.validate_all()` produces one result per registered dataset
- `DataCatalog.checksum_file()` recomputes the on-disk SHA-256 for a
  referenced file
- `DataCatalog.reproducibility_metadata()` returns manifest hash, kind,
  source, imported timestamp, and per-file checksums for run manifests
- `build_dataset_manifest()` operator helper computes checksums offline
- `sha256_file()` streaming hash utility
- Manifest `stable_hash()` ignores `imported_at` for deterministic run ids

### Tests
- `tests/test_data_catalog.py` — 45 tests covering:
  - Dataset kind constants
  - `DatasetFile` roundtrip, tuple coercion, and validation errors
  - `DatasetManifest` roundtrip, deterministic hashing, validation errors,
    and duplicate-file-path rejection
  - `build_dataset_manifest` computes real checksums and rejects missing files
  - `sha256_file` matches `hashlib` for identical bytes
  - Catalog loading (sorted, filtered, missing dir, duplicate id rejection)
  - Validation success, missing file, checksum mismatch, size mismatch,
    CSV schema mismatch, JSON schema success, JSON schema missing key,
    unspecified-schema warning, and `validate_all` result ordering
  - `validate` and `validate_all` do not mutate manifest or data bytes
  - `checksum_file` matches on-disk digest and rejects unregistered paths
  - `reproducibility_metadata` contains manifest hash, kind, source,
    imported timestamp, symbols/benchmarks, and file checksums; is
    deterministic across calls
  - Observational-only guarantees: no `alpaca`, `place_order`, or
    `TradingClient` references, and feature flags remain all disabled
- 473 passing total (0 failures)

### Notes
- Registry is strictly read-only — no dataset files or manifests are
  written by the catalog itself
- Operator helper `build_dataset_manifest` is offline and not called by
  catalog reads
- No live brokerage calls, order placement, buy/sell logic, runner
  behavior, or feature flags changed
- `research_data/` remains untracked in this commit; manifests are
  produced offline before being checked in

---

## v0.11.0 — Phase 3: Backtest Lab Foundation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 968b5f5, plus validation board update

### Added
- `strategy/backtest_lab.py` — Backtest Lab foundation data contracts
- Deterministic `BacktestConfig` with stable hashing and validation
- `BacktestRunMetadata`, `BacktestArtifactPaths`, and `BacktestRunManifest`
  for reproducible run identity and artifact locations
- `BacktestStrategyResult` and `BacktestReport` shells for future
  Champion/Challenger result reporting
- `BacktestEvent`, `DeterministicReplayClock`, `StrategyEvaluation`, and
  `NoOpStrategyAdapter` fixtures for deterministic replay scaffolding
- Stable JSON/hash helpers for deterministic experiment metadata
- Empty report factory for future dry-run backtest workflows

### Tests
- `tests/test_backtest_lab.py` — 27 tests covering:
  - Stable JSON and hash determinism
  - Config serialization and validation errors
  - Run metadata and timestamp-independent run ids
  - Artifact path generation
  - Manifest serialization and stable hashes
  - Deterministic replay event ordering
  - No-op strategy adapter evaluation
  - Strategy result and report serialization
  - Markdown report shell output
  - Observational-only guarantees
- 428 passing total (0 failures)

### Notes
- Foundation only — no historical replay engine yet
- No Relative Strength Challenger implementation
- No live brokerage calls, order placement, buy/sell logic, runner behavior, or feature flags changed
- Card `t_phase3_backtest_lab` validated and moved to Done
- Card `t_phase3_data_catalog` unblocked and moved to Ready

---

## v0.10.0 — Sprint 10: Market Breadth Analysis

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 5bcbf83

### Added
- `strategy/market_breadth.py` — observational market breadth tracker
- Watchlist participation analysis across 20-day and 50-day moving averages
- Advancer, decliner, unchanged, new-high, and new-low counts
- Aggregate breadth score and breadth regime classification
- `BreadthSymbolObservation` and `MarketBreadthReport` structured outputs with JSON-ready serialization
- Market breadth summaries exposed through the read-only research platform
- Disabled-by-default `enable_market_breadth` feature flag inventory

### Tests
- `tests/test_market_breadth.py` — 27 tests covering:
  - Moving average, period return, latest close, percentage, and A/D ratio helpers
  - New high and new low edge cases
  - Breadth score and regime classification
  - Report/result serialization
  - Missing data, invalid close, insufficient history, and provider failures
  - Mocked price provider behavior
  - Observational-only guarantees
- `tests/test_research_platform.py` — 3 new tests plus snapshot coverage updates covering:
  - Market breadth data contract defaults
  - Snapshot JSON compatibility
  - Mocked research platform market breadth handoff
  - Failure fallback behavior
- 401 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, runner behavior, or enabled feature flags changed
- Card `t_5ec6406e` validated and moved to Done
- Phase 2 Market Intelligence implementation completed

---

## v0.9.0 — Sprint 9: Sector Leadership Tracking

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 465faeb

### Added
- `strategy/sector_leadership.py` — observational sector leadership tracker
- Sector ETF leadership ranking against SPY across 5-day, 20-day, and 60-day windows
- `SectorLeadershipResult` and `SectorLeadershipReport` structured outputs with JSON-ready serialization
- Strongest/weakest sector summaries exposed through the read-only research platform
- Disabled-by-default `enable_sector_leadership` feature flag inventory

### Tests
- `tests/test_sector_leadership.py` — 20 tests covering:
  - Period return calculations
  - Leadership scoring and trend classification
  - Report/result serialization
  - Missing sector data and missing benchmark handling
  - Invalid/zero price handling
  - Mocked price provider behavior
  - Observational-only guarantees
- `tests/test_research_platform.py` — 3 new tests plus snapshot coverage updates covering:
  - Sector leadership data contract defaults
  - Snapshot JSON compatibility
  - Mocked research platform sector leadership handoff
  - Failure fallback behavior
- 371 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, runner behavior, or enabled feature flags changed
- Card `t_c0ab3a10` validated and moved to Done
- Card `t_5ec6406e` unblocked and moved to Ready

---

## v0.8.0 — Sprint 8: Enhanced Daily Digest + Trade Metadata

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** c60f807

### Added
- `DigestMetadataSummary` for observational trade metadata aggregation
- Daily Digest metadata sections for symbols, exit reasons, market context,
  active flags, entry scores, custom metadata, and RS snapshots
- Optional `metadata` payloads on `TradeLogger.log_trade_entry()` and
  `TradeLogger.log_trade_exit()`
- SQLite migration for nullable `trade_metadata_entry` and
  `trade_metadata_exit` columns
- JSONL export parsing for RS snapshots and custom metadata payloads
- `rs_data` passthrough in `DailyDigestService.generate_and_deliver()`

### Changed
- Digest stats now preserve the requested report date
- Trade details render both legacy digest keys and TradeLogger row keys

### Tests
- 9 new/updated tests covering:
  - Metadata summary aggregation
  - Malformed metadata handling
  - Digest metadata and RS snapshot rendering
  - TradeLogger metadata storage and export
  - Existing database migration
  - Service RS data passthrough
- 348 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, or feature flags changed
- Card `t_194b8638` validated and moved to Done

---

## v0.7.0 — Sprint 7: Morning Intelligence Agent

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** c1ed907

### Added
- `strategy/morning_intelligence.py` — Observational pre-market briefing agent
- Composes existing market regime, overnight risk, and relative strength data
- `MorningIntelligenceReport` structured output with JSON serialization
- `MorningIntelligenceRenderer` Markdown output for review/delivery workflows
- Provider injection for deterministic tests and future scheduler integration
- Feature flag recording: `enable_morning_intelligence` remains disabled by default

### Tests
- `tests/test_morning_intelligence.py` — 19 tests covering:
  - Report serialization
  - Markdown rendering
  - Mocked provider aggregation
  - Feature flag recording
  - Empty watchlist and provider failure handling
  - Observational-only guarantees
- 339 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No scheduler, Telegram, buy/sell, ranking, or strategy behavior changes
- Card `t_909faeab` validated and moved to Done

---

## v0.6.0 — Sprint 6: Overnight Risk Engine

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest

### Added
- `strategy/overnight_risk.py` — Overnight gap risk analysis engine
- Measures overnight market risk by comparing previous close to
  pre-market/after-hours prices
- Purely observational — produces NO trading signals, places NO trades,
  and does NOT influence strategy scoring
- Per-symbol observations: previous_close, current_price, overnight_gap_pct,
  gap_direction, significant_gap, risk_score, data_quality
- Aggregate report: symbols_analyzed, significant_gaps, avg_risk_score,
  max_gap_up, max_gap_down
- Gap calculation helpers: calculate_gap_pct, determine_gap_direction,
  is_significant_gap, compute_risk_score
- PriceFetcher with yfinance integration and graceful error handling
- Feature flag: `enable_overnight_risk_engine` (disabled by default)
- Runner stubs in `_run_challenger` and `_challenger_sell` (observational only)

### Changed
- Removed dead imports (`pandas`, `OVERNIGHT_RISK_THRESHOLD`,
  `OVERNIGHT_EVAL_START_HOUR`, `OVERNIGHT_EVAL_START_MINUTE`) from
  `strategy/overnight_risk.py`
- Documented threshold design: engine uses `DEFAULT_GAP_THRESHOLD` (2.0 %)
  as the natural unit; config risk-score threshold reserved for Phase 3

### Tests
- `tests/test_overnight_risk.py` — 78 tests covering:
  - OvernightGapObservation and OvernightRiskReport dataclasses
  - Gap calculation helpers (calculate_gap_pct, determine_gap_direction,
    is_significant_gap, compute_risk_score)
  - PriceFetcher (mocked yfinance)
  - OvernightRiskEngine (assess, assess_symbol, get_summary)
  - get_engine convenience function
  - Feature flag integration
  - Observational-only guarantees (no trading methods, no signals)
  - Edge cases (missing data, zero/invalid close, extreme gaps, single price)
- 320 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- Gap threshold default is wired through `strategy.config`
- Timing/risk-score constants defined for future scheduler integration
- Engine can be wired into Phase 3 Decision Engine as a sell signal source

---

## v0.5.0 — Sprint 5: Market Regime Classification

**Date:** 2026-07-02
**Commit:** b785a35
**Branch:** sprint-3/daily-digest

### Added
- `strategy/market_regime.py` — Market regime classification engine
- Classifies market as bullish/bearish/volatile/neutral
- SPY/QQQ analysis: price vs MA, ATR volatility, ADX trend strength, MACD momentum
- 8 signals across 2 benchmarks with weighted scoring
- 43 new tests for indicators, signals, and classification

### Tests
- 151 passing (0 failures)

### Notes
- Observational only — no impact on trading decisions
- Feature flag: `enable_market_regime` (disabled by default)
- Classification uses 20-day and 50-day MAs, 14-day ATR/ADX
- Foundation for Phase 3 Decision Engine regime-based scoring

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
