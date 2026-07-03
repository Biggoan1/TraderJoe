# Environment File Isolation and Production Safeguards

Trader Joe operates across four independent trading contexts. Each
context has its own credentials, its own env file, and its own strict
isolation guarantees. Sharing credentials — even accidentally — breaks
the risk model that lets research and paper trading coexist safely with
future live trading.

**None of the four env files are tracked in git.** Only `.env.example`,
the placeholder template, is tracked.

---

## The four env files

### `.env.paper` — normal paper trading

- **Used by:** `trader.py` (equities paper trading) and any script that
  drives the live runner in paper mode.
- **Credentials:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
  `ALPACA_ENDPOINT` (paper endpoint), plus `OPENAI_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` for approvals.
- **Alpaca account:** the normal paper account. `PAPER = True` in
  `trader.py` gates every `TradingClient(..., paper=PAPER)`
  construction — do not change it without following the Production
  Safeguards below.
- **Do NOT** put research or production credentials in this file.

### `.env.crypto` — crypto paper trading / crypto testing

- **Used by:** `crypto_trader.py`.
- **Credentials:** `CRYPTO_ALPACA_API_KEY`, `CRYPTO_ALPACA_SECRET_KEY`
  (optionally falling back to `ALPACA_*` per current code), plus
  Telegram + OpenAI keys.
- **Alpaca account:** the dedicated crypto testing paper account.
  `PAPER = True` in `crypto_trader.py` gates every
  `TradingClient(..., paper=PAPER)` construction.
- **Do NOT** put equities paper, research, or production credentials
  in this file.

### `.env.research` — historical validation / replay / Backtest Lab

- **Used by:** `strategy/research_account.py` (the isolated Research
  Alpaca client) and, in the future, `t_phase5_two_month_validation_run`.
- **Credentials:** `RESEARCH_ALPACA_API_KEY`,
  `RESEARCH_ALPACA_SECRET_KEY`, `RESEARCH_ALPACA_ENDPOINT` (trading
  API — account & calendar reads), and
  `RESEARCH_ALPACA_DATA_ENDPOINT` (market-data API — historical
  bars). Alpaca hosts the two APIs on distinct domains
  (`paper-api.alpaca.markets` vs `data.alpaca.markets`), so both
  endpoints must be set — the client refuses to guess which one to
  use.
- **Alpaca account:** the dedicated Research paper account —
  distinct from the normal paper account. Research is
  read-only: `ResearchAccountClient` exposes only `fetch_bars`
  (routed through the data endpoint), `list_calendar`, and
  `paper_account_info` (both routed through the trading endpoint).
  No order-placement method exists on it.
- **Hard rules:**
  - `strategy/research_account.py` refuses to fall back to
    `ALPACA_*` / `APCA_*` env values — a Research run that does not
    have `RESEARCH_ALPACA_*` set will error out rather than reuse
    the paper account's credentials.
  - No Phase 1-4 module may import `strategy.research_account`.
  - The live runner (`trader.py`, `crypto_trader.py`,
    `strategy/runner.py`, `trader_cli.py`, `telegram_approvals.py`,
    any scheduler) must NEVER read `RESEARCH_ALPACA_*` or import
    the Research client.

### `.env.production` — future live trading (real money)

- **Used by:** nothing today. The codebase does not run against a
  production account and does not read a production env file. This
  file exists only as a placeholder in the isolation model.
- **Credentials:** none yet. **`.env.production` MUST remain empty
  until an explicit production ApprovalRecord authorises live
  trading.** See `strategy/promotion_gates.py` (`STATE_APPROVED`
  and `STATE_PRODUCTION`) and the "v1.0 Production Readiness"
  section in `ROADMAP.md` for the promotion gate that must be
  cleared before any credentials are ever written here.
- **Production means real money.** Real orders, real fills, real
  capital at risk. Every safeguard elsewhere in this repo is
  designed to keep that boundary clear.
- **Do NOT** copy credentials from any other `.env.*` file into
  `.env.production`.

---

## Isolation rules — all four contexts

These invariants hold across every phase of the codebase:

1. **Credentials are never shared across accounts.** Paper, crypto,
   research, and production each hold their own Alpaca account with
   its own key pair.
2. **Research credentials never reach the live runner.**
   `strategy/research_account.py` is the only module that reads
   `RESEARCH_ALPACA_*`, and it must not be imported by any live
   runner, scheduler, Telegram approval path, CLI entry, or plugin.
3. **Paper and crypto credentials never reach research paths.** The
   Research client refuses to fall back to `ALPACA_*` / `APCA_*` so
   a misconfigured environment fails loudly.
4. **Production credentials do not exist yet.** No module reads
   `.env.production`. Any future code that would use it must ship
   inside an approved promotion transition with an
   `ApprovalRecord` naming the approver, scope, monitoring
   dashboard, and rollback plan.

---

## Production Safeguards

Trader Joe does not currently run against a live production account.
The guard rail today is a hardcoded module constant:

- `trader.py`: `PAPER = True` (line ~25)
- `crypto_trader.py`: `PAPER = True` (line ~39)

Both are passed as `paper=PAPER` to the Alpaca `TradingClient`.

### Before flipping `PAPER` to `False`

All of the following must be satisfied:

1. An `ApprovalRecord` (see `strategy/promotion_gates.py`) exists in
   an operations record that names the approver, dated approval,
   flag scope, monitoring dashboard, and rollback plan.
2. The relevant `PromotionEntry` sits at
   `STATE_APPROVED` or `STATE_PRODUCTION` with no triggered rollback
   alerts across `STANDARD_ROLLBACK_CRITERIA`.
3. Walk-forward evidence, paper-trading evidence, and (once
   `t_phase5_two_month_validation_run` lands) the two-month
   historical validation evidence all pass the promotion criteria in
   ROADMAP §"v1.0 Production Readiness".
4. `.env.production` has been populated with the approved
   credentials and is loaded by the same commit that flips
   `PAPER = False`.
5. A rollback drill has been executed and documented within the
   last 30 days.

The current codebase does not enforce these programmatically because
no live-mode code path exists yet. Reviewers of any PR that touches
`PAPER =` on either file MUST verify the checklist above and cite the
approving `ApprovalRecord` id in the PR body.

### For automation and CI

- CI runs must not have `.env.production` present. If a future CI
  step needs credentials, it must load `.env.paper`, `.env.crypto`,
  or `.env.research` explicitly and never fall through to
  `.env.production`.
- Any future orchestrator that resolves an env file by name should
  refuse `.env.production` unless a `HERMES_PRODUCTION_APPROVAL_ID`
  env var is set and matches a recorded `ApprovalRecord`.

---

## `.gitignore` invariants

The following must always hold:

- `.env` and every `.env.*` variant are ignored.
- `.env.example` (the placeholder template) is tracked as an
  explicit `!.env.example` whitelist entry.

If you edit `.gitignore`, keep the whitelist entry in place and
verify with:

```
git check-ignore -v .env .env.paper .env.crypto .env.research .env.production .env.example
```

`.env.example` must not appear as ignored; every other listed file
must.

---

## What lives in `.env.example`

`.env.example` documents the union of every namespace the codebase
knows how to read. Every value is a placeholder. Contributors copy
the sections they need into the appropriate `.env.<context>` file
and populate them locally.

---

## Launcher scripts

`scripts/run-paper`, `scripts/run-crypto`, and `scripts/run-research`
load exactly one env file each and exec the matching entrypoint.

| Launcher | Env file loaded | Exec target |
|---|---|---|
| `scripts/run-paper` | `.env.paper` | `python trader.py "$@"` |
| `scripts/run-crypto` | `.env.crypto` | `python crypto_trader.py "$@"` |
| `scripts/run-research` | `.env.research` | `python "$@"` (default: `ResearchAccountConfig.from_env()` self-check) |

Guarantees enforced by every launcher:

- Refuses to run if the target env file is missing.
- Never sources `.env`.
- Never sources `.env.production`.
- Loads its own env file with `set -a; . "$ENV_FILE"; set +a` so
  every variable is exported.
- Sets `HERMES_CONTEXT=<paper|crypto|research>` so downstream code
  can assert it is running under the expected context.
- Preserves positional arguments via `"$@"`.

There is intentionally no `scripts/run-production`.  A live-trading
launcher must ship in the same commit that satisfies the
Production Safeguards below.

## systemd examples

Under `docs/systemd/`:

- `traderjoe-paper.service`
- `traderjoe-crypto.service`
- `traderjoe-research.service`
- `traderjoe-production.service.example`  ← template only, not
  installable

Each real unit uses `EnvironmentFile=` pointing at the matching
`.env.<context>` and `Environment=HERMES_CONTEXT=<context>`.

The production template ends in `.service.example` so systemd will
not load it, `ExecStart=/bin/false` so even a mistakenly-renamed
copy exits non-zero, `ConditionPathExists=/etc/traderjoe/PRODUCTION_APPROVED`
so it refuses to start until an operator manually creates the marker
file, and the `[Install]` section is omitted so `systemctl enable`
fails until the operator adds it deliberately.

---

## AI model configuration

Each context has its own model env var; there is no shared
"active model" variable and Hermes is not assumed to export one.

| Context | Env var |
|---|---|
| Paper | `PAPER_AI_MODEL` |
| Crypto | `CRYPTO_AI_MODEL` |
| Research | `RESEARCH_AI_MODEL` |
| Production | `PRODUCTION_AI_MODEL` |

Model selection is resolved by
`strategy.model_config.resolve_model(context, explicit=None,
default=None, env=None)` with the following precedence:

1. Explicit caller-provided value (highest priority).
2. Context-specific env var
   (`CONTEXT_MODEL_ENV[context]`).
3. Caller-provided default (may be `None`).

The resolver never hardcodes a model name.  Existing callers
(`trader.py`, `crypto_trader.py`) preserve their pre-env-var default
via a local `_LEGACY_MODEL_DEFAULT` fallback so nothing regresses
when the env var is unset.

### Hermes routing note

Future Hermes integration may pass an explicit model argument
through the `explicit=` parameter of `resolve_model`.  Do NOT assume
Hermes exports a shared model env var — the resolver deliberately
supports explicit arguments so Hermes can hand a specific model to a
specific call without polluting the process environment.

---

## Research LLM endpoint policy

The Local LLM Research Analyst (`strategy/research_analyst.py`) only
talks to a local inference server.  Three env vars govern which
endpoints are accepted:

| Env var | Purpose | Default |
|---|---|---|
| `RESEARCH_LLM_ENDPOINT` | Base URL of the inference server | `http://127.0.0.1:8080` |
| `RESEARCH_LLM_API_STYLE` | Provider protocol: `openai` or `ollama` | `openai` |
| `RESEARCH_LLM_ALLOW_REMOTE` | Opt-in to allow a non-loopback endpoint | `false` |
| `RESEARCH_LLM_ALLOWED_HOSTS` | Comma-separated allowlist of trusted hosts | *(empty)* |

### API style — pick the provider protocol explicitly

The client speaks one of two protocols, selected only via
`RESEARCH_LLM_API_STYLE`:

| Style | Model listing | Chat |
|---|---|---|
| `openai` (default) | `GET /v1/models` | `POST /v1/chat/completions` |
| `ollama` | `GET /api/tags` | `POST /api/chat` |

There is no automatic detection.  The client never inspects the
endpoint URL or response bodies to guess the provider — it always
uses the paths and body shape configured for the selected style.

For a local llama.cpp / llama-swap / LM Studio / Hermes Gateway
server, keep the default (`openai`).  For a local Ollama install,
set `RESEARCH_LLM_API_STYLE=ollama`.

### Loopback mode — the default

Out of the box the client accepts only loopback hosts:
`127.0.0.1`, `localhost`, `::1`.  Anything else raises
`LocalLLMEndpointError` at construction time.  This is the safest
mode and covers a local Ollama, llama.cpp, LM Studio, or llama-swap
installation running on the same box.

Example `.env.research`:

```
RESEARCH_LLM_ENDPOINT=http://127.0.0.1:8080
RESEARCH_AI_MODEL=qwen2.5-coder
```

### Trusted-LAN mode — explicit opt-in

Larger local rigs may run the LLM on a separate LAN machine.
Enable this by setting BOTH:

- `RESEARCH_LLM_ALLOW_REMOTE=true` (also accepts `1`, `yes`, `on`)
- `RESEARCH_LLM_ALLOWED_HOSTS=<comma-separated hosts>`

Both must be present.  The client rejects every non-loopback
endpoint whose host is not in the allowlist, even when opt-in is
enabled.

Example — a llama.cpp box at `10.100.0.13:8080`:

```
RESEARCH_LLM_ENDPOINT=http://10.100.0.13:8080
RESEARCH_LLM_ALLOW_REMOTE=true
RESEARCH_LLM_ALLOWED_HOSTS=10.100.0.13
RESEARCH_AI_MODEL=qwen2.5-coder
```

Allowed host classes when opt-in is enabled:

- **RFC1918 private IPs:** `10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`
- **IPv6 unique local addresses (ULA):** `fc00::/7`
- **Trusted internal hostnames:** any DNS name in the allowlist
  that does NOT match a cloud provider substring

### Never allowed — cloud endpoints

Regardless of `RESEARCH_LLM_ALLOW_REMOTE`, the client refuses any
endpoint host that matches a cloud-provider substring
(`openai.com`, `anthropic.com`, `claude.ai`, `chatgpt.com`,
`googleapis.com`, `gemini.google.com`, `google.com`,
`azurewebsites.net`, `amazonaws.com`, `cohere.ai`, `huggingface.co`,
`replicate.com`, etc.).  Public IPv4 / IPv6 addresses are also
refused.  The unspecified addresses `0.0.0.0` and `::` are refused.

The Research Analyst is a validation / replay / research tool.  It
never queries a cloud provider, regardless of allowlist state or
opt-in.  This mirrors the same isolation invariant that governs
`strategy/research_account.py` (the Alpaca account client).

### Never allowed — cloud model names

Independent of endpoint policy, the client refuses any model name
containing `openai`, `anthropic`, `google`, `azure`, `aws`,
`gemini`, `claude`, or `chatgpt`.  This is a defense-in-depth guard
against a compromised local endpoint that would proxy to a cloud
model.

### Endpoint failure modes and their messages

| Failure | Message pattern |
|---|---|
| Missing `http://` / `https://` scheme | `must start with http:// or https://` |
| Cloud provider substring in host | `matches a cloud provider domain` |
| Public IP or `0.0.0.0` | `is a public IP address` |
| Non-loopback host without opt-in | `is not loopback and RESEARCH_LLM_ALLOW_REMOTE is not enabled` |
| Non-loopback host missing from allowlist | `is not in RESEARCH_LLM_ALLOWED_HOSTS` |
