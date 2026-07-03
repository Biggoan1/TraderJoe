"""Relative Strength Challenger Overlay.

Read-only, disabled-by-default Challenger adapter that wraps a base
strategy evaluator (typically Champion) and applies a Relative Strength
composite-score overlay for Champion / Challenger comparison inside the
Backtest Lab.

Contracts:

* Satisfies :class:`strategy.comparison_harness.ComparisonEvaluator`.
* When the ``enable_relative_strength`` feature flag on the configured
  :class:`strategy.config.FeatureFlags` instance is ``False``, the
  overlay is a passthrough — scores, rankings, and explanations are
  identical to the base evaluator.  Only the ``strategy_id`` label
  differs.
* When the flag is ``True``, symbols receive an additive contribution
  proportional to ``(rs_score - neutral_score) / score_range``, clipped
  to the ``[neutral - range, neutral + range]`` window and scaled by the
  configured ``overlay_weight``.
* Symbols without RS data keep their base score and are logged in a
  warning; the wrapper never raises on missing or malformed provider
  responses.
* The wrapper never places orders, never mutates feature flags, never
  imports order-path modules, and never wires the historical validation
  paper account.  All RS values are supplied by the caller via a
  provider callable (see :func:`rs_provider_from_map`).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import ComparisonEvaluator
from strategy.config import FeatureFlags, get_feature_flags


RS_CHALLENGER_STRATEGY_ID = "rs-challenger-v0.1.0"
RS_CHALLENGER_FLAG_NAME = "enable_relative_strength"

DEFAULT_RS_OVERLAY_WEIGHT = 0.20
DEFAULT_RS_NEUTRAL_SCORE = 50.0
DEFAULT_RS_SCORE_RANGE = 50.0


class RelativeStrengthProvider(Protocol):
    """Callable returning an RS composite score for one symbol at one event.

    Implementations must return ``None`` when RS data is unavailable and a
    float in the ``[neutral - range, neutral + range]`` window (default
    ``[0, 100]``) otherwise.  Providers should be deterministic — the
    Backtest Lab replays events in stable order, and non-deterministic
    providers break reproducibility.
    """

    def __call__(
        self, symbol: str, event: BacktestEvent
    ) -> Optional[float]: ...


class RelativeStrengthChallenger:
    """Disabled-by-default RS overlay wrapping a base evaluator."""

    def __init__(
        self,
        base_evaluator: ComparisonEvaluator,
        rs_provider: RelativeStrengthProvider,
        flags: Optional[FeatureFlags] = None,
        strategy_id: str = RS_CHALLENGER_STRATEGY_ID,
        overlay_weight: float = DEFAULT_RS_OVERLAY_WEIGHT,
        neutral_score: float = DEFAULT_RS_NEUTRAL_SCORE,
        score_range: float = DEFAULT_RS_SCORE_RANGE,
    ):
        if overlay_weight < 0:
            raise ValueError("overlay_weight cannot be negative")
        if score_range <= 0:
            raise ValueError("score_range must be positive")
        if not strategy_id:
            raise ValueError("strategy_id is required")
        self._base = base_evaluator
        self._rs_provider = rs_provider
        self._flags = flags if flags is not None else get_feature_flags()
        self.strategy_id = strategy_id
        self.overlay_weight = float(overlay_weight)
        self.neutral_score = float(neutral_score)
        self.score_range = float(score_range)

    @property
    def base_strategy_id(self) -> str:
        return self._base.strategy_id

    @property
    def is_enabled(self) -> bool:
        return bool(getattr(self._flags, RS_CHALLENGER_FLAG_NAME, False))

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        base = self._base.evaluate(event)
        if not self.is_enabled:
            return self._passthrough(base)
        return self._apply_overlay(base, event)

    # -- internals ----------------------------------------------------------

    def _passthrough(self, base: StrategyEvaluation) -> StrategyEvaluation:
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=base.event_timestamp,
            scores=dict(base.scores),
            rankings=[dict(entry) for entry in base.rankings],
            explanations=dict(base.explanations),
            warnings=list(base.warnings),
        )

    def _apply_overlay(
        self, base: StrategyEvaluation, event: BacktestEvent
    ) -> StrategyEvaluation:
        new_scores: Dict[str, float] = {}
        explanations: Dict[str, str] = dict(base.explanations)
        warnings: List[str] = list(base.warnings)
        missing: List[str] = []

        for symbol in sorted(base.scores):
            base_score = base.scores[symbol]
            rs_value = self._safe_fetch(symbol, event, warnings)
            if rs_value is None:
                new_scores[symbol] = base_score
                missing.append(symbol)
                explanations[symbol] = self._explanation(
                    base.explanations.get(symbol, ""),
                    base_score=base_score,
                    rs_value=None,
                    contribution=0.0,
                )
                continue
            contribution = self._contribution(rs_value)
            new_scores[symbol] = base_score + contribution
            explanations[symbol] = self._explanation(
                base.explanations.get(symbol, ""),
                base_score=base_score,
                rs_value=rs_value,
                contribution=contribution,
            )

        if missing:
            warnings.append(
                "rs data missing for "
                f"{len(missing)} symbol(s): {', '.join(missing)}"
            )

        rankings = self._rerank(base, new_scores)

        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=base.event_timestamp,
            scores=new_scores,
            rankings=rankings,
            explanations=explanations,
            warnings=warnings,
        )

    def _safe_fetch(
        self,
        symbol: str,
        event: BacktestEvent,
        warnings: List[str],
    ) -> Optional[float]:
        try:
            value = self._rs_provider(symbol, event)
        except Exception as exc:  # noqa: BLE001 -- provider errors are per-symbol
            warnings.append(f"rs_provider error for {symbol}: {exc}")
            return None
        if value is None:
            return None
        return float(value)

    def _contribution(self, rs_value: float) -> float:
        low = self.neutral_score - self.score_range
        high = self.neutral_score + self.score_range
        clamped = max(low, min(high, rs_value))
        return self.overlay_weight * (
            (clamped - self.neutral_score) / self.score_range
        )

    def _explanation(
        self,
        base_explanation: str,
        base_score: float,
        rs_value: Optional[float],
        contribution: float,
    ) -> str:
        parts: List[str] = []
        if base_explanation:
            parts.append(f"base: {base_explanation}")
        if rs_value is None:
            parts.append(
                "rs: unavailable "
                f"(base_score={base_score:.4f}, contribution=+0.0000)"
            )
        else:
            parts.append(
                f"rs: {rs_value:.2f} "
                f"contribution={contribution:+.4f} "
                f"(base_score={base_score:.4f}, "
                f"weight={self.overlay_weight}, "
                f"neutral={self.neutral_score})"
            )
        return " | ".join(parts)

    def _rerank(
        self,
        base: StrategyEvaluation,
        new_scores: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        base_ranked_symbols = [
            str(entry["symbol"])
            for entry in base.rankings
            if entry.get("symbol") is not None
        ]
        # Preserve the base set of ranked symbols; only rescore + reorder.
        # Symbols in scores but not rankings stay unranked, matching the
        # Champion's original selection surface.
        ordered = sorted(
            base_ranked_symbols,
            key=lambda symbol: (
                -new_scores.get(symbol, base.scores.get(symbol, 0.0)),
                symbol,
            ),
        )
        return [
            {"symbol": symbol, "rank": index + 1}
            for index, symbol in enumerate(ordered)
        ]


def rs_provider_from_map(
    data: Dict[str, Dict[str, float]],
) -> RelativeStrengthProvider:
    """Build a deterministic provider from a ``{timestamp: {symbol: rs}}`` map.

    Useful for tests and for wiring RS snapshots from the Data Catalog
    into the Backtest Lab.  Unknown ``(timestamp, symbol)`` combinations
    return ``None`` so the wrapper falls back to the base score.
    """
    snapshot = {
        str(timestamp): dict(scores) for timestamp, scores in data.items()
    }

    def provider(symbol: str, event: BacktestEvent) -> Optional[float]:
        by_timestamp = snapshot.get(event.timestamp)
        if by_timestamp is None:
            return None
        value = by_timestamp.get(symbol)
        return None if value is None else float(value)

    return provider
