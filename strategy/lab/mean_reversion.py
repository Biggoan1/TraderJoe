"""Mean-reversion strategy — Strategy #5.

Score = (SMA_lookback - close) / SMA_lookback.  Positive when the
close is below its trailing mean (buy-the-dip signal); negative
when the close is above.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

from strategy.lab.single_factor import SingleFactorStrategy


MEAN_REVERSION_STRATEGY_NAME = "mean_reversion"
MEAN_REVERSION_STRATEGY_VERSION = "0.1.0"


def _sma_of(bars: Sequence[Mapping[str, Any]], period: int) -> float:
    if period <= 0 or len(bars) < period:
        return 0.0
    tail = bars[-period:]
    total = sum(float(b.get("c", 0.0)) for b in tail)
    return total / period


@dataclass
class MeanReversionStrategy(SingleFactorStrategy):
    name: str = MEAN_REVERSION_STRATEGY_NAME
    version: str = MEAN_REVERSION_STRATEGY_VERSION
    strategy_id: str = "lab-mean-reversion-v0.1.0"
    lookback: int = 20

    def parameters(self) -> Mapping[str, Any]:
        return {"lookback": self.lookback, "strategy_id": self.strategy_id}

    def required_history(self) -> int:
        return self.lookback + 1

    def score_symbol(
        self,
        bars: Sequence[Mapping[str, Any]],
        cutoff: str,
        symbol: str,
    ) -> Tuple[float, Dict[str, Dict[str, Any]]]:
        sma = _sma_of(bars, self.lookback)
        close = float(bars[-1].get("c", 0.0))
        if sma == 0.0:
            return 0.0, {
                "mean_reversion": {
                    "contribution": 0.0, "detail": "sma=0"
                }
            }
        score = (sma - close) / sma
        return score, {
            "mean_reversion": {
                "contribution": score,
                "detail": (
                    f"sma{self.lookback}={sma:.2f} "
                    f"close={close:.2f}"
                ),
            }
        }


__all__ = [
    "MEAN_REVERSION_STRATEGY_NAME",
    "MEAN_REVERSION_STRATEGY_VERSION",
    "MeanReversionStrategy",
]
