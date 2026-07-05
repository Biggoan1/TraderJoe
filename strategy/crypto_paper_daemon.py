"""Crypto 24/7 paper-trading daemon.

Runs a single "tick" of the crypto paper strategy: score the
crypto allowlist, apply notional / trade / cooldown / duplicate
caps, and either write a plan or hand off to a crypto paper
executor.  Never touches equity paper, never touches production.

Safety invariants:

* Loads ``.env.crypto`` (or an override with basename starting
  ``.env.crypto``).  Refuses ``.env.paper`` and ``.env.production``.
* Refuses if ``HERMES_CONTEXT=production`` or ``PAPER`` is set to a
  false value.
* Refuses when the emergency stop file exists.
* Dry-run by default.  Auto-execute requires both the env flag
  ``PAPER_AUTO_EXECUTE_CRYPTO=true`` and the CLI flag
  ``--execute-paper-orders``.
* Alpaca ``TradingClient`` is constructed only inside the execute
  branch, with ``paper=True`` pinned.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

from strategy.paper_bridge import _parse_env_file  # small helper reuse


DEFAULT_EMERGENCY_STOP_FILE = "/etc/traderjoe/STOP_CRYPTO_PAPER"
DEFAULT_STATE_FILE = "reports/paper_daemon/crypto_state.json"
DEFAULT_LOG_ROOT = "reports/paper_daemon/crypto"
DEFAULT_PLAN_ROOT = "reports/paper_daemon/crypto/plans"
DEFAULT_MAX_PER_ORDER_NOTIONAL = 1_500.0
DEFAULT_MAX_TOTAL_DAILY_NOTIONAL = 4_500.0
DEFAULT_MAX_TRADES_PER_DAY = 6
DEFAULT_COOLDOWN_MINUTES = 60

AUTO_EXECUTE_ENV_VAR = "PAPER_AUTO_EXECUTE_CRYPTO"
PAPER_ENDPOINT_MARKERS: Tuple[str, ...] = ("paper-api.alpaca.markets",)


class CryptoPaperDaemonError(RuntimeError):
    """Raised when the crypto daemon refuses to run."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoPaperDaemonConfig:
    """Configuration for a single crypto daemon tick."""

    symbol_allowlist: Tuple[str, ...]
    strategy_key: str = "momentum-v0.1.0"
    execute: bool = False
    max_per_order_notional: float = DEFAULT_MAX_PER_ORDER_NOTIONAL
    max_total_daily_notional: float = DEFAULT_MAX_TOTAL_DAILY_NOTIONAL
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    top_n_per_tick: int = 2
    env_file: str = ".env.crypto"
    emergency_stop_file: str = DEFAULT_EMERGENCY_STOP_FILE
    state_file: str = DEFAULT_STATE_FILE
    log_root: str = DEFAULT_LOG_ROOT
    plan_root: str = DEFAULT_PLAN_ROOT

    def validate(self) -> None:
        if not self.symbol_allowlist:
            raise CryptoPaperDaemonError(
                "symbol_allowlist must not be empty"
            )
        if not self.strategy_key:
            raise CryptoPaperDaemonError("strategy_key must be provided")
        base = os.path.basename(self.env_file)
        if base == ".env.production":
            raise CryptoPaperDaemonError(
                "refusing to run: env_file is production"
            )
        if base == ".env.paper" or base.startswith(".env.paper"):
            raise CryptoPaperDaemonError(
                f"refusing to run: crypto daemon requires .env.crypto*, "
                f"not {base!r}"
            )
        if not base.startswith(".env.crypto"):
            raise CryptoPaperDaemonError(
                f"env_file basename must start with '.env.crypto' "
                f"(got {base!r})"
            )
        if self.max_per_order_notional <= 0:
            raise CryptoPaperDaemonError(
                "max_per_order_notional must be positive"
            )
        if self.max_total_daily_notional < self.max_per_order_notional:
            raise CryptoPaperDaemonError(
                "max_total_daily_notional must be >= max_per_order_notional"
            )
        if self.max_trades_per_day < 1:
            raise CryptoPaperDaemonError(
                "max_trades_per_day must be >= 1"
            )
        if self.cooldown_minutes < 0:
            raise CryptoPaperDaemonError(
                "cooldown_minutes cannot be negative"
            )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class CryptoDaemonState:
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
    def from_dict(cls, payload: Mapping[str, Any]) -> "CryptoDaemonState":
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


def _load_state(path: str) -> CryptoDaemonState:
    if not path or not os.path.exists(path):
        return CryptoDaemonState()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return CryptoDaemonState()
    if not isinstance(payload, Mapping):
        return CryptoDaemonState()
    return CryptoDaemonState.from_dict(payload)


def _save_state(state: CryptoDaemonState, path: str) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
    except OSError:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Env loader — crypto-specific
# ---------------------------------------------------------------------------


def load_crypto_env(env_file: str) -> Dict[str, str]:
    base = os.path.basename(env_file)
    if base == ".env.production":
        raise CryptoPaperDaemonError(
            "refusing to load .env.production in crypto daemon"
        )
    if base.startswith(".env.paper"):
        raise CryptoPaperDaemonError(
            "refusing to load a paper env file in crypto daemon"
        )
    if not base.startswith(".env.crypto"):
        raise CryptoPaperDaemonError(
            f"refusing to load {env_file}: basename must start with "
            "'.env.crypto'"
        )
    if not os.path.exists(env_file):
        raise CryptoPaperDaemonError(
            f"refusing to run: {env_file} not found. Populate crypto "
            "credentials there first."
        )
    file_env = _parse_env_file(env_file)
    merged: Dict[str, str] = {**os.environ, **file_env}
    ctx = str(merged.get("HERMES_CONTEXT", "")).strip().lower()
    if ctx == "production":
        raise CryptoPaperDaemonError(
            "refusing to run: HERMES_CONTEXT=production"
        )
    paper = str(merged.get("PAPER", "")).strip().lower()
    if paper in ("false", "0", "no", "off"):
        raise CryptoPaperDaemonError(
            f"refusing to run: PAPER={paper!r}"
        )
    return merged


def _resolve_endpoint(env: Mapping[str, str]) -> str:
    for key in ("CRYPTO_ALPACA_ENDPOINT", "ALPACA_ENDPOINT"):
        value = str(env.get(key, "")).strip()
        if value:
            if not any(m in value for m in PAPER_ENDPOINT_MARKERS):
                raise CryptoPaperDaemonError(
                    f"refusing to run: {key}={value!r} does not look "
                    "like a paper endpoint"
                )
            return value
    raise CryptoPaperDaemonError(
        "refusing to run: no CRYPTO_ALPACA_ENDPOINT set"
    )


def _resolve_credentials(env: Mapping[str, str]) -> Tuple[str, str]:
    key = str(env.get("CRYPTO_ALPACA_API_KEY", "")).strip()
    secret = str(env.get("CRYPTO_ALPACA_SECRET_KEY", "")).strip()
    if not key or not secret:
        raise CryptoPaperDaemonError(
            "refusing to run: CRYPTO_ALPACA_API_KEY / "
            "CRYPTO_ALPACA_SECRET_KEY missing"
        )
    return key, secret


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CryptoDaemonTickResult:
    tick_id: str
    generated_at: str
    would_have_executed: bool
    executed: bool
    reason: str
    proposed_orders: List[Dict[str, Any]] = field(default_factory=list)
    rejected_orders: List[Dict[str, Any]] = field(default_factory=list)
    plan_path: str = ""
    log_path: str = ""
    submitted: List[Dict[str, Any]] = field(default_factory=list)
    state_after: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "generated_at": self.generated_at,
            "would_have_executed": self.would_have_executed,
            "executed": self.executed,
            "reason": self.reason,
            "proposed_orders": list(self.proposed_orders),
            "rejected_orders": list(self.rejected_orders),
            "plan_path": self.plan_path,
            "log_path": self.log_path,
            "submitted": list(self.submitted),
            "state_after": self.state_after,
        }


# ---------------------------------------------------------------------------
# Bar loader (in-memory by default for tests; production version fetches
# hourly crypto bars from the caller-configured provider)
# ---------------------------------------------------------------------------


ScoringFn = Any


def _default_scorer(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    symbols: Sequence[str],
    strategy_key: str,
    lookback: int = 20,
) -> Dict[str, float]:
    """Deterministic default scorer: trailing return over ``lookback``
    bars.  Kept simple so the crypto daemon can run against any
    provider that hands us OHLCV bars — no strategy-lab dependency
    required at production runtime.  Tests can swap in a custom
    ``scorer`` on the daemon constructor.
    """
    scores: Dict[str, float] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol) or []
        if len(bars) < lookback + 1:
            continue
        prev = float(bars[-(lookback + 1)].get("c", 0.0))
        curr = float(bars[-1].get("c", 0.0))
        if prev == 0.0:
            continue
        scores[symbol] = (curr - prev) / prev
    return scores


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class CryptoPaperDaemon:
    """24/7 crypto paper daemon.  One tick per :meth:`run_tick`."""

    def __init__(
        self,
        config: CryptoPaperDaemonConfig,
        *,
        bar_loader=None,
        scorer=None,
    ):
        config.validate()
        self._config = config
        self._bar_loader = bar_loader
        self._scorer = scorer or _default_scorer

    def run_tick(
        self,
        *,
        now: Optional[datetime] = None,
        env: Optional[Mapping[str, str]] = None,
        bars_by_symbol: Optional[
            Mapping[str, Sequence[Mapping[str, Any]]]
        ] = None,
        state: Optional[CryptoDaemonState] = None,
    ) -> CryptoDaemonTickResult:
        now_utc = (
            now.astimezone(timezone.utc)
            if now is not None and now.tzinfo
            else (
                now.replace(tzinfo=timezone.utc)
                if now is not None
                else datetime.now(timezone.utc)
            )
        )
        tick_id = f"crypto-{now_utc.strftime('%Y%m%dT%H%M%S')}"
        result = CryptoDaemonTickResult(
            tick_id=tick_id,
            generated_at=now_utc.isoformat(),
            would_have_executed=False,
            executed=False,
            reason="",
        )

        # 1. Emergency stop.
        if self._emergency_stop_active():
            result.reason = "emergency_stop_file_present"
            result.log_path = self._write_log(result)
            return result

        # 2. Env gate.
        try:
            resolved_env: Mapping[str, str]
            if env is None:
                resolved_env = load_crypto_env(self._config.env_file)
            else:
                self._refuse_production_env(env)
                resolved_env = env
        except CryptoPaperDaemonError as exc:
            result.reason = f"env_refused:{exc}"
            result.log_path = self._write_log(result)
            return result

        # 3. Load state.
        current_day = now_utc.date().isoformat()
        active_state = state or _load_state(self._config.state_file)
        if active_state.current_day != current_day:
            active_state = CryptoDaemonState(current_day=current_day)

        # 4. Load bars.
        try:
            if bars_by_symbol is None:
                if self._bar_loader is None:
                    raise CryptoPaperDaemonError(
                        "no bar_loader configured; supply bars_by_symbol "
                        "or configure a loader"
                    )
                bars = self._bar_loader(
                    self._config.symbol_allowlist, now_utc
                )
            else:
                bars = {sym: list(v) for sym, v in bars_by_symbol.items()}
        except Exception as exc:
            result.reason = f"bar_load_failed:{exc}"
            result.log_path = self._write_log(result)
            return result

        # 5. Score.
        scores = self._scorer(
            bars, self._config.symbol_allowlist, self._config.strategy_key
        )

        # 6. Plan.
        proposed, rejected = self._score_and_plan(
            scores=scores, bars=bars, now_utc=now_utc, state=active_state
        )
        result.proposed_orders = proposed
        result.rejected_orders = rejected
        result.plan_path = self._write_plan(tick_id, proposed, now_utc)

        # 7. Execute gate.
        auto_env = str(
            resolved_env.get(AUTO_EXECUTE_ENV_VAR, "")
        ).strip().lower()
        auto_execute_enabled = auto_env in ("1", "true", "yes", "on")
        result.would_have_executed = (
            self._config.execute and auto_execute_enabled and bool(proposed)
        )

        if not self._config.execute:
            result.reason = "cli_execute_flag_not_set"
        elif not auto_execute_enabled:
            result.reason = (
                f"{AUTO_EXECUTE_ENV_VAR}_not_true "
                f"(dry-run: env value was {auto_env!r})"
            )
        elif not proposed:
            result.reason = "no_orders_proposed"
        else:
            submitted = self._submit(
                proposed, env=resolved_env
            )
            result.submitted = submitted
            result.executed = any(
                s.get("status") == "submitted" for s in submitted
            )
            if result.executed:
                self._commit_state(active_state, proposed, now_utc)
                result.reason = "executed"
            else:
                result.reason = "no_submissions_succeeded"

        _save_state(active_state, self._config.state_file)
        result.state_after = active_state.to_dict()
        result.log_path = self._write_log(result)
        return result

    # ------------------------------------------------------------------
    # Scoring / capping
    # ------------------------------------------------------------------

    def _score_and_plan(
        self,
        scores: Mapping[str, float],
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        now_utc: datetime,
        state: CryptoDaemonState,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        ranked = sorted(
            scores.items(), key=lambda pair: (-pair[1], pair[0])
        )
        last_close: Dict[str, float] = {}
        for symbol, symbol_bars in bars.items():
            if symbol_bars:
                last_close[symbol] = float(symbol_bars[-1].get("c", 0.0))

        cooldown_seconds = self._config.cooldown_minutes * 60
        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        for symbol, score in ranked[: self._config.top_n_per_tick]:
            price = last_close.get(symbol, 0.0)
            base = {
                "symbol": symbol,
                "side": "buy",
                "reference_price": price,
                "score": score,
                "reason": f"top_rank score={score:+.4f}",
            }
            if score <= 0:
                rejected.append({**base, "rejection": "non_positive_score"})
                continue
            if price <= 0:
                rejected.append({**base, "rejection": "no_reference_price"})
                continue
            # Crypto supports fractional quantity; size to notional cap.
            notional = round(
                min(self._config.max_per_order_notional, price * 1_000_000),
                2,
            )
            quantity = round(notional / price, 8)
            if quantity <= 0:
                rejected.append(
                    {**base, "rejection": "quantity_would_be_zero"}
                )
                continue
            base["quantity"] = quantity
            base["reference_notional"] = notional
            if symbol in state.submitted_order_ids:
                rejected.append(
                    {**base, "rejection": "duplicate_symbol_today"}
                )
                continue
            last_iso = state.last_order_iso_by_symbol.get(symbol)
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(last_iso)
                except ValueError:
                    last_dt = None
                if last_dt is not None:
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
            if (
                state.trades_today + len(accepted)
                >= self._config.max_trades_per_day
            ):
                rejected.append(
                    {**base, "rejection": "max_trades_per_day_reached"}
                )
                continue
            projected = (
                state.notional_today
                + sum(o["reference_notional"] for o in accepted)
                + notional
            )
            if projected > self._config.max_total_daily_notional:
                rejected.append(
                    {**base, "rejection": "max_total_daily_notional_reached"}
                )
                continue
            accepted.append(base)
        return accepted, rejected

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _submit(
        self,
        orders: Sequence[Mapping[str, Any]],
        env: Mapping[str, str],
    ) -> List[Dict[str, Any]]:
        key, secret = _resolve_credentials(env)
        _resolve_endpoint(env)  # validates the endpoint is a paper URL
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except Exception as exc:  # pragma: no cover - env guard
            raise CryptoPaperDaemonError(
                f"alpaca-py SDK not available: {exc}"
            ) from exc

        client = TradingClient(key, secret, paper=True)
        submitted: List[Dict[str, Any]] = []
        for order in orders:
            request = MarketOrderRequest(
                symbol=str(order["symbol"]),
                qty=float(order["quantity"]),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,  # crypto is GTC-friendly
            )
            try:
                broker_order = client.submit_order(order_data=request)
                submitted.append(
                    {
                        "symbol": order["symbol"],
                        "quantity": order["quantity"],
                        "broker_order_id": getattr(broker_order, "id", None),
                        "status": "submitted",
                    }
                )
            except Exception as exc:
                submitted.append(
                    {
                        "symbol": order["symbol"],
                        "quantity": order["quantity"],
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        return submitted

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _commit_state(
        self,
        state: CryptoDaemonState,
        orders: Sequence[Mapping[str, Any]],
        now_utc: datetime,
    ) -> None:
        for order in orders:
            symbol = str(order["symbol"])
            state.submitted_order_ids.append(symbol)
            state.last_order_iso_by_symbol[symbol] = now_utc.isoformat()
            state.trades_today += 1
            state.notional_today += float(order["reference_notional"])

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _write_plan(
        self,
        tick_id: str,
        orders: Sequence[Mapping[str, Any]],
        now_utc: datetime,
    ) -> str:
        if not orders:
            return ""
        try:
            root = Path(self._config.plan_root)
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{tick_id}.json"
            payload = {
                "tick_id": tick_id,
                "generated_at": now_utc.isoformat(),
                "strategy_key": self._config.strategy_key,
                "orders": [dict(o) for o in orders],
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            return str(path)
        except OSError:
            return ""

    def _write_log(self, result: CryptoDaemonTickResult) -> str:
        try:
            root = Path(self._config.log_root)
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{result.tick_id}.log.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    result.to_dict(), fh, indent=2, sort_keys=True
                )
            return str(path)
        except OSError:
            return ""

    def _emergency_stop_active(self) -> bool:
        path = self._config.emergency_stop_file
        return bool(path) and os.path.exists(path)

    def _refuse_production_env(self, env: Mapping[str, str]) -> None:
        ctx = str(env.get("HERMES_CONTEXT", "")).strip().lower()
        if ctx == "production":
            raise CryptoPaperDaemonError(
                "refusing to run: HERMES_CONTEXT=production"
            )
        paper = str(env.get("PAPER", "")).strip().lower()
        if paper in ("false", "0", "no", "off"):
            raise CryptoPaperDaemonError(
                f"refusing to run: PAPER={paper!r}"
            )


__all__ = [
    "AUTO_EXECUTE_ENV_VAR",
    "CryptoDaemonState",
    "CryptoDaemonTickResult",
    "CryptoPaperDaemon",
    "CryptoPaperDaemonConfig",
    "CryptoPaperDaemonError",
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_EMERGENCY_STOP_FILE",
    "DEFAULT_LOG_ROOT",
    "DEFAULT_MAX_PER_ORDER_NOTIONAL",
    "DEFAULT_MAX_TOTAL_DAILY_NOTIONAL",
    "DEFAULT_MAX_TRADES_PER_DAY",
    "DEFAULT_PLAN_ROOT",
    "DEFAULT_STATE_FILE",
    "load_crypto_env",
]
