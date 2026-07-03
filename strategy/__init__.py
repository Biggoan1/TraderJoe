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
from strategy.data_catalog import (
    DataCatalog,
    DatasetFile,
    DatasetManifest,
    DatasetValidationResult,
    KNOWN_DATASET_KINDS,
    build_dataset_manifest,
    sha256_file,
)
from strategy.comparison_harness import (
    ChampionChallengerComparison,
    ChampionChallengerRunMetadata,
    ComparisonEvaluator,
    ComparisonHarness,
    DisagreementRecord,
    KNOWN_DISAGREEMENT_KINDS,
    ScoreRow,
    ScoreTable,
)
from strategy.rs_challenger import (
    RS_CHALLENGER_FLAG_NAME,
    RS_CHALLENGER_STRATEGY_ID,
    RelativeStrengthChallenger,
    RelativeStrengthProvider,
    rs_provider_from_map,
)
from strategy.walk_forward import (
    WalkForwardPipeline,
    WalkForwardReport,
    WalkForwardSchedule,
    WalkForwardSplit,
    WalkForwardSplitResult,
    generate_walk_forward_schedule,
)
from strategy.research_reports import (
    KNOWN_REPORT_KINDS,
    REPORT_KIND_COMPARISON,
    REPORT_KIND_WALK_FORWARD,
    ResearchReport,
    ResearchReportPaths,
    render_comparison_report,
    render_walk_forward_report,
)
from strategy.promotion_gates import (
    APPROVAL_EVIDENCE_KEY,
    KNOWN_COMPARATORS,
    PROMOTION_STATES,
    REQUIRED_EVIDENCE_PER_STATE,
    STANDARD_ROLLBACK_CRITERIA,
    STATE_APPROVED,
    STATE_BACKTEST,
    STATE_CANDIDATE,
    STATE_DISABLED,
    STATE_PAPER_TRADING,
    STATE_PRODUCTION,
    STATE_WALK_FORWARD,
    ApprovalRecord,
    PromotionEntry,
    PromotionReport,
    RollbackAlert,
    RollbackCriterion,
    evaluate_promotion,
    is_terminal_state,
    next_state,
    state_index,
    triggered_alerts,
)
from strategy.stats_engine import (
    KNOWN_STATS_LABELS,
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    METRIC_DISAGREEMENT_RATE,
    METRIC_RANK_DELTA_MEAN,
    METRIC_SCORE_DELTA_MEAN,
    METRIC_SELECTION_AGREEMENT_RATE,
    METRIC_WF_DISAGREEMENT_RATE,
    METRIC_WF_SCORE_DELTA_MEAN,
    STATS_DEFAULT_CONFIDENCE_LEVEL,
    STATS_SAMPLE_SIZE_FLOOR,
    SUPPORTED_CONFIDENCE_LEVELS,
    StatisticalFinding,
    analyze_comparison,
    analyze_walk_forward,
    cohens_d_one_sample,
    cohens_d_two_sample,
    findings_stable_hash,
    normal_mean_ci,
    wilson_proportion_ci,
)
from strategy.pattern_discovery import (
    OUTCOME_KEY_RETURN_PCT,
    OUTCOME_KEY_WIN,
    PATTERN_MIN_SAMPLE_SIZE,
    PatternHypothesis,
    PatternObservation,
    discover_patterns,
    hypotheses_stable_hash,
    validate_patterns_out_of_sample,
)
from strategy.feature_importance import (
    FI_SAMPLE_SIZE_FLOOR,
    FLAG_CI_SPANS_ZERO,
    FLAG_LOW_SAMPLE,
    FLAG_ZERO_VARIANCE,
    METHOD_PEARSON,
    FeatureImportanceScore,
    FeatureObservation,
    analyze_feature_importance,
    filter_lookahead_observations,
    fisher_z_ci,
    importance_stable_hash,
    pearson_r,
)
from strategy.weight_recommender import (
    ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER,
    DEFAULT_EVIDENCE_KEY,
    DEFAULT_MAX_WEIGHT_CHANGE_PCT,
    DEFAULT_MIN_FEATURE_SCORE,
    FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER,
    MAX_ALLOWED_PROMOTION_STATE,
    RecommendationEnvelope,
    WeightRecommendation,
    envelope_for,
    recommend_weights,
    recommendations_stable_hash,
)
from strategy.learning_reports import (
    DEFAULT_LEARNING_REPORT_OUTPUT_DIR,
    LEARNING_REPORT_KIND,
    LearningReport,
    LearningReportPaths,
    render_learning_report,
)
from strategy.learning_pipeline import (
    LearningPipeline,
    LearningPipelineError,
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
    "DataCatalog",
    "DatasetFile",
    "DatasetManifest",
    "DatasetValidationResult",
    "KNOWN_DATASET_KINDS",
    "build_dataset_manifest",
    "sha256_file",
    "ChampionChallengerComparison",
    "ChampionChallengerRunMetadata",
    "ComparisonEvaluator",
    "ComparisonHarness",
    "DisagreementRecord",
    "KNOWN_DISAGREEMENT_KINDS",
    "ScoreRow",
    "ScoreTable",
    "RS_CHALLENGER_FLAG_NAME",
    "RS_CHALLENGER_STRATEGY_ID",
    "RelativeStrengthChallenger",
    "RelativeStrengthProvider",
    "rs_provider_from_map",
    "WalkForwardPipeline",
    "WalkForwardReport",
    "WalkForwardSchedule",
    "WalkForwardSplit",
    "WalkForwardSplitResult",
    "generate_walk_forward_schedule",
    "KNOWN_REPORT_KINDS",
    "REPORT_KIND_COMPARISON",
    "REPORT_KIND_WALK_FORWARD",
    "ResearchReport",
    "ResearchReportPaths",
    "render_comparison_report",
    "render_walk_forward_report",
    "APPROVAL_EVIDENCE_KEY",
    "KNOWN_COMPARATORS",
    "PROMOTION_STATES",
    "REQUIRED_EVIDENCE_PER_STATE",
    "STANDARD_ROLLBACK_CRITERIA",
    "STATE_APPROVED",
    "STATE_BACKTEST",
    "STATE_CANDIDATE",
    "STATE_DISABLED",
    "STATE_PAPER_TRADING",
    "STATE_PRODUCTION",
    "STATE_WALK_FORWARD",
    "ApprovalRecord",
    "PromotionEntry",
    "PromotionReport",
    "RollbackAlert",
    "RollbackCriterion",
    "evaluate_promotion",
    "is_terminal_state",
    "next_state",
    "state_index",
    "triggered_alerts",
    "KNOWN_STATS_LABELS",
    "LABEL_HYPOTHESIS",
    "LABEL_VALIDATED",
    "METRIC_DISAGREEMENT_RATE",
    "METRIC_RANK_DELTA_MEAN",
    "METRIC_SCORE_DELTA_MEAN",
    "METRIC_SELECTION_AGREEMENT_RATE",
    "METRIC_WF_DISAGREEMENT_RATE",
    "METRIC_WF_SCORE_DELTA_MEAN",
    "STATS_DEFAULT_CONFIDENCE_LEVEL",
    "STATS_SAMPLE_SIZE_FLOOR",
    "SUPPORTED_CONFIDENCE_LEVELS",
    "StatisticalFinding",
    "analyze_comparison",
    "analyze_walk_forward",
    "cohens_d_one_sample",
    "cohens_d_two_sample",
    "findings_stable_hash",
    "normal_mean_ci",
    "wilson_proportion_ci",
    "OUTCOME_KEY_RETURN_PCT",
    "OUTCOME_KEY_WIN",
    "PATTERN_MIN_SAMPLE_SIZE",
    "PatternHypothesis",
    "PatternObservation",
    "discover_patterns",
    "hypotheses_stable_hash",
    "validate_patterns_out_of_sample",
    "FI_SAMPLE_SIZE_FLOOR",
    "FLAG_CI_SPANS_ZERO",
    "FLAG_LOW_SAMPLE",
    "FLAG_ZERO_VARIANCE",
    "METHOD_PEARSON",
    "FeatureImportanceScore",
    "FeatureObservation",
    "analyze_feature_importance",
    "filter_lookahead_observations",
    "fisher_z_ci",
    "importance_stable_hash",
    "pearson_r",
    "ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER",
    "DEFAULT_EVIDENCE_KEY",
    "DEFAULT_MAX_WEIGHT_CHANGE_PCT",
    "DEFAULT_MIN_FEATURE_SCORE",
    "FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER",
    "MAX_ALLOWED_PROMOTION_STATE",
    "RecommendationEnvelope",
    "WeightRecommendation",
    "envelope_for",
    "recommend_weights",
    "recommendations_stable_hash",
    "DEFAULT_LEARNING_REPORT_OUTPUT_DIR",
    "LEARNING_REPORT_KIND",
    "LearningReport",
    "LearningReportPaths",
    "render_learning_report",
    "LearningPipeline",
    "LearningPipelineError",
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
