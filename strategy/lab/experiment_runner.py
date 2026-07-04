"""Experiment runner — Card 2.

``run_strategy_experiment`` replays a
:class:`~strategy.historical_validation.HistoricalValidationConfig`
with the *challenger* slot replaced by an injected
:class:`~strategy.lab.strategy.Strategy`.  Champion stays the
research-only baseline scorer.

Outputs match ``HistoricalValidation``:

* comparison (deterministic Champion vs Strategy score diff)
* walk-forward research report
* learning report
* analyst report (when ``llm_client`` supplied)
* dataset manifest
* :class:`ExperimentManifest` binding strategy identity + bundle

Never touches a live trading path, never enables a feature flag,
never advances :class:`PromotionEntry`, never constructs an
``ApprovalRecord``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from strategy.config import FeatureFlags
from strategy.historical_validation import (
    HistoricalValidationBundle,
    HistoricalValidationConfig,
    run_historical_validation,
)
from strategy.lab.champion_rs import ChampionRelativeStrengthStrategy
from strategy.lab.strategy import Strategy, StrategyIdentity
from strategy.rs_challenger import rs_provider_from_map


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


@dataclass(frozen=True)
class ExperimentManifest:
    """Provenance record binding one strategy identity to one
    validation bundle.  Emitted alongside the standard artifacts.
    """

    experiment_id: str
    dataset_id: str
    strategy: StrategyIdentity
    seed: int
    window_start: str
    window_end: str
    symbols: Sequence[str]
    benchmarks: Sequence[str]
    config_hash: str
    generated_at: str
    comparison_report_id: str
    walk_forward_report_id: str
    learning_report_id: str
    analyst_report_ids: Sequence[str]
    warnings: Sequence[str]
    live_fetch_used: bool
    dataset_provenance: Mapping[str, Any]
    promotion_state: str
    promotion_approvals: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "dataset_id": self.dataset_id,
            "strategy": self.strategy.to_dict(),
            "seed": self.seed,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "symbols": list(self.symbols),
            "benchmarks": list(self.benchmarks),
            "config_hash": self.config_hash,
            "generated_at": self.generated_at,
            "comparison_report_id": self.comparison_report_id,
            "walk_forward_report_id": self.walk_forward_report_id,
            "learning_report_id": self.learning_report_id,
            "analyst_report_ids": list(self.analyst_report_ids),
            "warnings": list(self.warnings),
            "live_fetch_used": self.live_fetch_used,
            "dataset_provenance": dict(self.dataset_provenance),
            "promotion_state": self.promotion_state,
            "promotion_approvals": self.promotion_approvals,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


@dataclass(frozen=True)
class ExperimentBundle:
    """Wraps a HistoricalValidationBundle with the ExperimentManifest
    that names the strategy under test.
    """

    manifest: ExperimentManifest
    validation_bundle: HistoricalValidationBundle
    manifest_path: str = ""


def _make_challenger_factory(strategy: Strategy):
    """Return a ``challenger_factory`` callable that
    :func:`run_historical_validation` can invoke to build the
    strategy under test.

    The factory receives the constructed champion (used as base
    evaluator for overlay-style strategies), the bar map, symbol
    list, rs_map, and local_flags.  Overlay strategies like
    ``ChampionRelativeStrengthStrategy`` build their own base and
    ignore the passed champion.
    """
    def factory(
        *,
        champion: Any,
        bars_by_symbol: Mapping[str, Any],
        symbols: Sequence[str],
        rs_map: Mapping[str, Any],
        local_flags: FeatureFlags,
    ) -> Any:
        kwargs: Dict[str, Any] = {}
        if isinstance(strategy, ChampionRelativeStrengthStrategy):
            kwargs["rs_provider"] = rs_provider_from_map(rs_map)
            kwargs["flags"] = local_flags
        return strategy.build_evaluator(
            bars_by_symbol, symbols, **kwargs
        )
    return factory


def run_strategy_experiment(
    config: HistoricalValidationConfig,
    strategy: Strategy,
    *,
    research_client: Any = None,
    llm_client: Any = None,
    warehouse_reader: Any = None,
    generated_at: Optional[str] = None,
    manifest_root: str = "reports/experiments",
    write_manifest: bool = True,
) -> ExperimentBundle:
    """Replay the given config with ``strategy`` in the challenger
    slot.

    ``config.challenger_id`` is respected for reporting IDs but
    the actual scorer used is ``strategy.build_evaluator``.

    Deterministic: identical inputs produce identical outputs.
    Never enables feature flags; never constructs an
    ``ApprovalRecord``; the returned bundle's ``promotion_entry``
    stays at ``current_state = "disabled"``.
    """
    generated = generated_at or _utc_now_iso()
    challenger_factory = _make_challenger_factory(strategy)
    bundle = run_historical_validation(
        config,
        research_client=research_client,
        llm_client=llm_client,
        warehouse_reader=warehouse_reader,
        generated_at=generated,
        challenger_factory=challenger_factory,
    )

    identity = strategy.identity()
    experiment_id = (
        f"exp_{config.dataset_id}_{identity.name}_{identity.stable_hash[:12]}"
    )
    manifest = ExperimentManifest(
        experiment_id=experiment_id,
        dataset_id=config.dataset_id,
        strategy=identity,
        seed=config.seed,
        window_start=config.window_start,
        window_end=config.window_end,
        symbols=tuple(config.symbols),
        benchmarks=tuple(config.benchmarks),
        config_hash=config.stable_hash(),
        generated_at=generated,
        comparison_report_id=bundle.comparison_report.report_id,
        walk_forward_report_id=bundle.walk_forward_research_report.report_id,
        learning_report_id=bundle.learning_report.report_id,
        analyst_report_ids=tuple(
            r.report_id for r in bundle.analyst_reports
        ),
        warnings=tuple(bundle.warnings),
        live_fetch_used=bundle.live_fetch_used,
        dataset_provenance=dict(bundle.dataset_provenance),
        promotion_state=bundle.promotion_entry.current_state,
        promotion_approvals=len(bundle.promotion_entry.approvals),
    )

    manifest_path = ""
    if write_manifest:
        root = Path(manifest_root) / config.dataset_id
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{experiment_id}.json"
        target.write_text(manifest.to_json(), encoding="utf-8")
        manifest_path = str(target)

    return ExperimentBundle(
        manifest=manifest,
        validation_bundle=bundle,
        manifest_path=manifest_path,
    )


__all__ = [
    "ExperimentBundle",
    "ExperimentManifest",
    "run_strategy_experiment",
]
