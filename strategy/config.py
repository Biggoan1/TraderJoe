"""
Trader Joe Strategy Configuration & Feature Flags
==================================================

All new features are DISABLED by default. Each flag can be toggled
independently. Disabling a feature must completely restore existing
production behavior.

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

from dataclasses import dataclass, field
from typing import List


@dataclass
class FeatureFlags:
    """All new features disabled by default.

    Each flag guards a single feature. When disabled, the Champion
    strategy runs identically to the previous codebase.
    """

    # --- Data collection (enable first) ---
    enable_historical_statistics: bool = False
    enable_post_game_review: bool = False

    # --- Analysis features ---
    enable_relative_strength: bool = False
    enable_market_regime: bool = False
    enable_sector_leadership: bool = False
    enable_market_breadth: bool = False
    enable_confidence_score: bool = False
    enable_statistical_decision_support: bool = False

    # --- Decision features (enable after analysis is proven) ---
    enable_sell_score: bool = False
    enable_risk_based_position_sizing: bool = False

    # --- Timing features ---
    enable_overnight_risk_engine: bool = False
    enable_morning_intelligence: bool = False

    @property
    def enabled_flags(self) -> List[str]:
        """Return list of currently enabled feature names."""
        return [name for name, value in self.__dict__.items() if value is True]

    @property
    def all_disabled(self) -> bool:
        """True when no new features are active."""
        return len(self.enabled_flags) == 0

    def enable(self, flag_name: str) -> None:
        """Enable a single feature by name."""
        if hasattr(self, flag_name):
            setattr(self, flag_name, True)
        else:
            raise ValueError(f"Unknown feature flag: {flag_name}")

    def disable(self, flag_name: str) -> None:
        """Disable a single feature by name."""
        if hasattr(self, flag_name):
            setattr(self, flag_name, False)
        else:
            raise ValueError(f"Unknown feature flag: {flag_name}")

    def enable_all(self) -> None:
        """Enable every new feature (use only for full integration testing)."""
        for key in self.__dict__:
            setattr(self, key, True)


# --- Champion/Challenger config ---

# Champion = current production strategy. Executes trades.
# Challenger = new features. Paper recommendations only.
CHAMPION_NAME = "champion"
CHALLENGER_NAME = "challenger"

# --- Sell Score config ---
SELL_SCORE_THRESHOLD = 70           # Score above which we recommend selling (0-100)
SELL_SCORE_HOLD_THRESHOLD = 40      # Score below which we confidently hold
SELL_SCORE_FACTOR_WEIGHTS = {
    "price_below_sma20": 15,
    "macd_deterioration": 15,
    "relative_strength_weakening": 10,
    "adx_weakening": 10,
    "high_volume_selling": 10,
    "support_failure": 15,
    "trailing_stop_breach": 10,
    "time_stop": 5,
    "profit_target_reached": 10,
}

# --- Market Regime config ---
REGIME_SPY_SMA_PERIOD = 20
REGIME_QQQ_SMA_PERIOD = 20
REGIME_BREADTH_THRESHOLD = 0.5     # Fraction of SPY components above SMA20
REGIME_VIX_BULL_MAX = 20
REGIME_VIX_BEAR_MIN = 30

# --- Risk-Based Position Sizing config ---
RISK_PER_TRADE_DEFAULT = 0.02      # 2% of portfolio per trade
RISK_PER_TRADE_MAX = 0.05          # 5% max
RISK_PER_TRADE_MIN = 0.005         # 0.5% min
ATR_PERIOD = 14
MAX_SECTOR_EXPOSURE = 0.25         # 25% max in single sector
MAX_PORTFOLIO_RISK = 0.10          # 10% total portfolio risk

# --- Overnight Risk Engine config ---
OVERNIGHT_GAP_THRESHOLD_PERCENT = 2.0  # Gap % flagged as significant
OVERNIGHT_RISK_THRESHOLD = 60      # Future risk-score threshold (0-100)
OVERNIGHT_EVAL_START_HOUR = 15     # 3:00 PM ET
OVERNIGHT_EVAL_START_MINUTE = 45   # 3:45 PM ET

# --- Morning Intelligence config ---
MORNING_INTEL_HOUR = 4             # 4:00 AM ET
MORNING_INTEL_MINUTE = 0

# --- Historical Statistics config ---
STATS_AGGREGATION_PERIODS = ["1d", "5d", "1w", "1mo", "3mo", "all"]

# --- Singleton ---
_flags: FeatureFlags | None = None


def get_feature_flags() -> FeatureFlags:
    """Return the global FeatureFlags instance (lazy singleton)."""
    global _flags
    if _flags is None:
        _flags = FeatureFlags()
    return _flags


def reset_feature_flags() -> FeatureFlags:
    """Reset to all-disabled defaults (for testing)."""
    global _flags
    _flags = FeatureFlags()
    return _flags
