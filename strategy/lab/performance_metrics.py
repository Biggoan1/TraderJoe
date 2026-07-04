"""Performance metrics — Card 9.

Turns a strategy experiment into an equity curve, then derives
the standard trading-performance metric set:

* CAGR, Annualized Return, Sharpe, Sortino, Calmar
* MaxDrawdown, UlcerIndex
* Win rate, Avg Gain, Avg Loss, Profit Factor
* Exposure, Turnover

The equity curve is deterministic: at every replay event, the
challenger's top-ranked symbol is the position held until the
next event.  If the challenger produced no ranking at an event
(insufficient history, no eligible symbols), we hold cash for
that period.  Daily returns come from the caller-supplied
``bars_by_symbol`` price series.

Read-only.  No live-trading path, no order-path references, no
credential env reads, no ``ApprovalRecord`` / ``PromotionEntry``
construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.lab.experiment_runner import ExperimentBundle


TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class PerformanceMetrics:
    """Full metric block for one strategy experiment."""

    # required equity-curve metrics
    cagr: float
    annualized_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    ulcer_index: float
    win_rate: float
    avg_gain: float
    avg_loss: float
    profit_factor: float
    exposure: float
    turnover: float
    # sample sizes
    num_events: int
    num_holding_periods: int
    num_trades: int
    # equity curve
    final_equity: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cagr": self.cagr,
            "annualized_return": self.annualized_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "max_drawdown": self.max_drawdown,
            "ulcer_index": self.ulcer_index,
            "win_rate": self.win_rate,
            "avg_gain": self.avg_gain,
            "avg_loss": self.avg_loss,
            "profit_factor": self.profit_factor,
            "exposure": self.exposure,
            "turnover": self.turnover,
            "num_events": self.num_events,
            "num_holding_periods": self.num_holding_periods,
            "num_trades": self.num_trades,
            "final_equity": self.final_equity,
        }

    def to_leaderboard_kwargs(self) -> Dict[str, Any]:
        """Subset of metrics used by ``LeaderboardEntry``."""
        return {
            "cagr": self.cagr,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
        }


# ---------------------------------------------------------------------------
# Equity curve builder
# ---------------------------------------------------------------------------


def _closes_up_to(
    bars: Sequence[Mapping[str, Any]], cutoff: str
) -> List[Tuple[str, float]]:
    result: List[Tuple[str, float]] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        t = bar.get("t")
        if t is None or str(t) > cutoff:
            continue
        try:
            c = float(bar.get("c"))
        except (TypeError, ValueError):
            continue
        result.append((str(t), c))
    return result


def _price_at(
    bars: Sequence[Mapping[str, Any]], cutoff: str
) -> Optional[float]:
    closes = _closes_up_to(bars, cutoff)
    if not closes:
        return None
    return closes[-1][1]


def _top_pick(
    rankings: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    if not rankings:
        return None
    for entry in rankings:
        if entry.get("rank") == 1:
            return entry.get("symbol")
    return None


@dataclass(frozen=True)
class HoldingPeriod:
    from_ts: str
    to_ts: str
    symbol: str
    entry_price: float
    exit_price: float
    period_return: float

    @property
    def is_cash(self) -> bool:
        return self.symbol == ""


def build_equity_curve(
    bundle: ExperimentBundle,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    side: str = "challenger",
) -> Tuple[List[HoldingPeriod], List[float]]:
    """Build the deterministic equity curve for one bundle.

    ``side='challenger'`` (default) uses the challenger's per-event
    rankings; ``side='champion'`` uses champion rankings.

    Returns a ``(holding_periods, equity_series)`` tuple.  The
    equity series starts at 1.0 and multiplies by ``1 + period_return``
    per holding period.
    """
    if side not in {"challenger", "champion"}:
        raise ValueError("side must be 'challenger' or 'champion'")
    tables = list(bundle.validation_bundle.comparison.score_tables)
    holdings: List[HoldingPeriod] = []
    equity: List[float] = [1.0]

    for i in range(len(tables) - 1):
        current = tables[i]
        next_table = tables[i + 1]
        cutoff = current.event_timestamp
        next_cutoff = next_table.event_timestamp

        # Pick top-ranked symbol at the current event.  We inspect
        # the ScoreRow list directly since ScoreTable doesn't
        # expose rankings — rank == 1 corresponds to
        # min(champion_rank / challenger_rank).
        pick = _pick_top(current, side)
        if pick is None:
            # Hold cash
            holdings.append(HoldingPeriod(
                from_ts=cutoff, to_ts=next_cutoff,
                symbol="", entry_price=0.0, exit_price=0.0,
                period_return=0.0,
            ))
            equity.append(equity[-1])
            continue

        entry_price = _price_at(bars_by_symbol.get(pick) or [], cutoff)
        exit_price = _price_at(bars_by_symbol.get(pick) or [], next_cutoff)
        if entry_price is None or exit_price is None or entry_price == 0.0:
            holdings.append(HoldingPeriod(
                from_ts=cutoff, to_ts=next_cutoff,
                symbol=pick, entry_price=0.0, exit_price=0.0,
                period_return=0.0,
            ))
            equity.append(equity[-1])
            continue

        period_return = (exit_price - entry_price) / entry_price
        holdings.append(HoldingPeriod(
            from_ts=cutoff, to_ts=next_cutoff,
            symbol=pick, entry_price=entry_price,
            exit_price=exit_price, period_return=period_return,
        ))
        equity.append(equity[-1] * (1.0 + period_return))

    return holdings, equity


def _pick_top(
    table: Any, side: str,
) -> Optional[str]:
    """Return the symbol with rank == 1 on the given side of the
    score table.  Falls back to the highest score if no rank == 1
    row exists.
    """
    best: Optional[Tuple[int, float, str]] = None
    for row in table.rows:
        rank = getattr(row, f"{side}_rank")
        score = getattr(row, f"{side}_score")
        symbol = row.symbol
        if rank == 1:
            return symbol
        if isinstance(score, (int, float)):
            key = (rank if isinstance(rank, int) else 1_000_000,
                   -float(score), symbol)
            if best is None or key < best:
                best = key
    if best is None:
        return None
    return best[2]


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------


def _safe_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _sharpe(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    std = _safe_std(returns)
    if std == 0.0:
        return 0.0
    return (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _sortino(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    mean = sum(returns) / len(returns)
    down_std = math.sqrt(
        sum(r * r for r in downside) / len(downside)
    )
    if down_std == 0.0:
        return 0.0
    return (mean / down_std) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _max_drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _ulcer_index(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    squared_drawdowns: List[float] = []
    for value in equity:
        if value > peak:
            peak = value
        dd_pct = 100.0 * (value - peak) / peak if peak > 0 else 0.0
        squared_drawdowns.append(dd_pct * dd_pct)
    mean_sq = sum(squared_drawdowns) / len(squared_drawdowns)
    return math.sqrt(mean_sq)


def _cagr(final_equity: float, num_periods: int) -> float:
    if num_periods <= 0 or final_equity <= 0.0:
        return 0.0
    years = num_periods / TRADING_DAYS_PER_YEAR
    if years <= 0.0:
        return 0.0
    return final_equity ** (1.0 / years) - 1.0


def compute_performance_metrics(
    bundle: ExperimentBundle,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    side: str = "challenger",
) -> PerformanceMetrics:
    """Compute the full metric block.

    The equity curve is deterministic given the same bundle +
    bars; identical inputs produce identical metrics.
    """
    holdings, equity = build_equity_curve(bundle, bars_by_symbol, side=side)
    returns = [h.period_return for h in holdings]

    invested = [h for h in holdings if not h.is_cash]
    num_invested = len(invested)
    num_trades = _count_trades(holdings)
    exposure = num_invested / len(holdings) if holdings else 0.0
    turnover = num_trades / len(holdings) if holdings else 0.0

    wins = [r for r in returns if r > 0.0]
    losses = [r for r in returns if r < 0.0]
    win_rate = len(wins) / len(returns) if returns else 0.0
    avg_gain = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if losses and sum(-r for r in losses) > 0:
        profit_factor = sum(wins) / sum(-r for r in losses)
    else:
        profit_factor = math.inf if wins else 0.0

    final_equity = equity[-1] if equity else 1.0
    num_periods = len(holdings)
    cagr = _cagr(final_equity, num_periods)
    annualized_return = cagr
    sharpe = _sharpe(returns)
    sortino = _sortino(returns)
    max_dd = _max_drawdown(equity)
    ulcer = _ulcer_index(equity)
    calmar = (cagr / max_dd) if max_dd > 0 else 0.0

    # Guard: any NaN / inf from degenerate paths becomes 0.0 so
    # the leaderboard sort keys stay well-defined.  profit_factor
    # keeps math.inf if a strategy has zero losses so the caller
    # can spot the corner case.
    def _guard(x: float) -> float:
        if math.isnan(x):
            return 0.0
        return x

    return PerformanceMetrics(
        cagr=_guard(cagr),
        annualized_return=_guard(annualized_return),
        sharpe=_guard(sharpe),
        sortino=_guard(sortino),
        calmar=_guard(calmar),
        max_drawdown=_guard(max_dd),
        ulcer_index=_guard(ulcer),
        win_rate=_guard(win_rate),
        avg_gain=_guard(avg_gain),
        avg_loss=_guard(avg_loss),
        profit_factor=profit_factor if not math.isnan(profit_factor) else 0.0,
        exposure=_guard(exposure),
        turnover=_guard(turnover),
        num_events=len(bundle.validation_bundle.comparison.score_tables),
        num_holding_periods=len(holdings),
        num_trades=num_trades,
        final_equity=_guard(final_equity),
    )


def _count_trades(holdings: Sequence[HoldingPeriod]) -> int:
    """Number of position changes.  A hold from AAPL -> AAPL is
    not a trade; a change of symbol or a transition to/from cash
    is one trade.
    """
    trades = 0
    prev_symbol: Optional[str] = None
    for h in holdings:
        symbol = h.symbol
        if prev_symbol is None:
            if symbol:
                trades += 1
        elif symbol != prev_symbol:
            trades += 1
        prev_symbol = symbol
    return trades


__all__ = [
    "HoldingPeriod",
    "PerformanceMetrics",
    "TRADING_DAYS_PER_YEAR",
    "build_equity_curve",
    "compute_performance_metrics",
]
