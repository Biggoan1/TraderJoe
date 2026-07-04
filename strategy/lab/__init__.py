"""Strategy Laboratory (Phase 5.8).

Research-only strategy plug-in framework.  Modules here define the
abstract :class:`Strategy` interface and concrete adapters wrapping
existing Champion / RS scorers so they can plug into the
experiment runner, registry, and leaderboard.

Everything under this package is read-only, disabled-by-default,
never touches a live trading path, never enables a feature flag,
never advances a PromotionEntry, and never constructs an
ApprovalRecord.  All strategies here are for
validation / replay / research only — never "training."
"""

from __future__ import annotations

from strategy.lab.strategy import (
    Strategy,
    StrategyIdentity,
    stable_parameter_hash,
)
from strategy.lab.experiment_runner import (
    ExperimentBundle,
    ExperimentManifest,
    run_strategy_experiment,
)
from strategy.lab.registry import (
    RegistryConflictError,
    StrategyNotFoundError,
    StrategyRegistration,
    StrategyRegistry,
    discover_strategies,
    get_default_registry,
    register,
)
from strategy.lab.leaderboard import (
    Leaderboard,
    LeaderboardEntry,
    compute_score_metrics,
    entry_from_bundle,
    rank_leaderboard,
    write_leaderboard,
)
from strategy.lab.parameter_sweep import (
    ParameterGrid,
    SweepManifest,
    SweepRow,
    run_parameter_sweep,
)
from strategy.lab.hypothesis_queue import (
    ExperimentProposal,
    HypothesisQueue,
)
from strategy.lab.performance_metrics import (
    HoldingPeriod,
    PerformanceMetrics,
    build_equity_curve,
    compute_performance_metrics,
)

__all__ = [
    "ExperimentBundle",
    "ExperimentManifest",
    "ExperimentProposal",
    "HoldingPeriod",
    "HypothesisQueue",
    "Leaderboard",
    "LeaderboardEntry",
    "ParameterGrid",
    "PerformanceMetrics",
    "RegistryConflictError",
    "Strategy",
    "StrategyIdentity",
    "StrategyNotFoundError",
    "StrategyRegistration",
    "StrategyRegistry",
    "SweepManifest",
    "SweepRow",
    "build_equity_curve",
    "compute_performance_metrics",
    "compute_score_metrics",
    "discover_strategies",
    "entry_from_bundle",
    "get_default_registry",
    "rank_leaderboard",
    "register",
    "run_parameter_sweep",
    "run_strategy_experiment",
    "stable_parameter_hash",
    "write_leaderboard",
]
