"""Alpaca market-data provider plugin.

Wraps the existing
:class:`strategy.research_account.ResearchAccountClient` so it
implements
:class:`strategy.market_data_provider.MarketDataProvider`.  The
client's public surface is unchanged — Phase 5's existing tests
continue to pass byte-identically.  No new credentials are read,
no new HTTP endpoints are added, and no order path is introduced.

The plugin translates the interface's normalized data models
(``Bar``, ``CalendarDay``, ``ProviderResponse``) to and from the
Alpaca ``/v2/stocks/bars`` / ``/v2/calendar`` payload shapes.
Endpoints Alpaca does not expose (corporate actions, symbol
metadata beyond what bars already carry) return an empty
``ProviderResponse`` with an explanatory warning — a callable
protocol satisfies the interface without pretending to have data
we don't have.

Design doc:
``docs/architecture/phase-5-6-historical-warehouse.md``
(§Architecture.1 — provider roster).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    CalendarDay,
    CalendarSessionKind,
    CorporateAction,
    MarketDataRequestError,
    MarketDataValidationError,
    ProviderCapabilities,
    ProviderResponse,
    SymbolMetadata,
)
from strategy.providers import register
from strategy.research_account import (
    ResearchAccountClient,
    ResearchAccountRequestError,
)


PROVIDER_NAME = "alpaca"

# Alpaca-specific vocab that maps onto the interface enums.
_ALPACA_TO_ADJUSTMENT: Dict[str, str] = {
    "raw": "raw",
    "split": "split",
    "dividend": "split_dividend",  # Alpaca uses "dividend" for split+div
    "all": "split_dividend",
}
_ADJUSTMENT_TO_ALPACA: Dict[AdjustmentMode, str] = {
    AdjustmentMode.RAW: "raw",
    AdjustmentMode.SPLIT: "split",
    AdjustmentMode.SPLIT_DIVIDEND: "all",
}


def _interval_to_alpaca(interval: BarInterval) -> str:
    # BarInterval values are already Alpaca-shaped.
    return interval.value


class AlpacaProvider:
    """Read-only Alpaca provider plugin.

    Callers construct with an existing
    :class:`ResearchAccountClient` so credential handling stays in
    one place.  The plugin never reads env vars itself.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        client: ResearchAccountClient,
        *,
        feed: str = "sip",
    ) -> None:
        if not feed:
            raise ValueError("feed must be non-empty (e.g. 'sip', 'iex')")
        self._client = client
        self._feed = feed

    @property
    def feed(self) -> str:
        return self._feed

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supported_intervals=(
                BarInterval.DAILY,
                BarInterval.HOURLY,
                BarInterval.MINUTE_1,
                BarInterval.MINUTE_5,
                BarInterval.MINUTE_15,
                BarInterval.MINUTE_30,
            ),
            supported_adjustments=(
                AdjustmentMode.RAW,
                AdjustmentMode.SPLIT,
                AdjustmentMode.SPLIT_DIVIDEND,
            ),
            supported_asset_classes=(
                AssetClass.EQUITY,
                AssetClass.ETF,
            ),
            supports_corporate_actions=False,
            supports_symbol_metadata=False,
            supports_calendar=True,
            supported_exchanges=("XNYS", "XNAS", "ARCX"),
            latest_data_lag_minutes=15,
            rate_limit_per_minute=200,
            cost_per_request_usd=0.0,
            notes=(
                "Wraps ResearchAccountClient. Corporate actions and "
                "symbol metadata not exposed at this layer."
            ),
        )

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------

    def fetch_daily_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        return self._fetch_bars_at(
            symbols, start, end, BarInterval.DAILY, adjustment
        )

    def fetch_intraday_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        interval: BarInterval,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        if interval is BarInterval.DAILY:
            raise MarketDataValidationError(
                "fetch_intraday_bars called with DAILY interval; "
                "use fetch_daily_bars"
            )
        if interval is BarInterval.SECOND_1:
            raise MarketDataValidationError(
                "AlpacaProvider does not expose second-level bars"
            )
        return self._fetch_bars_at(
            symbols, start, end, interval, adjustment
        )

    def _fetch_bars_at(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        interval: BarInterval,
        adjustment: AdjustmentMode,
    ) -> ProviderResponse[Sequence[Bar]]:
        if not symbols:
            raise MarketDataValidationError("symbols is required")
        alpaca_adjustment = _ADJUSTMENT_TO_ALPACA[adjustment]
        try:
            payload = self._client.fetch_bars(
                symbols=list(symbols),
                timeframe=_interval_to_alpaca(interval),
                start=start,
                end=end,
                adjustment=alpaca_adjustment,
                feed=self._feed,
            )
        except ResearchAccountRequestError as exc:
            raise MarketDataRequestError(str(exc)) from exc

        per_symbol_status: Dict[str, str] = {sym: "empty" for sym in symbols}
        bars_out: List[Bar] = []
        bars_by_symbol = payload.get("bars") or {}
        if not isinstance(bars_by_symbol, dict):
            raise MarketDataRequestError(
                f"unexpected /v2/stocks/bars shape: bars={type(bars_by_symbol).__name__}"
            )
        for symbol, rows in bars_by_symbol.items():
            if not isinstance(rows, list) or not rows:
                continue
            per_symbol_status[symbol] = "ok"
            for row in rows:
                bar = self._row_to_bar(symbol, row, interval, adjustment)
                if bar is not None:
                    bars_out.append(bar)
        bars_out.sort(key=lambda b: (b.symbol, b.timestamp))
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=bars_out,
            request_url_redacted="",
            per_symbol_status=per_symbol_status,
        )

    @staticmethod
    def _row_to_bar(
        symbol: str,
        row: Mapping[str, Any],
        interval: BarInterval,
        adjustment: AdjustmentMode,
    ) -> Optional[Bar]:
        if not isinstance(row, Mapping):
            return None
        try:
            return Bar(
                symbol=symbol,
                timestamp=str(row["t"]),
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row.get("v", 0.0)),
                interval=interval,
                adjustment_mode=adjustment,
                vwap=(
                    float(row["vw"])
                    if "vw" in row and row["vw"] is not None
                    else None
                ),
                trade_count=(
                    int(row["n"])
                    if "n" in row and row["n"] is not None
                    else None
                ),
                adjustment_version="",
            )
        except (KeyError, ValueError, MarketDataValidationError):
            return None

    # ------------------------------------------------------------------
    # Corporate actions / symbol metadata (not exposed by this plugin)
    # ------------------------------------------------------------------

    def fetch_corporate_actions(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
    ) -> ProviderResponse[Sequence[CorporateAction]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=(
                "AlpacaProvider does not expose corporate actions; "
                "route to a dedicated corporate-actions provider",
            ),
        )

    def fetch_symbol_metadata(
        self,
        symbols: Sequence[str],
    ) -> ProviderResponse[Sequence[SymbolMetadata]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=(
                "AlpacaProvider does not expose symbol metadata at "
                "this layer",
            ),
        )

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------

    def fetch_calendar(
        self,
        start: str,
        end: str,
        exchange: str = "XNYS",
    ) -> ProviderResponse[Sequence[CalendarDay]]:
        try:
            payload = self._client.list_calendar(start=start, end=end)
        except ResearchAccountRequestError as exc:
            raise MarketDataRequestError(str(exc)) from exc
        days: List[CalendarDay] = []
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            date = row.get("date")
            if not date:
                continue
            open_time = row.get("open")
            close_time = row.get("close")
            kind = CalendarSessionKind.FULL_TRADING
            if not open_time or not close_time:
                kind = CalendarSessionKind.HOLIDAY
                open_time = None
                close_time = None
            try:
                days.append(
                    CalendarDay(
                        exchange=exchange,
                        date=str(date),
                        kind=kind,
                        open_time=open_time,
                        close_time=close_time,
                        timezone="America/New_York",
                    )
                )
            except MarketDataValidationError:
                continue
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=days,
        )


def factory(
    client: ResearchAccountClient, *, feed: str = "sip"
) -> AlpacaProvider:
    return AlpacaProvider(client=client, feed=feed)


register(PROVIDER_NAME, factory)
