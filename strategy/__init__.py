"""
Trader Joe Strategy Evolution Package
======================================
Champion/Challenger framework for disciplined strategy experimentation.

All new features are disabled by default. Disabling any feature must
completely restore existing production behavior.

Feature flags:
    enable_historical_statistics
    enable_post_game_review
    enable_relative_strength
    enable_market_regime
    enable_sell_score
    enable_overnight_risk_engine
    enable_morning_intelligence
    enable_sector_leadership
    enable_market_breadth
    enable_risk_based_position_sizing
    enable_confidence_score
    enable_statistical_decision_support

Rollout order:
    1. Historical statistics
    2. Post-game review
    3. Relative Strength
    4. Market regime
    5. Sell score
    6. Overnight risk engine
    7. Morning intelligence
    8. Sector leadership
    9. Market breadth
    10. Risk-based position sizing
    11. Confidence scoring
    12. Statistical decision support
"""

from strategy.config import FeatureFlags, get_feature_flags, reset_feature_flags
from strategy.runner import (
    ChampionChallengerRunner,
    EvaluationResult,
    SetupResult,
)
from strategy.morning_intelligence import (
    MorningIntelligenceAgent,
    MorningIntelligenceReport,
)
from strategy.sector_leadership import (
    SectorLeadershipAnalyzer,
    SectorLeadershipReport,
    SectorLeadershipResult,
)
from strategy.market_breadth import (
    MarketBreadthAnalyzer,
    MarketBreadthReport,
    BreadthSymbolObservation,
)
from strategy.backtest_lab import (
    BacktestArtifactPaths,
    BacktestConfig,
    BacktestEvent,
    BacktestReport,
    BacktestRunManifest,
    BacktestRunMetadata,
    BacktestStrategyResult,
    DeterministicReplayClock,
    NoOpStrategyAdapter,
    StrategyEvaluation,
)

__all__ = [
    "FeatureFlags",
    "get_feature_flags",
    "reset_feature_flags",
    "BacktestArtifactPaths",
    "BacktestConfig",
    "BacktestEvent",
    "BacktestReport",
    "BacktestRunManifest",
    "BacktestRunMetadata",
    "BacktestStrategyResult",
    "DeterministicReplayClock",
    "NoOpStrategyAdapter",
    "StrategyEvaluation",
    "ChampionChallengerRunner",
    "EvaluationResult",
    "MorningIntelligenceAgent",
    "MorningIntelligenceReport",
    "BreadthSymbolObservation",
    "MarketBreadthAnalyzer",
    "MarketBreadthReport",
    "SectorLeadershipAnalyzer",
    "SectorLeadershipReport",
    "SectorLeadershipResult",
    "SetupResult",
]
