"""Momentum strategy — Strategy #3.

Deterministic single-factor scorer: trailing ``lookback``-day
return.  Higher = stronger recent uptrend.  Purely research; no
live path, no order construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

from strategy.lab.single_factor import SingleFactorStrategy


MOMENTUM_STRATEGY_NAME = "momentum"
MOMENTUM_STRATEGY_VERSION = "0.1.0"


@dataclass
class MomentumStrategy(SingleFactorStrategy):
    name: str = MOMENTUM_STRATEGY_NAME
    version: str = MOMENTUM_STRATEGY_VERSION
    strategy_id: str = "lab-momentum-v0.1.0"
    lookback: int = 20

    def parameters(self) -> Mapping[str, Any]:
        return {"lookback": self.lookback, "strategy_id": self.strategy_id}

    def required_history(self) -> int:
        # need lookback+1 bars to compute the trailing return
        return max(2, self.lookback + 1)

    def score_symbol(
        self,
        bars: Sequence[Mapping[str, Any]],
        cutoff: str,
        symbol: str,
    ) -> Tuple[float, Dict[str, Dict[str, Any]]]:
        # Prices are floats in bar["c"]; oldest first, newest last.
        recent = bars[-(self.lookback + 1):]
        prev = float(recent[0].get("c", 0.0))
        current = float(recent[-1].get("c", 0.0))
        if prev == 0.0:
            return 0.0, {
                "momentum": {"contribution": 0.0, "detail": "prev_close=0"}
            }
        pct_return = (current - prev) / prev
        return pct_return, {
            "momentum": {
                "contribution": pct_return,
                "detail": (
                    f"trailing_{self.lookback}d_return="
                    f"{pct_return:+.4f}"
                ),
            }
        }


__all__ = [
    "MOMENTUM_STRATEGY_NAME",
    "MOMENTUM_STRATEGY_VERSION",
    "MomentumStrategy",
]
