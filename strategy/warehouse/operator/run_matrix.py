"""Matrix validation runner — one command, multiple historical windows.

Launched by ``scripts/research-run-matrix``.  For each configured
window, runs the offline validation pipeline and drops a
:class:`ResearchRunManifest`.  Emits a matrix summary with pass/
fail per window and continues independent windows even if one
fails.  Final exit code is non-zero iff any window failed.

Warehouse-first per window.  ``--allow-provider-fallback``
mirrors the offline runner's semantics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


DEFAULT_SYMBOLS = ("AAPL", "MSFT", "NVDA")
DEFAULT_BENCHMARKS = ("SPY", "QQQ")
DEFAULT_WINDOWS = ("60d", "90d", "6mo", "1y", "ytd")


class MatrixRunnerError(RuntimeError):
    """Raised when the matrix runner refuses to start."""


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    label: str
    start: str
    end: str

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "start": self.start, "end": self.end}


def _iso(d: date) -> str:
    return d.isoformat()


def resolve_window(label: str, today: date) -> Window:
    """Resolve a window label to concrete ISO start/end dates.

    Supported labels:

    * ``60d`` / ``90d`` / ``180d`` / ``NNd`` — trailing N calendar days
    * ``6mo`` — trailing 180 days
    * ``1y``  — trailing 365 days
    * ``2y`` etc — trailing NNN×365 days
    * ``ytd`` — 01-Jan of today's year through today
    * ``<start>..<end>`` — explicit ISO range
    """
    if ".." in label:
        parts = label.split("..", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise MatrixRunnerError(f"invalid custom window: {label!r}")
        return Window(label=label, start=parts[0], end=parts[1])
    lower = label.lower()
    if lower == "ytd":
        return Window(label="ytd", start=_iso(date(today.year, 1, 1)),
                      end=_iso(today))
    if lower.endswith("d") and lower[:-1].isdigit():
        days = int(lower[:-1])
        return Window(label=lower, start=_iso(today - timedelta(days=days)),
                      end=_iso(today))
    if lower.endswith("mo") and lower[:-2].isdigit():
        months = int(lower[:-2])
        return Window(label=lower, start=_iso(today - timedelta(days=months * 30)),
                      end=_iso(today))
    if lower.endswith("y") and lower[:-1].isdigit():
        years = int(lower[:-1])
        return Window(label=lower, start=_iso(today - timedelta(days=years * 365)),
                      end=_iso(today))
    raise MatrixRunnerError(f"unknown window: {label!r}")


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixRow:
    window: Window
    ok: bool
    error: str = ""
    disagreements: int = 0
    provider_contacted: bool = False
    provenance_source: str = ""
    promotion_state: str = "disabled"
    run_manifest_path: str = ""
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window": self.window.to_dict(),
            "ok": self.ok,
            "error": self.error,
            "disagreements": self.disagreements,
            "provider_contacted": self.provider_contacted,
            "provenance_source": self.provenance_source,
            "promotion_state": self.promotion_state,
            "run_manifest_path": self.run_manifest_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class MatrixSummary:
    rows: Tuple[MatrixRow, ...]
    total: int
    passed: int
    failed: int
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-run-matrix",
        description="Run offline validation across multiple windows.",
    )
    parser.add_argument(
        "--windows", nargs="+", default=list(DEFAULT_WINDOWS),
    )
    parser.add_argument(
        "--symbols", nargs="+", default=list(DEFAULT_SYMBOLS),
    )
    parser.add_argument(
        "--benchmarks", nargs="+", default=list(DEFAULT_BENCHMARKS),
    )
    parser.add_argument(
        "--dataset-id-template",
        default="matrix-{label}-{end}",
        help="dataset id template; substitutes {label} and {end}",
    )
    parser.add_argument("--report-root", default="reports/validation")
    parser.add_argument("--research-data-root", default="research_data")
    parser.add_argument(
        "--run-manifest-root", default="reports/run_manifests",
    )
    parser.add_argument(
        "--allow-provider-fallback", action="store_true",
    )
    parser.add_argument("--with-llm", action="store_true")
    parser.add_argument("--model", default="")
    return parser


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _default_run_offline():
    # Lazy import so tests can inject a mock.
    from strategy.warehouse.operator.validate_offline import run as run_offline
    return run_offline


def _default_build_manifest():
    from strategy.research_run_manifest import build_from_bundle
    return build_from_bundle


def _default_write_manifest():
    from strategy.research_run_manifest import write as write_manifest
    return write_manifest


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def run(
    args: argparse.Namespace,
    env: Optional[Dict[str, str]] = None,
    today: Optional[date] = None,
    run_offline=None,
    build_manifest=None,
    write_manifest=None,
    printer=print,
) -> MatrixSummary:
    env = env if env is not None else dict(os.environ)
    today = today or date.today()
    run_offline = run_offline or _default_run_offline()
    build_manifest = build_manifest or _default_build_manifest()
    write_manifest = write_manifest or _default_write_manifest()

    printer("=== research-run-matrix ===")
    printer(f"today: {today.isoformat()}")
    printer(f"windows: {args.windows}")
    printer(f"symbols: {args.symbols}")
    printer(f"benchmarks: {args.benchmarks}")
    printer(f"allow_provider_fallback: {args.allow_provider_fallback}")

    rows: List[MatrixRow] = []
    passed = 0
    failed = 0

    for label in args.windows:
        try:
            window = resolve_window(label, today)
        except MatrixRunnerError as exc:
            rows.append(
                MatrixRow(
                    window=Window(label=label, start="", end=""),
                    ok=False,
                    error=str(exc),
                )
            )
            failed += 1
            continue

        started_at = _utc_now_iso()
        dataset_id = args.dataset_id_template.format(
            label=window.label.replace("..", "-"),
            end=window.end,
        )
        # Build namespace for the offline runner
        offline_ns = argparse.Namespace(
            dataset_id=dataset_id,
            start=window.start,
            end=window.end,
            symbols=list(args.symbols),
            benchmarks=list(args.benchmarks),
            report_root=str(
                Path(args.report_root) / dataset_id
            ),
            research_data_root=str(
                Path(args.research_data_root) / dataset_id
            ),
            allow_provider_fallback=args.allow_provider_fallback,
            with_llm=args.with_llm,
            model=args.model,
        )
        printer(f"\n--- window {window.label} ({window.start}..{window.end}) ---")
        try:
            bundle = run_offline(
                offline_ns, env=env, printer=printer,
            )
        except Exception as exc:  # noqa: BLE001 — isolate window failure
            failed += 1
            rows.append(
                MatrixRow(
                    window=window,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    started_at=started_at,
                    completed_at=_utc_now_iso(),
                )
            )
            printer(f"FAIL: {exc}")
            continue

        # Build + write ResearchRunManifest
        run_id = f"matrix-{dataset_id}-{started_at.replace(':','-')}"
        manifest = build_manifest(
            bundle,
            run_id=run_id,
            started_at=started_at,
            completed_at=_utc_now_iso(),
        )
        written = write_manifest(
            manifest, Path(args.run_manifest_root) / dataset_id,
        )
        rows.append(
            MatrixRow(
                window=window,
                ok=True,
                disagreements=len(bundle.comparison.disagreements),
                provider_contacted=(
                    bundle.dataset_provenance.get("source") == "provider"
                ),
                provenance_source=bundle.dataset_provenance.get("source", ""),
                promotion_state=bundle.promotion_entry.current_state,
                run_manifest_path=str(written.get("json", "")),
                started_at=started_at,
                completed_at=manifest.completed_at,
            )
        )
        passed += 1

    summary = MatrixSummary(
        rows=tuple(rows),
        total=len(rows),
        passed=passed,
        failed=failed,
        generated_at=_utc_now_iso(),
    )
    printer("\n=== matrix summary ===")
    printer(f"total: {summary.total}  passed: {summary.passed}  failed: {summary.failed}")
    for row in summary.rows:
        status = "PASS" if row.ok else "FAIL"
        printer(
            f"  {status} {row.window.label} "
            f"(start={row.window.start} end={row.window.end}) "
            f"disagreements={row.disagreements} "
            f"provider_contacted={row.provider_contacted}"
        )
        if not row.ok and row.error:
            printer(f"    error: {row.error}")
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run(args)
    except MatrixRunnerError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
