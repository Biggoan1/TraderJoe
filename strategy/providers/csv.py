"""CSV importer plugin.

Reads bars from a local CSV file with the canonical columns:

    symbol, timestamp, open, high, low, close, volume,
    [vwap], [trade_count]

Providers that need to onboard legacy CSV bundles (research
exports, vendor archives) drive this plugin.  The plugin never
fetches over the network — it satisfies the
``MarketDataProvider`` Protocol so the warehouse can treat CSV
imports the same way it treats live fetches.

Corporate actions, symbol metadata, and calendars are unsupported
at this layer — they return empty
:class:`~strategy.market_data_provider.ProviderResponse` with a
diagnostic warning.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

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


PROVIDER_NAME = "csv"

REQUIRED_COLUMNS = (
    "symbol",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


class CsvProvider:
    """CSV file importer.

    Callers construct with a ``Path`` (or dict of
    ``symbol -> Path``) and the interval / adjustment_mode the
    file was written under.  The plugin does not infer either.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        source: Path,
        interval: BarInterval,
        adjustment_mode: AdjustmentMode = AdjustmentMode.RAW,
        adjustment_version: str = "",
    ) -> None:
        self._source = Path(source)
        self._interval = interval
        self._adjustment_mode = adjustment_mode
        self._adjustment_version = adjustment_version

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supported_intervals=(self._interval,),
            supported_adjustments=(self._adjustment_mode,),
            supported_asset_classes=(
                AssetClass.EQUITY,
                AssetClass.ETF,
                AssetClass.CRYPTO,
            ),
            supports_corporate_actions=False,
            supports_symbol_metadata=False,
            supports_calendar=False,
            notes="Reads local CSV files with the canonical column shape",
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
        if self._interval is not BarInterval.DAILY:
            raise MarketDataValidationError(
                f"CsvProvider was configured for {self._interval.name}, "
                "not DAILY"
            )
        return self._read_and_filter(symbols, start, end)

    def fetch_intraday_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        interval: BarInterval,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        if interval is not self._interval:
            raise MarketDataValidationError(
                f"CsvProvider was configured for {self._interval.name}, "
                f"not {interval.name}"
            )
        return self._read_and_filter(symbols, start, end)

    def _read_and_filter(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
    ) -> ProviderResponse[Sequence[Bar]]:
        if not self._source.is_file():
            raise MarketDataRequestError(
                f"CSV source not found: {self._source}"
            )
        wanted = set(symbols) if symbols else None
        per_symbol_status: Dict[str, str] = (
            {sym: "empty" for sym in symbols} if symbols else {}
        )
        bars_out: List[Bar] = []
        warnings: List[str] = []
        with self._source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise MarketDataRequestError(
                    f"CSV {self._source} missing required columns: {missing}"
                )
            for row in reader:
                symbol = row.get("symbol", "")
                if wanted is not None and symbol not in wanted:
                    continue
                if start and row.get("timestamp", "") < start:
                    continue
                if end and row.get("timestamp", "") > end:
                    continue
                bar = self._row_to_bar(row)
                if bar is None:
                    warnings.append(f"skipped invalid row for {symbol}")
                    continue
                per_symbol_status[symbol] = "ok"
                bars_out.append(bar)
        bars_out.sort(key=lambda b: (b.symbol, b.timestamp))
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=bars_out,
            request_url_redacted=f"file://{self._source.resolve()}",
            per_symbol_status=per_symbol_status,
            warnings=tuple(warnings),
        )

    def _row_to_bar(self, row: Mapping[str, Any]) -> Optional[Bar]:
        try:
            vwap = row.get("vwap")
            trade_count = row.get("trade_count")
            return Bar(
                symbol=row["symbol"],
                timestamp=row["timestamp"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                interval=self._interval,
                adjustment_mode=self._adjustment_mode,
                vwap=(
                    None if not vwap else float(vwap)
                ),
                trade_count=(
                    None if not trade_count else int(trade_count)
                ),
                adjustment_version=self._adjustment_version,
            )
        except (KeyError, ValueError, MarketDataValidationError):
            return None

    # ------------------------------------------------------------------
    # Empty surfaces for the interfaces this plugin doesn't cover
    # ------------------------------------------------------------------

    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: str, end: str
    ) -> ProviderResponse[Sequence[CorporateAction]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=("CsvProvider does not expose corporate actions",),
        )

    def fetch_symbol_metadata(
        self, symbols: Sequence[str]
    ) -> ProviderResponse[Sequence[SymbolMetadata]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=("CsvProvider does not expose symbol metadata",),
        )

    def fetch_calendar(
        self, start: str, end: str, exchange: str = "XNYS"
    ) -> ProviderResponse[Sequence[CalendarDay]]:
        return ProviderResponse(
            provider=PROVIDER_NAME,
            batch=[],
            warnings=("CsvProvider does not expose exchange calendars",),
        )


def factory(
    source: Path,
    interval: BarInterval,
    adjustment_mode: AdjustmentMode = AdjustmentMode.RAW,
    adjustment_version: str = "",
) -> CsvProvider:
    return CsvProvider(
        source=source,
        interval=interval,
        adjustment_mode=adjustment_mode,
        adjustment_version=adjustment_version,
    )


register(PROVIDER_NAME, factory)
