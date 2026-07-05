"""Sector Rotation Daily — Strategy #9.

Score = symbol's trailing return minus its sector benchmark's
trailing return, over ``lookback`` daily bars.  Positive when the
symbol is leading its sector; negative when it is lagging.  The
sector-benchmark map is supplied by the caller at
:meth:`build_evaluator` time via ``sector_map``.

Daily bars only.  When no ``sector_map`` is provided, the strategy
falls back to a broad-market benchmark (the first key of the
constructor-supplied ``bars_by_symbol`` mapping that matches
``fallback_benchmark``), which lets the strategy still produce
meaningful scores in a simple universe test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.interval_requirements import (
    DAILY_ONLY,
    assert_supported_interval,
)
from strategy.lab.strategy import StrategyBase
from strategy.market_data_provider import BarInterval
from strategy.score_explanation import (
    ScoreComponent,
    ScoreExplanation,
    blank_explanation,
)


SECTOR_ROTATION_DAILY_STRATEGY_NAME = "sector_rotation_daily_v1"
SECTOR_ROTATION_DAILY_STRATEGY_VERSION = "0.1.0"


def _trailing_return(
    bars: Sequence[Mapping[str, Any]], lookback: int
) -> Optional[float]:
    if len(bars) < lookback + 1:
        return None
    prev = float(bars[-(lookback + 1)].get("c", 0.0))
    curr = float(bars[-1].get("c", 0.0))
    if prev == 0.0:
        return None
    return (curr - prev) / prev


def _bars_up_to(
    bars: Sequence[Mapping[str, Any]], cutoff: str
) -> Tuple[Mapping[str, Any], ...]:
    kept = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        t = bar.get("t")
        if t is None or str(t) > cutoff:
            continue
        kept.append(bar)
    return tuple(kept)


class _SectorRotationEvaluator:
    """Evaluator that ranks symbols by relative return vs. their
    sector benchmark.
    """

    def __init__(
        self,
        strategy_id: str,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        sector_map: Mapping[str, str],
        lookback: int,
        fallback_benchmark: str,
    ):
        self.strategy_id = strategy_id
        self._bars = bars_by_symbol
        self._symbols = tuple(symbols)
        self._sector_map = {k.upper(): v for k, v in sector_map.items()}
        self._lookback = int(lookback)
        self._fallback = fallback_benchmark

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        cutoff = event.timestamp
        scores: Dict[str, float] = {}
        structured: Dict[str, ScoreExplanation] = {}
        explanations: Dict[str, str] = {}
        selected = []
        for symbol in self._symbols:
            sym_bars = _bars_up_to(
                self._bars.get(symbol) or [], cutoff
            )
            if len(sym_bars) < self._lookback + 1:
                exp = blank_explanation(
                    self.strategy_id,
                    symbol,
                    event.timestamp,
                    0.0,
                    note=(
                        f"insufficient_history: have={len(sym_bars)} "
                        f"need>={self._lookback + 1}"
                    ),
                )
                structured[symbol] = exp
                explanations[symbol] = exp.to_summary_str()
                continue
            sector_symbol = self._sector_map.get(
                symbol.upper(), self._fallback
            )
            sector_bars = _bars_up_to(
                self._bars.get(sector_symbol) or [], cutoff
            )
            sym_return = _trailing_return(sym_bars, self._lookback)
            sector_return = _trailing_return(sector_bars, self._lookback)
            if sym_return is None:
                exp = blank_explanation(
                    self.strategy_id,
                    symbol,
                    event.timestamp,
                    0.0,
                    note="symbol trailing return unavailable",
                )
                structured[symbol] = exp
                explanations[symbol] = exp.to_summary_str()
                continue
            if sector_return is None:
                # No benchmark coverage; use absolute return as a
                # degraded signal and note it.
                relative = sym_return
                detail = (
                    f"sym_return={sym_return:+.4f} "
                    f"(no benchmark data for {sector_symbol})"
                )
            else:
                relative = sym_return - sector_return
                detail = (
                    f"sym_return={sym_return:+.4f} "
                    f"sector={sector_symbol} "
                    f"sector_return={sector_return:+.4f} "
                    f"relative={relative:+.4f}"
                )
            components = (
                ScoreComponent(
                    name="relative_return",
                    contribution=relative,
                    detail=detail,
                ),
            )
            exp = ScoreExplanation(
                strategy_id=self.strategy_id,
                symbol=symbol,
                event_timestamp=event.timestamp,
                final_score=float(relative),
                components=components,
                penalties=(),
                bonuses=(),
                notes=(),
                rejected=False,
                rejection_reasons=(),
            )
            scores[symbol] = float(relative)
            structured[symbol] = exp
            explanations[symbol] = exp.to_summary_str()
            selected.append((symbol, float(relative)))
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
            warnings=[],
        )


@dataclass
class SectorRotationDailyStrategy(StrategyBase):
    """Daily sector-rotation ranker."""

    name: str = SECTOR_ROTATION_DAILY_STRATEGY_NAME
    version: str = SECTOR_ROTATION_DAILY_STRATEGY_VERSION
    strategy_id: str = "lab-sector-rotation-daily-v0.1.0"
    lookback_days: int = 20
    fallback_benchmark: str = "SPY"

    def parameters(self) -> Mapping[str, Any]:
        return {
            "lookback_days": self.lookback_days,
            "fallback_benchmark": self.fallback_benchmark,
            "strategy_id": self.strategy_id,
        }

    def required_history(self) -> int:
        return self.lookback_days + 1

    @classmethod
    def supported_intervals(cls) -> Tuple[BarInterval, ...]:
        return DAILY_ONLY

    @classmethod
    def requires_intraday_data(cls) -> bool:
        return False

    def build_evaluator(
        self,
        bars_by_symbol,
        symbols,
        *,
        sector_map: Optional[Mapping[str, str]] = None,
        interval: Optional[BarInterval] = None,
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        if interval is not None:
            assert_supported_interval(
                self.name, self.supported_intervals(), interval
            )
        return _SectorRotationEvaluator(
            strategy_id=self.strategy_id,
            bars_by_symbol=bars_by_symbol,
            symbols=symbols,
            sector_map=dict(sector_map or {}),
            lookback=self.lookback_days,
            fallback_benchmark=self.fallback_benchmark,
        )


__all__ = [
    "SECTOR_ROTATION_DAILY_STRATEGY_NAME",
    "SECTOR_ROTATION_DAILY_STRATEGY_VERSION",
    "SectorRotationDailyStrategy",
]
