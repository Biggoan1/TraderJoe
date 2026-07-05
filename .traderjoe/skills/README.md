# Trader Joe Operator Skills

This directory holds the operator-persona **skills** Claude Code can load
while working on Trader Joe.  Each skill is a small playbook describing
what one operator role does, which commands it is allowed to run, and
what safety rules bind it.

Skills are read-only guidance for the assistant.  They do not run code
on their own, do not schedule anything, and do not enable feature flags.
Trader Joe's usual paper/live isolation rules always apply on top of any
skill.

## Layout

Each skill lives in its own folder with a `SKILL.md` file:

```
.traderjoe/skills/
├── README.md                       (this file)
├── morning-operator/SKILL.md
├── evening-operator/SKILL.md
├── research-analyst/SKILL.md
├── portfolio-manager/SKILL.md
├── risk-manager/SKILL.md
└── crypto-operator/SKILL.md
```

Every `SKILL.md` begins with YAML frontmatter:

```markdown
---
name: <kebab-case-slug>
description: <one-line summary shown when the skill is offered>
---

# <Human-readable Title>

## Mission
...
```

The `name` must match the folder slug.  The `description` is what an
operator sees when they ask "what skills are available?" — keep it under
~200 characters.

## Discovery

Claude Code discovers skills by scanning the top-level folders under
`.traderjoe/skills/` for a `SKILL.md`.  The frontmatter `name` becomes
the skill's identifier; the folder name is the addressable slug.  New
skills land by adding a folder — no registration file to edit.

Skills are surfaced to the assistant when:

- the user names one directly ("act as morning-operator", "run the
  research-analyst playbook"),
- a slash-command or session hook asks for the persona,
- a routine (see `docs/systemd/`) invokes a Trader Joe script whose
  playbook matches the skill's mission.

Skills do **not** auto-fire on a timer; they are opt-in playbooks that
Claude follows when the operator asks.

## Command mapping

Each skill maps to a set of Trader Joe entry points.  This table is a
convenience index — the authoritative allowed-actions list lives in
each skill's own `SKILL.md`.

| Skill | Primary commands |
|---|---|
| `morning-operator` | `python trader.py --morning`, `python trader_cli.py positions`, `strategy/morning_intelligence.py`, `scripts/traderjoe-simulate --strategy champion-v0.4.0 ... --window 60d` (dry-run review) |
| `evening-operator` | `python trader.py --eod`, `python scripts/generate_daily_digest.py`, `python trader_cli.py trades`, journal update under `research_notes/` |
| `research-analyst` | `scripts/traderjoe-research`, `scripts/research-run-matrix`, `scripts/research-validate-offline`, `scripts/traderjoe-simulate`, `strategy/lab/*` presets |
| `portfolio-manager` | `python trader_cli.py positions`, `python trader_cli.py pending`, `scripts/traderjoe-simulate` for what-if reviews, `scripts/traderjoe-paper-execute --dry-run` |
| `risk-manager` | Read-only monitors: `python trader_cli.py positions`, `strategy/promotion_gates.py` review, `strategy/overnight_risk.py`, log inspection under `reports/paper_daemon/`.  Owns the emergency-stop files under `/etc/traderjoe/` |
| `crypto-operator` | `python crypto_trader.py --scan`, `scripts/traderjoe-crypto-paper-daemon` (dry-run by default; `--execute-paper-orders` only when the operator has approved) |

## Safety invariants (bind every skill)

Every skill inherits these system-wide rules and cannot relax them:

- **Paper only.**  No skill may set `PAPER = False` or load
  `.env.production`.  See `docs/agent/env-isolation.md`.
- **Account isolation.**  Equity paper reads `.env.paper`, crypto paper
  reads `.env.crypto`, research reads `.env.research`.  Skills never
  cross accounts.
- **No auto-promotion.**  A skill may propose a promotion transition
  but must not construct an `ApprovalRecord` or advance a
  `PromotionEntry` state on its own.
- **No feature-flag flipping.**  Global `FeatureFlags` are toggled only
  by an approved operator change.
- **Auto-execute is opt-in and doubly gated.**  Paper daemons only
  submit orders when both the env flag (`PAPER_AUTO_EXECUTE_EQUITIES`
  or `PAPER_AUTO_EXECUTE_CRYPTO`) and the CLI flag
  (`--execute-paper-orders`) are set.  Skills describe how to opt in;
  they never opt in on the operator's behalf.
- **Emergency stops honoured.**  Skills must check the relevant
  `/etc/traderjoe/STOP_*` file before recommending any execute path
  and treat its presence as a hard veto.

## Authoring a new skill

1. Create `.traderjoe/skills/<slug>/SKILL.md`.
2. Add frontmatter with matching `name` and a one-line `description`.
3. Structure the body under these headings, in order:
   - `## Mission`
   - `## Responsibilities`
   - `## Allowed Actions` (be specific — list commands, not verbs)
   - `## Forbidden Actions`
   - `## Handoffs` (what other skill or human owns the follow-up)
4. Reference source files by path and function so future
   readers can find them without grep.
5. Do not include credentials, secrets, or trade sizing constants;
   leave those to `.env.*` files and command-line flags.

## What skills are NOT

- **Not automation.**  A skill is a playbook Claude follows, not a
  scheduler.  Automation lives under `docs/systemd/` and
  `scripts/traderjoe-*` and is opt-in per host.
- **Not policy sources.**  Promotion gates, risk limits, and
  approval records are authoritative in `strategy/promotion_gates.py`
  and the docs under `docs/agent/`.  Skills cite policy; they do not
  replace it.
- **Not memory.**  Persistent facts about a specific run belong in
  `research_notes/` or `reports/`.  Skills stay generic across runs.
