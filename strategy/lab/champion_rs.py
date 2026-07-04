"""Champion + Relative Strength strategy adapter (Strategy #2).

Combines :class:`~strategy.champion_scoring.ChampionScorer` with
:class:`~strategy.rs_challenger.RelativeStrengthChallenger`.  Same
math as the existing challenger — this file only exposes it
through the Lab's Strategy interface.

Read-only: no live-runner imports, no order-path tokens, no
credential env reads.  The RS overlay's own
``enable_relative_strength`` feature flag is *not* touched by
this adapter; when the flag is off, the challenger's built-in
passthrough returns Champion scores unchanged.  Turning the flag
on is an operator/promotion decision and lives entirely outside
this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from strategy.champion_scoring import (
    ChampionScorer,
    ChampionScoringConfig,
    RESEARCH_CHAMPION_STRATEGY_ID,
)
from strategy.comparison_harness import ComparisonEvaluator
from strategy.config import FeatureFlags
from strategy.lab.strategy import StrategyBase
from strategy.rs_challenger import (
    DEFAULT_RS_NEUTRAL_SCORE,
    DEFAULT_RS_OVERLAY_WEIGHT,
    DEFAULT_RS_SCORE_RANGE,
    RS_CHALLENGER_STRATEGY_ID,
    RelativeStrengthChallenger,
    RelativeStrengthProvider,
)


CHAMPION_RS_STRATEGY_NAME = "champion_rs"
CHAMPION_RS_STRATEGY_VERSION = "0.1.0"


@dataclass
class ChampionRelativeStrengthStrategy(StrategyBase):
    """Concrete strategy: Champion with RS composite overlay.

    ``overlay_weight`` / ``neutral_score`` / ``score_range`` are
    sweep-friendly parameters (Card 5 will iterate over them).
    ``build_evaluator`` requires an ``rs_provider`` kwarg — the
    caller supplies a
    :class:`RelativeStrengthProvider` bound to the current
    bar map.
    """

    name: str = CHAMPION_RS_STRATEGY_NAME
    version: str = CHAMPION_RS_STRATEGY_VERSION
    config: ChampionScoringConfig = field(default_factory=ChampionScoringConfig)
    overlay_weight: float = DEFAULT_RS_OVERLAY_WEIGHT
    neutral_score: float = DEFAULT_RS_NEUTRAL_SCORE
    score_range: float = DEFAULT_RS_SCORE_RANGE
    strategy_id: str = RS_CHALLENGER_STRATEGY_ID
    base_strategy_id: str = RESEARCH_CHAMPION_STRATEGY_ID

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
            "overlay_weight": self.overlay_weight,
            "neutral_score": self.neutral_score,
            "score_range": self.score_range,
            "strategy_id": self.strategy_id,
            "base_strategy_id": self.base_strategy_id,
        }

    def required_history(self) -> int:
        return self.config.min_history_required()

    def build_evaluator(
        self,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        *,
        rs_provider: Optional[RelativeStrengthProvider] = None,
        flags: Optional[FeatureFlags] = None,
        **kwargs: Any,
    ) -> ComparisonEvaluator:
        if rs_provider is None:
            raise ValueError(
                "ChampionRelativeStrengthStrategy.build_evaluator "
                "requires an rs_provider keyword argument"
            )
        base = ChampionScorer(
            strategy_id=self.base_strategy_id,
            bars_by_symbol=bars_by_symbol,
            symbols=tuple(symbols),
            config=self.config,
        )
        return RelativeStrengthChallenger(
            base_evaluator=base,
            rs_provider=rs_provider,
            flags=flags,
            strategy_id=self.strategy_id,
            overlay_weight=self.overlay_weight,
            neutral_score=self.neutral_score,
            score_range=self.score_range,
        )


__all__ = [
    "CHAMPION_RS_STRATEGY_NAME",
    "CHAMPION_RS_STRATEGY_VERSION",
    "ChampionRelativeStrengthStrategy",
]
