"""Abstract Strategy interface for the Strategy Laboratory.

Every strategy the lab evaluates conforms to this interface.  It is
deliberately narrow: identity (``name`` + ``version``), a
deterministic parameter dict, a ``stable_hash`` binding the two,
and a small evaluation surface.  The scorer itself is constructed
by ``build_evaluator(bars_by_symbol, symbols)`` — this keeps the
existing Champion / RS constructors (which want bars at
construction time) working without any modification.

Read-only guarantees enforced by source-safety tests on every
concrete strategy module: no live-runner imports, no order-path
references, no ApprovalRecord construction, no PromotionEntry
state advancement, no credential env reads.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import ComparisonEvaluator
from strategy.score_explanation import ScoreExplanation


def stable_parameter_hash(
    name: str,
    version: str,
    parameters: Mapping[str, Any],
) -> str:
    """SHA-256 over the strategy's ``name``, ``version``, and
    JSON-serialised parameters (sorted keys).  Deterministic
    across runs, insensitive to insertion order in ``parameters``.
    """
    payload = {
        "name": name,
        "version": version,
        "parameters": _canonicalize(parameters),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonicalize(value: Any) -> Any:
    """Recursively convert value into JSON-friendly primitives
    with deterministic ordering — tuples become lists, mappings
    become dicts (sorted at serialise time), non-primitives are
    stringified.
    """
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class StrategyIdentity:
    """Immutable identity block for a strategy — cheap to pass
    around, JSON-serialisable, and referenced by ExperimentRunner
    manifests.
    """

    name: str
    version: str
    parameters: Mapping[str, Any]
    stable_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "parameters": _canonicalize(self.parameters),
            "stable_hash": self.stable_hash,
        }


@runtime_checkable
class Strategy(Protocol):
    """Read-only strategy plug-in.

    Concrete strategies are typically small dataclasses that carry
    their parameter block and expose the six methods below.
    ``build_evaluator`` constructs a
    :class:`~strategy.comparison_harness.ComparisonEvaluator` bound
    to the caller-supplied bar map — the same shape the existing
    ``HistoricalValidation`` pipeline already consumes.

    Concrete implementations must:

    * be deterministic given the same ``bars_by_symbol`` and event
      sequence.
    * not read env vars, open network sockets, or open write file
      handles.
    * not enable feature flags, mutate
      :class:`~strategy.promotion_gates.PromotionEntry`, or build
      an ``ApprovalRecord``.
    """

    name: str
    version: str

    def parameters(self) -> Mapping[str, Any]:
        """Deterministic parameter dict — same input, same output."""

    def stable_hash(self) -> str:
        """SHA-256 over ``{name, version, parameters}`` in a stable
        JSON encoding.  Two strategies with equal identity yield
        the same hash across runs.
        """

    def required_history(self) -> int:
        """Minimum bar count required per symbol before the
        strategy can score.  ``ExperimentRunner`` uses this to
        decide whether the requested window is even viable.
        """

    def identity(self) -> StrategyIdentity:
        """Snapshot of the strategy's identity — name, version,
        parameters, and hash — for manifest recording.
        """

    def build_evaluator(
        self,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        """Build the :class:`ComparisonEvaluator` this strategy
        uses.  ``kwargs`` allows strategy-specific injection
        (e.g. ``rs_provider`` for RS overlays).
        """

    def score(
        self,
        event: BacktestEvent,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> Dict[str, float]:
        """Convenience: build the evaluator, evaluate one event,
        return the ``scores`` dict.
        """

    def explain(
        self,
        event: BacktestEvent,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> Dict[str, ScoreExplanation]:
        """Convenience: build the evaluator, evaluate one event,
        return the ``structured_explanations`` dict.
        """


class StrategyBase:
    """Convenience base class for concrete strategies.

    Subclasses must:

    * set ``name`` and ``version`` class attributes (or pass them
      through ``__init__``).
    * override :meth:`parameters` to return a deterministic dict
      of the strategy's config.
    * override :meth:`required_history` to return the minimum bar
      count needed.
    * override :meth:`build_evaluator` to construct the underlying
      scorer.

    The default :meth:`score` / :meth:`explain` / :meth:`identity`
    / :meth:`stable_hash` implementations delegate to those four
    methods, so most subclasses only override those four.
    """

    name: str = ""
    version: str = ""

    def parameters(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def required_history(self) -> int:
        raise NotImplementedError

    def build_evaluator(
        self,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        raise NotImplementedError

    def stable_hash(self) -> str:
        return stable_parameter_hash(
            self.name, self.version, self.parameters()
        )

    def identity(self) -> StrategyIdentity:
        return StrategyIdentity(
            name=self.name,
            version=self.version,
            parameters=_canonicalize(self.parameters()),
            stable_hash=self.stable_hash(),
        )

    def score(
        self,
        event: BacktestEvent,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> Dict[str, float]:
        evaluator = self.build_evaluator(bars_by_symbol, symbols, **kwargs)
        result: StrategyEvaluation = evaluator.evaluate(event)
        return dict(result.scores)

    def explain(
        self,
        event: BacktestEvent,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> Dict[str, ScoreExplanation]:
        evaluator = self.build_evaluator(bars_by_symbol, symbols, **kwargs)
        result: StrategyEvaluation = evaluator.evaluate(event)
        structured = getattr(result, "structured_explanations", {}) or {}
        return dict(structured)


__all__ = [
    "Strategy",
    "StrategyBase",
    "StrategyIdentity",
    "stable_parameter_hash",
]
