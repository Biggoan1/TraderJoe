# Strategy Identities

Every registered strategy in `strategy/lab/*.py`, documented from the
source. Each section covers purpose, timeframe, ideal market,
weaknesses, required history, decision process, inputs, outputs,
current research findings, current production status, and future ideas.

**All strategies are research-only.** No strategy in this document is
promoted; every `PromotionEntry` sits at `STATE_DISABLED`.

## Champion

**File:** `strategy/lab/champion.py` (adapter),
`strategy/champion_scoring.py` (scorer).

**Identity:**

- `name` — `champion`
- `version` — `0.4.0`
- `strategy_id` — `research-champion-v0.1.0`

**Purpose.** Reproduces the six-gate scorer from the live path
(`trader.py::evaluate_etf_setup`) as a pure function of caller-supplied
bar dicts. No I/O, no market-hours state, no broker calls.

**Timeframe.** Daily bars. Not declared for intraday.

**Ideal market.** Trending environments where all six gates align.
The `min_gate_count=4` threshold means a candidate must fire the
majority of gates before it's a scoreable setup.

**Weaknesses.** Poor performance in whipsaw / mean-reversion
regimes. The gates are momentum-heavy. In a Sideways regime
(`Sideways_2023H1`) the scorer produces few candidates and those it
does produce are prone to false starts.

**Required history.** `config.min_history_required()` =
`max(sma_long+1, rsi_period+1, adx_period*2+1, macd_slow+macd_signal,
volume_lookback+1)`. Defaults: 51 bars.

**Decision process.** Six independent gates, each contributing to a
composite score. `rejected = gate_count < min_gate_count`. Rejected
symbols are dropped from `rankings`.

The six gates and their contributions
(`strategy/champion_scoring.py:11-20`):

| Gate | Condition | Contribution |
|---|---|---|
| `above_sma20` | `close > SMA(20)` | +1.5 |
| `sma20_above_sma50` | `SMA(20) > SMA(50)` | +1.5 |
| `rsi_not_overbought` | `RSI(14) < 70` | +1.0 |
| `macd_positive` | MACD histogram > 0 | +1.5 |
| `adx_strength` | `ADX(14) > 15` | +1.0 plus `min((ADX-15)/10, 1.5)` continuous bonus |
| `volume_confirmation` | `volume > 0.8 * avg20` | +0.5 plus up to +1.0 continuous bonus |

Base contributions sum to `7.0` before continuous bonuses. Maximum
gate count is 6; `confidence = round(gate_count / 6, 4)`.

**Parameters (defaults, from `ChampionScoringConfig`
`champion_scoring.py:45-56`):** `sma_short=20`, `sma_long=50`,
`rsi_period=14`, `rsi_overbought=70.0`, `adx_period=14`,
`adx_min=15.0`, `macd_fast=12`, `macd_slow=26`, `macd_signal=9`,
`volume_lookback=20`, `volume_ratio_floor=0.8`, `min_gate_count=4`.

**Inputs.** `bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]`,
`symbols: Sequence[str]`, `strategy_id`. Bars need `t, o, h, l, c, v`
keys.

**Outputs.** `StrategyEvaluation` — `scores` (float per symbol),
`rankings` (only non-rejected, sorted by `(-score, symbol)`),
`structured_explanations` (per-symbol `ScoreExplanation` with
`ScoreComponent`s, `rejected` flag, `rejection_reasons`,
`confidence`).

**Current research findings.**

- On the current watchlist (AAPL, MSFT, NVDA, SPY, QQQ), a six-month
  simulation (`reports/research_summaries/portfolio-sim-6mo-watchlist.md`)
  produced 20 trades, 65% win rate, 17.8-day average hold, max
  drawdown 3.93%, Sharpe 2.00.
- Gate rejection is the primary risk filter. During the January-March
  2026 tape, the scorer rejected most bars and only started firing in
  late March — sparing the account from a whipsaw window.

**Current production status.** `STATE_DISABLED`. The live runner
(`trader.py`) implements the same math independently; the research
scorer is used only for evidence generation.

**Future ideas.** Sweep `min_gate_count` from 3 to 5 on regime-tagged
windows. Test with `sma_long=100` on the multi-year matrix. Compare
per-regime performance.

## Champion RS

**File:** `strategy/lab/champion_rs.py` (adapter),
`strategy/rs_challenger.py` (evaluator).

**Identity:**

- `name` — `champion_rs`
- `version` — `0.1.0`
- `strategy_id` — `rs-challenger-v0.1.0`

**Purpose.** Wraps `ChampionScorer` with a Relative-Strength
composite overlay. When `FeatureFlags.enable_relative_strength` is
off (default), returns Champion scores unchanged — only the
`strategy_id` label differs. When on, adds an overlay contribution
proportional to `(rs_score - neutral_score) / score_range` scaled by
`overlay_weight`.

**Timeframe.** Daily bars.

**Ideal market.** Regimes where Relative Strength adds signal beyond
absolute price action. Not empirically demonstrated on the current
watchlist (see research findings).

**Weaknesses.** At every weight tested (0.20, 0.30, 0.50, 0.75, 1.00)
across the 1y matrix, the RS overlay produced worse realized returns
than the Champion baseline. Sharpe drops from 1.11 (Champion) to as
low as 0.67 at weight 0.75.

**Required history.** Same as Champion:
`config.min_history_required()`.

**Decision process.** Base = Champion score; overlay = `overlay_weight
* (rs_score - neutral_score) / score_range`, clipped to
`[neutral - range, neutral + range]`. Rejected symbols pass through
unchanged (post-B02 fix, commit `f3000d3`).

**Parameters (defaults, from `RS_CHALLENGER_*` in
`strategy/rs_challenger.py`):** `overlay_weight=0.20`,
`neutral_score=50.0`, `score_range=50.0`, `rs_lookback_days=20`.
Inherits `ChampionScoringConfig`.

**Inputs.** Same as Champion, plus an `rs_provider:
RelativeStrengthProvider` kwarg on `build_evaluator` (raises if
omitted, line 100). RS providers are typically built via
`rs_provider_from_map(rs_map)` and
`build_rs_map_from_bars(bars_by_symbol, symbols, benchmarks,
lookback_days)`.

**Outputs.** Same shape as Champion. Rankings can differ; entry-
selection frequently does not.

**Current research findings
(`reports/research_summaries/rs-weight-sweep-post-b02-2026-07-04.md`).**

- Zero effect on entry selection at any weight: `selection_agreement_rate
  = 1.000` across 250 events × 5 weights.
- All flips are ranking-only; RS shifts which selected symbol is
  ranked highest but never changes *which* symbol gets selected.
- Regime-conditioned means scale linearly with weight; credit_stress
  bucket empty; TLT bond-trend marginal.
- **Verdict:** do not promote.

**Current production status.** `STATE_DISABLED` behind
`enable_relative_strength=False`. Retained as a research artifact.

**Future ideas.** Test RS on a different universe (small-cap /
sector-neutral). Explore a nonlinear overlay (rank-based, not
linear). Consider RS as a filter (reject symbols below RS threshold)
instead of a score modifier.

## Momentum

**File:** `strategy/lab/momentum.py`.

**Identity:**

- `name` — `momentum`
- `version` — `0.1.0`
- `strategy_id` — `lab-momentum-v0.1.0`

**Purpose.** Rank symbols by trailing-`lookback`-day return.

**Timeframe.** Daily bars.

**Ideal market.** Trending markets with clear directional persistence.

**Weaknesses.** Symmetric — positive and negative returns rank
identically absent an entry threshold. Trend reversals produce
whipsaws. No volatility normalization.

**Required history.** `max(2, lookback + 1)` — with default
`lookback=20`, 21 bars.

**Decision process.**
`score = (close_now - close_{now-lookback}) / close_{now-lookback}`
(line 41-48).

**Parameters.** `lookback: int = 20`.

**Inputs.** Bars with `t` and `c` keys.

**Outputs.** `StrategyEvaluation.scores` and `.rankings` sorted by
descending score. Never rejects.

**Current research findings.** Baseline single-factor strategy.
Weekend Lab default sweep tests `lookback ∈ {10, 15, 20, 30}`.

**Current production status.** `STATE_DISABLED`.

**Future ideas.** Volatility-normalized momentum (return / stdev).
Compare against `volatility_regime_filter_v1` results.

## Momentum 15m

**File:** `strategy/lab/momentum_15m.py`.

**Identity:**

- `name` — `momentum_15m_v1`
- `version` — `0.1.0`
- `strategy_id` — `lab-momentum-15m-v0.1.0`

**Purpose.** Intraday momentum on 15-minute bars. Same shape as
daily `MomentumStrategy` but constrained to 15Min data.

**Timeframe.** 15-minute bars only.

**Ideal market.** Trending intraday sessions with steady flow.

**Weaknesses.** Session-open volatility. Lunch-time flat. Whipsaws on
news gaps.

**Required history.** `max(2, lookback_bars + 1)` — with default
`lookback_bars=26` (~one regular-hours session), 27 bars.

**Decision process.** Trailing-return over `lookback_bars`
15-minute closes.

**Parameters.** `lookback_bars: int = 26`.

**Supported intervals.** `INTRADAY_15M = (BarInterval.MINUTE_15,)`.
`requires_intraday_data()=True`. Raises `IntervalMismatchError` if
`build_evaluator` receives any other interval kwarg.

**Inputs.** 15-minute bars with `t` and `c` keys.

**Outputs.** Same as `MomentumStrategy`.

**Current research findings.** Not yet exercised — the equities
warehouse has no 15-minute datasets landed
(`market_data/equities/minute/` is empty). Simulator invocations
against warehouse data will hit `ResearchCacheError`.

**Current production status.** `STATE_DISABLED`. Awaiting warehouse
data.

**Future ideas.** Once 15-minute bars land, sweep `lookback_bars ∈
{13, 20, 26, 34}` on the intraday preset.

## Opening Range Breakout

**File:** `strategy/lab/opening_range_breakout.py`.

**Identity:**

- `name` — `opening_range_breakout_v1`
- `version` — `0.1.0`
- `strategy_id` — `lab-opening-range-breakout-v0.1.0`

**Purpose.** Intraday breakout signal computed from the first N bars
of the current regular-session date. Score is the current close's
displacement above/below the opening range's high/low, expressed as a
fraction of the range.

**Timeframe.** Intraday (any interval in `INTRADAY_ANY`).

**Ideal market.** Sessions with clear open-range bias (post-catalyst
opens, earnings days, macro-print days).

**Weaknesses.** Whipsaws in low-volatility ranges. False breakouts
from stop-runs. Score of `0.0` on flat ranges — the strategy can't
distinguish "no signal" from "inside the range."

**Required history.** `range_bars + 1` — with default `range_bars=2`
(30 minutes on 15-min bars), 3 bars.

**Decision process.** Take the first `range_bars` of the current
session. Score = `(close - range_high) / range_width` if breaking
above; `(close - range_low) / range_width` if breaking below;
`0.0` otherwise.

**Parameters.** `range_bars: int = 2`.

**Supported intervals.** `INTRADAY_ANY`.
`requires_intraday_data()=True`.

**Inputs.** Intraday bars with `t`, `o`, `h`, `l`, `c`, `v`.

**Outputs.** Positive score = breakout above range; negative =
breakdown below; near zero = inside the range or flat range or
insufficient session history.

**Current research findings.** Not yet exercised (see Momentum 15m).

**Current production status.** `STATE_DISABLED`.

**Future ideas.** Sweep `range_bars ∈ {1, 2, 3, 4}`. Add a
volatility-normalized variant. Combine with `volatility_regime_filter`
so quiet days skip the breakout.

## Trend

**File:** `strategy/lab/trend.py`.

**Identity:**

- `name` — `trend`
- `version` — `0.1.0`
- `strategy_id` — `lab-trend-v0.1.0`

**Purpose.** Fast/slow SMA crossover ranker.

**Timeframe.** Daily bars.

**Ideal market.** Persistent trends where SMA crossover lags gently
behind price. Bull markets like `Rally_2023H2`.

**Weaknesses.** Whipsaws when price oscillates around the slow SMA.
Late entry / late exit relative to breakout strategies.

**Required history.** `max(sma_fast, sma_slow) + 1` — with defaults
`sma_fast=20`, `sma_slow=50`, 51 bars.

**Decision process.** `score = (SMA_fast - SMA_slow) / close`.

**Parameters.** `sma_fast: int = 20`, `sma_slow: int = 50`.

**Inputs.** Bars with `t` and `c`.

**Outputs.** Positive score = fast leads slow (uptrend); negative =
slow leads fast (downtrend).

**Current research findings.** Baseline single-factor strategy used
as a matrix reference.

**Current production status.** `STATE_DISABLED`.

**Future ideas.** EMA variant. Golden-cross / death-cross event
detection instead of continuous score.

## Mean Reversion

**File:** `strategy/lab/mean_reversion.py`.

**Identity:**

- `name` — `mean_reversion`
- `version` — `0.1.0`
- `strategy_id` — `lab-mean-reversion-v0.1.0`

**Purpose.** Buy-the-dip signal relative to trailing SMA.

**Timeframe.** Daily bars.

**Ideal market.** Range-bound markets. `Sideways_2023H1`. Sector
consolidations.

**Weaknesses.** Trending markets — buying dips that keep dipping.

**Required history.** `lookback + 1` — with default `lookback=20`, 21
bars.

**Decision process.** `score = (SMA_lookback - close) / SMA_lookback`.

**Parameters.** `lookback: int = 20`.

**Inputs.** Bars with `t` and `c`.

**Outputs.** Positive score = close is below its trailing mean
(buy-the-dip); negative = close is above (fade-the-rally). Never
rejects.

**Current research findings.** Baseline mean-reversion counterpart to
Momentum. Weekend Lab does not sweep it by default.

**Current production status.** `STATE_DISABLED`.

**Future ideas.** Combine with `rsi_mean_reversion` for
double-filtering. Add volatility floor to skip low-vol tape.

## RSI Mean Reversion

**File:** `strategy/lab/rsi_mean_reversion.py`.

**Identity:**

- `name` — `rsi_mean_reversion_v1`
- `version` — `0.1.0`
- `strategy_id` — `lab-rsi-mean-reversion-v0.1.0`

**Purpose.** RSI-based mean reversion. Piecewise-linear reversal
signal centered around a neutral RSI.

**Timeframe.** Daily bars by default; also supported on intraday
(`DAILY_OR_INTRADAY`).

**Ideal market.** Range-bound with clear overbought/oversold turns.
`Sideways_2023H1` type regimes.

**Weaknesses.** RSI stays overbought / oversold in strong trends.
Whipsaws at neutral-zone reversals.

**Required history.** `rsi_period + 1` — with default `rsi_period=14`,
15 bars.

**Decision process.** Piecewise:

- `RSI ≤ oversold` → `score = (oversold - RSI) / max(oversold, 1.0)`
  (positive, magnitude scales toward 1 as RSI → 0).
- `RSI ≥ overbought` → `score = -(RSI - overbought) / max(100 -
  overbought, 1.0)` (negative).
- Neutral zone → `score = -(RSI - neutral) / half_width * 0.1` where
  `half_width = max(overbought - neutral, neutral - oversold, 1.0)`.

**Parameters.** `rsi_period: int = 14`, `oversold: float = 30.0`,
`overbought: float = 70.0`, `neutral: float = 50.0`.

**Supported intervals.** `DAILY_OR_INTRADAY`.
`requires_intraday_data()=False`.

**Inputs.** Bars with `t` and `c`.

**Outputs.** Positive score = oversold reversal signal; negative =
overbought fade or above-neutral drift; near zero in the neutral
zone.

**Current research findings.** New in this milestone. Included in
the `daily_swing_watchlist` and `crypto_24x7_paper_research` presets.

**Current production status.** `STATE_DISABLED`.

**Future ideas.** Sweep `rsi_period ∈ {7, 14, 21}` and `oversold /
overbought` bands. Compare intraday-15m vs daily fitness.

## Sector Rotation Daily

**File:** `strategy/lab/sector_rotation_daily.py`.

**Identity:**

- `name` — `sector_rotation_daily_v1`
- `version` — `0.1.0`
- `strategy_id` — `lab-sector-rotation-daily-v0.1.0`

**Purpose.** Rank symbols by their trailing-`lookback_days` return
*minus* their sector benchmark's trailing return. Positive =
leading; negative = lagging.

**Timeframe.** Daily only. Uses its own `_SectorRotationEvaluator`
rather than `SingleFactorStrategy`.

**Ideal market.** Regimes with clear sector leadership. Bull markets
where growth outperforms defensives, or vice versa.

**Weaknesses.** Requires the caller to provide a `sector_map`
otherwise falls back to `fallback_benchmark` for every symbol
(defaults to `SPY`). Sector benchmark bars must exist in the
`bars_by_symbol` payload.

**Required history.** `lookback_days + 1` — with default
`lookback_days=20`, 21 bars.

**Decision process.** For each symbol:

1. Get symbol's trailing return over `lookback_days`.
2. Look up sector from `sector_map[symbol.upper()]` else use
   `fallback_benchmark`.
3. Get sector's trailing return.
4. `score = sym_return - sector_return`.

If sector bars are unavailable, uses `sym_return` alone with a note.

**Parameters.** `lookback_days: int = 20`, `fallback_benchmark: str =
"SPY"`.

**Supported intervals.** `DAILY_ONLY`.

**Inputs.** Bars with `t` and `c`; caller-supplied `sector_map:
Mapping[str, str]` on `build_evaluator`.

**Outputs.** Positive = symbol leading its sector; negative =
lagging. Symbols with insufficient history or missing return get a
`blank_explanation` with a note.

**Current research findings.** New in this milestone.
`daily_swing_watchlist` preset supplies a sector map
`{"AAPL":"SPY","MSFT":"SPY","NVDA":"SPY","QQQ":"SPY"}` (uses SPY as
fallback broad-market benchmark since sector ETFs aren't in the
current watchlist).

**Current production status.** `STATE_DISABLED`.

**Future ideas.** Expand watchlist to include sector ETFs
(`XLK`, `XLV`, `XLF`, etc.) and provide a real sector map. Test on
`Bear_2022` (defensives-lead) vs `Rally_2023H2` (growth-lead).

## Volatility Regime Filter

**File:** `strategy/lab/volatility_regime_filter.py`.

**Identity:**

- `name` — `volatility_regime_filter_v1`
- `version` — `0.1.0`
- `strategy_id` — `lab-volatility-regime-filter-v0.1.0`

**Purpose.** Trailing return dampened by realised volatility. In
high-vol regimes the score collapses to zero — the strategy skips
the trade. In quiet regimes the raw return passes through.

**Timeframe.** Daily bars by default; also supported on intraday
(`DAILY_OR_INTRADAY`).

**Ideal market.** Low- and moderate-volatility trends. `Bull_2020_Q4`
before COVID_recovery-era whipsaws.

**Weaknesses.** In extended high-vol regimes (`COVID_crash`,
`Bear_2022` early Q1) the strategy produces mostly zeros — it
sits out even when there are opportunities in the tape.

**Required history.** `lookback_bars + 1` — with default
`lookback_bars=20`, 21 bars.

**Decision process.**

```
raw_return = (close_last - close_first) / close_first
vol = stdev(daily_returns(closes))
if vol >= vol_ceiling: multiplier = 0.0  (regime="high")
elif vol <= vol_floor: multiplier = 1.0  (regime="quiet")
else: multiplier = 1.0 - (vol - vol_floor) / (vol_ceiling - vol_floor)  (regime="moderate")
score = raw_return * multiplier
```

**Parameters.** `lookback_bars: int = 20`, `vol_floor: float = 0.005`
(0.5%), `vol_ceiling: float = 0.03` (3%).

**Supported intervals.** `DAILY_OR_INTRADAY`.
`requires_intraday_data()=False`.

**Inputs.** Bars with `t` and `c`.

**Outputs.** Positive = uptrend in low/moderate-vol; negative =
downtrend in low/moderate-vol; zero in high-vol regimes.

**Current research findings.** New in this milestone. Regime tagging
in `strategy/lab/regime_tagger.py` already labels observed vol
buckets — the two can be composed for regime-conditioned analysis.

**Current production status.** `STATE_DISABLED`.

**Future ideas.** Multi-timeframe vol (short-term realised vs
long-term realised). Use `implied_vol` if a data source lands. Sweep
`vol_ceiling` on `COVID_crash` and `Bear_2022` regimes.

---

## Strategy comparison — invariants

Every strategy in this document:

- Is registered by `strategy/lab/registry.discover_strategies()`
  automatically at discovery time.
- Carries a stable, JSON-serializable identity (`name`, `version`,
  parameters, `stable_hash`).
- Is deterministic given the same bars and event sequence.
- Never enables a feature flag.
- Never advances a `PromotionEntry`.
- Never constructs an `ApprovalRecord`.
- Never imports `trader.py`, `crypto_trader.py`, or
  `strategy/runner.py`.
- Never reads a credential env variable.

Adding a new strategy requires only a new `strategy/lab/<slug>.py`
file with a `StrategyBase` subclass that respects those invariants.
