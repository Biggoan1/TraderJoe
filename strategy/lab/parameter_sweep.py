"""Parameter sweeps — Card 5.

Grid-sweeps a strategy factory over every combination of a
declared parameter grid, runs an experiment per combo, and
returns one :class:`ExperimentBundle` per combo plus a
:class:`SweepManifest` summarising the run.

Grid semantics: ``ParameterGrid({"lookback": [10, 20, 30],
"weight": [0.1, 0.2]})`` yields the Cartesian product — 6 combos
in that example.  Combos are enumerated in a deterministic order
(sorted axis names, positional value order preserved).

Read-only: no live trading path, no order-path references, no
credential env reads.  Every experiment inherits the read-only
guarantees of :func:`run_strategy_experiment`.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.historical_validation import HistoricalValidationConfig
from strategy.lab.experiment_runner import (
    ExperimentBundle,
    run_strategy_experiment,
)
from strategy.lab.strategy import Strategy


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


class SweepError(RuntimeError):
    """Base class for parameter-sweep errors."""


@dataclass(frozen=True)
class ParameterGrid:
    """Grid of parameter values.  Iteration order:

    * Axes sorted alphabetically by name (deterministic).
    * Within each axis, values kept in the caller's insertion
      order (deterministic per Python 3.7+ dict semantics).

    ``ParameterGrid({}).combos()`` yields a single empty combo,
    so a caller who wants "run the strategy once with defaults"
    can still use this API.
    """

    axes: Mapping[str, Sequence[Any]]

    def __post_init__(self) -> None:
        for name, values in self.axes.items():
            if not isinstance(name, str) or not name:
                raise SweepError(f"invalid axis name: {name!r}")
            if not isinstance(values, (list, tuple)) or len(values) == 0:
                raise SweepError(
                    f"axis {name!r} must be a non-empty list/tuple"
                )

    def combos(self) -> List[Dict[str, Any]]:
        if not self.axes:
            return [{}]
        names = sorted(self.axes.keys())
        value_lists = [list(self.axes[n]) for n in names]
        return [
            dict(zip(names, combo))
            for combo in itertools.product(*value_lists)
        ]

    def size(self) -> int:
        if not self.axes:
            return 1
        n = 1
        for values in self.axes.values():
            n *= len(values)
        return n

    def to_dict(self) -> Dict[str, Any]:
        return {name: list(values) for name, values in sorted(self.axes.items())}


@dataclass(frozen=True)
class SweepRow:
    """One row of the sweep result — the parameter combo, the
    resulting experiment bundle, and a per-combo warning list.
    """

    combo: Mapping[str, Any]
    bundle: Optional[ExperimentBundle] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.bundle is not None and not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "combo": dict(self.combo),
            "ok": self.ok,
            "error": self.error,
            "experiment_id": (
                self.bundle.manifest.experiment_id if self.bundle else ""
            ),
            "manifest_path": (
                self.bundle.manifest_path if self.bundle else ""
            ),
        }


@dataclass(frozen=True)
class SweepManifest:
    """Manifest binding one grid to its result rows."""

    sweep_id: str
    strategy_name: str
    grid: ParameterGrid
    rows: Tuple[SweepRow, ...]
    total: int
    passed: int
    failed: int
    generated_at: str
    dataset_id: str
    window_start: str
    window_end: str
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sweep_id": self.sweep_id,
            "strategy_name": self.strategy_name,
            "grid": self.grid.to_dict(),
            "rows": [r.to_dict() for r in self.rows],
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "generated_at": self.generated_at,
            "dataset_id": self.dataset_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "warnings": list(self.warnings),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


StrategyFactory = Callable[..., Strategy]


def run_parameter_sweep(
    strategy_factory: StrategyFactory,
    grid: ParameterGrid,
    config: HistoricalValidationConfig,
    *,
    sweep_id: Optional[str] = None,
    research_client: Any = None,
    warehouse_reader: Any = None,
    llm_client: Any = None,
    manifest_root: str = "reports/sweeps",
    write_row_manifests: bool = True,
    write_sweep_manifest: bool = True,
    generated_at: Optional[str] = None,
    isolate_failures: bool = True,
) -> SweepManifest:
    """Run one experiment per grid combo.

    ``strategy_factory(**combo)`` is called once per combo to
    construct the strategy under test.  Each experiment writes its
    own manifest into ``<manifest_root>/<sweep_id>/rows/`` when
    ``write_row_manifests`` is True; the aggregate sweep manifest
    is written into ``<manifest_root>/<sweep_id>/sweep.json`` when
    ``write_sweep_manifest`` is True.

    ``isolate_failures=True`` (default): a failing combo becomes a
    ``SweepRow(error=...)`` and the sweep continues.  Set to
    ``False`` to re-raise on the first failure.
    """
    generated = generated_at or _utc_now_iso()
    sweep_id = sweep_id or f"sweep_{config.dataset_id}_{generated.replace(':', '-')}"
    combos = grid.combos()

    sweep_root = Path(manifest_root) / sweep_id
    if write_row_manifests or write_sweep_manifest:
        sweep_root.mkdir(parents=True, exist_ok=True)
    row_root = sweep_root / "rows"

    rows: List[SweepRow] = []
    passed = 0
    failed = 0
    strategy_name = ""
    for combo in combos:
        try:
            strategy = strategy_factory(**combo)
        except Exception as exc:  # noqa: BLE001 — combo-level failure
            failed += 1
            rows.append(SweepRow(combo=combo,
                                 error=f"factory: {type(exc).__name__}: {exc}"))
            if not isolate_failures:
                raise
            continue
        if not strategy_name:
            strategy_name = strategy.name
        try:
            bundle = run_strategy_experiment(
                config,
                strategy,
                research_client=research_client,
                warehouse_reader=warehouse_reader,
                llm_client=llm_client,
                generated_at=generated,
                manifest_root=str(row_root),
                write_manifest=write_row_manifests,
            )
        except Exception as exc:  # noqa: BLE001 — combo-level failure
            failed += 1
            rows.append(SweepRow(combo=combo,
                                 error=f"experiment: {type(exc).__name__}: {exc}"))
            if not isolate_failures:
                raise
            continue
        passed += 1
        rows.append(SweepRow(combo=combo, bundle=bundle))

    manifest = SweepManifest(
        sweep_id=sweep_id,
        strategy_name=strategy_name,
        grid=grid,
        rows=tuple(rows),
        total=len(rows),
        passed=passed,
        failed=failed,
        generated_at=generated,
        dataset_id=config.dataset_id,
        window_start=config.window_start,
        window_end=config.window_end,
    )
    if write_sweep_manifest:
        (sweep_root / "sweep.json").write_text(
            manifest.to_json(), encoding="utf-8"
        )
    return manifest


__all__ = [
    "ParameterGrid",
    "SweepError",
    "SweepManifest",
    "SweepRow",
    "run_parameter_sweep",
]
