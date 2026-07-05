"""RSI Mean Reversion — Strategy #8.

Daily (default) mean-reversion signal from an RSI oscillator.
Score is a piecewise-linear reversal signal centred around a
neutral RSI:

  * RSI below ``oversold`` scores positively (larger the more
    oversold), i.e. a "buy the dip" signal.
  * RSI above ``overbought`` scores negatively.
  * Between the two, score is scaled toward zero.

Read-only, research-only.  Works on daily bars by default; the
strategy also runs on intraday bars if the caller declares the
interval matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.interval_requirements import (
    DAILY_OR_INTRADAY,
    assert_supported_interval,
)
from strategy.lab.single_factor import SingleFactorStrategy
from strategy.market_data_provider import BarInterval


RSI_MEAN_REVERSION_STRATEGY_NAME = "rsi_mean_reversion_v1"
RSI_MEAN_REVERSION_STRATEGY_VERSION = "0.1.0"


def _rsi(closes: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for prev, curr in zip(closes[-(period + 1):-1], closes[-period:]):
        change = curr - prev
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-change)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass
class RSIMeanReversionStrategy(SingleFactorStrategy):
    """RSI-based mean-reversion signal."""

    name: str = RSI_MEAN_REVERSION_STRATEGY_NAME
    version: str = RSI_MEAN_REVERSION_STRATEGY_VERSION
    strategy_id: str = "lab-rsi-mean-reversion-v0.1.0"
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    neutral: float = 50.0

    def parameters(self) -> Mapping[str, Any]:
        return {
            "rsi_period": self.rsi_period,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "neutral": self.neutral,
            "strategy_id": self.strategy_id,
        }

    def required_history(self) -> int:
        return self.rsi_period + 1

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
        closes = [float(b.get("c", 0.0)) for b in bars]
        rsi = _rsi(closes, self.rsi_period)
        if rsi is None:
            return 0.0, {
                "rsi_mean_reversion": {
                    "contribution": 0.0,
                    "detail": "insufficient closes for RSI",
                }
            }
        if rsi <= self.oversold:
            # Buy-the-dip: normalise to [0, 1] as RSI falls to 0.
            score = (self.oversold - rsi) / max(self.oversold, 1.0)
        elif rsi >= self.overbought:
            # Fade the rally: negative signal.
            score = -(rsi - self.overbought) / max(
                100.0 - self.overbought, 1.0
            )
        else:
            # Neutral zone: scale toward zero.
            distance = rsi - self.neutral
            half_width = max(
                self.overbought - self.neutral,
                self.neutral - self.oversold,
                1.0,
            )
            score = -distance / half_width * 0.1
        return score, {
            "rsi_mean_reversion": {
                "contribution": score,
                "detail": (
                    f"rsi={rsi:.2f} bands=[{self.oversold},{self.overbought}] "
                    f"score={score:+.4f}"
                ),
            }
        }


__all__ = [
    "RSI_MEAN_REVERSION_STRATEGY_NAME",
    "RSI_MEAN_REVERSION_STRATEGY_VERSION",
    "RSIMeanReversionStrategy",
]
