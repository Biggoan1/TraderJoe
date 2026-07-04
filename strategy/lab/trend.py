"""Trend strategy — Strategy #4.

Fast/slow SMA crossover.  Score = (SMA_fast - SMA_slow) / close;
positive when the fast SMA leads (uptrend), negative when the
slow SMA leads (downtrend).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

from strategy.lab.single_factor import SingleFactorStrategy


TREND_STRATEGY_NAME = "trend"
TREND_STRATEGY_VERSION = "0.1.0"


def _sma_of(bars: Sequence[Mapping[str, Any]], period: int) -> float:
    if period <= 0 or len(bars) < period:
        return 0.0
    tail = bars[-period:]
    total = sum(float(b.get("c", 0.0)) for b in tail)
    return total / period


@dataclass
class TrendStrategy(SingleFactorStrategy):
    name: str = TREND_STRATEGY_NAME
    version: str = TREND_STRATEGY_VERSION
    strategy_id: str = "lab-trend-v0.1.0"
    sma_fast: int = 20
    sma_slow: int = 50

    def parameters(self) -> Mapping[str, Any]:
        return {
            "sma_fast": self.sma_fast,
            "sma_slow": self.sma_slow,
            "strategy_id": self.strategy_id,
        }

    def required_history(self) -> int:
        return max(self.sma_fast, self.sma_slow) + 1

    def score_symbol(
        self,
        bars: Sequence[Mapping[str, Any]],
        cutoff: str,
        symbol: str,
    ) -> Tuple[float, Dict[str, Dict[str, Any]]]:
        fast = _sma_of(bars, self.sma_fast)
        slow = _sma_of(bars, self.sma_slow)
        close = float(bars[-1].get("c", 0.0))
        if close == 0.0:
            return 0.0, {
                "trend": {"contribution": 0.0, "detail": "close=0"}
            }
        score = (fast - slow) / close
        return score, {
            "trend": {
                "contribution": score,
                "detail": (
                    f"sma{self.sma_fast}={fast:.2f} "
                    f"sma{self.sma_slow}={slow:.2f} "
                    f"close={close:.2f}"
                ),
            }
        }


__all__ = [
    "TREND_STRATEGY_NAME",
    "TREND_STRATEGY_VERSION",
    "TrendStrategy",
]
