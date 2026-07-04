# Trader Joe — Research Mode

**Audience:** the operator (you).
**Purpose:** to run research end-to-end without a Claude Code session in between.

Trader Joe now understands three research patterns and provides one operator command (`scripts/traderjoe-research`) to plan, run, and summarise experiments against the local warehouse. This document is the handoff: everything you need to know to talk to Trader Joe directly.

The design is intentionally boring. There is no free-form AI in the operator loop; the planner is deterministic regex, the runners are the same warehouse-backed pipelines that have been landing for the last few phases, and the summariser is a manifest reader. The LLM only shows up inside the `--with-llm` path where it writes narratives against compact payloads.

---

## The core idea

You have three verbs:

```
traderjoe-research plan "<question>"     # deterministic parse -> plan JSON
traderjoe-research run <plan-file>       # execute the plan
traderjoe-research summarize <run-id>    # read the manifest of a completed run
```

Everything else is optional. The warehouse is populated (five datasets, 80k daily SIP bars from 2016-01-04). The matrix / regime tagger / performance metrics / hypothesis queue are already wired. You point Trader Joe at a question, it produces a plan you can eyeball, and you decide whether to run it.

---

## Talking to Trader Joe

### 1. Planning an experiment

Every session starts with a question in plain English. The planner is regex-based, so it will only match phrases it recognises.

**Recognised patterns:**

| Pattern | Example |
|---|---|
| RS weight sweep | `"Sweep RS weight from 0.2 to 1.0"` |
| RS weight sweep (explicit list) | `"RS weight sweep 0.30, 0.50, 0.75, 1.00"` |
| Regime analysis (volatility) | `"Test whether RS works better in high-vol regimes"` |
| Regime analysis (credit) | `"Slice RS results by credit stress"` |
| Regime analysis (bond) | `"Regime-tag by TLT trend"` |
| Strategy comparison | `"Compare Champion vs Momentum over 2020-2026"` |
| Strategy comparison (all years) | `"Champion vs Trend across all years"` |

**Producing a plan:**

```bash
./scripts/traderjoe-research plan "Sweep RS weight from 0.2 to 1.0"
```

Output:

```
plan written: reports/research_plans/plan-2026-07-04T....json
  kind: rs_weight_sweep
  steps: 7
```

The plan file is a small JSON blob describing what will be run:

```json
{
  "kind": "rs_weight_sweep",
  "question": "Sweep RS weight from 0.2 to 1.0",
  "parameters": {
    "strategy": "rs-challenger-v0.1.0",
    "overlay_weights": [0.2, 0.4, 0.6, 0.8, 1.0],
    "window": {"label": "1y", "start": "2025-07-04", "end": "2026-07-04"},
    "symbols": ["AAPL", "MSFT", "NVDA"],
    "benchmarks": ["SPY", "QQQ"],
    "dataset_id_prefix": "planned-rs-sweep"
  },
  "steps": [...],
  "reasons": [...],
  "warnings": [],
  "generated_at": "..."
}
```

**If the planner doesn't recognise your question**, it emits an `unrecognised` plan with a warning listing the supported patterns and returns exit code 3. That's the signal to rephrase — not to run.

**Determinism.** The same question always produces the same plan (only `generated_at` changes). Copy-paste the question exactly and you get the same JSON.

### 2. Running a plan

```bash
./scripts/traderjoe-research run reports/research_plans/plan-....json
```

For every plan kind the runner:

1. Reads the plan JSON.
2. Refuses non-runnable kinds (unrecognised, unknown).
3. Creates a run directory under `reports/research_runs/<kind>-<timestamp>/`.
4. Dispatches to the appropriate existing runner.
5. Writes a `manifest.json` describing what was executed.

**Warehouse-only by default.** The runner refuses to fetch anything from Alpaca unless you pass `--allow-provider-fallback`. Even then, missing `RESEARCH_ALPACA_*` env vars cause an immediate abort — no accidental provider calls.

**Dry-run.** `--dry-run` prints the resolved plan and does nothing else. Use it before every unfamiliar plan.

**Per-kind dispatch behaviour:**

- `rs_weight_sweep` — documented delegation. The runner prints the exact operator command needed to reproduce the sweep and cites `reports/research_summaries/rs-weight-sweep-post-b02-2026-07-04.md`, the canonical write-up. Sweeps are heavy enough that we deliberately don't auto-run them; you inspect the plan and invoke the underlying loop yourself.
- `regime_analysis` — actually invokes `strategy.lab.regime_report.main` and captures the output JSON under the run directory. This one runs to completion because it's fast (< 1 second).
- `strategy_comparison` — prints the exact `./scripts/research-run-matrix` command needed. Same rationale as the sweep: matrix runs take minutes and shouldn't be auto-triggered by a plan.

### 3. Summarising a run

```bash
./scripts/traderjoe-research summarize latest
# or:
./scripts/traderjoe-research summarize regime_analysis-2026-07-04T....
```

Prints the run's kind, question, status, and the executed steps. This is your log — every completed `run` shows up here.

---

## Full worked example

```bash
# 1. Ask a question
./scripts/traderjoe-research plan "Test whether RS works better in high-vol regimes"
# -> writes reports/research_plans/plan-2026-07-04T....json

# 2. Look at the plan (optional but recommended)
cat reports/research_plans/plan-2026-07-04T....json
#   kind: regime_analysis
#   parameters.regime_dimensions: ["vol"]
#   parameters.focus_bucket: "high"
#   parameters.source_manifest: "reports/run_manifests/matrix-1y-2026-07-04/latest.json"

# 3. Run it
./scripts/traderjoe-research run reports/research_plans/plan-2026-07-04T....json
# -> runs strategy.lab.regime_report against the 1y matrix
# -> writes reports/research_runs/regime_analysis-2026-07-04T..../manifest.json
# -> writes the regime JSON under the same run dir

# 4. Summarise (or re-summarise days later)
./scripts/traderjoe-research summarize latest
```

You never left the shell. No Claude Code session was needed.

---

## When to bring Claude Code back in

Trader Joe handles the "known patterns" cleanly. Bring me back for:

1. **Adding a new plan kind** (extending `strategy/lab/nl_planner.py` with a new parser and dispatch).
2. **Investigating a bug** (test suite failing, unexpected disagreement counts, corrupt manifest).
3. **Writing a substantive analysis report** where an executive-summary narrative is needed — Trader Joe's summariser prints run metadata, not a narrative.
4. **Any change to `trader.py`, `crypto_trader.py`, `strategy/runner.py`, or any live-trading path.** These files are not part of research mode and should stay bytewise-frozen until you explicitly ask for a production change.
5. **Warehouse expansion** (e.g., importing the S&P 500 or a new asset class).

---

## Hard rules that never change

Trader Joe's operator surface refuses to:

- **Promote any strategy.** `PromotionEntry` states remain `disabled` in every run manifest. There is no operator command that advances promotion state.
- **Enable feature flags.** `feature_flags_all_disabled: True` is asserted in every run bundle. Turning a flag on requires editing config directly and restarting the underlying process — not something the research CLI can do.
- **Construct `ApprovalRecord`.** Source-safety tests guard every research module against `ApprovalRecord(` appearing in its source.
- **Touch production credentials.** The bash launcher explicitly refuses to source `.env` or `.env.production`; it only accepts `.env.research` (or `--env-file <path>` for local overrides). Only `RESEARCH_ALPACA_*` and `RESEARCH_LLM_*` env vars are recognised.
- **Modify trader.py / crypto_trader.py / strategy/runner.py.** These files are byte-identical to their state before Phase 5.8. Any accidental edit would break the test suite immediately.
- **Place orders.** No research code path imports `submit_order` / `place_order` / `TradingClient` / `cancel_order`. This is tested per-module.

If you catch me breaking any of these, that's a bug.

---

## Directory conventions

| Location | Purpose |
|---|---|
| `market_data/` | Warehouse data (Parquet + manifests). Untracked. |
| `market_data/manifests/` | Per-dataset JSON manifests with sha256 + validation status. |
| `reports/research_plans/` | Plan JSON files emitted by `plan`. |
| `reports/research_runs/` | Run manifests emitted by `run`. |
| `reports/research_summaries/` | Long-form narrative reports (executive summaries). Committed to git. |
| `reports/run_manifests/matrix-*/` | Matrix run manifests (from `./scripts/research-run-matrix`). |
| `reports/validation/matrix-*/` | Full comparison / walk-forward / learning / analyst artifacts. |

`.env.research` — the ONLY env file the research CLI will source. Contains `RESEARCH_ALPACA_*` and `RESEARCH_LLM_*`.

---

## What's changed in this handoff sprint

Four commits landed to make direct-to-Trader-Joe research viable:

- **`65a0b9d`** — Compact analyst payloads on by default. Fixes the LLM context-overflow that blocked 90d+ matrix windows. Full artifacts still on disk; only the LLM prompt is compacted.
- **`b299dfc`** — Natural-language experiment planner. Regex-based, deterministic, three known kinds.
- **`fcc056f`** — `scripts/traderjoe-research` operator entrypoint. Warehouse-only default; refuses provider fallback unless explicit; never enables flags.
- **This document.**

Full suite: **2,232 tests passing** at commit `fcc056f`. `trader.py` / `crypto_trader.py` / `strategy/runner.py` bytes untouched.

---

## What's *not* in this handoff

Deliberate scope limits:

- **The runner doesn't auto-execute heavy sweeps.** `rs_weight_sweep` and `strategy_comparison` produce documented delegations, not running processes. This is because the underlying calls take minutes to hours and should be operator-triggered explicitly. If you want automation, use the existing `./scripts/research-run-matrix` directly.
- **No user-facing UI.** JSON files and stdout only. If a dashboard is wanted, that's the F1 backlog item — but nothing about research mode requires it.
- **No new strategies.** Champion, Champion+RS, Momentum, Trend, MeanReversion are what's available. New strategies land through the Strategy Lab plug-in interface (see `strategy/lab/strategy.py`) and require a Claude Code session.
- **The 60d window remains structurally unusable.** Champion needs 51 sessions of history before it can score, so 60 calendar days ≈ 41 sessions can't produce meaningful research signal. All shorter-window questions should use 90d or larger.

---

## When something breaks

The escape hatch is always: **run the test suite, read the failure, ask me.**

```bash
python -m pytest -q
```

If it says `2232 passed`, the infrastructure is in a good state.

If it says anything else, something regressed. Don't try to work around it — the failure is the useful signal.

---

*Last updated: 2026-07-04. Commit chain: `65a0b9d` → `b299dfc` → `fcc056f`.*
