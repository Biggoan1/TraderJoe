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

__all__ = [
    "ExperimentBundle",
    "ExperimentManifest",
    "RegistryConflictError",
    "Strategy",
    "StrategyIdentity",
    "StrategyNotFoundError",
    "StrategyRegistration",
    "StrategyRegistry",
    "discover_strategies",
    "get_default_registry",
    "register",
    "run_strategy_experiment",
    "stable_parameter_hash",
]
