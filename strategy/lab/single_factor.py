"""Shared scaffolding for single-factor lab strategies.

Momentum / Trend / MeanReversion (and any future single-factor
strategy) subclass :class:`SingleFactorStrategy` and override
``score_symbol(bars, cutoff, symbol) -> (float, dict)``.  The base
class handles evaluator construction, ranking, warnings, and
:class:`ScoreExplanation` construction so subclasses stay focused
on their one factor.

Read-only: no live-runner imports, no order-path references, no
credential env reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.strategy import StrategyBase
from strategy.score_explanation import (
    ScoreComponent,
    ScoreExplanation,
    blank_explanation,
)


def _bars_up_to(bars: Sequence[Mapping[str, Any]], cutoff: str) -> List[Mapping[str, Any]]:
    kept: List[Mapping[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        t = bar.get("t")
        if t is None or str(t) > cutoff:
            continue
        kept.append(bar)
    return kept


class _SingleFactorEvaluator:
    """Adapter — walks the base class's ``score_symbol`` for every
    symbol at ``event.timestamp``, sorts, and packages the result
    as a :class:`StrategyEvaluation`.
    """

    def __init__(
        self,
        strategy_id: str,
        strategy_name: str,
        strategy_version: str,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        score_symbol,
        required_history: int,
    ):
        self.strategy_id = strategy_id
        self._strategy_name = strategy_name
        self._strategy_version = strategy_version
        self._bars = bars_by_symbol
        self._symbols = tuple(symbols)
        self._score_symbol = score_symbol
        self._required_history = int(required_history)

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        cutoff = event.timestamp
        scores: Dict[str, float] = {}
        structured: Dict[str, ScoreExplanation] = {}
        explanations: Dict[str, str] = {}
        warnings: List[str] = []
        selected: List[Tuple[str, float]] = []
        for symbol in self._symbols:
            bars = _bars_up_to(self._bars.get(symbol) or [], cutoff)
            if len(bars) < self._required_history:
                exp = blank_explanation(
                    self.strategy_id,
                    symbol,
                    event.timestamp,
                    0.0,
                    note=(
                        f"insufficient_history: have={len(bars)} "
                        f"need>={self._required_history}"
                    ),
                )
                structured[symbol] = exp
                explanations[symbol] = exp.to_summary_str()
                continue
            score, detail = self._score_symbol(bars, cutoff, symbol)
            scores[symbol] = float(score)
            components = tuple(
                ScoreComponent(
                    name=str(k),
                    contribution=float(v.get("contribution", 0.0)),
                    detail=str(v.get("detail", "")),
                )
                for k, v in detail.items()
            )
            exp = ScoreExplanation(
                strategy_id=self.strategy_id,
                symbol=symbol,
                event_timestamp=event.timestamp,
                final_score=float(score),
                components=components,
                penalties=(),
                bonuses=(),
                notes=(),
                rejected=False,
                rejection_reasons=(),
            )
            structured[symbol] = exp
            explanations[symbol] = exp.to_summary_str()
            selected.append((symbol, float(score)))
        selected.sort(key=lambda pair: (-pair[1], pair[0]))
        rankings = [
            {"symbol": s, "rank": r + 1}
            for r, (s, _) in enumerate(selected)
        ]
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=scores,
            rankings=rankings,
            explanations=explanations,
            structured_explanations=dict(structured),
            warnings=warnings,
        )


class SingleFactorStrategy(StrategyBase):
    """Base class for single-factor lab strategies."""

    strategy_id: str = ""

    def score_symbol(
        self,
        bars: Sequence[Mapping[str, Any]],
        cutoff: str,
        symbol: str,
    ) -> Tuple[float, Dict[str, Dict[str, Any]]]:
        """Compute the factor score for one symbol at one cutoff.
        Returns ``(score, components_dict)`` where components_dict
        maps component name to ``{"contribution": float,
        "detail": str}``.  Subclasses override this.
        """
        raise NotImplementedError

    def build_evaluator(
        self,
        bars_by_symbol,
        symbols,
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        return _SingleFactorEvaluator(
            strategy_id=self.strategy_id or self.name,
            strategy_name=self.name,
            strategy_version=self.version,
            bars_by_symbol=bars_by_symbol,
            symbols=symbols,
            score_symbol=self.score_symbol,
            required_history=self.required_history(),
        )


__all__ = ["SingleFactorStrategy"]
