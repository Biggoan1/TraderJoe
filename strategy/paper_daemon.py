"""Equity paper-trading daemon (15-minute intraday scheduler).

Runs a single "tick" of the intraday paper strategy: load bars for
the allowlisted equity symbols, run the requested strategy, apply
notional / trade / cooldown / duplicate caps, and either
(a) write the plan to disk without touching the broker (default),
or (b) if the operator has explicitly enabled unattended paper
execution via ``PAPER_AUTO_EXECUTE_EQUITIES=true`` **and** passed
``--execute-paper-orders``, hand the plan off to the paper bridge.

Safety invariants:

* Equity paper only.  ``PAPER_ALPACA_*`` credentials, paper endpoint.
* Refuses to load ``.env.production``.
* Refuses to run if ``HERMES_CONTEXT=production`` or ``PAPER`` is
  set to a false value.
* Refuses execution outside NYSE regular session.
* Refuses execution when the emergency stop file exists.
* Dry-run by default.  Auto-execute requires BOTH the env flag AND
  the CLI flag.
* Never touches the crypto env, the production env, or the live
  runner (``trader.py``, ``crypto_trader.py``, ``strategy/runner.py``).
"""

from __future__ import annotations

import json
import logging
import os
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

from strategy.backtest_lab import BacktestEvent
from strategy.market_calendar import SessionStatus, session_status
from strategy.paper_bridge import (
    PaperBridgeConfig,
    PaperBridgeError,
    PaperBridgeResult,
    PaperTradingBridge,
    load_paper_env,
)


DEFAULT_EMERGENCY_STOP_FILE = "/etc/traderjoe/STOP_EQUITY_PAPER"
DEFAULT_STATE_FILE = "reports/paper_daemon/equity_state.json"
DEFAULT_LOG_ROOT = "reports/paper_daemon/equity"
DEFAULT_PLAN_ROOT = "reports/paper_daemon/equity/plans"
DEFAULT_MAX_PER_ORDER_NOTIONAL = 2_000.0
DEFAULT_MAX_TOTAL_DAILY_NOTIONAL = 10_000.0
DEFAULT_MAX_TRADES_PER_DAY = 8
DEFAULT_COOLDOWN_MINUTES = 60

AUTO_EXECUTE_ENV_VAR = "PAPER_AUTO_EXECUTE_EQUITIES"


class PaperDaemonError(RuntimeError):
    """Raised when the daemon refuses to run."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityPaperDaemonConfig:
    """Configuration for a single equity daemon tick."""

    symbol_allowlist: Tuple[str, ...]
    strategy_key: str
    interval: str  # BarInterval.value; caller decides
    execute: bool = False
    max_per_order_notional: float = DEFAULT_MAX_PER_ORDER_NOTIONAL
    max_total_daily_notional: float = DEFAULT_MAX_TOTAL_DAILY_NOTIONAL
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    top_n_per_tick: int = 3
    env_file: str = ".env.paper"
    emergency_stop_file: str = DEFAULT_EMERGENCY_STOP_FILE
    state_file: str = DEFAULT_STATE_FILE
    log_root: str = DEFAULT_LOG_ROOT
    plan_root: str = DEFAULT_PLAN_ROOT
    lookback_days: int = 20

    def validate(self) -> None:
        if not self.symbol_allowlist:
            raise PaperDaemonError("symbol_allowlist must not be empty")
        if not self.strategy_key:
            raise PaperDaemonError("strategy_key must be provided")
        if self.max_per_order_notional <= 0:
            raise PaperDaemonError("max_per_order_notional must be positive")
        if self.max_total_daily_notional < self.max_per_order_notional:
            raise PaperDaemonError(
                "max_total_daily_notional must be >= max_per_order_notional"
            )
        if self.max_trades_per_day < 1:
            raise PaperDaemonError("max_trades_per_day must be >= 1")
        if self.cooldown_minutes < 0:
            raise PaperDaemonError("cooldown_minutes cannot be negative")
        base = os.path.basename(self.env_file)
        if base == "" or not base.startswith(".env.paper"):
            raise PaperDaemonError(
                f"env_file basename must start with '.env.paper' "
                f"(got {base!r})"
            )
        if base == ".env.production":  # pragma: no cover — belt+braces
            raise PaperDaemonError("refusing production env file")


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------


@dataclass
class DaemonState:
    """Persisted per-day state used to enforce daily caps and cooldowns."""

    current_day: str = ""
    trades_today: int = 0
    notional_today: float = 0.0
    last_order_iso_by_symbol: Dict[str, str] = field(default_factory=dict)
    submitted_order_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_day": self.current_day,
            "trades_today": self.trades_today,
            "notional_today": self.notional_today,
            "last_order_iso_by_symbol": dict(self.last_order_iso_by_symbol),
            "submitted_order_ids": list(self.submitted_order_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DaemonState":
        return cls(
            current_day=str(payload.get("current_day", "")),
            trades_today=int(payload.get("trades_today", 0)),
            notional_today=float(payload.get("notional_today", 0.0)),
            last_order_iso_by_symbol=dict(
                payload.get("last_order_iso_by_symbol", {}) or {}
            ),
            submitted_order_ids=list(
                payload.get("submitted_order_ids", []) or []
            ),
        )


def _load_state(path: str) -> DaemonState:
    if not path or not os.path.exists(path):
        return DaemonState()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return DaemonState()
    if not isinstance(payload, Mapping):
        return DaemonState()
    return DaemonState.from_dict(payload)


def _save_state(state: DaemonState, path: str) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                state.to_dict(),
                fh,
                indent=2,
                sort_keys=True,
                default=str,
            )
    except OSError:  # pragma: no cover
        pass


def _reset_state_for_new_day(state: DaemonState, day: str) -> DaemonState:
    if state.current_day == day:
        return state
    return DaemonState(current_day=day)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class DaemonTickResult:
    """Everything one daemon tick produced."""

    tick_id: str
    generated_at: str
    session_status: Dict[str, Any]
    would_have_executed: bool
    executed: bool
    reason: str
    proposed_orders: List[Dict[str, Any]] = field(default_factory=list)
    rejected_orders: List[Dict[str, Any]] = field(default_factory=list)
    plan_path: str = ""
    log_path: str = ""
    bridge_result: Optional[Dict[str, Any]] = None
    state_after: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "generated_at": self.generated_at,
            "session_status": self.session_status,
            "would_have_executed": self.would_have_executed,
            "executed": self.executed,
            "reason": self.reason,
            "proposed_orders": list(self.proposed_orders),
            "rejected_orders": list(self.rejected_orders),
            "plan_path": self.plan_path,
            "log_path": self.log_path,
            "bridge_result": self.bridge_result,
            "state_after": self.state_after,
        }


# ---------------------------------------------------------------------------
# Bar-loader adapter
# ---------------------------------------------------------------------------


def _default_bar_loader(
    symbols: Sequence[str],
    interval: str,
    start: str,
    end: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Warehouse-first bar loader used by the daemon in production.

    Kept as a function (not a method) so tests can inject their own
    without patching module globals.
    """
    from strategy.local_warehouse import WarehouseLayout
    from strategy.market_data_provider import (
        AdjustmentMode,
        AssetClass,
        BarInterval,
    )
    from strategy.portfolio_simulator import bars_from_warehouse_hit_bars
    from strategy.warehouse.research_cache import WarehouseReader

    bar_interval = BarInterval(interval)
    layout = WarehouseLayout.from_env(dict(os.environ))
    bars: Dict[str, List[Dict[str, Any]]] = {}
    with WarehouseReader(layout=layout) as reader:
        for symbol in symbols:
            hit = reader.fetch_bars(
                asset_class=AssetClass.EQUITY,
                interval=bar_interval,
                symbols=[symbol],
                start=start,
                end=end,
                adjustment=AdjustmentMode.RAW,
            )
            bars[symbol] = bars_from_warehouse_hit_bars(hit.bars)
    return bars


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


class EquityPaperDaemon:
    """15-minute equity paper daemon.

    Each :meth:`run_tick` call resolves the session, loads bars,
    scores symbols, applies caps and cooldowns, and either writes a
    plan or hands off to the paper bridge.
    """

    def __init__(
        self,
        config: EquityPaperDaemonConfig,
        *,
        bar_loader=None,
        strategy_builder=None,
    ) -> None:
        config.validate()
        self._config = config
        self._logger = logging.getLogger("strategy.paper_daemon")
        self._bar_loader = bar_loader or _default_bar_loader
        self._strategy_builder = strategy_builder or self._default_strategy_builder

    def run_tick(
        self,
        *,
        now: Optional[datetime] = None,
        env: Optional[Mapping[str, str]] = None,
        bars_by_symbol: Optional[
            Mapping[str, Sequence[Mapping[str, Any]]]
        ] = None,
        state: Optional[DaemonState] = None,
    ) -> DaemonTickResult:
        """Execute a single scheduling tick.

        ``env``, ``bars_by_symbol``, ``state``, and ``now`` are
        exposed for tests.  Production callers should let the
        daemon load its own env and state.
        """
        status = session_status(now)
        tick_id = self._make_tick_id(status)
        generated_at = datetime.now(timezone.utc).isoformat()
        result = DaemonTickResult(
            tick_id=tick_id,
            generated_at=generated_at,
            session_status=status.to_dict(),
            would_have_executed=False,
            executed=False,
            reason="",
        )

        # 1. Emergency stop.
        if self._emergency_stop_active():
            result.reason = "emergency_stop_file_present"
            result.log_path = self._write_log(result)
            return result

        # 2. Session gate.
        if not status.is_regular_session:
            result.reason = f"session_not_open:{status.reason}"
            result.log_path = self._write_log(result)
            return result

        # 3. Env gate.
        try:
            resolved_env: Mapping[str, str]
            if env is None:
                resolved_env = load_paper_env(self._config.env_file)
            else:
                # Still enforce the refusals so tests exercise them.
                self._refuse_production_env(env)
                resolved_env = env
        except PaperBridgeError as exc:
            result.reason = f"env_refused:{exc}"
            result.log_path = self._write_log(result)
            return result

        # 4. Load state.
        current_day = status.now_et.date().isoformat()
        active_state = state or _load_state(self._config.state_file)
        active_state = _reset_state_for_new_day(active_state, current_day)

        # 5. Load bars.
        try:
            if bars_by_symbol is None:
                bars = self._bar_loader(
                    self._config.symbol_allowlist,
                    self._config.interval,
                    self._lookback_start(status.now_et.date()),
                    current_day,
                )
            else:
                bars = {
                    sym: list(v) for sym, v in bars_by_symbol.items()
                }
        except Exception as exc:  # pragma: no cover - defensive
            result.reason = f"bar_load_failed:{exc}"
            result.log_path = self._write_log(result)
            return result

        # 6. Score.
        try:
            proposed, rejected = self._score_and_plan(
                bars=bars,
                now_et=status.now_et,
                state=active_state,
            )
        except Exception as exc:
            result.reason = f"scoring_failed:{exc}"
            result.log_path = self._write_log(result)
            return result

        result.proposed_orders = [o for o in proposed]
        result.rejected_orders = [o for o in rejected]

        # 7. Write plan JSON.
        result.plan_path = self._write_plan(tick_id, proposed, generated_at)

        # 8. Execute-gate: dry-run is default.
        auto_execute_env = str(
            resolved_env.get(AUTO_EXECUTE_ENV_VAR, "")
        ).strip().lower()
        auto_execute_enabled = auto_execute_env in ("1", "true", "yes", "on")
        result.would_have_executed = (
            self._config.execute and auto_execute_enabled and bool(proposed)
        )

        if not self._config.execute:
            result.reason = "cli_execute_flag_not_set"
        elif not auto_execute_enabled:
            result.reason = (
                f"{AUTO_EXECUTE_ENV_VAR}_not_true "
                f"(dry-run: env value was {auto_execute_env!r})"
            )
        elif not proposed:
            result.reason = "no_orders_proposed"
        else:
            # 9. Execute via paper bridge.
            bridge_result = self._execute_via_bridge(
                proposed_orders=proposed,
                env=resolved_env,
            )
            result.executed = bridge_result.executed
            result.bridge_result = bridge_result.to_dict()
            if bridge_result.executed:
                self._commit_state(
                    active_state, proposed, status.now_et
                )
                result.reason = "executed"
            else:
                result.reason = "bridge_did_not_execute"

        _save_state(active_state, self._config.state_file)
        result.state_after = active_state.to_dict()
        result.log_path = self._write_log(result)
        return result

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_and_plan(
        self,
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        now_et: datetime,
        state: DaemonState,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return ``(accepted, rejected)`` order dicts.

        Applies per-symbol cooldown, daily trade cap, and daily
        notional cap in a single pass.  Symbols not present in
        ``bars`` are silently skipped.  Duplicate protection
        rejects any symbol already on today's submitted list.
        """
        from strategy.market_data_provider import BarInterval
        from strategy.portfolio_simulator import events_from_bars

        interval_enum = BarInterval(self._config.interval)
        events = events_from_bars(bars)
        if not events:
            return [], []
        last_event = events[-1]
        evaluator = self._strategy_builder(
            strategy_key=self._config.strategy_key,
            bars_by_symbol=bars,
            symbols=list(self._config.symbol_allowlist),
            interval=interval_enum,
        )
        evaluation = evaluator.evaluate(last_event)
        scores = dict(evaluation.scores or {})
        rankings = list(evaluation.rankings or [])
        ranked = [r["symbol"] for r in rankings if r.get("symbol")]

        # Snapshot last close per symbol.
        last_close: Dict[str, float] = {}
        for symbol, symbol_bars in bars.items():
            if not symbol_bars:
                continue
            last_close[symbol] = float(symbol_bars[-1].get("c", 0.0))

        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        cooldown_seconds = self._config.cooldown_minutes * 60
        for symbol in ranked[: self._config.top_n_per_tick]:
            price = last_close.get(symbol, 0.0)
            score = scores.get(symbol, 0.0)
            base = {
                "symbol": symbol,
                "side": "buy",
                "reference_price": price,
                "score": score,
                "reason": f"top_rank score={score:+.4f}",
            }
            if price <= 0:
                rejected.append({**base, "rejection": "no_reference_price"})
                continue
            quantity = int(
                self._config.max_per_order_notional // price
            )
            if quantity < 1:
                rejected.append(
                    {**base, "rejection": "quantity_would_be_zero"}
                )
                continue
            notional = round(quantity * price, 2)
            base["quantity"] = quantity
            base["reference_notional"] = notional
            # Duplicate protection: skip symbols already traded today.
            if symbol in state.submitted_order_ids:
                rejected.append(
                    {**base, "rejection": "duplicate_symbol_today"}
                )
                continue
            # Cooldown protection.
            last_iso = state.last_order_iso_by_symbol.get(symbol)
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(last_iso)
                except ValueError:
                    last_dt = None
                if last_dt is not None:
                    now_utc = (
                        now_et.astimezone(timezone.utc)
                        if now_et.tzinfo
                        else now_et.replace(tzinfo=timezone.utc)
                    )
                    delta = (now_utc - last_dt).total_seconds()
                    if delta < cooldown_seconds:
                        rejected.append(
                            {
                                **base,
                                "rejection": (
                                    f"cooldown_active "
                                    f"({int(cooldown_seconds - delta)}s left)"
                                ),
                            }
                        )
                        continue
            # Daily trade cap.
            if (
                state.trades_today + len(accepted)
                >= self._config.max_trades_per_day
            ):
                rejected.append(
                    {**base, "rejection": "max_trades_per_day_reached"}
                )
                continue
            # Daily notional cap.
            projected_notional = (
                state.notional_today
                + sum(o["reference_notional"] for o in accepted)
                + notional
            )
            if projected_notional > self._config.max_total_daily_notional:
                rejected.append(
                    {**base, "rejection": "max_total_daily_notional_reached"}
                )
                continue
            accepted.append(base)
        return accepted, rejected

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_via_bridge(
        self,
        proposed_orders: List[Dict[str, Any]],
        env: Mapping[str, str],
    ) -> PaperBridgeResult:
        # Build an in-memory plan for the bridge.
        plan_payload = {
            "run_id": "paper-daemon-tick",
            "strategy": {
                "name": self._config.strategy_key,
                "version": "unknown",
                "strategy_id": self._config.strategy_key,
            },
            "window": {
                "start": "",
                "end": "",
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "orders": [
                {
                    "symbol": o["symbol"],
                    "side": o["side"],
                    "quantity": o["quantity"],
                    "reference_price": o["reference_price"],
                    "reference_notional": o["reference_notional"],
                    "reason": o["reason"],
                }
                for o in proposed_orders
            ],
        }
        plan_dir = Path(self._config.plan_root)
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plan_dir / "daemon-tick-latest.json"
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(
                plan_payload,
                fh,
                indent=2,
                sort_keys=True,
                default=str,
            )
        bridge_cfg = PaperBridgeConfig(
            execute=True,
            symbol_allowlist=tuple(self._config.symbol_allowlist),
            max_per_order_notional=self._config.max_per_order_notional,
            max_total_notional=self._config.max_total_daily_notional,
            max_order_quantity=1_000_000.0,
            env_file=self._config.env_file,
            log_root=self._config.log_root,
        )
        bridge = PaperTradingBridge(bridge_cfg)
        return bridge.run(str(plan_path), env=env)

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _commit_state(
        self,
        state: DaemonState,
        orders: Sequence[Mapping[str, Any]],
        now_et: datetime,
    ) -> None:
        now_iso = (
            now_et.astimezone(timezone.utc).isoformat()
            if now_et.tzinfo
            else now_et.replace(tzinfo=timezone.utc).isoformat()
        )
        for order in orders:
            symbol = str(order["symbol"])
            state.submitted_order_ids.append(symbol)
            state.last_order_iso_by_symbol[symbol] = now_iso
            state.trades_today += 1
            state.notional_today += float(order["reference_notional"])

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _write_plan(
        self,
        tick_id: str,
        orders: Sequence[Mapping[str, Any]],
        generated_at: str,
    ) -> str:
        if not orders:
            return ""
        try:
            root = Path(self._config.plan_root)
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{tick_id}.json"
            payload = {
                "tick_id": tick_id,
                "generated_at": generated_at,
                "strategy_key": self._config.strategy_key,
                "interval": self._config.interval,
                "orders": [dict(o) for o in orders],
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    payload,
                    fh,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            return str(path)
        except (OSError, TypeError):
            return ""

    def _write_log(self, result: DaemonTickResult) -> str:
        try:
            root = Path(self._config.log_root)
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{result.tick_id}.log.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    result.to_dict(),
                    fh,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            return str(path)
        except (OSError, TypeError):
            return ""

    def _emergency_stop_active(self) -> bool:
        path = self._config.emergency_stop_file
        return bool(path) and os.path.exists(path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_tick_id(self, status: SessionStatus) -> str:
        stamp = status.now_et.strftime("%Y%m%dT%H%M%S")
        return f"equity-{stamp}"

    def _lookback_start(self, today: date) -> str:
        return (
            date.fromordinal(
                today.toordinal() - self._config.lookback_days * 3
            )
        ).isoformat()

    def _refuse_production_env(self, env: Mapping[str, str]) -> None:
        ctx = str(env.get("HERMES_CONTEXT", "")).strip().lower()
        if ctx == "production":
            raise PaperBridgeError(
                "refusing to run: HERMES_CONTEXT=production"
            )
        paper = str(env.get("PAPER", "")).strip().lower()
        if paper in ("false", "0", "no", "off"):
            raise PaperBridgeError(
                f"refusing to run: PAPER={paper!r}"
            )

    def _default_strategy_builder(
        self,
        strategy_key: str,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        symbols: Sequence[str],
        interval,
    ):
        # Local import to avoid pulling the strategy universe at
        # module import time (keeps unit tests fast).
        from strategy.portfolio_simulator_cli import build_strategy_bundle

        bundle = build_strategy_bundle(
            strategy_key=strategy_key,
            bars_by_symbol=bars_by_symbol,
            symbols=symbols,
            interval=interval,
        )
        return bundle.evaluator


__all__ = [
    "AUTO_EXECUTE_ENV_VAR",
    "DEFAULT_EMERGENCY_STOP_FILE",
    "DEFAULT_LOG_ROOT",
    "DEFAULT_MAX_PER_ORDER_NOTIONAL",
    "DEFAULT_MAX_TOTAL_DAILY_NOTIONAL",
    "DEFAULT_MAX_TRADES_PER_DAY",
    "DEFAULT_PLAN_ROOT",
    "DEFAULT_STATE_FILE",
    "DaemonState",
    "DaemonTickResult",
    "EquityPaperDaemon",
    "EquityPaperDaemonConfig",
    "PaperDaemonError",
]
