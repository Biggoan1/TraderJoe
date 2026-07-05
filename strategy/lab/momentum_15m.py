"""Momentum 15m — Strategy #6.

Intraday momentum on 15-minute bars.  Score is the trailing
``lookback``-bar close return, matching the daily :class:`MomentumStrategy`
shape but constrained to 15-minute data.

Read-only, research-only.  Refuses to score against daily bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.interval_requirements import (
    INTRADAY_15M,
    assert_supported_interval,
)
from strategy.lab.single_factor import SingleFactorStrategy
from strategy.market_data_provider import BarInterval


MOMENTUM_15M_STRATEGY_NAME = "momentum_15m_v1"
MOMENTUM_15M_STRATEGY_VERSION = "0.1.0"


@dataclass
class Momentum15mStrategy(SingleFactorStrategy):
    """Trailing-return momentum on 15-minute bars."""

    name: str = MOMENTUM_15M_STRATEGY_NAME
    version: str = MOMENTUM_15M_STRATEGY_VERSION
    strategy_id: str = "lab-momentum-15m-v0.1.0"
    lookback_bars: int = 26  # ~one regular-hours session

    def parameters(self) -> Mapping[str, Any]:
        return {
            "lookback_bars": self.lookback_bars,
            "strategy_id": self.strategy_id,
            "interval": BarInterval.MINUTE_15.value,
        }

    def required_history(self) -> int:
        return max(2, self.lookback_bars + 1)

    @classmethod
    def supported_intervals(cls) -> Tuple[BarInterval, ...]:
        return INTRADAY_15M

    @classmethod
    def requires_intraday_data(cls) -> bool:
        return True

    def build_evaluator(
        self,
        bars_by_symbol,
        symbols,
        *,
        interval: Optional[BarInterval] = None,
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        if interval is not None:
            assert_supported_interval(
                self.name, self.supported_intervals(), interval
            )
        return super().build_evaluator(bars_by_symbol, symbols, **kwargs)

    def score_symbol(
        self,
        bars: Sequence[Mapping[str, Any]],
        cutoff: str,
        symbol: str,
    ) -> Tuple[float, Dict[str, Dict[str, Any]]]:
        recent = bars[-(self.lookback_bars + 1):]
        prev = float(recent[0].get("c", 0.0))
        current = float(recent[-1].get("c", 0.0))
        if prev == 0.0:
            return 0.0, {
                "momentum_15m": {
                    "contribution": 0.0,
                    "detail": "prev_close=0",
                }
            }
        pct_return = (current - prev) / prev
        return pct_return, {
            "momentum_15m": {
                "contribution": pct_return,
                "detail": (
                    f"trailing_{self.lookback_bars}bar_return="
                    f"{pct_return:+.4f}"
                ),
            }
        }


__all__ = [
    "MOMENTUM_15M_STRATEGY_NAME",
    "MOMENTUM_15M_STRATEGY_VERSION",
    "Momentum15mStrategy",
]
