"""Portfolio Simulator — deterministic paper-sized replay.

Read-only research module.  Given a strategy adapter, a bars-by-symbol
payload, and a starting cash balance, replay the strategy's evaluations
event-by-event and simulate the trades a paper-sized account would
have taken.  Produces a full blotter, a daily equity curve, realized /
unrealized P&L, drawdown, and standard risk-adjusted metrics.

Simulation is **not** a broker, not a scheduler, not an approver.  It
does not:

  * open network sockets,
  * read broker credentials,
  * construct an :class:`ApprovalRecord`,
  * advance a :class:`PromotionEntry`,
  * enable a feature flag,
  * import the live runner or ``trader.py`` / ``crypto_trader.py``,
  * emit real orders.

The paper-trading bridge that turns the simulator's proposed orders
into paper Alpaca orders is a separate CLI (``scripts/traderjoe-paper-execute``)
gated behind ``--execute-paper-orders`` and env-file checks.

Terminology follows the project convention: this is *simulation* /
*replay* / *research*, never "training".
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.backtest_lab import (
    BacktestEvent,
    DeterministicReplayClock,
    StrategyEvaluation,
    stable_hash,
    stable_json,
)


DEFAULT_STARTING_CASH = 100_000.0
DEFAULT_MAX_OPEN_POSITIONS = 10
DEFAULT_MAX_DOLLARS_PER_TRADE = 10_000.0
DEFAULT_POSITION_SIZE_PCT = 0.10
DEFAULT_COMMISSION_PER_TRADE = 0.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_SCORE_ENTRY_THRESHOLD = 0.0
DEFAULT_SCORE_EXIT_THRESHOLD = 0.0
DEFAULT_TOP_N = 5
DEFAULT_ANNUAL_TRADING_DAYS = 252
DEFAULT_RISK_FREE_RATE = 0.0

SIMULATOR_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioSimulatorConfig:
    """All knobs for a simulation run.

    Only pure data — no I/O.  ``stable_hash`` reflects every field
    that affects the simulation trajectory so replays are
    verifiable across environments.
    """

    starting_cash: float = DEFAULT_STARTING_CASH
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS
    max_dollars_per_trade: float = DEFAULT_MAX_DOLLARS_PER_TRADE
    position_size_pct: float = DEFAULT_POSITION_SIZE_PCT
    commission_per_trade: float = DEFAULT_COMMISSION_PER_TRADE
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    score_entry_threshold: float = DEFAULT_SCORE_ENTRY_THRESHOLD
    score_exit_threshold: float = DEFAULT_SCORE_EXIT_THRESHOLD
    top_n_per_event: int = DEFAULT_TOP_N
    allow_short: bool = False
    allow_negative_cash: bool = False
    annual_trading_days: int = DEFAULT_ANNUAL_TRADING_DAYS
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    seed: int = 0

    def validate(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.max_dollars_per_trade <= 0:
            raise ValueError("max_dollars_per_trade must be positive")
        if self.position_size_pct <= 0 or self.position_size_pct > 1:
            raise ValueError("position_size_pct must be in (0, 1]")
        if self.commission_per_trade < 0:
            raise ValueError("commission_per_trade cannot be negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        if self.top_n_per_event < 1:
            raise ValueError("top_n_per_event must be >= 1")
        if self.annual_trading_days < 1:
            raise ValueError("annual_trading_days must be >= 1")
        if self.seed < 0:
            raise ValueError("seed cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "starting_cash": self.starting_cash,
            "max_open_positions": self.max_open_positions,
            "max_dollars_per_trade": self.max_dollars_per_trade,
            "position_size_pct": self.position_size_pct,
            "commission_per_trade": self.commission_per_trade,
            "slippage_bps": self.slippage_bps,
            "score_entry_threshold": self.score_entry_threshold,
            "score_exit_threshold": self.score_exit_threshold,
            "top_n_per_event": self.top_n_per_event,
            "allow_short": self.allow_short,
            "allow_negative_cash": self.allow_negative_cash,
            "annual_trading_days": self.annual_trading_days,
            "risk_free_rate": self.risk_free_rate,
            "seed": self.seed,
        }

    def stable_hash(self) -> str:
        return stable_hash(self.to_dict())


# ---------------------------------------------------------------------------
# Ledger and blotter records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeFill:
    """A single simulated fill (buy or sell)."""

    timestamp: str
    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    price: float  # slippage-adjusted execution price
    reference_price: float  # bar close before slippage
    commission: float
    cash_delta: float  # signed change in cash
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "reference_price": self.reference_price,
            "commission": self.commission,
            "cash_delta": self.cash_delta,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TradeRecord:
    """A round-trip: entry fill + exit fill, with realized P&L."""

    symbol: str
    entry_timestamp: str
    exit_timestamp: str
    quantity: float
    entry_price: float
    exit_price: float
    commission: float
    realized_pnl: float
    hold_days: int
    entry_reason: str
    exit_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_timestamp": self.entry_timestamp,
            "exit_timestamp": self.exit_timestamp,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "commission": self.commission,
            "realized_pnl": self.realized_pnl,
            "hold_days": self.hold_days,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
        }


@dataclass
class _OpenPosition:
    symbol: str
    quantity: float
    entry_timestamp: str
    entry_price: float
    entry_commission: float
    entry_reason: str


@dataclass(frozen=True)
class EquityPoint:
    timestamp: str
    cash: float
    positions_value: float
    equity: float
    drawdown: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cash": self.cash,
            "positions_value": self.positions_value,
            "equity": self.equity,
            "drawdown": self.drawdown,
        }


@dataclass(frozen=True)
class ProposedOrder:
    """An order the simulator would send to the paper bridge next.

    Emitted only when a simulation is run in "generate paper plan"
    mode.  The paper bridge validates and (optionally) submits.
    Notional and quantity are the *reference* values from the
    simulator's last-known close and current available cash — the
    bridge re-checks against live paper quote before submitting.
    """

    symbol: str
    side: str
    quantity: float
    reference_price: float
    reference_notional: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "reference_price": self.reference_price,
            "reference_notional": self.reference_notional,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioMetrics:
    """Everything derivable from the equity curve and trade list."""

    starting_cash: float
    ending_equity: float
    net_pnl: float
    total_return_pct: float
    realized_pnl: float
    unrealized_pnl: float
    max_drawdown_pct: float
    win_rate: Optional[float]
    average_hold_days: Optional[float]
    trade_count: int
    winning_trades: int
    losing_trades: int
    cagr: Optional[float]
    sharpe: Optional[float]
    sortino: Optional[float]
    calmar: Optional[float]
    trading_days: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "starting_cash": self.starting_cash,
            "ending_equity": self.ending_equity,
            "net_pnl": self.net_pnl,
            "total_return_pct": self.total_return_pct,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "average_hold_days": self.average_hold_days,
            "trade_count": self.trade_count,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "cagr": self.cagr,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "trading_days": self.trading_days,
        }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioSimulationResult:
    """Immutable snapshot of one simulation run."""

    run_id: str
    config_hash: str
    strategy_name: str
    strategy_version: str
    strategy_id: str
    window_start: str
    window_end: str
    symbols: Tuple[str, ...]
    fills: Tuple[TradeFill, ...]
    trades: Tuple[TradeRecord, ...]
    equity_curve: Tuple[EquityPoint, ...]
    metrics: PortfolioMetrics
    proposed_orders: Tuple[ProposedOrder, ...]
    dataset_provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "simulator_version": SIMULATOR_VERSION,
            "strategy": {
                "name": self.strategy_name,
                "version": self.strategy_version,
                "strategy_id": self.strategy_id,
            },
            "window": {
                "start": self.window_start,
                "end": self.window_end,
            },
            "symbols": list(self.symbols),
            "metrics": self.metrics.to_dict(),
            "trade_count": len(self.trades),
            "fill_count": len(self.fills),
            "equity_curve_points": len(self.equity_curve),
            "proposed_orders": [o.to_dict() for o in self.proposed_orders],
            "dataset_provenance": dict(self.dataset_provenance),
        }


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


class PortfolioSimulatorError(RuntimeError):
    """Raised when the simulator refuses to run."""


class PortfolioSimulator:
    """Deterministic single-pass portfolio simulator.

    The simulator iterates the caller's ``events`` in
    :class:`DeterministicReplayClock` order.  For each event it asks
    the strategy evaluator to score the universe, then:

      1. exits any open position whose score is below
         ``score_exit_threshold`` (or missing from the ranking),
      2. opens new positions for the top-N unowned symbols above
         ``score_entry_threshold``, capped by
         ``max_open_positions`` and available cash,
      3. records a daily equity point valued at the day's close.

    All fills execute at that event's bar close with slippage;
    commissions are subtracted from cash.  Selling a position that
    the ledger does not hold is a programmer error.
    """

    def __init__(
        self,
        evaluator: Any,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        events: Sequence[BacktestEvent],
        config: Optional[PortfolioSimulatorConfig] = None,
        strategy_name: str = "unknown",
        strategy_version: str = "unknown",
        window_start: str = "",
        window_end: str = "",
        dataset_provenance: Optional[Mapping[str, Any]] = None,
    ):
        self._evaluator = evaluator
        self._bars = dict(bars_by_symbol)
        self._symbols = tuple(symbols)
        self._events = tuple(events)
        self._config = config or PortfolioSimulatorConfig()
        self._config.validate()
        self._strategy_name = strategy_name
        self._strategy_version = strategy_version
        strategy_id = getattr(evaluator, "strategy_id", "")
        self._strategy_id = str(strategy_id) if strategy_id else ""
        self._window_start = window_start
        self._window_end = window_end
        self._provenance = dict(dataset_provenance or {})

        self._cash: float = float(self._config.starting_cash)
        self._positions: Dict[str, _OpenPosition] = {}
        self._fills: List[TradeFill] = []
        self._trades: List[TradeRecord] = []
        self._equity_curve: List[EquityPoint] = []
        self._peak_equity: float = self._cash
        self._proposed_orders: List[ProposedOrder] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> PortfolioSimulationResult:
        clock = DeterministicReplayClock(list(self._events))
        for event in clock:
            evaluation = self._evaluator.evaluate(event)
            self._apply_event(event, evaluation)
        proposed = self._build_proposed_orders()
        metrics = self._compute_metrics()
        run_id = self._make_run_id()
        return PortfolioSimulationResult(
            run_id=run_id,
            config_hash=self._config.stable_hash(),
            strategy_name=self._strategy_name,
            strategy_version=self._strategy_version,
            strategy_id=self._strategy_id,
            window_start=self._window_start,
            window_end=self._window_end,
            symbols=self._symbols,
            fills=tuple(self._fills),
            trades=tuple(self._trades),
            equity_curve=tuple(self._equity_curve),
            metrics=metrics,
            proposed_orders=tuple(proposed),
            dataset_provenance=dict(self._provenance),
        )

    # ------------------------------------------------------------------
    # Per-event loop
    # ------------------------------------------------------------------

    def _apply_event(
        self,
        event: BacktestEvent,
        evaluation: StrategyEvaluation,
    ) -> None:
        close_prices = self._close_prices_at(event.timestamp)
        scores = dict(evaluation.scores or {})
        rankings = list(evaluation.rankings or [])
        ranked_symbols = [
            str(r.get("symbol", ""))
            for r in rankings
            if isinstance(r, Mapping) and r.get("symbol")
        ]

        # 1. Exit positions whose score falls out of the top or below floor.
        for symbol in sorted(self._positions):
            price = close_prices.get(symbol)
            if price is None:
                continue
            score = scores.get(symbol)
            in_rankings = symbol in ranked_symbols
            should_exit = False
            reason = ""
            if score is None or not in_rankings:
                should_exit = True
                reason = "signal_lost"
            elif score < self._config.score_exit_threshold:
                should_exit = True
                reason = (
                    f"score_below_exit "
                    f"({score:.4f} < {self._config.score_exit_threshold})"
                )
            if should_exit:
                self._sell(event.timestamp, symbol, price, reason)

        # 2. Enter new positions from top-N eligible ranked symbols.
        capacity = self._config.max_open_positions - len(self._positions)
        if capacity > 0:
            candidates: List[str] = []
            for candidate in ranked_symbols[: self._config.top_n_per_event]:
                if candidate in self._positions:
                    continue
                if candidate not in close_prices:
                    continue
                score = scores.get(candidate)
                if score is None:
                    continue
                if score < self._config.score_entry_threshold:
                    continue
                candidates.append(candidate)
                if len(candidates) >= capacity:
                    break
            for symbol in candidates:
                price = close_prices[symbol]
                score = scores.get(symbol, 0.0)
                self._buy(
                    event.timestamp,
                    symbol,
                    price,
                    reason=f"top_rank score={score:.4f}",
                )

        # 3. Record equity point at end of event.
        cash = self._cash
        positions_value = sum(
            close_prices.get(sym, pos.entry_price) * pos.quantity
            for sym, pos in self._positions.items()
        )
        equity = cash + positions_value
        if equity > self._peak_equity:
            self._peak_equity = equity
        drawdown = (
            0.0
            if self._peak_equity <= 0
            else (self._peak_equity - equity) / self._peak_equity
        )
        self._equity_curve.append(
            EquityPoint(
                timestamp=event.timestamp,
                cash=cash,
                positions_value=positions_value,
                equity=equity,
                drawdown=drawdown,
            )
        )

    # ------------------------------------------------------------------
    # Ledger operations
    # ------------------------------------------------------------------

    def _buy(
        self,
        timestamp: str,
        symbol: str,
        reference_price: float,
        reason: str,
    ) -> None:
        if reference_price <= 0:
            return
        slippage = reference_price * self._config.slippage_bps / 10_000.0
        exec_price = reference_price + slippage
        commission = float(self._config.commission_per_trade)
        # Position-size cap and per-trade cap.
        equity_estimate = self._cash + self._positions_value(
            {symbol: reference_price}
        )
        target_dollars = min(
            self._config.max_dollars_per_trade,
            equity_estimate * self._config.position_size_pct,
        )
        max_affordable = self._cash - commission
        if not self._config.allow_negative_cash:
            target_dollars = min(target_dollars, max(max_affordable, 0.0))
        if target_dollars <= 0:
            return
        quantity = math.floor(target_dollars / exec_price)
        if quantity < 1:
            return
        cost = quantity * exec_price + commission
        if not self._config.allow_negative_cash and cost > self._cash + 1e-9:
            return
        self._cash -= cost
        fill = TradeFill(
            timestamp=timestamp,
            symbol=symbol,
            side="buy",
            quantity=float(quantity),
            price=exec_price,
            reference_price=reference_price,
            commission=commission,
            cash_delta=-cost,
            reason=reason,
        )
        self._fills.append(fill)
        self._positions[symbol] = _OpenPosition(
            symbol=symbol,
            quantity=float(quantity),
            entry_timestamp=timestamp,
            entry_price=exec_price,
            entry_commission=commission,
            entry_reason=reason,
        )

    def _sell(
        self,
        timestamp: str,
        symbol: str,
        reference_price: float,
        reason: str,
    ) -> None:
        position = self._positions.get(symbol)
        if position is None:
            return
        if reference_price <= 0:
            return
        slippage = reference_price * self._config.slippage_bps / 10_000.0
        exec_price = reference_price - slippage
        commission = float(self._config.commission_per_trade)
        proceeds = position.quantity * exec_price - commission
        self._cash += proceeds
        fill = TradeFill(
            timestamp=timestamp,
            symbol=symbol,
            side="sell",
            quantity=position.quantity,
            price=exec_price,
            reference_price=reference_price,
            commission=commission,
            cash_delta=proceeds,
            reason=reason,
        )
        self._fills.append(fill)
        total_commission = position.entry_commission + commission
        realized = (
            (exec_price - position.entry_price) * position.quantity
            - commission
            - position.entry_commission
        )
        hold_days = _iso_day_delta(position.entry_timestamp, timestamp)
        self._trades.append(
            TradeRecord(
                symbol=symbol,
                entry_timestamp=position.entry_timestamp,
                exit_timestamp=timestamp,
                quantity=position.quantity,
                entry_price=position.entry_price,
                exit_price=exec_price,
                commission=total_commission,
                realized_pnl=realized,
                hold_days=hold_days,
                entry_reason=position.entry_reason,
                exit_reason=reason,
            )
        )
        del self._positions[symbol]

    def _positions_value(self, price_overrides: Mapping[str, float]) -> float:
        total = 0.0
        for sym, pos in self._positions.items():
            price = price_overrides.get(sym, pos.entry_price)
            total += price * pos.quantity
        return total

    # ------------------------------------------------------------------
    # Bar lookup
    # ------------------------------------------------------------------

    def _close_prices_at(self, timestamp: str) -> Dict[str, float]:
        """Return a symbol → close-price map for bars stamped at
        ``timestamp``.  Symbols with no bar at this exact timestamp
        are omitted.
        """
        result: Dict[str, float] = {}
        for symbol in self._symbols:
            bars = self._bars.get(symbol) or []
            for bar in bars:
                if not isinstance(bar, Mapping):
                    continue
                if str(bar.get("t")) == timestamp:
                    close = bar.get("c")
                    if close is not None:
                        result[symbol] = float(close)
                    break
        return result

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self) -> PortfolioMetrics:
        starting = float(self._config.starting_cash)
        if not self._equity_curve:
            return PortfolioMetrics(
                starting_cash=starting,
                ending_equity=starting,
                net_pnl=0.0,
                total_return_pct=0.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                max_drawdown_pct=0.0,
                win_rate=None,
                average_hold_days=None,
                trade_count=0,
                winning_trades=0,
                losing_trades=0,
                cagr=None,
                sharpe=None,
                sortino=None,
                calmar=None,
                trading_days=0,
            )

        ending = self._equity_curve[-1].equity
        net_pnl = ending - starting
        total_return_pct = (net_pnl / starting) * 100.0 if starting else 0.0
        realized = sum(t.realized_pnl for t in self._trades)
        unrealized = self._equity_curve[-1].positions_value - sum(
            pos.entry_price * pos.quantity for pos in self._positions.values()
        )
        max_dd_pct = max(pt.drawdown for pt in self._equity_curve) * 100.0
        winning = sum(1 for t in self._trades if t.realized_pnl > 0)
        losing = sum(1 for t in self._trades if t.realized_pnl < 0)
        win_rate: Optional[float]
        if self._trades:
            win_rate = winning / len(self._trades)
        else:
            win_rate = None
        avg_hold: Optional[float]
        if self._trades:
            avg_hold = sum(t.hold_days for t in self._trades) / len(self._trades)
        else:
            avg_hold = None

        trading_days = len(self._equity_curve)
        cagr = _compute_cagr(
            starting,
            ending,
            trading_days,
            self._config.annual_trading_days,
        )
        daily_returns = _daily_returns(self._equity_curve)
        sharpe = _compute_sharpe(
            daily_returns,
            self._config.risk_free_rate,
            self._config.annual_trading_days,
        )
        sortino = _compute_sortino(
            daily_returns,
            self._config.risk_free_rate,
            self._config.annual_trading_days,
        )
        calmar = _compute_calmar(cagr, max_dd_pct / 100.0)

        return PortfolioMetrics(
            starting_cash=starting,
            ending_equity=ending,
            net_pnl=net_pnl,
            total_return_pct=total_return_pct,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            max_drawdown_pct=max_dd_pct,
            win_rate=win_rate,
            average_hold_days=avg_hold,
            trade_count=len(self._trades),
            winning_trades=winning,
            losing_trades=losing,
            cagr=cagr,
            sharpe=sharpe,
            sortino=sortino,
            calmar=calmar,
            trading_days=trading_days,
        )

    # ------------------------------------------------------------------
    # Proposed-orders plan (paper-bridge input)
    # ------------------------------------------------------------------

    def _build_proposed_orders(self) -> List[ProposedOrder]:
        """Freeze the simulator's current open positions as the
        starting plan the paper bridge should reconcile toward.

        Every open position becomes a ``buy`` proposal for that
        symbol at the last-known reference price.  No sells are
        emitted (the paper bridge should only add to what the
        research plan agrees is currently a good position; unwinding
        an already-live paper position is the operator's decision,
        not the simulator's).
        """
        if not self._positions:
            return []
        last_prices = (
            self._close_prices_at(self._equity_curve[-1].timestamp)
            if self._equity_curve
            else {}
        )
        plan: List[ProposedOrder] = []
        for symbol in sorted(self._positions):
            position = self._positions[symbol]
            ref = last_prices.get(symbol, position.entry_price)
            notional = ref * position.quantity
            plan.append(
                ProposedOrder(
                    symbol=symbol,
                    side="buy",
                    quantity=position.quantity,
                    reference_price=ref,
                    reference_notional=notional,
                    reason=(
                        f"simulator_open_position entry={position.entry_timestamp} "
                        f"reason={position.entry_reason}"
                    ),
                )
            )
        return plan

    # ------------------------------------------------------------------
    # Run id
    # ------------------------------------------------------------------

    def _make_run_id(self) -> str:
        payload = {
            "config_hash": self._config.stable_hash(),
            "strategy_name": self._strategy_name,
            "strategy_version": self._strategy_version,
            "strategy_id": self._strategy_id,
            "symbols": list(self._symbols),
            "window_start": self._window_start,
            "window_end": self._window_end,
            "event_count": len(self._events),
        }
        digest = stable_hash(payload)
        return f"psim_{digest[:12]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def bars_from_warehouse_hit_bars(
    warehouse_bars: Iterable[Any],
) -> List[Dict[str, Any]]:
    """Convert warehouse :class:`Bar` objects into the ``{t,o,h,l,c,v}``
    dict shape the strategy evaluators consume.
    """
    result: List[Dict[str, Any]] = []
    for bar in warehouse_bars:
        result.append(
            {
                "t": bar.timestamp,
                "o": float(bar.open),
                "h": float(bar.high),
                "l": float(bar.low),
                "c": float(bar.close),
                "v": float(bar.volume),
            }
        )
    return result


def events_from_bars(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> List[BacktestEvent]:
    """Derive ``BacktestEvent`` objects — one per distinct timestamp —
    from a bars-by-symbol payload, matching the shape
    :func:`strategy.historical_validation._events_from_bar_dict`
    produces.
    """
    timestamps = sorted(
        {
            str(bar.get("t"))
            for symbol_bars in bars_by_symbol.values()
            if isinstance(symbol_bars, Iterable)
            for bar in symbol_bars
            if isinstance(bar, Mapping) and bar.get("t") is not None
        }
    )
    return [
        BacktestEvent(
            timestamp=timestamp,
            event_type="market_snapshot",
            sequence=index + 1,
        )
        for index, timestamp in enumerate(timestamps)
    ]


def _iso_day_delta(start: str, end: str) -> int:
    def _parse(value: str) -> date:
        s = value[:10]
        return date.fromisoformat(s)

    try:
        return (_parse(end) - _parse(start)).days
    except ValueError:
        return 0


def _daily_returns(curve: Sequence[EquityPoint]) -> List[float]:
    returns: List[float] = []
    for prev, curr in zip(curve, curve[1:]):
        if prev.equity <= 0:
            returns.append(0.0)
            continue
        returns.append((curr.equity - prev.equity) / prev.equity)
    return returns


def _compute_cagr(
    starting: float,
    ending: float,
    trading_days: int,
    annual_trading_days: int,
) -> Optional[float]:
    if starting <= 0 or ending <= 0 or trading_days <= 1:
        return None
    years = trading_days / float(annual_trading_days)
    if years <= 0:
        return None
    try:
        return (ending / starting) ** (1.0 / years) - 1.0
    except (ValueError, ZeroDivisionError):
        return None


def _compute_sharpe(
    returns: Sequence[float],
    risk_free_rate: float,
    annual_trading_days: int,
) -> Optional[float]:
    if len(returns) < 2:
        return None
    daily_rf = risk_free_rate / float(annual_trading_days)
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(annual_trading_days)


def _compute_sortino(
    returns: Sequence[float],
    risk_free_rate: float,
    annual_trading_days: int,
) -> Optional[float]:
    if len(returns) < 2:
        return None
    daily_rf = risk_free_rate / float(annual_trading_days)
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return None
    downside_variance = sum(r * r for r in downside) / len(downside)
    downside_std = math.sqrt(downside_variance)
    if downside_std == 0:
        return None
    return (mean / downside_std) * math.sqrt(annual_trading_days)


def _compute_calmar(
    cagr: Optional[float],
    max_drawdown: float,
) -> Optional[float]:
    if cagr is None or max_drawdown <= 0:
        return None
    return cagr / max_drawdown


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


DEFAULT_REPORT_ROOT = "reports/portfolio_simulations"


def write_simulation_report(
    result: PortfolioSimulationResult,
    root: str = DEFAULT_REPORT_ROOT,
) -> Dict[str, str]:
    """Write the four artifacts and return their paths.

    Files written:

      * ``report.md`` — human-readable summary
      * ``report.json`` — machine-readable summary
      * ``trades.csv`` — round-trip blotter
      * ``equity_curve.csv`` — daily equity series
      * ``orders.json`` — the proposed-orders plan for the paper bridge
    """
    run_dir = Path(root) / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    report_json_path = run_dir / "report.json"
    report_md_path = run_dir / "report.md"
    trades_path = run_dir / "trades.csv"
    equity_path = run_dir / "equity_curve.csv"
    orders_path = run_dir / "orders.json"

    with open(report_json_path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2, sort_keys=True)

    with open(report_md_path, "w", encoding="utf-8") as fh:
        fh.write(_render_markdown_report(result))

    with open(trades_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "symbol",
                "entry_timestamp",
                "exit_timestamp",
                "quantity",
                "entry_price",
                "exit_price",
                "commission",
                "realized_pnl",
                "hold_days",
                "entry_reason",
                "exit_reason",
            ]
        )
        for trade in result.trades:
            writer.writerow(
                [
                    trade.symbol,
                    trade.entry_timestamp,
                    trade.exit_timestamp,
                    f"{trade.quantity:.6f}",
                    f"{trade.entry_price:.6f}",
                    f"{trade.exit_price:.6f}",
                    f"{trade.commission:.6f}",
                    f"{trade.realized_pnl:.6f}",
                    trade.hold_days,
                    trade.entry_reason,
                    trade.exit_reason,
                ]
            )

    with open(equity_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["timestamp", "cash", "positions_value", "equity", "drawdown"]
        )
        for point in result.equity_curve:
            writer.writerow(
                [
                    point.timestamp,
                    f"{point.cash:.6f}",
                    f"{point.positions_value:.6f}",
                    f"{point.equity:.6f}",
                    f"{point.drawdown:.6f}",
                ]
            )

    with open(orders_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "run_id": result.run_id,
                "strategy": {
                    "name": result.strategy_name,
                    "version": result.strategy_version,
                    "strategy_id": result.strategy_id,
                },
                "window": {
                    "start": result.window_start,
                    "end": result.window_end,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "orders": [o.to_dict() for o in result.proposed_orders],
            },
            fh,
            indent=2,
            sort_keys=True,
        )

    return {
        "run_dir": str(run_dir),
        "report_json": str(report_json_path),
        "report_md": str(report_md_path),
        "trades_csv": str(trades_path),
        "equity_curve_csv": str(equity_path),
        "orders_json": str(orders_path),
    }


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _fmt_num(value: Optional[float], places: int = 4) -> str:
    if value is None:
        return "—"
    return f"{value:.{places}f}"


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _render_markdown_report(result: PortfolioSimulationResult) -> str:
    metrics = result.metrics
    lines: List[str] = []
    lines.append(f"# Portfolio Simulation — {result.run_id}")
    lines.append("")
    lines.append(
        f"- Strategy: `{result.strategy_name}-v{result.strategy_version}` "
        f"(`{result.strategy_id}`)"
    )
    lines.append(
        f"- Window: `{result.window_start}` → `{result.window_end}` "
        f"({metrics.trading_days} trading day(s))"
    )
    lines.append(f"- Symbols: {', '.join(result.symbols)}")
    lines.append(f"- Config hash: `{result.config_hash}`")
    lines.append(f"- Simulator version: `{SIMULATOR_VERSION}`")
    if result.dataset_provenance:
        source = result.dataset_provenance.get("source", "unknown")
        lines.append(f"- Data source: `{source}`")
    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Starting cash | {_fmt_money(metrics.starting_cash)} |")
    lines.append(f"| Ending equity | {_fmt_money(metrics.ending_equity)} |")
    lines.append(f"| Net P/L | {_fmt_money(metrics.net_pnl)} |")
    lines.append(f"| Total return | {metrics.total_return_pct:.2f}% |")
    lines.append(f"| Realized P/L | {_fmt_money(metrics.realized_pnl)} |")
    lines.append(f"| Unrealized P/L | {_fmt_money(metrics.unrealized_pnl)} |")
    lines.append(f"| Max drawdown | {metrics.max_drawdown_pct:.2f}% |")
    lines.append(f"| Trades | {metrics.trade_count} |")
    lines.append(f"| Winning trades | {metrics.winning_trades} |")
    lines.append(f"| Losing trades | {metrics.losing_trades} |")
    lines.append(f"| Win rate | {_fmt_pct(metrics.win_rate)} |")
    lines.append(
        f"| Avg hold (days) | {_fmt_num(metrics.average_hold_days, 2)} |"
    )
    lines.append(f"| CAGR | {_fmt_pct(metrics.cagr)} |")
    lines.append(f"| Sharpe | {_fmt_num(metrics.sharpe, 3)} |")
    lines.append(f"| Sortino | {_fmt_num(metrics.sortino, 3)} |")
    lines.append(f"| Calmar | {_fmt_num(metrics.calmar, 3)} |")
    lines.append("")
    lines.append("## Trade blotter")
    lines.append("")
    if not result.trades:
        lines.append("_No closed trades._")
    else:
        lines.append(
            "| Symbol | Entry | Exit | Qty | Entry px | Exit px | "
            "Realized P/L | Days | Exit reason |"
        )
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---|")
        for trade in result.trades:
            lines.append(
                f"| {trade.symbol} | {trade.entry_timestamp} | "
                f"{trade.exit_timestamp} | {trade.quantity:g} | "
                f"{trade.entry_price:.2f} | {trade.exit_price:.2f} | "
                f"{trade.realized_pnl:+.2f} | {trade.hold_days} | "
                f"{trade.exit_reason} |"
            )
    lines.append("")
    lines.append("## Open positions at end of window")
    lines.append("")
    if not result.proposed_orders:
        lines.append("_None._")
    else:
        lines.append("| Symbol | Qty | Ref px | Notional | Reason |")
        lines.append("|---|---:|---:|---:|---|")
        for order in result.proposed_orders:
            lines.append(
                f"| {order.symbol} | {order.quantity:g} | "
                f"{order.reference_price:.2f} | "
                f"{_fmt_money(order.reference_notional)} | "
                f"{order.reason} |"
            )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Simulation is research-only; no orders were placed. "
        "The paper-trading bridge (`scripts/traderjoe-paper-execute`) is "
        "a separate command and defaults to dry-run."
    )
    lines.append(
        "- Slippage modelled as a fixed basis-point adjustment "
        "against the bar close; commissions applied per fill."
    )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_REPORT_ROOT",
    "DEFAULT_STARTING_CASH",
    "EquityPoint",
    "PortfolioMetrics",
    "PortfolioSimulationResult",
    "PortfolioSimulator",
    "PortfolioSimulatorConfig",
    "PortfolioSimulatorError",
    "ProposedOrder",
    "SIMULATOR_VERSION",
    "TradeFill",
    "TradeRecord",
    "bars_from_warehouse_hit_bars",
    "events_from_bars",
    "write_simulation_report",
]
