"""Research-only Champion scoring engine.

Mirrors the six-gate scoring math of ``trader.py::evaluate_etf_setup``
without any of the live path's yfinance I/O, market-hours state, or
broker calls.  The live scorer at ``trader.py:157-280`` fetches its
own bars inside the function and is therefore not extractable as a
pure function; this module reproduces the same gate arithmetic on
caller-supplied bar dicts so the historical validation pipeline can
emit meaningful Champion explanations without touching the live path.

The score components map 1:1 to the live path:

- ``above_sma20``          contributes +1.5 when close > SMA(20)
- ``sma20_above_sma50``    contributes +1.5 when SMA(20) > SMA(50)
- ``rsi_not_overbought``   contributes +1.0 when RSI(14) < 70
- ``macd_positive``        contributes +1.5 when MACD histogram > 0
- ``adx_strength``         contributes +1.0 when ADX(14) > 15 plus a
                           continuous bonus of ``min((ADX-15)/10, 1.5)``
- ``volume_confirmation``  contributes +0.5 when volume > 0.8 * avg20
                           plus a continuous bonus up to +1.0

Six gates, integer gate count, final score is the unclamped sum.
A symbol is flagged ``rejected=True`` when fewer than
``min_gate_count`` gates fired — matching the live path's
``qualified = gate_count >= 4`` filter.

This module never imports ``trader.py`` and never emits any order
or broker signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import ComparisonEvaluator
from strategy.score_explanation import (
    ScoreComponent,
    ScoreExplanation,
    blank_explanation,
)


DEFAULT_SMA_SHORT = 20
DEFAULT_SMA_LONG = 50
DEFAULT_RSI_PERIOD = 14
DEFAULT_RSI_OVERBOUGHT = 70.0
DEFAULT_ADX_PERIOD = 14
DEFAULT_ADX_MIN = 15.0
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_VOLUME_LOOKBACK = 20
DEFAULT_VOLUME_RATIO_FLOOR = 0.8
DEFAULT_MIN_GATE_COUNT = 4

RESEARCH_CHAMPION_STRATEGY_ID = "research-champion-v0.1.0"


@dataclass(frozen=True)
class ChampionScoringConfig:
    """Parameters for the research-only Champion scorer."""

    sma_short: int = DEFAULT_SMA_SHORT
    sma_long: int = DEFAULT_SMA_LONG
    rsi_period: int = DEFAULT_RSI_PERIOD
    rsi_overbought: float = DEFAULT_RSI_OVERBOUGHT
    adx_period: int = DEFAULT_ADX_PERIOD
    adx_min: float = DEFAULT_ADX_MIN
    macd_fast: int = DEFAULT_MACD_FAST
    macd_slow: int = DEFAULT_MACD_SLOW
    macd_signal: int = DEFAULT_MACD_SIGNAL
    volume_lookback: int = DEFAULT_VOLUME_LOOKBACK
    volume_ratio_floor: float = DEFAULT_VOLUME_RATIO_FLOOR
    min_gate_count: int = DEFAULT_MIN_GATE_COUNT

    def min_history_required(self) -> int:
        """Minimum bar count required to fire every gate."""
        return max(
            self.sma_long + 1,
            self.rsi_period + 1,
            self.adx_period * 2 + 1,
            self.macd_slow + self.macd_signal,
            self.volume_lookback + 1,
        )


# ---------------------------------------------------------------------------
# Pure indicator math (float lists in, floats out)
# ---------------------------------------------------------------------------


def _sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def _rsi_wilder(closes: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    alpha = 1.0 / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = avg_gain * (1 - alpha) + gain * alpha
        avg_loss = avg_loss * (1 - alpha) + loss * alpha
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    ema = seed
    for value in values[period:]:
        ema = value * alpha + ema * (1 - alpha)
    return ema


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    if period <= 0 or len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    series: List[float] = []
    seed = sum(values[:period]) / period
    ema = seed
    series.append(ema)
    for value in values[period:]:
        ema = value * alpha + ema * (1 - alpha)
        series.append(ema)
    return series


def _macd_histogram(
    closes: Sequence[float],
    fast: int,
    slow: int,
    signal: int,
) -> Optional[float]:
    if slow <= fast or signal <= 0:
        return None
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    if not fast_series or not slow_series:
        return None
    # Align both series to the shorter one (slow starts later).
    offset = len(fast_series) - len(slow_series)
    if offset < 0:
        return None
    fast_aligned = fast_series[offset:]
    macd_line = [f - s for f, s in zip(fast_aligned, slow_series)]
    if len(macd_line) < signal:
        return None
    signal_line = _ema(macd_line, signal)
    if signal_line is None:
        return None
    return macd_line[-1] - signal_line


def _adx_wilder(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> Optional[float]:
    if period <= 0:
        return None
    n = min(len(highs), len(lows), len(closes))
    if n < period * 2 + 1:
        return None
    tr_list: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_list.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    if len(tr_list) < period:
        return None
    atr = sum(tr_list[:period]) / period
    plus_di_smooth = sum(plus_dm[:period]) / period
    minus_di_smooth = sum(minus_dm[:period]) / period
    dx_values: List[float] = []
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_di_smooth = (
            plus_di_smooth * (period - 1) + plus_dm[i]
        ) / period
        minus_di_smooth = (
            minus_di_smooth * (period - 1) + minus_dm[i]
        ) / period
        if atr == 0:
            continue
        plus_di = 100.0 * plus_di_smooth / atr
        minus_di = 100.0 * minus_di_smooth / atr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)
    if len(dx_values) < period:
        return None
    return sum(dx_values[-period:]) / period


def _volume_avg(volumes: Sequence[float], period: int) -> Optional[float]:
    return _sma(volumes, period)


# ---------------------------------------------------------------------------
# Per-symbol scoring
# ---------------------------------------------------------------------------


def _extract_series(
    bars: Sequence[Mapping[str, Any]],
) -> Tuple[List[float], List[float], List[float], List[float]]:
    closes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    volumes: List[float] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        close = bar.get("c")
        if close is None:
            continue
        try:
            closes.append(float(close))
        except (TypeError, ValueError):
            continue
        highs.append(float(bar.get("h", close)))
        lows.append(float(bar.get("l", close)))
        volumes.append(float(bar.get("v", 0.0)))
    return closes, highs, lows, volumes


def score_bars_for_event(
    bars: Sequence[Mapping[str, Any]],
    strategy_id: str,
    symbol: str,
    event_timestamp: str,
    config: Optional[ChampionScoringConfig] = None,
) -> Tuple[float, ScoreExplanation]:
    """Score a single symbol at a single event.

    ``bars`` must be sorted ascending by timestamp and cover only the
    period at or before ``event_timestamp``.  Callers slice history
    themselves — this function trusts the input.

    Returns ``(final_score, explanation)``.  When the input is too
    short for any gate, ``final_score`` is ``0.0`` and the explanation
    is a rejected placeholder describing the missing history.
    """
    cfg = config if config is not None else ChampionScoringConfig()
    closes, highs, lows, volumes = _extract_series(bars)

    if len(closes) < cfg.min_history_required():
        return 0.0, ScoreExplanation(
            strategy_id=strategy_id,
            symbol=symbol,
            event_timestamp=event_timestamp,
            final_score=0.0,
            rejected=True,
            rejection_reasons=(
                f"insufficient_history: have={len(closes)} "
                f"need>={cfg.min_history_required()}",
            ),
        )

    sma_short = _sma(closes, cfg.sma_short)
    sma_long = _sma(closes, cfg.sma_long)
    rsi = _rsi_wilder(closes, cfg.rsi_period)
    macd_hist = _macd_histogram(
        closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
    )
    adx = _adx_wilder(highs, lows, closes, cfg.adx_period)
    vol_avg = _volume_avg(volumes, cfg.volume_lookback)

    close = closes[-1]
    volume = volumes[-1] if volumes else 0.0

    components: List[ScoreComponent] = []
    bonuses: List[Tuple[str, float]] = []
    ranking_factors: List[str] = []
    gate_count = 0

    def _add(name: str, contribution: float, detail: str) -> None:
        components.append(
            ScoreComponent(name=name, contribution=contribution, detail=detail)
        )
        ranking_factors.append(name)

    if sma_short is not None and close > sma_short:
        _add(
            "above_sma20",
            1.5,
            f"close={close:.4f} > sma{cfg.sma_short}={sma_short:.4f}",
        )
        gate_count += 1
    if sma_short is not None and sma_long is not None and sma_short > sma_long:
        _add(
            "sma20_above_sma50",
            1.5,
            f"sma{cfg.sma_short}={sma_short:.4f} > sma{cfg.sma_long}={sma_long:.4f}",
        )
        gate_count += 1
    if rsi is not None and rsi < cfg.rsi_overbought:
        _add(
            "rsi_not_overbought",
            1.0,
            f"rsi={rsi:.2f} < {cfg.rsi_overbought}",
        )
        gate_count += 1
    if macd_hist is not None and macd_hist > 0:
        _add(
            "macd_positive",
            1.5,
            f"macd_hist={macd_hist:.4f}",
        )
        gate_count += 1
    momentum_contribution: Optional[float] = None
    if adx is not None and adx > cfg.adx_min:
        _add(
            "adx_strength",
            1.0,
            f"adx={adx:.2f} > {cfg.adx_min}",
        )
        gate_count += 1
        continuous = min(max(adx - cfg.adx_min, 0.0) / 10.0, 1.5)
        if continuous > 0:
            bonuses.append(("adx_continuous", continuous))
            momentum_contribution = continuous
    if (
        vol_avg is not None
        and vol_avg > 0
        and volume > vol_avg * cfg.volume_ratio_floor
    ):
        _add(
            "volume_confirmation",
            0.5,
            f"volume={volume:.0f} > {cfg.volume_ratio_floor} * avg{cfg.volume_lookback}",
        )
        gate_count += 1
        continuous = min(
            max(volume / vol_avg - cfg.volume_ratio_floor, 0.0), 1.0
        )
        if continuous > 0:
            bonuses.append(("volume_continuous", continuous))

    final_score = sum(c.contribution for c in components) + sum(
        m for _, m in bonuses
    )

    rejected = gate_count < cfg.min_gate_count
    rejection_reasons: Tuple[str, ...] = ()
    if rejected:
        rejection_reasons = (
            f"gate_count={gate_count} < min={cfg.min_gate_count}",
        )

    confidence: Optional[float] = None
    max_gates = 6
    if gate_count > 0:
        confidence = round(gate_count / max_gates, 4)

    explanation = ScoreExplanation(
        strategy_id=strategy_id,
        symbol=symbol,
        event_timestamp=event_timestamp,
        final_score=round(final_score, 6),
        components=tuple(components),
        ranking_factors=tuple(ranking_factors),
        momentum_contribution=(
            None
            if momentum_contribution is None
            else round(momentum_contribution, 6)
        ),
        bonuses=tuple(bonuses),
        rejected=rejected,
        rejection_reasons=rejection_reasons,
        confidence=confidence,
        notes=(f"gate_count={gate_count}/{max_gates}",),
    )
    return round(final_score, 6), explanation


# ---------------------------------------------------------------------------
# Multi-symbol evaluator (ComparisonEvaluator surface)
# ---------------------------------------------------------------------------


@dataclass
class ChampionScorer:
    """Deterministic multi-symbol Champion scorer for research replay.

    Constructed once from the fetched bars and asked to
    ``evaluate(event)`` per replay event.  Returns a
    :class:`~strategy.backtest_lab.StrategyEvaluation` populated with
    scores, rankings (only symbols passing the gate floor), free-text
    explanations for backward compat, and structured
    :class:`ScoreExplanation` objects that the comparison harness
    forwards downstream.
    """

    strategy_id: str
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]
    symbols: Sequence[str]
    config: ChampionScoringConfig = field(default_factory=ChampionScoringConfig)

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        cutoff = event.timestamp
        scores: Dict[str, float] = {}
        structured: Dict[str, ScoreExplanation] = {}
        explanations_str: Dict[str, str] = {}
        warnings: List[str] = []
        selected: List[Tuple[str, float]] = []
        for symbol in self.symbols:
            bars = self._bars_up_to(symbol, cutoff)
            if not bars:
                exp = blank_explanation(
                    self.strategy_id,
                    symbol,
                    event.timestamp,
                    0.0,
                    note=f"no bars available at or before {cutoff}",
                )
                structured[symbol] = exp
                explanations_str[symbol] = exp.to_summary_str()
                continue
            score, exp = score_bars_for_event(
                bars,
                strategy_id=self.strategy_id,
                symbol=symbol,
                event_timestamp=event.timestamp,
                config=self.config,
            )
            scores[symbol] = score
            structured[symbol] = exp
            explanations_str[symbol] = exp.to_summary_str()
            if not exp.rejected:
                selected.append((symbol, score))
        selected.sort(key=lambda pair: (-pair[1], pair[0]))
        rankings = [
            {"symbol": symbol, "rank": rank + 1}
            for rank, (symbol, _) in enumerate(selected)
        ]
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=scores,
            rankings=rankings,
            explanations=explanations_str,
            structured_explanations=dict(structured),
            warnings=warnings,
        )

    def _bars_up_to(
        self, symbol: str, cutoff: str
    ) -> List[Mapping[str, Any]]:
        bars = self.bars_by_symbol.get(symbol) or []
        filtered: List[Mapping[str, Any]] = []
        for bar in bars:
            if not isinstance(bar, Mapping):
                continue
            t = bar.get("t")
            if t is None:
                continue
            if str(t) > cutoff:
                continue
            filtered.append(bar)
        # Preserve input order (bars already come sorted from Alpaca)
        return filtered
