"""Champion strategy adapter (Strategy #1).

Thin wrapper over :class:`strategy.champion_scoring.ChampionScorer`
that exposes the strategy through the Lab's
:class:`~strategy.lab.strategy.Strategy` interface.  No math is
duplicated — every score, gate, and explanation comes from the
existing scorer, verbatim.

Read-only: no live-runner imports, no order-path tokens, no
credential env reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from strategy.champion_scoring import (
    ChampionScorer,
    ChampionScoringConfig,
    RESEARCH_CHAMPION_STRATEGY_ID,
)
from strategy.comparison_harness import ComparisonEvaluator
from strategy.lab.strategy import StrategyBase


CHAMPION_STRATEGY_NAME = "champion"
CHAMPION_STRATEGY_VERSION = "0.4.0"


@dataclass
class ChampionStrategy(StrategyBase):
    """Concrete strategy: Champion (research-only six-gate scorer).

    Parameters come from :class:`ChampionScoringConfig`; callers
    who want to sweep gate settings pass a customised config.
    """

    name: str = CHAMPION_STRATEGY_NAME
    version: str = CHAMPION_STRATEGY_VERSION
    config: ChampionScoringConfig = field(default_factory=ChampionScoringConfig)
    strategy_id: str = RESEARCH_CHAMPION_STRATEGY_ID

    def parameters(self) -> Mapping[str, Any]:
        c = self.config
        return {
            "sma_short": c.sma_short,
            "sma_long": c.sma_long,
            "rsi_period": c.rsi_period,
            "rsi_overbought": c.rsi_overbought,
            "adx_period": c.adx_period,
            "adx_min": c.adx_min,
            "macd_fast": c.macd_fast,
            "macd_slow": c.macd_slow,
            "macd_signal": c.macd_signal,
            "volume_lookback": c.volume_lookback,
            "volume_ratio_floor": c.volume_ratio_floor,
            "min_gate_count": c.min_gate_count,
            "strategy_id": self.strategy_id,
        }

    def required_history(self) -> int:
        return self.config.min_history_required()

    def build_evaluator(
        self,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        return ChampionScorer(
            strategy_id=self.strategy_id,
            bars_by_symbol=bars_by_symbol,
            symbols=tuple(symbols),
            config=self.config,
        )


__all__ = [
    "CHAMPION_STRATEGY_NAME",
    "CHAMPION_STRATEGY_VERSION",
    "ChampionStrategy",
]
