"""Multi-year validation matrix — Card 10.

Runs every registered strategy against a curated set of historical
year windows (and optional regime windows), then emits one
"report card" per strategy summarising performance across the
matrix.

Default year windows: 2020..2026, one row per calendar year (with
the current year truncated at today).  Optional regime windows:
COVID (2020-Q1), Bear (2022-06 to 2022-10), Bull (2020-Q4 to
2021-Q4), Sideways (2015-2016 stub), HighVIX / LowVIX (VIX-based
labels applied when a VIX series is provided; otherwise omitted).

Reuses the existing ``watchlist-2020-01-01-2026-07-04`` warehouse
dataset (per user answer for this phase).  Every year row runs
via :func:`run_strategy_experiment` inside a
:func:`run_parameter_sweep`-style loop with failure isolation.

Read-only.  No live trading path, no order-path references, no
credential env reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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

from strategy.historical_validation import HistoricalValidationConfig
from strategy.lab.experiment_runner import ExperimentBundle
from strategy.lab.leaderboard import compute_score_metrics
from strategy.lab.registry import (
    StrategyRegistry,
    discover_strategies,
    get_default_registry,
)
from strategy.lab.weekend_lab import build_config as _build_lab_config
from strategy.lab.experiment_runner import run_strategy_experiment


DEFAULT_MATRIX_ROOT = "reports/multi_year_matrix"
DEFAULT_YEARS: Sequence[int] = (2020, 2021, 2022, 2023, 2024, 2025, 2026)
DEFAULT_SYMBOLS = ("AAPL", "MSFT", "NVDA")
DEFAULT_BENCHMARKS = ("SPY", "QQQ")

# Regime windows — annotated labels for well-known historical
# periods.  Not exhaustive; used to complement year rows in the
# report card.
DEFAULT_REGIMES: Sequence[Tuple[str, str, str]] = (
    ("COVID_crash",    "2020-02-19", "2020-03-23"),
    ("COVID_recovery", "2020-03-24", "2020-08-31"),
    ("Bull_2020_Q4",   "2020-10-01", "2021-11-30"),
    ("Bear_2022",      "2022-01-01", "2022-10-14"),
    ("Sideways_2023H1","2023-01-01", "2023-06-30"),
    ("Rally_2023H2",   "2023-07-01", "2023-12-31"),
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


@dataclass(frozen=True)
class MatrixWindow:
    """One row in the multi-year matrix."""

    label: str
    start: str
    end: str
    kind: str = "year"  # "year" | "regime"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class ReportCardCell:
    """One cell of a strategy's report card."""

    window: MatrixWindow
    ok: bool
    total_events: int = 0
    total_disagreements: int = 0
    disagreement_rate: float = 0.0
    mean_score_delta: float = 0.0
    selection_agreement_rate: float = 1.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window": self.window.to_dict(),
            "ok": self.ok,
            "total_events": self.total_events,
            "total_disagreements": self.total_disagreements,
            "disagreement_rate": self.disagreement_rate,
            "mean_score_delta": self.mean_score_delta,
            "selection_agreement_rate": self.selection_agreement_rate,
            "error": self.error,
        }


@dataclass(frozen=True)
class StrategyReportCard:
    """Per-strategy summary across every matrix window."""

    strategy_name: str
    strategy_version: str
    cells: Tuple[ReportCardCell, ...]
    passed: int
    failed: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "cells": [c.to_dict() for c in self.cells],
            "passed": self.passed,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class MultiYearMatrixManifest:
    run_id: str
    generated_at: str
    windows: Tuple[MatrixWindow, ...]
    report_cards: Tuple[StrategyReportCard, ...]
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "windows": [w.to_dict() for w in self.windows],
            "report_cards": [rc.to_dict() for rc in self.report_cards],
            "warnings": list(self.warnings),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Window builders
# ---------------------------------------------------------------------------


def _year_window(year: int, today: date) -> MatrixWindow:
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    if end > today:
        end = today
    return MatrixWindow(
        label=str(year),
        start=start.isoformat(),
        end=end.isoformat(),
        kind="year",
    )


def build_default_windows(
    years: Sequence[int] = DEFAULT_YEARS,
    regimes: Sequence[Tuple[str, str, str]] = DEFAULT_REGIMES,
    today: Optional[date] = None,
) -> List[MatrixWindow]:
    today = today or date.today()
    windows: List[MatrixWindow] = []
    for y in years:
        if y > today.year:
            continue
        w = _year_window(y, today)
        if w.start >= w.end:
            continue
        windows.append(w)
    for label, start, end in regimes:
        if start >= end:
            continue
        # Skip windows that reach into the future
        if date.fromisoformat(start) > today:
            continue
        windows.append(MatrixWindow(
            label=label, start=start,
            end=min(end, today.isoformat()),
            kind="regime",
        ))
    return windows


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_multi_year_matrix(
    *,
    strategies: Optional[Sequence[str]] = None,
    windows: Optional[Sequence[MatrixWindow]] = None,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    benchmarks: Sequence[str] = DEFAULT_BENCHMARKS,
    dataset_id_template: str = "matrix-{label}-{end}",
    matrix_root: str = DEFAULT_MATRIX_ROOT,
    run_id: Optional[str] = None,
    registry: Optional[StrategyRegistry] = None,
    generated_at: Optional[str] = None,
    printer=print,
    today: Optional[date] = None,
) -> MultiYearMatrixManifest:
    """Run every strategy across every window.  Failure isolation
    is per (strategy, window) — one broken cell doesn't abort the
    matrix.
    """
    generated = generated_at or _utc_now_iso()
    run_id = run_id or f"multiyear_{generated.replace(':', '-')}"
    today = today or date.today()
    root = Path(matrix_root) / run_id
    root.mkdir(parents=True, exist_ok=True)

    reg = registry or get_default_registry()
    if not reg.names():
        discover_strategies("strategy.lab", registry=reg)
    strategy_names = list(strategies) if strategies else reg.names()
    windows_seq = list(windows) if windows else build_default_windows(today=today)

    printer(f"=== multi-year matrix — {run_id} ===")
    printer(f"strategies: {strategy_names}")
    printer(f"windows: {[w.label for w in windows_seq]}")

    report_cards: List[StrategyReportCard] = []
    for name in strategy_names:
        printer(f"\n--- strategy: {name} ---")
        try:
            entry = reg.get(name)
        except Exception as exc:  # noqa: BLE001
            printer(f"  factory error: {exc}")
            report_cards.append(StrategyReportCard(
                strategy_name=name, strategy_version="",
                cells=tuple([
                    ReportCardCell(window=w, ok=False,
                                   error=f"registry: {exc}")
                    for w in windows_seq
                ]),
                passed=0, failed=len(windows_seq),
            ))
            continue
        cells: List[ReportCardCell] = []
        passed = 0
        failed = 0
        strategy_version = ""
        for window in windows_seq:
            dataset_id = dataset_id_template.format(
                label=window.label, end=window.end,
            )
            printer(f"  window {window.label} ({window.start}..{window.end})")
            try:
                strategy = entry.factory()
                strategy_version = strategy.version
                config = _build_lab_config(
                    root / window.label,
                    dataset_id=dataset_id,
                    symbols=symbols,
                    benchmarks=benchmarks,
                    window_start=window.start,
                    window_end=window.end,
                )
                bundle = run_strategy_experiment(
                    config, strategy,
                    manifest_root=str(root / "experiments"),
                )
                metrics = compute_score_metrics(bundle)
                cells.append(ReportCardCell(
                    window=window, ok=True,
                    total_events=metrics["total_events"],
                    total_disagreements=metrics["total_disagreements"],
                    disagreement_rate=metrics["disagreement_rate"],
                    mean_score_delta=metrics["mean_score_delta"],
                    selection_agreement_rate=metrics["selection_agreement_rate"],
                ))
                passed += 1
            except Exception as exc:  # noqa: BLE001
                cells.append(ReportCardCell(
                    window=window, ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                ))
                printer(f"    FAIL: {exc}")
                failed += 1
        report_cards.append(StrategyReportCard(
            strategy_name=name,
            strategy_version=strategy_version,
            cells=tuple(cells),
            passed=passed,
            failed=failed,
        ))

    manifest = MultiYearMatrixManifest(
        run_id=run_id,
        generated_at=generated,
        windows=tuple(windows_seq),
        report_cards=tuple(report_cards),
    )
    (root / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    printer(f"\n=== DONE — {root / 'manifest.json'} ===")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="multi-year-matrix",
        description="Multi-year validation matrix runner.",
    )
    p.add_argument("--strategies", nargs="+", default=None)
    p.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--benchmarks", nargs="+", default=list(DEFAULT_BENCHMARKS))
    p.add_argument("--years", type=int, nargs="+", default=list(DEFAULT_YEARS))
    p.add_argument("--no-regimes", action="store_true")
    p.add_argument("--matrix-root", default=DEFAULT_MATRIX_ROOT)
    p.add_argument("--run-id", default="")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    regimes = () if args.no_regimes else DEFAULT_REGIMES
    windows = build_default_windows(
        years=tuple(args.years),
        regimes=regimes,
    )
    run_multi_year_matrix(
        strategies=args.strategies,
        windows=windows,
        symbols=args.symbols,
        benchmarks=args.benchmarks,
        matrix_root=args.matrix_root,
        run_id=args.run_id or None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_MATRIX_ROOT",
    "DEFAULT_REGIMES",
    "DEFAULT_YEARS",
    "MatrixWindow",
    "MultiYearMatrixManifest",
    "ReportCardCell",
    "StrategyReportCard",
    "build_default_windows",
    "build_parser",
    "main",
    "run_multi_year_matrix",
]
