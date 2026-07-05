"""Paper-trading bridge — strictly gated adapter from research plan → paper orders.

This module is intentionally minimal and defensive.  It reads a
simulator- or research-generated ``orders.json``, applies safety
checks, and either previews the resulting paper orders (dry-run,
default) or submits them to a **paper** Alpaca account after
``--execute-paper-orders`` is explicitly passed.

Hard safety rules (enforced on every call, not just the CLI):

* Refuses to load ``.env.production``.
* Refuses if any env indicates production (``HERMES_CONTEXT=production``
  or ``PAPER`` set to a false-y value).
* Refuses if the resolved Alpaca endpoint is not a paper endpoint.
* Refuses to submit real orders unless the ``--execute-paper-orders``
  flag is explicitly set — the default is dry-run.
* Enforces a symbol allowlist, per-order notional cap, and
  aggregate notional cap.
* Never mutates feature flags, never advances promotion state,
  never records an ``ApprovalRecord``.
* Every proposed and (if executed) submitted order is logged to
  ``reports/paper_bridge/<generated_at>.log``.

The Alpaca SDK is imported **lazily** inside :meth:`execute` so
tests, dry-runs, and CI never depend on it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
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


DEFAULT_PAPER_ENV_FILE = ".env.paper"
PRODUCTION_ENV_FILE = ".env.production"
DEFAULT_MAX_PER_ORDER_NOTIONAL = 5_000.0
DEFAULT_MAX_TOTAL_NOTIONAL = 25_000.0
DEFAULT_MAX_ORDER_QUANTITY = 500.0
DEFAULT_LOG_ROOT = "reports/paper_bridge"

PAPER_ENDPOINT_MARKERS: Tuple[str, ...] = ("paper-api.alpaca.markets",)


class PaperBridgeError(RuntimeError):
    """Raised when the bridge refuses to run."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperBridgeConfig:
    """All knobs for a single bridge run."""

    execute: bool = False
    symbol_allowlist: Tuple[str, ...] = ()
    max_per_order_notional: float = DEFAULT_MAX_PER_ORDER_NOTIONAL
    max_total_notional: float = DEFAULT_MAX_TOTAL_NOTIONAL
    max_order_quantity: float = DEFAULT_MAX_ORDER_QUANTITY
    env_file: str = DEFAULT_PAPER_ENV_FILE
    log_root: str = DEFAULT_LOG_ROOT

    def validate(self) -> None:
        if not self.env_file:
            raise PaperBridgeError("env_file is required")
        base = os.path.basename(self.env_file)
        if base == PRODUCTION_ENV_FILE:
            raise PaperBridgeError(
                f"refusing to run: env_file={self.env_file} is production"
            )
        if not base.startswith(".env.paper"):
            raise PaperBridgeError(
                f"refusing to run: env_file basename must start with "
                f"'.env.paper' (got {base!r})"
            )
        if self.max_per_order_notional <= 0:
            raise PaperBridgeError("max_per_order_notional must be positive")
        if self.max_total_notional <= 0:
            raise PaperBridgeError("max_total_notional must be positive")
        if self.max_per_order_notional > self.max_total_notional:
            raise PaperBridgeError(
                "max_per_order_notional cannot exceed max_total_notional"
            )
        if self.max_order_quantity <= 0:
            raise PaperBridgeError("max_order_quantity must be positive")


# ---------------------------------------------------------------------------
# Order record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperOrder:
    """A single proposed order that would go to the paper broker."""

    symbol: str
    side: str
    quantity: float
    reference_price: float
    reference_notional: float
    reason: str
    accepted: bool
    rejection: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "reference_price": self.reference_price,
            "reference_notional": self.reference_notional,
            "reason": self.reason,
            "accepted": self.accepted,
            "rejection": self.rejection,
        }


@dataclass
class PaperBridgeResult:
    """Everything a bridge run produced."""

    plan_path: str
    generated_at: str
    orders: List[PaperOrder]
    submitted: List[Dict[str, Any]] = field(default_factory=list)
    executed: bool = False
    warnings: List[str] = field(default_factory=list)
    log_path: str = ""
    total_accepted_notional: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_path": self.plan_path,
            "generated_at": self.generated_at,
            "orders": [o.to_dict() for o in self.orders],
            "submitted": list(self.submitted),
            "executed": self.executed,
            "warnings": list(self.warnings),
            "log_path": self.log_path,
            "total_accepted_notional": self.total_accepted_notional,
        }


# ---------------------------------------------------------------------------
# Env loading and safety guards
# ---------------------------------------------------------------------------


def _parse_env_file(path: str) -> Dict[str, str]:
    """Very small ``.env`` reader — only ``KEY=VALUE`` lines.

    Deliberately not calling ``python-dotenv`` so the module has no
    optional dependencies and cannot be subverted by a plugin's
    load-hook.
    """
    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            values[key] = value
    return values


def _refuse_production_env(env: Mapping[str, str]) -> None:
    """Guard against production-shaped process env.

    Called after the paper env file is loaded so that a
    ``PAPER=False`` inside ``.env.paper`` also trips the guard.
    """
    hermes_ctx = str(env.get("HERMES_CONTEXT", "")).strip().lower()
    if hermes_ctx == "production":
        raise PaperBridgeError(
            "refusing to run: HERMES_CONTEXT=production"
        )
    paper_value = str(env.get("PAPER", "")).strip().lower()
    if paper_value in ("false", "0", "no", "off"):
        raise PaperBridgeError(
            f"refusing to run: PAPER={paper_value!r}; the bridge is "
            "paper-only and refuses anything that reads as False"
        )


def _resolve_endpoint(env: Mapping[str, str]) -> str:
    """Pick the paper endpoint from the env, honouring the
    ``PAPER_ALPACA_ENDPOINT`` convention in :file:`.env.paper` and
    falling back to ``ALPACA_ENDPOINT`` if that is the only one
    present.  Refuses anything that does not look like a paper
    endpoint.
    """
    for key in ("PAPER_ALPACA_ENDPOINT", "ALPACA_ENDPOINT"):
        value = env.get(key, "").strip()
        if value:
            if not any(marker in value for marker in PAPER_ENDPOINT_MARKERS):
                raise PaperBridgeError(
                    f"refusing to run: {key}={value!r} does not look "
                    "like a paper endpoint "
                    f"(expected one of {PAPER_ENDPOINT_MARKERS})"
                )
            return value
    # No endpoint set: the Alpaca SDK will infer it from paper=True,
    # but we still want to fail loudly rather than guess.
    raise PaperBridgeError(
        "refusing to run: neither PAPER_ALPACA_ENDPOINT nor "
        "ALPACA_ENDPOINT is set"
    )


def _resolve_credentials(env: Mapping[str, str]) -> Tuple[str, str]:
    """Return ``(api_key, secret_key)`` from the paper env.

    Prefers ``PAPER_ALPACA_*`` and falls back to ``ALPACA_*`` if
    the paper-specific keys are missing.  Refuses if either is
    absent.
    """
    key = env.get("PAPER_ALPACA_API_KEY", "").strip() or env.get(
        "ALPACA_API_KEY", ""
    ).strip()
    secret = env.get("PAPER_ALPACA_SECRET_KEY", "").strip() or env.get(
        "ALPACA_SECRET_KEY", ""
    ).strip()
    if not key or not secret:
        raise PaperBridgeError(
            "refusing to run: paper Alpaca API key/secret not set "
            "(looked for PAPER_ALPACA_API_KEY / PAPER_ALPACA_SECRET_KEY, "
            "then ALPACA_API_KEY / ALPACA_SECRET_KEY)"
        )
    return key, secret


def load_paper_env(env_file: str) -> Dict[str, str]:
    """Load a paper env file, honouring project isolation rules.

    Combines the file's contents with the caller's ``os.environ``
    (file values override — the file is the source of truth for
    the bridge).  Refuses to load ``.env.production`` and refuses
    if any production markers are present after the merge.
    """
    base = os.path.basename(env_file)
    if base == PRODUCTION_ENV_FILE:
        raise PaperBridgeError(
            f"refusing to load {env_file}: production env is off-limits"
        )
    if not base.startswith(".env.paper"):
        raise PaperBridgeError(
            f"refusing to load {env_file}: bridge requires a "
            "'.env.paper*' file"
        )
    if not os.path.exists(env_file):
        raise PaperBridgeError(
            f"refusing to load {env_file}: file not found. Populate "
            "your paper credentials there first."
        )
    file_env = _parse_env_file(env_file)
    merged: Dict[str, str] = {**os.environ, **file_env}
    _refuse_production_env(merged)
    return merged


# ---------------------------------------------------------------------------
# Plan loading
# ---------------------------------------------------------------------------


def load_plan(path: str) -> Dict[str, Any]:
    """Read the ``orders.json`` a simulation run wrote."""
    if not os.path.exists(path):
        raise PaperBridgeError(f"plan file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        try:
            plan = json.load(fh)
        except json.JSONDecodeError as exc:
            raise PaperBridgeError(
                f"plan file {path!r} is not valid JSON: {exc}"
            ) from exc
    if not isinstance(plan, Mapping):
        raise PaperBridgeError(
            f"plan file {path!r} must be a JSON object"
        )
    orders = plan.get("orders")
    if not isinstance(orders, list):
        raise PaperBridgeError(
            f"plan file {path!r} missing 'orders' list"
        )
    return dict(plan)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class PaperTradingBridge:
    """Turns a research plan into paper orders — safely."""

    def __init__(self, config: PaperBridgeConfig):
        config.validate()
        self._config = config
        self._logger = logging.getLogger("strategy.paper_bridge")

    def evaluate_plan(self, plan: Mapping[str, Any]) -> List[PaperOrder]:
        """Turn a plan's raw ``orders`` list into vetted
        :class:`PaperOrder` records.  Does not talk to a broker.
        """
        allow = {s.upper() for s in self._config.symbol_allowlist}
        allow_all = not allow
        raw_orders = plan.get("orders", [])
        vetted: List[PaperOrder] = []
        total_accepted = 0.0
        for raw in raw_orders:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol", "")).upper()
            side = str(raw.get("side", "")).lower()
            quantity = float(raw.get("quantity", 0) or 0)
            ref_price = float(raw.get("reference_price", 0) or 0)
            ref_notional = float(
                raw.get("reference_notional", ref_price * quantity) or 0
            )
            reason = str(raw.get("reason", ""))

            rejection: Optional[str] = None
            if not symbol:
                rejection = "missing symbol"
            elif side not in ("buy", "sell"):
                rejection = f"unsupported side {side!r}"
            elif quantity <= 0:
                rejection = "non-positive quantity"
            elif ref_price <= 0:
                rejection = "non-positive reference price"
            elif not allow_all and symbol not in allow:
                rejection = "symbol not in allowlist"
            elif quantity > self._config.max_order_quantity:
                rejection = (
                    f"quantity {quantity} exceeds "
                    f"max_order_quantity {self._config.max_order_quantity}"
                )
            elif ref_notional > self._config.max_per_order_notional:
                rejection = (
                    f"notional {ref_notional:.2f} exceeds "
                    f"max_per_order_notional "
                    f"{self._config.max_per_order_notional:.2f}"
                )
            elif (
                total_accepted + ref_notional
                > self._config.max_total_notional
            ):
                rejection = (
                    f"aggregate notional would exceed "
                    f"max_total_notional {self._config.max_total_notional:.2f}"
                )

            accepted = rejection is None
            if accepted:
                total_accepted += ref_notional
            vetted.append(
                PaperOrder(
                    symbol=symbol or "?",
                    side=side or "?",
                    quantity=quantity,
                    reference_price=ref_price,
                    reference_notional=ref_notional,
                    reason=reason,
                    accepted=accepted,
                    rejection=rejection,
                )
            )
        return vetted

    def run(
        self,
        plan_path: str,
        env: Optional[Mapping[str, str]] = None,
    ) -> PaperBridgeResult:
        """Load the plan, vet orders, log everything, and optionally
        submit to the paper broker.

        ``env`` is passed in for tests that want to bypass reading
        an actual paper env file.  Production callers should let
        the bridge load the env itself.
        """
        plan = load_plan(plan_path)
        resolved_env: Mapping[str, str]
        if env is None:
            resolved_env = load_paper_env(self._config.env_file)
        else:
            _refuse_production_env(env)
            resolved_env = env

        orders = self.evaluate_plan(plan)
        total_accepted = sum(
            o.reference_notional for o in orders if o.accepted
        )
        generated_at = datetime.now(timezone.utc).isoformat()
        result = PaperBridgeResult(
            plan_path=plan_path,
            generated_at=generated_at,
            orders=orders,
            executed=False,
            total_accepted_notional=total_accepted,
        )

        log_path = self._log(result, plan, mode="dry-run")
        result.log_path = log_path

        if not self._config.execute:
            return result

        # Execute path — this is the ONLY branch that talks to Alpaca.
        credentials = _resolve_credentials(resolved_env)
        endpoint = _resolve_endpoint(resolved_env)
        submitted = self._submit_orders(
            [o for o in orders if o.accepted],
            api_key=credentials[0],
            secret_key=credentials[1],
            endpoint=endpoint,
        )
        result.submitted = submitted
        result.executed = True
        result.log_path = self._log(result, plan, mode="execute")
        return result

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def _submit_orders(
        self,
        orders: Sequence[PaperOrder],
        *,
        api_key: str,
        secret_key: str,
        endpoint: str,
    ) -> List[Dict[str, Any]]:
        """Submit vetted orders to the paper broker.

        Lazy-imports ``alpaca-py`` so the module has no import-time
        dependency and tests never accidentally construct a client.
        """
        if not orders:
            return []
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except Exception as exc:  # pragma: no cover - environment guard
            raise PaperBridgeError(
                f"alpaca-py SDK not available: {exc}"
            ) from exc

        # Belt-and-suspenders: force paper=True.  We refuse to
        # honor an ALPACA_ENDPOINT that doesn't look like a paper
        # endpoint, but we still pin paper=True on the client so a
        # misconfigured SDK cannot route to live.
        client = TradingClient(api_key, secret_key, paper=True)
        submitted: List[Dict[str, Any]] = []
        for order in orders:
            side = (
                OrderSide.BUY if order.side == "buy" else OrderSide.SELL
            )
            request = MarketOrderRequest(
                symbol=order.symbol,
                qty=order.quantity,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            try:
                submitted_order = client.submit_order(order_data=request)
                raw_id = getattr(submitted_order, "id", None)
                broker_order_id = None if raw_id is None else str(raw_id)
                submitted.append(
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "quantity": order.quantity,
                        "broker_order_id": broker_order_id,
                        "status": "submitted",
                    }
                )
            except Exception as exc:
                submitted.append(
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "quantity": order.quantity,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                self._logger.error(
                    "paper order submit failed for %s: %s",
                    order.symbol,
                    exc,
                )
        return submitted

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(
        self,
        result: PaperBridgeResult,
        plan: Mapping[str, Any],
        mode: str,
    ) -> str:
        try:
            root = Path(self._config.log_root)
            root.mkdir(parents=True, exist_ok=True)
            timestamp = result.generated_at.replace(":", "").replace("-", "")
            filename = f"paper-bridge-{timestamp}.log.json"
            path = root / filename
            payload = {
                "mode": mode,
                "config": {
                    "execute": self._config.execute,
                    "env_file": self._config.env_file,
                    "symbol_allowlist": list(self._config.symbol_allowlist),
                    "max_per_order_notional": (
                        self._config.max_per_order_notional
                    ),
                    "max_total_notional": self._config.max_total_notional,
                    "max_order_quantity": self._config.max_order_quantity,
                },
                "plan_metadata": {
                    "run_id": plan.get("run_id"),
                    "strategy": plan.get("strategy"),
                    "window": plan.get("window"),
                    "generated_at": plan.get("generated_at"),
                },
                "result": result.to_dict(),
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


__all__ = [
    "DEFAULT_LOG_ROOT",
    "DEFAULT_MAX_ORDER_QUANTITY",
    "DEFAULT_MAX_PER_ORDER_NOTIONAL",
    "DEFAULT_MAX_TOTAL_NOTIONAL",
    "DEFAULT_PAPER_ENV_FILE",
    "PAPER_ENDPOINT_MARKERS",
    "PRODUCTION_ENV_FILE",
    "PaperBridgeConfig",
    "PaperBridgeError",
    "PaperBridgeResult",
    "PaperOrder",
    "PaperTradingBridge",
    "load_paper_env",
    "load_plan",
]
