"""Volatility Regime Filter — Strategy #10.

Score is trailing return dampened by realised volatility.  In
high-volatility regimes (rolling stdev of daily returns above
``vol_ceiling``) the score is fully suppressed to zero, matching
a filter policy of "skip the trade when the tape is too noisy".
In moderate regimes the score is a linear blend between the raw
trailing return and zero.  In quiet regimes the score is the raw
trailing return.

Daily by default; the same math also works on intraday bars if
the caller asserts the interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.interval_requirements import (
    DAILY_OR_INTRADAY,
    assert_supported_interval,
)
from strategy.lab.single_factor import SingleFactorStrategy
from strategy.market_data_provider import BarInterval


VOLATILITY_REGIME_FILTER_STRATEGY_NAME = "volatility_regime_filter_v1"
VOLATILITY_REGIME_FILTER_STRATEGY_VERSION = "0.1.0"


def _daily_returns(closes: Sequence[float]) -> List[float]:
    returns: List[float] = []
    for prev, curr in zip(closes, closes[1:]):
        if prev <= 0:
            returns.append(0.0)
            continue
        returns.append((curr - prev) / prev)
    return returns


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


@dataclass
class VolatilityRegimeFilterStrategy(SingleFactorStrategy):
    """Trailing return, dampened by realised volatility."""

    name: str = VOLATILITY_REGIME_FILTER_STRATEGY_NAME
    version: str = VOLATILITY_REGIME_FILTER_STRATEGY_VERSION
    strategy_id: str = "lab-volatility-regime-filter-v0.1.0"
    lookback_bars: int = 20
    vol_floor: float = 0.005
    vol_ceiling: float = 0.03

    def parameters(self) -> Mapping[str, Any]:
        return {
            "lookback_bars": self.lookback_bars,
            "vol_floor": self.vol_floor,
            "vol_ceiling": self.vol_ceiling,
            "strategy_id": self.strategy_id,
        }

    def required_history(self) -> int:
        return self.lookback_bars + 1

    @classmethod
    def supported_intervals(cls) -> Tuple[BarInterval, ...]:
        return DAILY_OR_INTRADAY

    @classmethod
    def requires_intraday_data(cls) -> bool:
        return False

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
        closes = [float(b.get("c", 0.0)) for b in recent]
        prev = closes[0]
        current = closes[-1]
        if prev == 0.0:
            return 0.0, {
                "volatility_regime_filter": {
                    "contribution": 0.0,
                    "detail": "prev_close=0",
                }
            }
        raw_return = (current - prev) / prev
        vol = _stdev(_daily_returns(closes))
        if vol >= self.vol_ceiling:
            multiplier = 0.0
            regime = "high"
        elif vol <= self.vol_floor:
            multiplier = 1.0
            regime = "quiet"
        else:
            multiplier = 1.0 - (
                (vol - self.vol_floor)
                / max(self.vol_ceiling - self.vol_floor, 1e-9)
            )
            regime = "moderate"
        score = raw_return * multiplier
        return score, {
            "volatility_regime_filter": {
                "contribution": score,
                "detail": (
                    f"raw_return={raw_return:+.4f} vol={vol:.4f} "
                    f"regime={regime} multiplier={multiplier:.2f}"
                ),
            }
        }


__all__ = [
    "VOLATILITY_REGIME_FILTER_STRATEGY_NAME",
    "VOLATILITY_REGIME_FILTER_STRATEGY_VERSION",
    "VolatilityRegimeFilterStrategy",
]
