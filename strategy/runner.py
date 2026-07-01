"""
Champion/Challenger Runner
===========================

Wraps the trading evaluation pipeline so the Champion (current production
strategy) executes trades while the Challenger (new features) runs in shadow
mode — recording recommendations without executing.

When ALL feature flags are disabled, the Challenger produces no output and
the system is functionally identical to the previous codebase.

Usage:
    runner = ChampionChallengerRunner()
    result = runner.evaluate_setup("NVDA", evaluate_etf_setup_fn)
    # result.champion → always runs, executes trades
    # result.challenger → runs when flags enabled, shadow only
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from strategy.config import (
    CHAMPION_NAME,
    CHALLENGER_NAME,
    FeatureFlags,
    get_feature_flags,
)
from strategy.trade_logger import TradeLogger

# Module-level trade logger — logs all Champion (executed) trades
trade_logger = TradeLogger()


@dataclass
class SetupResult:
    """Single evaluation result from either Champion or Challenger."""
    source: str                    # "champion" or "challenger"
    symbol: str
    setup: Optional[Dict[str, Any]]  # The raw setup dict from evaluate_etf_setup
    recommendation: Optional[str]  # "BUY", "SELL", "HOLD", or None
    reasons: list = field(default_factory=list)
    score: float = 0.0
    feature_flags_used: list = field(default_factory=list)


@dataclass
class EvaluationResult:
    """Combined result from both Champion and Challenger evaluations."""
    champion: SetupResult
    challenger: Optional[SetupResult] = None
    flags_enabled: list = field(default_factory=list)

    @property
    def should_execute(self) -> bool:
        """Only Champion decisions execute. Challenger is shadow only."""
        return self.champion.source == CHAMPION_NAME

    @property
    def champion_qualified(self) -> bool:
        """Is the Champion recommending a trade?"""
        if self.champion.setup is None:
            return False
        return self.champion.setup.get("qualified", False)

    @property
    def challenger_differs(self) -> bool:
        """Did the Challenger disagree with the Champion?"""
        if self.challenger is None:
            return False
        return (
            self.challenger.recommendation is not None
            and self.champion.recommendation is not None
            and self.challenger.recommendation != self.champion.recommendation
        )


class ChampionChallengerRunner:
    """Run Champion and Challenger strategies in parallel.

    Champion: Always runs existing evaluation logic. Trades execute on its
    decisions.

    Challenger: Runs when at least one feature flag is enabled. Runs in
    shadow mode — logs recommendations but never executes trades.

    When all flags are disabled, Challenger is skipped entirely and the
    system behaves identically to the codebase before this change.
    """

    def __init__(self, flags: Optional[FeatureFlags] = None):
        self.flags = flags or get_feature_flags()

    def evaluate_setup(
        self,
        symbol: str,
        evaluate_fn: Callable[[str], Optional[Dict[str, Any]]],
    ) -> EvaluationResult:
        """Evaluate a symbol through both Champion and Challenger.

        Args:
            symbol: Ticker to evaluate.
            evaluate_fn: The existing evaluation function
                (e.g., evaluate_etf_setup). Called by Champion.

        Returns:
            EvaluationResult with champion result and optional challenger result.
        """
        # --- Champion: Always runs existing logic ---
        setup = evaluate_fn(symbol)
        champion_result = self._build_champion_result(symbol, setup)

        # --- Challenger: Only runs when flags are enabled ---
        if self.flags.all_disabled:
            return EvaluationResult(
                champion=champion_result,
                challenger=None,
                flags_enabled=[],
            )

        challenger_result = self._run_challenger(symbol, setup)
        return EvaluationResult(
            champion=champion_result,
            challenger=challenger_result,
            flags_enabled=self.flags.enabled_flags,
        )

    def _build_champion_result(
        self, symbol: str, setup: Optional[Dict[str, Any]]
    ) -> SetupResult:
        """Build Champion result from existing evaluation output.

        This is a PASSTHROUGH — it does not modify the setup dict.
        The Champion is the existing strategy, unchanged.
        """
        if setup is None:
            return SetupResult(
                source=CHAMPION_NAME,
                symbol=symbol,
                setup=None,
                recommendation=None,
                score=0.0,
            )

        gate_count = setup.get("gate_count", 0)
        qualified = setup.get("qualified", False)
        score = setup.get("score", 0.0)

        # Champion recommendation: same logic as before
        if qualified and gate_count >= 4:
            recommendation = "BUY"
            reasons = [f"{gate_count}/6 gates passed"]
            for gate, passed in setup.get("gates", {}).items():
                if passed:
                    reasons.append(f"✓ {gate}")
        elif qualified:
            recommendation = "HOLD"
            reasons = [f"{gate_count}/6 gates passed (below threshold)"]
        else:
            recommendation = None
            reasons = [f"Insufficient gates: {gate_count}/6"]

        return SetupResult(
            source=CHAMPION_NAME,
            symbol=symbol,
            setup=setup,
            recommendation=recommendation,
            reasons=reasons,
            score=score,
            feature_flags_used=[],
        )

    def _run_challenger(
        self, symbol: str, setup: Optional[Dict[str, Any]]
    ) -> SetupResult:
        """Run Challenger evaluation with enabled feature flags.

        Currently a stub that forwards the Champion setup. Each new feature
        will add its own analysis here, gated by its flag.

        IMPORTANT: Challenger NEVER executes trades. It only produces
        shadow recommendations for comparison.
        """
        flags_used = []

        # --- Feature: Historical Statistics ---
        if self.flags.enable_historical_statistics:
            flags_used.append("historical_statistics")
            # TODO Sprint 1: Add win-rate history for this symbol

        # --- Feature: Post-Game Review ---
        if self.flags.enable_post_game_review:
            flags_used.append("post_game_review")
            # TODO Sprint 2: Add post-trade analysis

        # --- Feature: Relative Strength ---
        if self.flags.enable_relative_strength:
            flags_used.append("relative_strength")
            # TODO: Compare against sector/peer benchmark

        # --- Feature: Market Regime ---
        if self.flags.enable_market_regime:
            flags_used.append("market_regime")
            # TODO: Adjust signal based on regime detection

        # --- Feature: Sell Score ---
        if self.flags.enable_sell_score:
            flags_used.append("sell_score")
            # TODO: Composite sell signal from multiple factors

        # --- Feature: Risk-Based Position Sizing ---
        if self.flags.enable_risk_based_position_sizing:
            flags_used.append("risk_based_position_sizing")
            # TODO: ATR-based position sizing

        # --- Feature: Overnight Risk Engine ---
        if self.flags.enable_overnight_risk_engine:
            flags_used.append("overnight_risk_engine")
            # TODO: Overnight risk assessment

        # --- Feature: Morning Intelligence ---
        if self.flags.enable_morning_intelligence:
            flags_used.append("morning_intelligence")
            # TODO: Pre-market briefing

        # --- Feature: Confidence Score ---
        if self.flags.enable_confidence_score:
            flags_used.append("confidence_score")
            # TODO: Overall confidence metric

        # --- Feature: Statistical Decision Support ---
        if self.flags.enable_statistical_decision_support:
            flags_used.append("statistical_decision_support")
            # TODO: Bayesian/statistical decision layer

        # Challenger inherits Champion setup but can modify recommendation
        # when features are proven effective in shadow testing.
        champion_setup = setup
        if champion_setup is None:
            return SetupResult(
                source=CHALLENGER_NAME,
                symbol=symbol,
                setup=None,
                recommendation=None,
                score=0.0,
                feature_flags_used=flags_used,
            )

        return SetupResult(
            source=CHALLENGER_NAME,
            symbol=symbol,
            setup=champion_setup,
            recommendation=None,  # No modification until features are validated
            reasons=[
                f"Challenger running with {len(flags_used)} feature(s): "
                f"{', '.join(flags_used)}"
            ],
            score=champion_setup.get("score", 0.0),
            feature_flags_used=flags_used,
        )

    def evaluate_sell(
        self,
        symbol: str,
        setup: Dict[str, Any],
    ) -> EvaluationResult:
        """Evaluate sell signals through Champion/Challenger.

        Champion: Uses existing sell logic (overbought RSI, upper BB,
        MACD death cross).

        Challenger: Adds sell_score and other sell-side features when
        enabled.

        Args:
            symbol: Ticker to evaluate.
            setup: Raw setup dict from evaluate_etf_setup.

        Returns:
            EvaluationResult with sell recommendations.
        """
        # Champion sell: existing logic (no change)
        champion_result = self._champion_sell(symbol, setup)

        if self.flags.all_disabled:
            return EvaluationResult(
                champion=champion_result,
                challenger=None,
                flags_enabled=[],
            )

        challenger_result = self._challenger_sell(symbol, setup)
        return EvaluationResult(
            champion=champion_result,
            challenger=challenger_result,
            flags_enabled=self.flags.enabled_flags,
        )

    def _champion_sell(
        self, symbol: str, setup: Dict[str, Any]
    ) -> SetupResult:
        """Existing sell logic — unchanged.

        Triggers: RSI >= 75, price above upper Bollinger Band,
        or MACD histogram turns negative (death cross).
        """
        rsi = setup.get("rsi", 0)
        macd_hist = setup.get("macd_hist", 0)
        macd_hist_prev = setup.get("macd_hist_prev", 0)
        price = setup.get("price", 0)
        bb_upper = setup.get("bb_upper", float("inf"))

        reasons = []
        recommendation = None

        # Overbought RSI
        if rsi >= 75:
            reasons.append(f"Overbought RSI: {rsi:.1f}")
            recommendation = "SELL"

        # Upper Bollinger Band
        if price > bb_upper:
            reasons.append(f"Price ${price:.2f} above BB upper ${bb_upper:.2f}")
            recommendation = "SELL"

        # MACD death cross
        if macd_hist < 0 and macd_hist_prev > 0:
            reasons.append(
                f"MACD death cross: {macd_hist_prev:.2f} → {macd_hist:.2f}"
            )
            recommendation = "SELL"

        return SetupResult(
            source=CHAMPION_NAME,
            symbol=symbol,
            setup=setup,
            recommendation=recommendation,
            reasons=reasons,
            score=setup.get("score", 0),
            feature_flags_used=[],
        )

    def _challenger_sell(
        self, symbol: str, setup: Dict[str, Any]
    ) -> SetupResult:
        """Challenger sell evaluation with enabled feature flags.

        Currently inherits Champion sell logic. New sell features will
        layer on top when their flags are enabled.
        """
        flags_used = []

        if self.flags.enable_sell_score:
            flags_used.append("sell_score")
            # TODO: Compute composite sell score

        if self.flags.enable_overnight_risk_engine:
            flags_used.append("overnight_risk_engine")
            # TODO: Overnight risk-based sell signal

        # For now, Challenger inherits Champion sell decision
        champion = self._champion_sell(symbol, setup)
        champion.reasons.append(
            f"Challenger flags: {', '.join(flags_used) if flags_used else 'none'}"
        )
        champion.source = CHALLENGER_NAME
        champion.feature_flags_used = flags_used

        return champion
