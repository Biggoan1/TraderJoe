# Safety Model

Trader Joe is a paper-trading system today. It is not — and will not
become — a live-money system without meeting an explicit set of
prerequisites recorded in this document. Everything below is enforced
by code, by tests, and by the review process.

The safety model is layered. Any single layer failing must not enable
live trading; multiple layers must all agree.

## Layer 1 — Paper vs Production

The runtime guard is a hardcoded module constant:

- `trader.py:48` — `PAPER = True`
- `crypto_trader.py:62` — `PAPER = True`

Both files pass `paper=PAPER` to the Alpaca `TradingClient`
constructor. Both files carry an identical "PRODUCTION SAFEGUARD — DO
NOT FLIP WITHOUT AN ApprovalRecord" comment block above the constant.

**Before either constant may be flipped to `False`, all of the
following must hold (from `docs/agent/env-isolation.md:121-144`):**

1. An `ApprovalRecord` exists in an operations record naming the
   approver, dated approval, flag scope, monitoring dashboard, and
   rollback plan.
2. The relevant `PromotionEntry` sits at `STATE_APPROVED` or
   `STATE_PRODUCTION` with no triggered rollback alerts across
   `STANDARD_ROLLBACK_CRITERIA`.
3. Walk-forward evidence, paper-trading evidence, and (once
   `t_phase5_two_month_validation_run` lands) the two-month historical
   validation evidence all pass the promotion criteria in ROADMAP
   §"v1.0 Production Readiness".
4. `.env.production` has been populated with the approved credentials
   and is loaded by the same commit that flips `PAPER = False`.
5. A rollback drill has been executed and documented within the last
   30 days.

The current codebase does not enforce these programmatically because
no live-mode code path exists yet. Reviewers of any PR that touches
`PAPER =` on either file MUST verify the checklist above and cite the
approving `ApprovalRecord` id in the PR body.

**Why?** Once real money is at risk, the cost of a bug is not a lost
paper trade — it is a lost real dollar. The prerequisites are
deliberately expensive to satisfy so that no one flips them casually.

## Layer 2 — `ApprovalRecord`

Defined in `strategy/promotion_gates.py:107-152`.

```
@dataclass(frozen=True)
class ApprovalRecord:
    approver: str
    approved_at: str
    flag_name: str
    scope: str
    monitoring: str
    rollback_plan: str
    commit: str = ""
    notes: str = ""
```

`__post_init__` calls `validate()`, which raises `ValueError` if any
of `approver`, `approved_at`, `flag_name`, `scope`, `monitoring`, or
`rollback_plan` is empty.

**No module in the tree constructs an `ApprovalRecord` on its own.**
An `ApprovalRecord` is a data artifact — an operator writes it into a
`PromotionEntry`, and callers may consume it to validate that a
promotion transition is legal.

**Why?** Approval must be a deliberate human act, not a side effect
of a code path. If a script could construct an `ApprovalRecord`, the
entire promotion gate becomes performative.

## Layer 3 — `PromotionEntry` and states

Defined in `strategy/promotion_gates.py:332-367` (mutable dataclass)
with the state chain at lines 40-56:

```
STATE_DISABLED       = "disabled"
STATE_BACKTEST       = "backtest"
STATE_WALK_FORWARD   = "walk_forward"
STATE_PAPER_TRADING  = "paper_trading"
STATE_CANDIDATE      = "candidate"
STATE_APPROVED       = "approved"
STATE_PRODUCTION     = "production"
```

Required evidence per state (`promotion_gates.py:62-74`):

| State | Required evidence |
|---|---|
| `disabled` | (none) |
| `backtest` | `experiment_manifest`, `dataset_id` |
| `walk_forward` | `backtest_report_id` |
| `paper_trading` | `walk_forward_report_id`, `rollback_plan` |
| `candidate` | `paper_trading_report_id`, `disagreement_summary` |
| `approved` | `approval_record` |
| `production` | `monitoring_dashboard`, `rollback_owner`, `approval_record` |

**`PromotionEntry` has no `advance()` method.** The caller sets
`current_state` directly and re-evaluates. `evaluate_promotion(entry,
metrics, rollback_criteria, generated_at)` (line 522) returns a
`PromotionReport` with `required_evidence`, `missing_evidence`,
`approvals`, `rollback_alerts`, and `warnings`.

A warning fires if `current_state ∈ {STATE_APPROVED,
STATE_PRODUCTION}` and `entry.approvals` is empty (line 542).

**Why?** Advancement is metadata, not behaviour. The module refuses
to be complicit in a state change that isn't backed by evidence.

## Layer 4 — Rollback criteria

`STANDARD_ROLLBACK_CRITERIA` (`promotion_gates.py:274-324`) — seven
`RollbackCriterion` entries. Any single alert is disqualifying:

| Name | Metric | Threshold | Comparator |
|---|---|---|---|
| `expectancy_worse_than_champion` | `challenger_expectancy_delta` | `0.0` | `lt` |
| `drawdown_worse_than_limit` | `challenger_max_drawdown_delta` | `0.0` | `gt` |
| `profit_factor_worse_than_champion` | `challenger_profit_factor_delta` | `0.0` | `lt` |
| `trade_frequency_below_minimum` | `challenger_trade_count` | `1.0` | `lt` |
| `concentration_exceeds_limit` | `challenger_top_symbol_share` | `0.5` | `gt` |
| `data_quality_below_tolerance` | `data_quality_score` | `0.9` | `lt` |
| `reproducibility_check_failed` | `reproducibility_ok` | `1.0` | `lt` |

Comparators: `KNOWN_COMPARATORS = ("lt", "le", "gt", "ge", "eq")`
(`promotion_gates.py:160`).

**Why?** Every criterion has an objective numeric threshold. No
adjective. No wiggle room.

## Layer 5 — Feature flags

`strategy/config.py:28-83` defines `FeatureFlags`. Every field
defaults to `False`:

| Flag | Default |
|---|---|
| `enable_historical_statistics` | `False` |
| `enable_post_game_review` | `False` |
| `enable_relative_strength` | `False` |
| `enable_market_regime` | `False` |
| `enable_sector_leadership` | `False` |
| `enable_market_breadth` | `False` |
| `enable_confidence_score` | `False` |
| `enable_statistical_decision_support` | `False` |
| `enable_sell_score` | `False` |
| `enable_risk_based_position_sizing` | `False` |
| `enable_overnight_risk_engine` | `False` |
| `enable_morning_intelligence` | `False` |

Module-level helpers:

- `get_feature_flags()` — lazy singleton getter
  (`strategy/config.py:137-146`).
- `reset_feature_flags()` — resets to a fresh all-disabled instance
  (line 148); used in tests.
- `FeatureFlags.enable(name)` / `.disable(name)` — instance
  mutators.
- `FeatureFlags.enable_all()` — used only in tests.

**Every research module records the flag snapshot in its report** and
asserts `feature_flags_all_disabled=True` post-run when applicable
(e.g. `strategy/lab/weekend_lab.py`).

**Why?** A flag turning itself on is a bug, not a feature. The
default must be safe.

## Layer 6 — Emergency stop files

Two paths, one per daemon:

- `/etc/traderjoe/STOP_EQUITY_PAPER` — checked by the equity daemon
  every tick. Presence → daemon short-circuits with reason
  `emergency_stop_file_present` before touching bars or the bridge.
- `/etc/traderjoe/STOP_CRYPTO_PAPER` — same check for the crypto
  daemon.

Both defaults come from `strategy/paper_daemon.py:55` and
`strategy/crypto_paper_daemon.py:48`. Both are overridable via
`--emergency-stop-file`, which tests use.

The Risk Manager persona owns these files
(`.traderjoe/skills/risk-manager/SKILL.md`).

**Why?** A bug in the daemon must not prevent an operator from
stopping it. `touch /etc/traderjoe/STOP_CRYPTO_PAPER` is the
universal "halt now" command, regardless of what the daemon thinks
it is doing.

## Layer 7 — Paper daemons: doubly-gated auto-execute

Both `strategy/paper_daemon.py` and `strategy/crypto_paper_daemon.py`
require **both** conditions for the execute path:

1. **Env flag**:
   - Equity: `PAPER_AUTO_EXECUTE_EQUITIES=true` in `.env.paper`
     (`paper_daemon.py:64`).
   - Crypto: `PAPER_AUTO_EXECUTE_CRYPTO=true` in `.env.crypto`
     (`crypto_paper_daemon.py:57`).
2. **CLI flag**: `--execute-paper-orders` explicitly present in argv.

Either flag missing → daemon writes a plan file and exits with a
descriptive reason (`cli_execute_flag_not_set`,
`PAPER_AUTO_EXECUTE_CRYPTO_not_true`).

Additional per-tick caps (defaults, overridable):

| Cap | Equity | Crypto |
|---|---:|---:|
| max per-order notional | $2,000 | $1,500 |
| max total daily notional | $10,000 | $4,500 |
| max trades per day | 8 | 6 |
| cooldown per symbol (min) | 60 | 60 |

The equity daemon additionally refuses execution outside NYSE regular
session (session helper at `strategy/market_calendar.py`); the crypto
daemon runs 24/7.

**Why?** Two flags with different provenance (env file vs CLI) means
turning execute on requires two different actions by (potentially)
two different actors. Copy-pasting a CLI without setting the env — or
setting the env without adding the CLI flag — both fail closed.

## Layer 8 — Credential separation

From `docs/agent/env-isolation.md`:

| Env file | Namespace | Client | Endpoint |
|---|---|---|---|
| `.env.paper` | `ALPACA_*`, `PAPER_ALPACA_*` | `trader.py` | paper |
| `.env.crypto` | `CRYPTO_ALPACA_*` | `crypto_trader.py` | paper |
| `.env.research` | `RESEARCH_ALPACA_*` (+ `_DATA_ENDPOINT`) | `strategy/research_account.py` | paper (read-only) |
| `.env.production` | — (empty) | — (nothing reads it) | — |

**Invariants (all enforced):**

- `strategy/research_account.py` refuses to fall back to `ALPACA_*` /
  `APCA_*`.
- `trader.py` and `crypto_trader.py` refuse to read `RESEARCH_*`.
- Every daemon refuses env files whose basename doesn't match its
  namespace (`.env.crypto` for the crypto daemon; `.env.paper*` for
  the equity daemon and paper bridge).
- Every daemon and the bridge refuse endpoints that do not contain
  `paper-api.alpaca.markets`.
- Every daemon and the bridge refuse `HERMES_CONTEXT=production` or
  `PAPER=false`.

`scripts/traderjoe-simulate` refuses to source **any** env file — the
simulator is warehouse-only.

**Why?** Cross-contamination between accounts breaks the risk model.
If the research account could accidentally submit orders, its
"read-only" guarantee is worth nothing.

## Layer 9 — Risk limits

Runtime knobs in `trader.py:56-68`:

- `COOLDOWN_MINUTES = 30`
- `ORB_PERIOD_MINUTES = 15`
- `ORB_MAX_TRADE_MINUTES = 120`
- `DEFAULT_BUY_AMOUNT = 15000`
- `MAX_BUY_AMOUNT = 25000`
- `MAX_POSITIONS = 20`
- `TARGET_MAX_POSITIONS = 15`

Runtime knobs in `crypto_trader.py`:

- `DEFAULT_BUY_AMOUNT = 3000`
- `MAX_BUY_AMOUNT = 8000`
- `MAX_POSITIONS = 3`
- `TARGET_MAX_POSITIONS = 2`
- `COOLDOWN_MINUTES = 60`
- `CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]`

Additional caps from `strategy/config.py`:

- `SELL_SCORE_THRESHOLD = 70`
- `RISK_PER_TRADE_DEFAULT = 0.02`
- `MAX_SECTOR_EXPOSURE = 0.25`
- `OVERNIGHT_GAP_THRESHOLD_PERCENT = 2.0`

**Why?** Hard caps on notional, positions, and sector exposure keep a
single misbehaving iteration from producing a portfolio-level
disaster.

## Layer 10 — Operator approval flow

Every buy in paper trading requires one of:

1. **Six-gate rule match** — Champion scorer flags a setup with
   `gate_count >= 4` (the "qualified" line in the live scorer).
2. **User approval** — the operator explicitly approves a pending
   order via `trader_cli.py approve <id>` or the Telegram approval
   flow.

Pending orders live in `trades.db` in the `pending_approvals` table.
`telegram_approvals.py` and `telegram_daemon.py` route messages;
`trader_cli.py pending` lists them.

Every ordered position carries a five-minute tanking timer
(`trader.py`): if the position moves against by a configured
threshold within five minutes of entry, the runner exits.

Every sell also carries a reason recorded in the trade log (`RSI
overbought`, `MACD death cross`, `upper Bollinger`, `six-gate
failure`, `user decision`, `tanking timer`) — see
`strategy/trade_logger.py`.

**Why?** Every trade produces an audit trail. Every sell has a
reason. If the audit trail says "user decision," a human made the
call. If it says anything else, an objective rule made the call.

## Layer 11 — Model isolation

Per-context AI model resolution
(`docs/agent/env-isolation.md:240-268`):

| Context | Env var |
|---|---|
| Paper | `PAPER_AI_MODEL` |
| Crypto | `CRYPTO_AI_MODEL` |
| Research | `RESEARCH_AI_MODEL` |
| Production | `PRODUCTION_AI_MODEL` |

Resolver: `strategy.model_config.resolve_model(context, explicit=None,
default=None, env=None)`. Precedence: explicit caller argument →
context env var → caller default. The resolver never hardcodes a
model name.

**Why?** A production model must never be used in research; a
research model must never be reached from a paper runner. Context is
carried through the process env; the resolver honors it.

## Layer 12 — Research LLM endpoint policy

`strategy/research_analyst.py` refuses to reach the public internet
by default. Configuration
(`docs/agent/env-isolation.md:271-378`):

- `RESEARCH_LLM_ENDPOINT` — default `http://127.0.0.1:8080`.
- `RESEARCH_LLM_API_STYLE` — `openai` (default) or `ollama`.
- `RESEARCH_LLM_ALLOW_REMOTE` — default `false`; must be `true` to
  accept non-loopback endpoints.
- `RESEARCH_LLM_ALLOWED_HOSTS` — comma-separated allowlist required
  when opt-in is enabled.

Cloud provider substrings (`openai.com`, `anthropic.com`,
`googleapis.com`, `gemini.google.com`, `azurewebsites.net`,
`amazonaws.com`, `cohere.ai`, `huggingface.co`, `replicate.com`, etc.)
are refused regardless of opt-in. Cloud model names (`openai`,
`anthropic`, `google`, `azure`, `aws`, `gemini`, `claude`, `chatgpt`)
are refused regardless of endpoint.

**Why?** Research is an isolated read-only activity. Sending its
prompts to a cloud provider would create a side channel that the risk
model does not account for.

## Layer 13 — Systemd units

Under `docs/systemd/`:

- `traderjoe-paper.service` — installable equity paper runner.
- `traderjoe-crypto.service` — installable crypto paper runner.
- `traderjoe-research.service` — installable research runner
  (oneshot).
- `traderjoe-paper-plan.timer` + `.service` — installable equity
  daemon plan-only timer + service.
- `traderjoe-paper-daemon.service.example` — auto-execute daemon
  template. `ExecStart=/bin/false`, `[Install]` omitted, requires
  `/etc/traderjoe/PAPER_AUTO_EXECUTE_APPROVED` marker file.
- `traderjoe-crypto-paper-daemon.service.example` — crypto
  auto-execute template. `ExecStart=/bin/false`, `[Install]` omitted,
  requires `/etc/traderjoe/CRYPTO_AUTO_EXECUTE_APPROVED` marker file.
- `traderjoe-production.service.example` — `ExecStart=/bin/false`,
  `ConditionPathExists=/etc/traderjoe/PRODUCTION_APPROVED`, `[Install]`
  omitted so `systemctl enable` fails until the operator adds it
  deliberately.

**Why?** systemd will refuse to load `.service.example` files. If
someone renames one, `ExecStart=/bin/false` still makes it exit
non-zero. If someone adds `[Install]`, `ConditionPathExists=` still
refuses to start without the marker file. Three separate accidents
would have to align for a live-mode unit to boot.

## Layer 14 — Test enforcement

The test suite explicitly checks:

- `tests/test_new_strategies_and_daemons.py::TestSourceSafety` —
  asserts every new module never imports `trader`, `crypto_trader`,
  or `strategy.runner`, and never contains `PAPER = False` or
  `paper=False`. Also asserts `trader.py` and `crypto_trader.py`
  still contain `PAPER = True`.
- `tests/test_portfolio_simulator.py::TestSourceSafety` — same
  checks for the simulator and paper bridge.
- `tests/test_portfolio_simulator.py::TestPaperBridgeSafety` —
  covers env file refusal, endpoint refusal, notional caps, symbol
  allowlist, dry-run makes no submit calls.

Total test suite at the milestone: **2,361 tests passing**.

**Why?** Every safety rule that isn't in the test suite decays. If
the rule is important enough to write down here, it is important
enough to test.

## Emergency stops summary

If something is going wrong right now:

```bash
# Equity paper daemon
touch /etc/traderjoe/STOP_EQUITY_PAPER

# Crypto paper daemon
touch /etc/traderjoe/STOP_CRYPTO_PAPER

# Paper trading (equity runner)
sudo systemctl stop traderjoe-paper.service

# Crypto trading (crypto runner)
sudo systemctl stop traderjoe-crypto.service
```

Neither daemon nor runner should ever be running unmonitored.

The Risk Manager persona is authorised to invoke any of the above at
any time without consulting the other operator roles.
