"""Parquet importer plugin.

Reads bars from Parquet files that already match the canonical
warehouse schema
(:data:`strategy.warehouse.parquet_io.CANONICAL_SCHEMA`).  Useful
for onboarding vendor-supplied Parquet bundles (Databento
exports, prior warehouse snapshots) without going through the
provider fetch path.

The plugin delegates the actual read to
:func:`strategy.warehouse.parquet_io.read_bars` so the schema
contract is enforced in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    CalendarDay,
    CorporateAction,
    MarketDataRequestError,
    MarketDataValidationError,
    ProviderCapabilities,
    ProviderResponse,
    SymbolMetadata,
)
from strategy.providers import register


PROVIDER_NAME = "parquet"


class ParquetProvider:
    """Read-only Parquet importer.

    Constructed with a :class:`~strategy.local_warehouse.WarehouseLayout`
    and one or more relative Parquet paths.  Reads through
    :func:`strategy.warehouse.parquet_io.read_bars` so the
    canonical schema is enforced.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        layout: WarehouseLayout,
        relative_paths: Sequence[str],
    ) -> None:
        self._layout = layout
        self._relative_paths = list(relative_paths)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supported_intervals=tuple(BarInterval),
            supported_adjustments=tuple(AdjustmentMode),
            supported_asset_classes=(
                AssetClass.EQUITY,
                AssetClass.ETF,
                AssetClass.CRYPTO,
                AssetClass.OPTION,
                AssetClass.FUTURE,
            ),
            supports_corporate_actions=False,
            supports_symbol_metadata=False,
            supports_calendar=False,
            notes=(
                "Reads Parquet files with the canonical warehouse schema"
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
        return self._read(symbols, start, end, BarInterval.DAILY)

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
                "fetch_intraday_bars called with DAILY interval"
            )
        return self._read(symbols, start, end, interval)

    def _read(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        interval: BarInterval,
    ) -> ProviderResponse[Sequence[Bar]]:
        # Lazy import so the plugin stays importable when pyarrow
        # is absent.  Import errors surface as MarketDataRequestError.
        try:
            from strategy.warehouse.parquet_io import read_bars
        except ImportError as exc:
            raise MarketDataRequestError(
                f"parquet_io unavailable: {exc}"
            ) from exc
        bars = read_bars(self._layout, self._relative_paths)
        wanted = set(symbols) if symbols else None
        filtered: List[Bar] = []
        per_symbol_status = (
            {sym: "empty" for sym in symbols} if symbols else {}
        )
        for bar in bars:
            if bar.interval is not interval:
                continue
            if wanted is not None and bar.symbol not in wanted:
                continue
            if start and bar.timestamp < start:
                continue
            if end and bar.timestamp > end:
                continue
            per_symbol_status[bar.symbol] = "ok"
            filtered.append(bar)
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=filtered,
            request_url_redacted=(
                f"file://{self._layout.root.resolve()}"
            ),
            per_symbol_status=per_symbol_status,
        )

    # ------------------------------------------------------------------
    # Empty surfaces
    # ------------------------------------------------------------------

    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: str, end: str
    ) -> ProviderResponse[Sequence[CorporateAction]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=("ParquetProvider does not expose corporate actions",),
        )

    def fetch_symbol_metadata(
        self, symbols: Sequence[str]
    ) -> ProviderResponse[Sequence[SymbolMetadata]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=("ParquetProvider does not expose symbol metadata",),
        )

    def fetch_calendar(
        self, start: str, end: str, exchange: str = "XNYS"
    ) -> ProviderResponse[Sequence[CalendarDay]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=("ParquetProvider does not expose exchange calendars",),
        )


def factory(
    layout: WarehouseLayout, relative_paths: Sequence[str]
) -> ParquetProvider:
    return ParquetProvider(layout=layout, relative_paths=relative_paths)


register(PROVIDER_NAME, factory)
