"""Provider-agnostic Market Data Interface.

Read-only foundation for Phase 5.6 (Historical Data Warehouse).  This
module defines the normalized data models and Protocol that every
future market-data provider (Alpaca, Polygon, Databento, Tiingo,
Financial Modeling Prep, Alpha Vantage, CSV importer, Parquet
importer, manual imports, …) must satisfy so the research replay
engine never has to know which provider produced the underlying data.

This module ships **no provider implementation**.  It only names the
shape.  Plugin implementations live under ``strategy/providers/`` and
land in follow-up Phase 5.6 cards
(``t_phase56_provider_plugins`` onward).

Design doc:
``docs/architecture/phase-5-6-historical-warehouse.md`` (§Architecture.1).

Read-only guarantees enforced by tests in
``tests/test_market_data_provider.py``:

* Never places, submits, cancels, or replaces orders.
* Never imports ``trader``, ``crypto_trader``, ``trader_cli``,
  ``telegram_approvals``, or ``strategy.runner``.
* Never mutates the global :class:`~strategy.config.FeatureFlags`.
* Never constructs an ``ApprovalRecord``.
* Never advances ``PromotionEntry`` state.
* Uses only the ``WAREHOUSE_*`` env-var namespace when it needs env
  (disjoint from ``ALPACA_*``, ``APCA_*``, ``RESEARCH_ALPACA_*``,
  ``CRYPTO_ALPACA_*``).

Terminology follows the Phase 5 convention: "validation", "replay",
"research", "acquisition".  Never "training".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Dict,
    Generic,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
    runtime_checkable,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BarInterval(Enum):
    """Bar sampling interval.

    String values mirror the widely-used Alpaca convention so the
    Phase 5.6 Alpaca plugin can round-trip without a lookup table.
    Consumers should treat the ``value`` field as opaque and rely on
    :meth:`is_intraday` for behavioral checks.
    """

    DAILY = "1Day"
    HOURLY = "1Hour"
    MINUTE_1 = "1Min"
    MINUTE_5 = "5Min"
    MINUTE_15 = "15Min"
    MINUTE_30 = "30Min"
    SECOND_1 = "1Sec"

    @property
    def is_intraday(self) -> bool:
        return self is not BarInterval.DAILY


class AdjustmentMode(Enum):
    """Corporate-action adjustment applied to bar values."""

    RAW = "raw"                    # unadjusted historical prices
    SPLIT = "split"                # split-adjusted only
    SPLIT_DIVIDEND = "split_dividend"  # split + cash-dividend adjusted
    TOTAL_RETURN = "total_return"  # split + reinvested-dividend adjusted


class CorporateActionKind(Enum):
    """Categories of corporate action the warehouse tracks."""

    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    SPECIAL_DIVIDEND = "special_dividend"
    TICKER_CHANGE = "ticker_change"
    DELISTING = "delisting"
    MERGER = "merger"


class CalendarSessionKind(Enum):
    """Kind of trading session an exchange calendar day describes."""

    FULL_TRADING = "full_trading"
    HALF_TRADING = "half_trading"
    HOLIDAY = "holiday"


class AssetClass(Enum):
    """High-level asset category — mirrors the warehouse directory
    partitioning (``equities/`` / ``crypto/`` / ``options/`` /
    ``futures/``).
    """

    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    OPTION = "option"
    FUTURE = "future"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class MarketDataValidationError(ValueError):
    """Raised when a normalized data-model dataclass fails invariant
    validation on construction.  Provider plugins that receive
    malformed vendor data should raise
    :class:`~strategy.market_data_provider.MarketDataRequestError`
    instead so the caller can distinguish shape errors from vendor
    failures.
    """


class MarketDataRequestError(RuntimeError):
    """Raised by a :class:`MarketDataProvider` implementation when a
    fetch request could not be completed (HTTP error, timeout,
    authentication failure).  Structural per-symbol issues (empty
    series, symbol not found) should be reported on
    :attr:`ProviderResponse.per_symbol_status` instead.
    """


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar for one symbol at one point in time.

    Timestamps are ISO 8601 strings so the model stays JSON-portable
    without a datetime library dependency.  All numeric fields are
    floats.  Consumers that need Decimal precision should convert at
    the query layer.

    ``adjustment_version`` is an opaque provider tag identifying
    which corporate-action revision produced the bar values.  For a
    RAW bar the version is empty.  Two bars from the same provider
    with the same ``(symbol, timestamp, interval, adjustment_mode)``
    but different ``adjustment_version`` values represent different
    revisions of the historical record.
    """

    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: BarInterval
    adjustment_mode: AdjustmentMode
    vwap: Optional[float] = None
    trade_count: Optional[int] = None
    adjustment_version: str = ""

    def __post_init__(self) -> None:
        if not self.symbol:
            raise MarketDataValidationError("symbol is required")
        if not self.timestamp:
            raise MarketDataValidationError("timestamp is required")
        # OHLC sanity: low must be the min, high the max, and both
        # must bracket open and close.  These are content constraints,
        # not just type constraints, and provider plugins are expected
        # to normalize before constructing a Bar.
        if self.low > self.high:
            raise MarketDataValidationError(
                f"low ({self.low}) must be <= high ({self.high})"
            )
        for name, value in (("open", self.open), ("close", self.close)):
            if value < self.low or value > self.high:
                raise MarketDataValidationError(
                    f"{name} ({value}) must be within [low={self.low}, high={self.high}]"
                )
        if self.volume < 0:
            raise MarketDataValidationError(
                f"volume ({self.volume}) must be non-negative"
            )
        if self.trade_count is not None and self.trade_count < 0:
            raise MarketDataValidationError(
                f"trade_count ({self.trade_count}) must be non-negative"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
            "interval": self.interval.value,
            "adjustment_mode": self.adjustment_mode.value,
            "vwap": None if self.vwap is None else float(self.vwap),
            "trade_count": self.trade_count,
            "adjustment_version": self.adjustment_version,
        }


@dataclass(frozen=True)
class CorporateAction:
    """A single corporate-action event for one symbol.

    Field applicability by :attr:`kind`:

    * ``SPLIT`` / ``REVERSE_SPLIT`` — require ``ratio``.
    * ``CASH_DIVIDEND`` / ``SPECIAL_DIVIDEND`` — require ``amount``
      and ``currency``.
    * ``TICKER_CHANGE`` — require ``new_symbol``.
    * ``DELISTING`` — require ``reason``.
    * ``MERGER`` — require ``surviving_entity``.
    """

    symbol: str
    kind: CorporateActionKind
    ex_date: str
    pay_date: Optional[str] = None
    ratio: Optional[float] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    new_symbol: Optional[str] = None
    surviving_entity: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise MarketDataValidationError("symbol is required")
        if not self.ex_date:
            raise MarketDataValidationError("ex_date is required")
        if self.kind in {
            CorporateActionKind.SPLIT,
            CorporateActionKind.REVERSE_SPLIT,
        }:
            if self.ratio is None or self.ratio <= 0:
                raise MarketDataValidationError(
                    f"{self.kind.value} requires a positive ratio"
                )
        elif self.kind in {
            CorporateActionKind.CASH_DIVIDEND,
            CorporateActionKind.SPECIAL_DIVIDEND,
        }:
            if self.amount is None or self.amount <= 0:
                raise MarketDataValidationError(
                    f"{self.kind.value} requires a positive amount"
                )
            if not self.currency:
                raise MarketDataValidationError(
                    f"{self.kind.value} requires a currency"
                )
        elif self.kind is CorporateActionKind.TICKER_CHANGE:
            if not self.new_symbol:
                raise MarketDataValidationError(
                    "ticker_change requires new_symbol"
                )
        elif self.kind is CorporateActionKind.DELISTING:
            if not self.reason:
                raise MarketDataValidationError(
                    "delisting requires a reason"
                )
        elif self.kind is CorporateActionKind.MERGER:
            if not self.surviving_entity:
                raise MarketDataValidationError(
                    "merger requires surviving_entity"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind.value,
            "ex_date": self.ex_date,
            "pay_date": self.pay_date,
            "ratio": None if self.ratio is None else float(self.ratio),
            "amount": None if self.amount is None else float(self.amount),
            "currency": self.currency,
            "new_symbol": self.new_symbol,
            "surviving_entity": self.surviving_entity,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SymbolMetadata:
    """Descriptive metadata for one symbol.

    Vendors return different subsets of these fields; unknown values
    stay ``None`` rather than being fabricated.  The warehouse joins
    metadata across providers to build a canonical row.
    """

    symbol: str
    name: str
    exchange: str
    asset_class: AssetClass
    listing_date: Optional[str] = None
    delisting_date: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    currency: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise MarketDataValidationError("symbol is required")
        if not self.exchange:
            raise MarketDataValidationError("exchange is required")
        # Aliases are normalized to a tuple so the frozen dataclass
        # can be hashed.
        object.__setattr__(self, "aliases", tuple(self.aliases))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "asset_class": self.asset_class.value,
            "listing_date": self.listing_date,
            "delisting_date": self.delisting_date,
            "sector": self.sector,
            "industry": self.industry,
            "aliases": list(self.aliases),
            "currency": self.currency,
        }


@dataclass(frozen=True)
class CalendarDay:
    """One exchange trading-calendar row.

    ``open_time`` and ``close_time`` are ``HH:MM`` strings in the
    exchange's local timezone.  A ``HOLIDAY`` row leaves both as
    ``None``; a ``HALF_TRADING`` row uses the shortened session's
    open/close.
    """

    exchange: str
    date: str
    kind: CalendarSessionKind
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    timezone: str = ""

    def __post_init__(self) -> None:
        if not self.exchange:
            raise MarketDataValidationError("exchange is required")
        if not self.date:
            raise MarketDataValidationError("date is required")
        if self.kind is CalendarSessionKind.HOLIDAY:
            if self.open_time is not None or self.close_time is not None:
                raise MarketDataValidationError(
                    "holiday sessions must have no open_time / close_time"
                )
        else:
            if not self.open_time or not self.close_time:
                raise MarketDataValidationError(
                    f"{self.kind.value} sessions require open_time and close_time"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange,
            "date": self.date,
            "kind": self.kind.value,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "timezone": self.timezone,
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    """Advertised capabilities of a provider plugin.

    The warehouse consults this before choosing a provider for a
    request.  A capability of ``None`` means "unknown / not
    advertised" and callers should treat it as "assume worst case".
    """

    provider_name: str
    supported_intervals: Tuple[BarInterval, ...]
    supported_adjustments: Tuple[AdjustmentMode, ...]
    supported_asset_classes: Tuple[AssetClass, ...]
    supports_corporate_actions: bool
    supports_symbol_metadata: bool
    supports_calendar: bool
    supported_exchanges: Tuple[str, ...] = ()
    latest_data_lag_minutes: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None
    cost_per_request_usd: Optional[float] = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.provider_name:
            raise MarketDataValidationError("provider_name is required")
        # Freeze sequences to tuples so instances stay hashable.
        object.__setattr__(
            self, "supported_intervals", tuple(self.supported_intervals)
        )
        object.__setattr__(
            self, "supported_adjustments", tuple(self.supported_adjustments)
        )
        object.__setattr__(
            self,
            "supported_asset_classes",
            tuple(self.supported_asset_classes),
        )
        object.__setattr__(
            self, "supported_exchanges", tuple(self.supported_exchanges)
        )
        if (
            self.latest_data_lag_minutes is not None
            and self.latest_data_lag_minutes < 0
        ):
            raise MarketDataValidationError(
                "latest_data_lag_minutes must be non-negative"
            )
        if (
            self.rate_limit_per_minute is not None
            and self.rate_limit_per_minute <= 0
        ):
            raise MarketDataValidationError(
                "rate_limit_per_minute must be positive"
            )
        if (
            self.cost_per_request_usd is not None
            and self.cost_per_request_usd < 0
        ):
            raise MarketDataValidationError(
                "cost_per_request_usd must be non-negative"
            )

    def supports_interval(self, interval: BarInterval) -> bool:
        return interval in self.supported_intervals

    def supports_adjustment(self, adjustment: AdjustmentMode) -> bool:
        return adjustment in self.supported_adjustments

    def supports_asset_class(self, asset_class: AssetClass) -> bool:
        return asset_class in self.supported_asset_classes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "supported_intervals": [
                interval.value for interval in self.supported_intervals
            ],
            "supported_adjustments": [
                adjustment.value for adjustment in self.supported_adjustments
            ],
            "supported_asset_classes": [
                cls.value for cls in self.supported_asset_classes
            ],
            "supports_corporate_actions": self.supports_corporate_actions,
            "supports_symbol_metadata": self.supports_symbol_metadata,
            "supports_calendar": self.supports_calendar,
            "supported_exchanges": list(self.supported_exchanges),
            "latest_data_lag_minutes": self.latest_data_lag_minutes,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "cost_per_request_usd": self.cost_per_request_usd,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Response wrapper
# ---------------------------------------------------------------------------


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderResponse(Generic[T]):
    """Wrapper around a provider's response batch.

    Carries the batch itself plus per-request telemetry the warehouse
    consumes (redacted URL, provider trace id, per-symbol status,
    warnings, cost estimate).  ``request_url_redacted`` MUST never
    include credentials — implementations are responsible for
    replacing API keys with ``***``.
    """

    provider: str
    batch: T
    request_url_redacted: str = ""
    trace_id: str = ""
    warnings: Tuple[str, ...] = ()
    cost_estimate_usd: Optional[float] = None
    per_symbol_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider:
            raise MarketDataValidationError("provider is required")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(
            self, "per_symbol_status", dict(self.per_symbol_status)
        )
        # Guardrail: catch obvious credential leaks in the redacted URL.
        for token in ("api_key=", "apikey=", "APIKEY=", "secret="):
            if token in self.request_url_redacted:
                raise MarketDataValidationError(
                    "request_url_redacted appears to contain unredacted "
                    f"credentials (found {token!r})"
                )

    def to_dict(self) -> Dict[str, Any]:
        batch_dict: Any
        if isinstance(self.batch, list) or isinstance(self.batch, tuple):
            batch_dict = [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in self.batch
            ]
        elif hasattr(self.batch, "to_dict"):
            batch_dict = self.batch.to_dict()
        else:
            batch_dict = self.batch
        return {
            "provider": self.provider,
            "batch": batch_dict,
            "request_url_redacted": self.request_url_redacted,
            "trace_id": self.trace_id,
            "warnings": list(self.warnings),
            "cost_estimate_usd": self.cost_estimate_usd,
            "per_symbol_status": dict(self.per_symbol_status),
        }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProvider(Protocol):
    """Read-only provider-agnostic interface for market-data acquisition.

    Every concrete plugin (Alpaca, Polygon, Databento, CSV, Parquet,
    …) implements this Protocol.  The warehouse and the research
    replay engine talk only to this shape — they never depend on a
    specific vendor's API surface.

    Implementations MUST:

    * Be pure with respect to their inputs — no hidden global
      state, no side effects beyond an outbound HTTP or file read.
    * Never place, submit, cancel, or replace orders.
    * Never import ``trader``, ``crypto_trader``, ``trader_cli``,
      ``telegram_approvals``, or ``strategy.runner``.
    * Never mutate the global :class:`~strategy.config.FeatureFlags`.
    * Never construct an ``ApprovalRecord``.
    * Never advance ``PromotionEntry`` state.
    * Redact credentials from any URL surfaced in
      :attr:`ProviderResponse.request_url_redacted`.

    Failure modes:

    * Structural per-symbol issues (empty series, symbol not found)
      → report on :attr:`ProviderResponse.per_symbol_status`.
    * Wholesale request failures (HTTP error, timeout, auth failure)
      → raise :class:`MarketDataRequestError`.

    Callers pass all timestamps as ISO 8601 strings.  ``start`` is
    inclusive, ``end`` is inclusive.  Providers that use different
    conventions upstream must normalize on the boundary.
    """

    name: str

    def provider_capabilities(self) -> ProviderCapabilities: ...

    def fetch_daily_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]: ...

    def fetch_intraday_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        interval: BarInterval,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]: ...

    def fetch_corporate_actions(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
    ) -> ProviderResponse[Sequence[CorporateAction]]: ...

    def fetch_symbol_metadata(
        self,
        symbols: Sequence[str],
    ) -> ProviderResponse[Sequence[SymbolMetadata]]: ...

    def fetch_calendar(
        self,
        start: str,
        end: str,
        exchange: str = "XNYS",
    ) -> ProviderResponse[Sequence[CalendarDay]]: ...


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "AdjustmentMode",
    "AssetClass",
    "Bar",
    "BarInterval",
    "CalendarDay",
    "CalendarSessionKind",
    "CorporateAction",
    "CorporateActionKind",
    "MarketDataProvider",
    "MarketDataRequestError",
    "MarketDataValidationError",
    "ProviderCapabilities",
    "ProviderResponse",
    "SymbolMetadata",
]
