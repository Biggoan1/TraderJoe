"""Backtest Lab foundation data contracts.

This module defines deterministic, read-only backtest metadata and report
objects. It intentionally does not replay markets, evaluate strategies, place
orders, call broker APIs, or enable feature flags.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_OUTPUT_DIR = "reports/backtests"
DEFAULT_INITIAL_CASH = 100_000.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonicalize(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _canonicalize(value.to_dict())
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return value


def stable_json(data: Dict[str, Any]) -> str:
    """Return deterministic JSON for hashing and manifests."""
    return json.dumps(_canonicalize(data), sort_keys=True, separators=(",", ":"))


def stable_hash(data: Dict[str, Any]) -> str:
    """Return SHA-256 hash for deterministic run metadata."""
    return hashlib.sha256(stable_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BacktestConfig:
    """Deterministic experiment configuration.

    This config is intentionally explicit so runs can be reproduced from the
    manifest without relying on mutable production settings.
    """

    dataset_id: str
    strategy_id: str
    start_date: str
    end_date: str
    symbols: Tuple[str, ...] = field(default_factory=tuple)
    benchmarks: Tuple[str, ...] = ("SPY", "QQQ")
    initial_cash: float = DEFAULT_INITIAL_CASH
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    seed: int = 0
    output_dir: str = DEFAULT_OUTPUT_DIR
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "benchmarks", tuple(self.benchmarks))
        self.validate()

    def validate(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id is required")
        if not self.strategy_id:
            raise ValueError("strategy_id is required")
        if not self.start_date or not self.end_date:
            raise ValueError("start_date and end_date are required")
        if self.start_date > self.end_date:
            raise ValueError("start_date must be before or equal to end_date")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.fee_bps < 0:
            raise ValueError("fee_bps cannot be negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        if self.seed < 0:
            raise ValueError("seed cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "strategy_id": self.strategy_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "symbols": list(self.symbols),
            "benchmarks": list(self.benchmarks),
            "initial_cash": self.initial_cash,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "seed": self.seed,
            "output_dir": self.output_dir,
            "metadata": dict(self.metadata),
        }

    def stable_hash(self) -> str:
        return stable_hash(self.to_dict())


@dataclass(frozen=True)
class BacktestRunMetadata:
    """Run identity and version metadata."""

    run_id: str
    config_hash: str
    git_commit: str = "unknown"
    code_version: str = "unknown"
    created_at: str = ""
    operator: str = "unknown"
    notes: str = ""

    @classmethod
    def create(
        cls,
        config: BacktestConfig,
        git_commit: str = "unknown",
        code_version: str = "unknown",
        created_at: Optional[str] = None,
        operator: str = "unknown",
        notes: str = "",
    ) -> "BacktestRunMetadata":
        config_hash = config.stable_hash()
        return cls(
            run_id=f"bt_{config_hash[:12]}",
            config_hash=config_hash,
            git_commit=git_commit,
            code_version=code_version,
            created_at=created_at or _utc_now_iso(),
            operator=operator,
            notes=notes,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "git_commit": self.git_commit,
            "code_version": self.code_version,
            "created_at": self.created_at,
            "operator": self.operator,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class BacktestArtifactPaths:
    """Deterministic artifact locations for a run."""

    output_dir: str
    run_id: str

    @classmethod
    def for_run(
        cls,
        config: BacktestConfig,
        metadata: BacktestRunMetadata,
    ) -> "BacktestArtifactPaths":
        return cls(output_dir=config.output_dir, run_id=metadata.run_id)

    @property
    def run_dir(self) -> str:
        return str(Path(self.output_dir) / self.run_id)

    @property
    def artifacts_dir(self) -> str:
        return str(Path(self.run_dir) / "artifacts")

    @property
    def manifest_path(self) -> str:
        return str(Path(self.run_dir) / "manifest.json")

    @property
    def report_json_path(self) -> str:
        return str(Path(self.run_dir) / "report.json")

    @property
    def report_markdown_path(self) -> str:
        return str(Path(self.run_dir) / "report.md")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "artifacts_dir": self.artifacts_dir,
            "manifest_path": self.manifest_path,
            "report_json_path": self.report_json_path,
            "report_markdown_path": self.report_markdown_path,
        }


@dataclass
class BacktestEvent:
    """Single timestamped replay event for future historical replay."""

    timestamp: str
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "sequence": self.sequence,
        }


class DeterministicReplayClock:
    """Deterministic event ordering skeleton.

    This is not a full replay engine. It only provides stable ordering for
    future replay events.
    """

    def __init__(self, events: List[BacktestEvent]):
        self._events = tuple(
            sorted(
                events,
                key=lambda event: (
                    event.timestamp,
                    event.sequence,
                    event.event_type,
                ),
            )
        )

    def __iter__(self):
        return iter(self._events)

    def to_list(self) -> List[Dict[str, Any]]:
        return [event.to_dict() for event in self._events]


@dataclass
class StrategyEvaluation:
    """Read-only strategy evaluation output for replay events."""

    strategy_id: str
    event_timestamp: str
    scores: Dict[str, float] = field(default_factory=dict)
    rankings: List[Dict[str, Any]] = field(default_factory=list)
    explanations: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "event_timestamp": self.event_timestamp,
            "scores": dict(self.scores),
            "rankings": list(self.rankings),
            "explanations": dict(self.explanations),
            "warnings": list(self.warnings),
        }


class NoOpStrategyAdapter:
    """No-op strategy adapter fixture for Backtest Lab foundation tests."""

    def __init__(self, strategy_id: str = "noop"):
        self.strategy_id = strategy_id

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            warnings=["no-op adapter; no strategy behavior executed"],
        )


@dataclass
class BacktestStrategyResult:
    """Metrics and artifacts for one strategy evaluation."""

    strategy_id: str
    metrics: Dict[str, float] = field(default_factory=dict)
    trade_count: int = 0
    trades: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    data_quality: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "metrics": dict(self.metrics),
            "trade_count": self.trade_count,
            "trades": list(self.trades),
            "warnings": list(self.warnings),
            "data_quality": self.data_quality,
        }


@dataclass
class BacktestRunManifest:
    """Reproducible run manifest."""

    config: BacktestConfig
    metadata: BacktestRunMetadata
    artifacts: BacktestArtifactPaths
    status: str = "created"
    inputs: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        config: BacktestConfig,
        git_commit: str = "unknown",
        code_version: str = "unknown",
        created_at: Optional[str] = None,
        operator: str = "unknown",
        notes: str = "",
        inputs: Optional[Dict[str, Any]] = None,
    ) -> "BacktestRunManifest":
        metadata = BacktestRunMetadata.create(
            config=config,
            git_commit=git_commit,
            code_version=code_version,
            created_at=created_at,
            operator=operator,
            notes=notes,
        )
        artifacts = BacktestArtifactPaths.for_run(config, metadata)
        return cls(
            config=config,
            metadata=metadata,
            artifacts=artifacts,
            inputs=inputs or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "metadata": self.metadata.to_dict(),
            "artifacts": self.artifacts.to_dict(),
            "status": self.status,
            "inputs": dict(self.inputs),
        }

    def stable_hash(self) -> str:
        data = self.to_dict()
        data["metadata"] = {
            key: value
            for key, value in data["metadata"].items()
            if key != "created_at"
        }
        return stable_hash(data)


@dataclass
class BacktestReport:
    """Backtest report shell for future replay results."""

    manifest: BacktestRunManifest
    champion_result: Optional[BacktestStrategyResult] = None
    challenger_results: List[BacktestStrategyResult] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    status: str = "created"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "champion_result": (
                self.champion_result.to_dict()
                if self.champion_result is not None
                else None
            ),
            "challenger_results": [
                result.to_dict() for result in self.challenger_results
            ],
            "summary": dict(self.summary),
            "warnings": list(self.warnings),
            "status": self.status,
        }

    def to_json(self) -> str:
        return json.dumps(_canonicalize(self.to_dict()), indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        lines = [
            f"# Backtest Report: {self.manifest.metadata.run_id}",
            "",
            f"**Status:** {self.status}",
            f"**Dataset:** {self.manifest.config.dataset_id}",
            f"**Strategy:** {self.manifest.config.strategy_id}",
            f"**Window:** {self.manifest.config.start_date} to {self.manifest.config.end_date}",
            "",
            "Observational research only. Does not place trades or alter strategy behavior.",
        ]
        if self.warnings:
            lines.extend(["", "## Warnings"])
            lines.extend(f"- {warning}" for warning in self.warnings)
        return "\n".join(lines)


def create_backtest_report(
    config: BacktestConfig,
    git_commit: str = "unknown",
    code_version: str = "unknown",
    created_at: Optional[str] = None,
    operator: str = "unknown",
    notes: str = "",
    inputs: Optional[Dict[str, Any]] = None,
) -> BacktestReport:
    """Create an empty, reproducible BacktestReport shell."""
    manifest = BacktestRunManifest.create(
        config=config,
        git_commit=git_commit,
        code_version=code_version,
        created_at=created_at,
        operator=operator,
        notes=notes,
        inputs=inputs,
    )
    return BacktestReport(manifest=manifest)
