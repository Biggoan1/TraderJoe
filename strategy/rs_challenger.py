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

from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import ComparisonEvaluator
from strategy.config import FeatureFlags, get_feature_flags
from strategy.relative_strength import relative_return_pct
from strategy.score_explanation import (
    ScoreExplanation,
    append_overlay_component,
    blank_explanation,
)


RS_CHALLENGER_STRATEGY_ID = "rs-challenger-v0.1.0"
RS_CHALLENGER_FLAG_NAME = "enable_relative_strength"

DEFAULT_RS_OVERLAY_WEIGHT = 0.20
DEFAULT_RS_NEUTRAL_SCORE = 50.0
DEFAULT_RS_SCORE_RANGE = 50.0
# Percentage-point outperformance that maps to a full-scale RS
# contribution.  Aligns with the ``[-10, 10] pp -> [0, 50] points``
# scaling used by ``RelativeStrengthCalculator._calculate_score``.
DEFAULT_RS_FULLSCALE_PP = 10.0
DEFAULT_RS_LOOKBACK_DAYS = 20


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
        base_structured = getattr(base, "structured_explanations", {}) or {}
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=base.event_timestamp,
            scores=dict(base.scores),
            rankings=[dict(entry) for entry in base.rankings],
            explanations=dict(base.explanations),
            warnings=list(base.warnings),
            structured_explanations=dict(base_structured),
        )

    def _apply_overlay(
        self, base: StrategyEvaluation, event: BacktestEvent
    ) -> StrategyEvaluation:
        new_scores: Dict[str, float] = {}
        explanations: Dict[str, str] = dict(base.explanations)
        warnings: List[str] = list(base.warnings)
        missing: List[str] = []
        base_structured = getattr(base, "structured_explanations", {}) or {}
        structured: Dict[str, ScoreExplanation] = {}

        for symbol in sorted(base.scores):
            base_score = base.scores[symbol]
            rs_value = self._safe_fetch(symbol, event, warnings)
            base_exp = base_structured.get(symbol)
            if rs_value is None:
                new_scores[symbol] = base_score
                missing.append(symbol)
                explanations[symbol] = self._explanation(
                    base.explanations.get(symbol, ""),
                    base_score=base_score,
                    rs_value=None,
                    contribution=0.0,
                )
                if base_exp is not None:
                    structured[symbol] = append_overlay_component(
                        base_exp,
                        overlay_strategy_id=self.strategy_id,
                        component_name="rs_overlay",
                        contribution=0.0,
                        detail="rs data unavailable — challenger fell back to base",
                        rs_value=None,
                        notes=("rs_data_missing",),
                    )
                else:
                    structured[symbol] = blank_explanation(
                        self.strategy_id,
                        symbol,
                        base.event_timestamp,
                        base_score,
                        note="rs data unavailable and no base explanation",
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
            detail = (
                f"rs={rs_value:.2f} weight={self.overlay_weight} "
                f"neutral={self.neutral_score}"
            )
            if base_exp is not None:
                structured[symbol] = append_overlay_component(
                    base_exp,
                    overlay_strategy_id=self.strategy_id,
                    component_name="rs_overlay",
                    contribution=contribution,
                    detail=detail,
                    rs_value=rs_value,
                )
            else:
                structured[symbol] = blank_explanation(
                    self.strategy_id,
                    symbol,
                    base.event_timestamp,
                    new_scores[symbol],
                    note=detail,
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
            structured_explanations=structured,
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


def _closes_up_to(
    bars: Sequence[Mapping[str, Any]],
    ordered_timestamps: Sequence[str],
    upto_index: int,
) -> List[float]:
    """Extract in-order closes for bars whose timestamps sit in
    ``ordered_timestamps[: upto_index + 1]``.

    ``ordered_timestamps`` is the sorted union of timestamps across all
    symbols in the run.  We keep only closes at timestamps in that
    prefix and preserve their sorted order so
    :func:`relative_return_pct` sees the same time-aligned window it
    would over a DataFrame.
    """
    if upto_index < 0:
        return []
    kept = set(ordered_timestamps[: upto_index + 1])
    closes_by_t: Dict[str, float] = {}
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        t = bar.get("t")
        c = bar.get("c")
        if t is None or c is None or t not in kept:
            continue
        try:
            closes_by_t[str(t)] = float(c)
        except (TypeError, ValueError):
            continue
    return [
        closes_by_t[t] for t in ordered_timestamps[: upto_index + 1]
        if t in closes_by_t
    ]


def build_rs_map_from_bars(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    symbols: Sequence[str],
    benchmarks: Sequence[str],
    lookback_days: int = DEFAULT_RS_LOOKBACK_DAYS,
    neutral_score: float = DEFAULT_RS_NEUTRAL_SCORE,
    score_range: float = DEFAULT_RS_SCORE_RANGE,
    fullscale_pp: float = DEFAULT_RS_FULLSCALE_PP,
) -> Dict[str, Dict[str, float]]:
    """Build a ``{timestamp: {symbol: rs_value}}`` map from bar lists.

    Consumes the shape returned by
    :meth:`strategy.research_account.ResearchAccountClient.fetch_bars`
    (``{symbol: [{"t": iso_str, "c": close, ...}]}``) and produces the
    shape :func:`rs_provider_from_map` accepts.

    For each timestamp ``t`` with at least ``lookback_days + 1`` prior
    bars, computes the pp-outperformance of each symbol vs each
    benchmark via :func:`strategy.relative_strength.relative_return_pct`,
    averages across benchmarks that had usable data, and maps the
    average onto the ``[neutral_score - score_range, neutral_score +
    score_range]`` window.  ``fullscale_pp`` sets how much
    outperformance corresponds to the full-scale edge — the default of
    10 pp matches
    ``RelativeStrengthCalculator._calculate_score``'s convention.

    Timestamps whose earliest prior window has fewer than
    ``lookback_days + 1`` closes for a given symbol are omitted for
    that symbol; symbols with no benchmark data at ``t`` are omitted
    for ``t``.  The RS Challenger's fallback path then activates for
    those observations.
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    if score_range <= 0:
        raise ValueError("score_range must be positive")
    if fullscale_pp <= 0:
        raise ValueError("fullscale_pp must be positive")

    ordered_timestamps: List[str] = sorted(
        {
            str(bar["t"])
            for bars in bars_by_symbol.values()
            if isinstance(bars, Sequence)
            for bar in bars
            if isinstance(bar, Mapping) and bar.get("t") is not None
        }
    )
    if not ordered_timestamps:
        return {}

    low = neutral_score - score_range
    high = neutral_score + score_range
    rs_map: Dict[str, Dict[str, float]] = {}

    for index, timestamp in enumerate(ordered_timestamps):
        if index < lookback_days:
            continue
        rs_at_t: Dict[str, float] = {}
        bench_closes = {
            bench: _closes_up_to(
                bars_by_symbol.get(bench, ()), ordered_timestamps, index
            )
            for bench in benchmarks
        }
        for symbol in symbols:
            sym_closes = _closes_up_to(
                bars_by_symbol.get(symbol, ()), ordered_timestamps, index
            )
            if len(sym_closes) < lookback_days + 1:
                continue
            pp_diffs: List[float] = []
            for bench in benchmarks:
                bc = bench_closes.get(bench, [])
                pp = relative_return_pct(sym_closes, bc, lookback_days)
                if pp is not None:
                    pp_diffs.append(pp)
            if not pp_diffs:
                continue
            avg_pp = sum(pp_diffs) / len(pp_diffs)
            contribution = (avg_pp / fullscale_pp) * score_range
            rs_value = max(low, min(high, neutral_score + contribution))
            rs_at_t[symbol] = round(rs_value, 4)
        if rs_at_t:
            rs_map[timestamp] = rs_at_t
    return rs_map
