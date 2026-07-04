"""Offline validation runner — warehouse-first, no provider unless
explicitly allowed.

Launched by ``scripts/research-validate-offline``.  Consumes only
the local warehouse via :class:`WarehouseReader`; the operator
must pass ``--allow-provider-fallback`` to permit a
``ResearchAccountClient`` fetch when coverage is incomplete.

Prints:

* whether the provider was contacted
* dataset provenance
* disagreement count and top 10 disagreements
* PromotionEntry state
* feature-flag state

PromotionEntry is asserted to stay ``disabled``.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from strategy.config import get_feature_flags
from strategy.historical_validation import (
    HistoricalValidationBundle,
    HistoricalValidationConfig,
    LiveFetchNotAvailableError,
    run_historical_validation,
)
from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import AssetClass, BarInterval
from strategy.promotion_gates import STATE_DISABLED
from strategy.warehouse.research_cache import WarehouseReader


DEFAULT_SYMBOLS = ("AAPL", "MSFT", "NVDA")
DEFAULT_BENCHMARKS = ("SPY", "QQQ")


class OfflineValidationError(RuntimeError):
    """Raised when the offline runner refuses to proceed."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-validate-offline",
        description=(
            "Run a historical validation from the warehouse.  "
            "Refuses to contact the provider unless "
            "--allow-provider-fallback is passed."
        ),
    )
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(DEFAULT_SYMBOLS),
    )
    parser.add_argument(
        "--benchmarks",
        nargs="+",
        default=list(DEFAULT_BENCHMARKS),
    )
    parser.add_argument(
        "--report-root", default="reports/validation",
    )
    parser.add_argument(
        "--research-data-root", default="research_data",
    )
    parser.add_argument(
        "--allow-provider-fallback",
        action="store_true",
        help=(
            "permit the pipeline to consult the provider when "
            "warehouse coverage is incomplete (default: refuse)"
        ),
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="run analyst narratives (requires an LLM endpoint)",
    )
    parser.add_argument("--model", default="")
    return parser


def _build_llm_client(env: Dict[str, str], model: str = "") -> Any:
    from strategy.research_analyst import LocalLLMClient
    endpoint = env.get("RESEARCH_LLM_ENDPOINT", "")
    resolved_model = model or env.get("RESEARCH_AI_MODEL", "")
    if not endpoint or not resolved_model:
        raise OfflineValidationError(
            "--with-llm requires RESEARCH_LLM_ENDPOINT and "
            "RESEARCH_AI_MODEL (or --model) in env"
        )
    return LocalLLMClient(
        endpoint=endpoint, model=resolved_model, timeout=1200.0
    )


def _refuse_provider_env(env: Dict[str, str]) -> Dict[str, str]:
    """Strip RESEARCH_ALPACA_* out of the runtime env so any
    accidental ResearchAccountClient construction downstream would
    fail fast.
    """
    cleaned = dict(env)
    for name in list(cleaned):
        if name.startswith("RESEARCH_ALPACA_"):
            cleaned.pop(name, None)
    return cleaned


def run(
    args: argparse.Namespace,
    env: Optional[Dict[str, str]] = None,
    warehouse_reader: Optional[WarehouseReader] = None,
    layout: Optional[WarehouseLayout] = None,
    research_client: Any = None,
    llm_client: Any = None,
    now_iso: Optional[str] = None,
    printer=print,
) -> HistoricalValidationBundle:
    env = env if env is not None else dict(os.environ)
    now = now_iso or datetime.now(timezone.utc).isoformat()

    if args.start > args.end:
        raise OfflineValidationError(
            f"--start ({args.start}) must be <= --end ({args.end})"
        )

    if layout is None:
        layout = WarehouseLayout.from_env(env)

    close_reader = False
    if warehouse_reader is None:
        warehouse_reader = WarehouseReader(layout=layout)
        close_reader = True

    symbols = list(args.symbols)
    benchmarks = list(args.benchmarks)
    symbols_needed = symbols + benchmarks

    printer("=== research-validate-offline ===")
    printer(f"generated_at: {now}")
    printer(f"warehouse root: {layout.root}")
    printer(f"dataset_id: {args.dataset_id}")
    printer(f"window: {args.start} .. {args.end}")
    printer(f"symbols: {symbols}")
    printer(f"benchmarks: {benchmarks}")
    printer(f"allow_provider_fallback: {args.allow_provider_fallback}")

    complete = warehouse_reader.has_complete_coverage(
        asset_class=AssetClass.EQUITY,
        interval=BarInterval.DAILY,
        symbols=symbols_needed,
        start=args.start,
        end=args.end,
    )
    printer(f"warehouse_coverage_complete: {complete}")

    if not complete and not args.allow_provider_fallback:
        raise OfflineValidationError(
            "warehouse coverage is incomplete for the requested "
            "symbols × window; pass --allow-provider-fallback to "
            "permit provider fetches, or import the missing bars "
            "first via scripts/research-import-watchlist"
        )

    if not args.allow_provider_fallback:
        # Belt-and-braces: strip provider credentials from the env
        # exposed to downstream code.
        _refuse_provider_env(env)
        research_client = None

    if args.with_llm and llm_client is None:
        llm_client = _build_llm_client(env, args.model)

    config = HistoricalValidationConfig(
        dataset_id=args.dataset_id,
        symbols=tuple(symbols),
        benchmarks=tuple(benchmarks),
        window_start=args.start,
        window_end=args.end,
        research_data_root=args.research_data_root,
        report_root=args.report_root,
        live_fetch=True,
    )

    try:
        bundle = run_historical_validation(
            config,
            research_client=research_client,
            warehouse_reader=warehouse_reader,
            llm_client=llm_client,
            generated_at=now,
        )
    finally:
        if close_reader:
            warehouse_reader.close()

    _report(bundle, printer, provider_permitted=args.allow_provider_fallback)
    return bundle


def _report(
    bundle: HistoricalValidationBundle,
    printer,
    provider_permitted: bool,
) -> None:
    provenance = bundle.dataset_provenance
    source = provenance.get("source", "")
    printer("\n=== provenance ===")
    printer(f"source: {source}")
    printer(f"provider_permitted: {provider_permitted}")
    printer(f"provider_contacted: {source == 'provider'}")
    if source == "warehouse":
        datasets = provenance.get("datasets", [])
        for ds in datasets:
            printer(
                f"  {ds.get('dataset_id','')}@{ds.get('version','')}"
            )

    comparison = bundle.comparison
    disagreements = list(comparison.disagreements)
    disagreements.sort(
        key=lambda d: abs(d.score_delta or 0.0), reverse=True
    )
    printer("\n=== disagreements ===")
    printer(f"total: {len(disagreements)}")
    for i, d in enumerate(disagreements[:10], 1):
        printer(
            f"  [{i}] {d.event_timestamp} {d.symbol:6s} "
            f"kind={d.kind} score_delta={d.score_delta}"
        )

    printer("\n=== promotion state ===")
    printer(f"current_state: {bundle.promotion_entry.current_state}")
    printer(f"approvals: {len(bundle.promotion_entry.approvals)}")
    assert bundle.promotion_entry.current_state == STATE_DISABLED, (
        "PromotionEntry must remain disabled"
    )
    assert bundle.promotion_entry.approvals == [], (
        "no ApprovalRecord may be constructed"
    )

    flags = get_feature_flags()
    printer("\n=== feature flags ===")
    printer(f"all_disabled: {flags.all_disabled}")
    printer(f"enabled: {flags.enabled_flags}")

    printer("\n=== reports ===")
    printer(f"comparison: {bundle.comparison_report.report_id}")
    printer(f"walk_forward: {bundle.walk_forward_report.report_id}")
    printer(f"learning: {bundle.learning_report.report_id}")
    if bundle.analyst_reports:
        for r in bundle.analyst_reports:
            printer(f"analyst: {r.report_id}")
    printer("\n=== OFFLINE VALIDATION COMPLETE ===")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except OfflineValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except LiveFetchNotAvailableError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
