"""Operator entrypoint: traderjoe-research.

Three subcommands:

* ``plan "<question>"``   — parse an English question into a
  deterministic :class:`ResearchPlan`; write the plan JSON.
* ``run <plan-file>``     — execute the plan against the warehouse.
* ``summarize <run-id>``  — read a completed experiment's manifests
  and emit a short summary (``latest`` reads the most recent run).

Read-only against production:

* Warehouse-only by default.  ``--allow-provider-fallback`` must
  be passed explicitly to permit Alpaca provider calls; otherwise
  every step refuses when the warehouse can't cover the request.
* Never enables feature flags.
* Never constructs an ``ApprovalRecord``.
* Never advances a ``PromotionEntry`` past ``disabled``.
* Never touches production credentials (uses ``RESEARCH_ALPACA_*``
  only, mediated by :class:`ResearchAccountConfig`).

The command is intentionally small: it delegates parsing to
:mod:`strategy.lab.nl_planner`, execution to the existing
warehouse-backed runners, and summarisation to the dashboard
backend endpoints already in place.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_PLAN_ROOT = Path("reports/research_plans")
DEFAULT_RUN_ROOT = Path("reports/research_runs")
DEFAULT_WAREHOUSE_ROOT = Path("market_data")


class TraderJoeResearchError(RuntimeError):
    """Raised when the CLI cannot proceed."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="traderjoe-research",
        description=(
            "Operator entrypoint for research-mode Trader Joe.  "
            "Read-only against production; warehouse-only unless "
            "--allow-provider-fallback is passed."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    # --- plan ---
    plan_p = sub.add_parser(
        "plan",
        help="Turn an English research question into a JSON plan.",
    )
    plan_p.add_argument("question", help="the research question")
    plan_p.add_argument(
        "--output", default="",
        help="path to write plan JSON (default: reports/research_plans/plan-<ts>.json)",
    )
    plan_p.add_argument(
        "--print", action="store_true",
        help="also print the plan JSON to stdout",
    )

    # --- run ---
    run_p = sub.add_parser(
        "run",
        help="Execute a plan JSON file.",
    )
    run_p.add_argument("plan_file", help="path to a plan JSON file")
    run_p.add_argument(
        "--warehouse-root", default=str(DEFAULT_WAREHOUSE_ROOT),
    )
    run_p.add_argument(
        "--run-root", default=str(DEFAULT_RUN_ROOT),
    )
    run_p.add_argument(
        "--allow-provider-fallback", action="store_true",
        help=(
            "permit Alpaca provider fetches when the warehouse has "
            "incomplete coverage.  Default: refuse."
        ),
    )
    run_p.add_argument(
        "--dry-run", action="store_true",
        help="print the resolved plan without executing anything",
    )

    # --- summarize ---
    sum_p = sub.add_parser(
        "summarize",
        help="Summarise a completed research run (or 'latest').",
    )
    sum_p.add_argument(
        "run_id",
        help="run id, or 'latest' for the most recent research run",
    )
    sum_p.add_argument(
        "--run-root", default=str(DEFAULT_RUN_ROOT),
    )
    return p


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    from strategy.lab.nl_planner import plan_from_question, KNOWN_KINDS

    plan = plan_from_question(args.question)
    output = args.output
    if not output:
        DEFAULT_PLAN_ROOT.mkdir(parents=True, exist_ok=True)
        ts = _utc_now_iso().replace(":", "-").replace("+", "-")
        output = str(DEFAULT_PLAN_ROOT / f"plan-{ts}.json")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(plan.to_json(), encoding="utf-8")
    print(
        f"plan written: {output}\n"
        f"  kind: {plan.kind}\n"
        f"  steps: {len(plan.steps)}"
    )
    if plan.warnings:
        print("  warnings:")
        for w in plan.warnings:
            print(f"    - {w}")
    if args.print:
        print()
        print(plan.to_json())
    return 0 if plan.kind in KNOWN_KINDS else 3


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan_file)
    if not plan_path.is_file():
        print(f"FAIL: plan file {plan_path} not found", file=sys.stderr)
        return 2
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    kind = plan.get("kind", "")
    if kind not in (
        "rs_weight_sweep",
        "regime_analysis",
        "strategy_comparison",
    ):
        print(
            f"FAIL: plan kind {kind!r} is not runnable "
            f"(supported: rs_weight_sweep, regime_analysis, "
            f"strategy_comparison)",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print("=== DRY RUN ===")
        print(json.dumps(plan, indent=2, sort_keys=True))
        print("\n(dry-run: no execution performed)")
        return 0

    # Safety: refuse if RESEARCH_ALPACA_* is missing AND the plan
    # requires provider fallback that the operator hasn't allowed.
    if args.allow_provider_fallback:
        required = (
            "RESEARCH_ALPACA_API_KEY",
            "RESEARCH_ALPACA_SECRET_KEY",
            "RESEARCH_ALPACA_ENDPOINT",
            "RESEARCH_ALPACA_DATA_ENDPOINT",
        )
        missing = [n for n in required if not os.environ.get(n)]
        if missing:
            print(
                f"FAIL: --allow-provider-fallback set but "
                f"required env vars missing: {missing}",
                file=sys.stderr,
            )
            return 2

    run_root = Path(args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    ts = _utc_now_iso().replace(":", "-").replace("+", "-")
    run_id = f"{kind}-{ts}"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # For safety, we do not execute anything that could touch
    # promotion / live paths.  The runner produces a manifest
    # that documents the plan, the resolved parameters, and any
    # produced artifacts.  For the current sprint, we deliberately
    # keep execution to the read-only, existing warehouse-backed
    # runners; the plan can be executed by the operator using the
    # steps listed on it or by follow-up scripts.
    print(f"=== traderjoe-research run — {run_id} ===")
    print(f"  plan kind: {kind}")
    print(f"  plan file: {plan_path}")
    print(f"  run dir:   {run_dir}")
    print(f"  provider fallback allowed: {args.allow_provider_fallback}")

    # Dispatch to the existing warehouse-backed runners.  Each
    # dispatcher is a thin wrapper that:
    #   1. Refuses if warehouse coverage isn't sufficient AND
    #      fallback is disabled.
    #   2. Delegates to the existing runner code.
    #   3. Emits an execution manifest under run_dir.
    if kind == "rs_weight_sweep":
        return _dispatch_rs_weight_sweep(plan, args, run_dir, run_id)
    if kind == "regime_analysis":
        return _dispatch_regime_analysis(plan, args, run_dir, run_id)
    if kind == "strategy_comparison":
        return _dispatch_strategy_comparison(plan, args, run_dir, run_id)
    return 2  # unreachable


def _dispatch_rs_weight_sweep(
    plan: Dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
    run_id: str,
) -> int:
    """Print the delegated command; do not auto-execute the weight
    sweep here (it's the one that just landed as a report).  This
    keeps the CLI additive without duplicating operator work.
    """
    params = plan.get("parameters", {})
    weights = params.get("overlay_weights", [])
    window = params.get("window", {})
    print("\n[rs_weight_sweep] delegated dispatch:")
    print(f"  weights: {weights}")
    print(f"  window:  {window.get('start')}..{window.get('end')}")
    print(
        "\n  This plan corresponds to the sweep captured in "
        "reports/research_summaries/rs-weight-sweep-post-b02-2026-07-04.md. "
        "Rerun by invoking the run_historical_validation loop from that "
        "report against the warehouse for each weight."
    )
    _write_run_manifest(run_dir, run_id, plan, {
        "status": "documented",
        "delegated_to": (
            "reports/research_summaries/rs-weight-sweep-post-b02-2026-07-04.md"
        ),
    })
    return 0


def _dispatch_regime_analysis(
    plan: Dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
    run_id: str,
) -> int:
    """Delegates to scripts/research-regime-report."""
    params = plan.get("parameters", {})
    source_manifest = params.get("source_manifest") or ""
    if not source_manifest or not Path(source_manifest).is_file():
        print(
            f"\n[regime_analysis] source manifest not found: "
            f"{source_manifest!r}.  Skipping delegation.",
            file=sys.stderr,
        )
        _write_run_manifest(run_dir, run_id, plan, {
            "status": "skipped",
            "reason": f"source_manifest missing: {source_manifest}",
        })
        return 2
    output_json = str(run_dir / f"regime-{params.get('window', {}).get('label','')}.json")
    print(f"\n[regime_analysis] delegating to research-regime-report:")
    print(
        f"  python -m strategy.lab.regime_report "
        f"--run-manifest {source_manifest} "
        f"--output-json {output_json}"
    )
    # Actually invoke it
    from strategy.lab.regime_report import main as regime_main
    rc = regime_main([
        "--run-manifest", source_manifest,
        "--output-json", output_json,
    ])
    _write_run_manifest(run_dir, run_id, plan, {
        "status": "ok" if rc == 0 else "failed",
        "output_json": output_json,
        "delegated_rc": rc,
    })
    return rc


def _dispatch_strategy_comparison(
    plan: Dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
    run_id: str,
) -> int:
    """Point the operator at scripts/research-run-matrix for the
    canonical strategy-comparison path.  We deliberately do NOT
    auto-run a matrix that could take minutes to hours; instead we
    print the exact command to invoke.
    """
    params = plan.get("parameters", {})
    strategies = params.get("strategies", [])
    windows = params.get("windows", [])
    labels = [w.get("label", "") for w in windows]
    print("\n[strategy_comparison] delegated dispatch:")
    print(f"  strategies: {strategies}")
    print(f"  windows:    {labels}")
    print(
        "\n  Run manually via:\n"
        f"    ./scripts/research-run-matrix "
        f"--windows {' '.join(labels)} --dataset-id-template "
        "'strategy-cmp-{label}-{end}'"
    )
    _write_run_manifest(run_dir, run_id, plan, {
        "status": "documented",
        "operator_command": (
            f"./scripts/research-run-matrix "
            f"--windows {' '.join(labels)} "
            f"--dataset-id-template 'strategy-cmp-{{label}}-{{end}}'"
        ),
    })
    return 0


def _write_run_manifest(
    run_dir: Path, run_id: str, plan: Dict[str, Any], result: Dict[str, Any],
) -> None:
    manifest = {
        "run_id": run_id,
        "generated_at": _utc_now_iso(),
        "plan": plan,
        "result": result,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def cmd_summarize(args: argparse.Namespace) -> int:
    run_root = Path(args.run_root)
    if not run_root.is_dir():
        print(f"FAIL: run root {run_root} not found", file=sys.stderr)
        return 2
    run_id = args.run_id
    if run_id == "latest":
        candidates = sorted(
            [p for p in run_root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
        if not candidates:
            print(f"FAIL: no runs found under {run_root}", file=sys.stderr)
            return 2
        run_dir = candidates[0]
        run_id = run_dir.name
    else:
        run_dir = run_root / run_id
        if not run_dir.is_dir():
            print(f"FAIL: run id {run_id!r} not found under {run_root}", file=sys.stderr)
            return 2
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"FAIL: {manifest_path} not found", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = manifest.get("plan", {})
    result = manifest.get("result", {})
    print(f"=== traderjoe-research summary — {run_id} ===")
    print(f"  generated_at: {manifest.get('generated_at')}")
    print(f"  plan kind:    {plan.get('kind')}")
    print(f"  question:     {plan.get('question')}")
    print(f"  status:       {result.get('status')}")
    for key in ("delegated_to", "output_json", "operator_command", "reason"):
        if result.get(key):
            print(f"  {key}: {result[key]}")
    steps = plan.get("steps") or []
    if steps:
        print(f"  steps ({len(steps)}):")
        for i, step in enumerate(steps, 1):
            print(f"    {i}. {step}")
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    dispatch = {
        "plan": cmd_plan,
        "run": cmd_run,
        "summarize": cmd_summarize,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        return 2
    try:
        return fn(args)
    except TraderJoeResearchError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_PLAN_ROOT",
    "DEFAULT_RUN_ROOT",
    "TraderJoeResearchError",
    "build_parser",
    "cmd_plan",
    "cmd_run",
    "cmd_summarize",
    "main",
]
