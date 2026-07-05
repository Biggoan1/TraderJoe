"""Opening Range Breakout — Strategy #7.

Intraday breakout signal computed from the first N bars of the
current regular-session date.  Score is the current close's
displacement above/below the opening range's high/low, expressed
as a fraction of the range.  Positive = breakout above; negative
= breakdown below; near-zero = still inside the range.

Only meaningful on intraday bars.  Refuses daily-only data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.interval_requirements import (
    INTRADAY_ANY,
    assert_supported_interval,
)
from strategy.lab.single_factor import SingleFactorStrategy
from strategy.market_data_provider import BarInterval


OPENING_RANGE_BREAKOUT_STRATEGY_NAME = "opening_range_breakout_v1"
OPENING_RANGE_BREAKOUT_STRATEGY_VERSION = "0.1.0"


def _session_date(timestamp: str) -> str:
    """Return the ISO date portion of an ISO 8601 timestamp."""
    return timestamp[:10] if timestamp else ""


@dataclass
class OpeningRangeBreakoutStrategy(SingleFactorStrategy):
    """Score by breakout displacement above/below the day's OR."""

    name: str = OPENING_RANGE_BREAKOUT_STRATEGY_NAME
    version: str = OPENING_RANGE_BREAKOUT_STRATEGY_VERSION
    strategy_id: str = "lab-opening-range-breakout-v0.1.0"
    range_bars: int = 2  # first 30 minutes on 15-min bars

    def parameters(self) -> Mapping[str, Any]:
        return {
            "range_bars": self.range_bars,
            "strategy_id": self.strategy_id,
        }

    def required_history(self) -> int:
        # Need enough intraday bars to define the range plus at
        # least one bar to break out of it.
        return self.range_bars + 1

    @classmethod
    def supported_intervals(cls) -> Tuple[BarInterval, ...]:
        return INTRADAY_ANY

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
        session_date = _session_date(str(bars[-1].get("t", "")))
        session_bars = [
            b
            for b in bars
            if _session_date(str(b.get("t", ""))) == session_date
        ]
        if len(session_bars) < self.range_bars + 1:
            return 0.0, {
                "opening_range_breakout": {
                    "contribution": 0.0,
                    "detail": (
                        f"insufficient session bars: "
                        f"have={len(session_bars)} "
                        f"need>={self.range_bars + 1}"
                    ),
                }
            }
        range_bars = session_bars[: self.range_bars]
        highs = [float(b.get("h", 0.0)) for b in range_bars]
        lows = [float(b.get("l", 0.0)) for b in range_bars]
        range_high = max(highs)
        range_low = min(lows)
        range_width = range_high - range_low
        current_close = float(session_bars[-1].get("c", 0.0))
        if range_width <= 0:
            return 0.0, {
                "opening_range_breakout": {
                    "contribution": 0.0,
                    "detail": (
                        f"flat range high={range_high} low={range_low}"
                    ),
                }
            }
        if current_close > range_high:
            displacement = (current_close - range_high) / range_width
        elif current_close < range_low:
            displacement = (current_close - range_low) / range_width
        else:
            displacement = 0.0
        return displacement, {
            "opening_range_breakout": {
                "contribution": displacement,
                "detail": (
                    f"range=[{range_low:.4f},{range_high:.4f}] "
                    f"close={current_close:.4f} "
                    f"displacement={displacement:+.4f}"
                ),
            }
        }


__all__ = [
    "OPENING_RANGE_BREAKOUT_STRATEGY_NAME",
    "OPENING_RANGE_BREAKOUT_STRATEGY_VERSION",
    "OpeningRangeBreakoutStrategy",
]
