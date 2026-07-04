"""Research cache + provider priority for the Phase 5.6 warehouse.

Twelfth Phase 5.6 implementation card ``t_phase56_research_cache``.
Ships the ``WarehouseReader`` that turns the read side of the
warehouse into the primary research surface: warehouse first,
provider plugins second, manual import third.

Priority resolution (fixed order):

1. Local warehouse — reads from the Parquet tree.
2. Provider plugins — passed in as an ordered tuple; each is
   consulted in turn until one returns data.
3. Manual / CSV / Parquet import — surfaces as another plugin
   at the tail of the priority list.

The reader carries no credentials — plugins own their own.  A
research run that unsets ``RESEARCH_ALPACA_*`` and disables
provider plugins can still complete an end-to-end validation
against warehouse coverage — that's the offline-byte-identical
guarantee this card enforces via unit test.

``ResearchAccountClient.fetch_bars`` is NOT called when the
warehouse has complete coverage.  The card's regression test
checks a call counter on a stub to prove it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from strategy.data_catalog import DataCatalog, DatasetManifest
from strategy.local_warehouse import (
    STATUS_VALIDATED,
    WarehouseIntegrityError,
    WarehouseLayout,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    MarketDataProvider,
    MarketDataRequestError,
    ProviderResponse,
)
from strategy.warehouse.duckdb_query import WarehouseQueryReader


SOURCE_WAREHOUSE = "warehouse"
SOURCE_PROVIDER = "provider"
SOURCE_MANUAL = "manual"

KNOWN_SOURCES: Tuple[str, ...] = (
    SOURCE_WAREHOUSE,
    SOURCE_PROVIDER,
    SOURCE_MANUAL,
)


class ResearchCacheError(WarehouseIntegrityError):
    """Raised when the reader cannot honor the request through
    any priority level.
    """


@dataclass(frozen=True)
class CacheHit:
    """One successful bar fetch, tagged with its provenance."""

    source: str
    provider_name: str
    dataset_id: str
    dataset_version: str
    bars: Tuple[Bar, ...]
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "provider_name": self.provider_name,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "bar_count": len(self.bars),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# WarehouseReader — the priority-ordered cache
# ---------------------------------------------------------------------------


class WarehouseReader:
    """Primary research read surface with provider fallback.

    Consults the warehouse for the requested
    (asset_class, interval, symbols, window) first.  When
    coverage is complete, no provider call is made.  When
    coverage is incomplete, falls through the provider list in
    order and returns the first non-empty response.

    ``pinned_versions`` maps ``dataset_id → version_label``.  When
    a pinned dataset id is present in the catalog under the
    requested version, the reader will use that version's bars
    even if a newer version exists.  Callers use pinning to
    freeze a research run's provenance.
    """

    def __init__(
        self,
        layout: WarehouseLayout,
        catalog: Optional[DataCatalog] = None,
        provider_priority: Sequence[MarketDataProvider] = (),
        pinned_versions: Optional[Dict[str, str]] = None,
    ) -> None:
        self._layout = layout
        self._catalog = catalog or DataCatalog.from_warehouse_layout(layout)
        self._providers = tuple(provider_priority)
        self._pinned_versions = dict(pinned_versions or {})
        self._reader = WarehouseQueryReader(layout=layout)

    def close(self) -> None:
        self._reader.close()

    def __enter__(self) -> "WarehouseReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def layout(self) -> WarehouseLayout:
        return self._layout

    @property
    def catalog(self) -> DataCatalog:
        return self._catalog

    @property
    def provider_priority(self) -> Tuple[MarketDataProvider, ...]:
        return self._providers

    # ------------------------------------------------------------------
    # Coverage helpers
    # ------------------------------------------------------------------

    def has_complete_coverage(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        symbols: Sequence[str],
        start: str,
        end: str,
    ) -> bool:
        """Return True iff the warehouse can satisfy the request
        entirely from local coverage.

        A dataset is considered to have coverage when its manifest
        window ``[start_date, end_date]`` covers ``[start, end]``
        AND every requested symbol appears in the manifest.
        """
        if not symbols:
            return False
        for symbol in symbols:
            hits = self._catalog.find_coverage(
                symbol, interval.value, start, end
            )
            covers = False
            for hit in hits:
                if hit.asset_class != asset_class.value:
                    continue
                # Manifest window must cover the request
                if hit.start_date and hit.start_date > start:
                    continue
                if hit.end_date and hit.end_date < end:
                    continue
                covers = True
                break
            if not covers:
                return False
        return True

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_bars(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> CacheHit:
        """Fetch bars via the priority chain.

        Order:

        1. Warehouse first — if a dataset covers the request,
           return its bars.
        2. Provider plugins — in the order supplied to the
           constructor.  Each provider gets one attempt; the
           first non-empty ``ProviderResponse`` wins.
        3. Failure — raise
           :class:`ResearchCacheError` if no source returns
           data.
        """
        # 1. Warehouse first
        wh_hit = self._try_warehouse(
            asset_class, interval, symbols, start, end
        )
        if wh_hit is not None:
            return wh_hit

        # 2. Providers in priority order
        warnings: List[str] = []
        for provider in self._providers:
            try:
                if interval is BarInterval.DAILY:
                    response = provider.fetch_daily_bars(
                        symbols, start, end, adjustment,
                    )
                else:
                    response = provider.fetch_intraday_bars(
                        symbols, start, end, interval, adjustment,
                    )
            except MarketDataRequestError as exc:
                warnings.append(f"{provider.name}: {exc}")
                continue
            warnings.extend(response.warnings)
            if response.batch:
                return CacheHit(
                    source=SOURCE_PROVIDER,
                    provider_name=provider.name,
                    dataset_id="",
                    dataset_version="",
                    bars=tuple(response.batch),
                    warnings=tuple(warnings),
                )
        raise ResearchCacheError(
            f"no source in priority chain returned bars for "
            f"{list(symbols)} in {start}..{end} "
            f"(warnings={warnings})"
        )

    def _try_warehouse(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        symbols: Sequence[str],
        start: str,
        end: str,
    ) -> Optional[CacheHit]:
        if not self.has_complete_coverage(
            asset_class, interval, symbols, start, end
        ):
            return None

        # Resolve a dataset id for each symbol.  Prefer pinned
        # versions when present; otherwise fall back to the
        # latest_validated for the symbol.
        picked: List[Tuple[str, DatasetManifest]] = []
        for symbol in symbols:
            manifest = self._resolve_manifest(symbol, interval, start, end)
            if manifest is None:
                # Coverage said yes but resolve came back None —
                # inconsistent state; bail to provider fallback.
                return None
            picked.append((symbol, manifest))

        # Aggregate bars — read all datasets and filter by window.
        # The stored timestamps may include time zone suffixes
        # (e.g. "2020-05-04T14:30:00+00:00"); a date-only start /
        # end would sort lexically wrong.  Broaden the internal
        # scan window to full-day bounds so the compare succeeds
        # regardless of the suffix.
        scan_start = start if "T" in start else f"{start}T00:00:00+00:00"
        scan_end = end if "T" in end else f"{end}T23:59:59+00:00"
        combined_bars: List[Bar] = []
        seen_dataset_ids: List[str] = []
        seen_versions: List[str] = []
        for symbol, manifest in picked:
            bars = self._reader.scan_bars(
                AssetClass(manifest.asset_class),
                BarInterval(manifest.interval),
                [symbol],
                scan_start,
                scan_end,
                dataset_id=manifest.dataset_id,
            )
            combined_bars.extend(bars)
            if manifest.dataset_id not in seen_dataset_ids:
                seen_dataset_ids.append(manifest.dataset_id)
                seen_versions.append(
                    manifest.corporate_action_version or "1"
                )

        combined_bars.sort(key=lambda b: (b.symbol, b.timestamp))
        return CacheHit(
            source=SOURCE_WAREHOUSE,
            provider_name="",
            dataset_id=",".join(seen_dataset_ids),
            dataset_version=",".join(seen_versions),
            bars=tuple(combined_bars),
        )

    def _resolve_manifest(
        self,
        symbol: str,
        interval: BarInterval,
        start: str,
        end: str,
    ) -> Optional[DatasetManifest]:
        # Pinned versions win.
        for pinned_id, pinned_version in self._pinned_versions.items():
            if not self._catalog.has(pinned_id):
                continue
            candidate = self._catalog.get(pinned_id)
            if candidate.covers_symbol(symbol) and (
                candidate.interval == interval.value
            ) and candidate.covers_window(start, end) and (
                candidate.corporate_action_version == pinned_version
                or not pinned_version
            ):
                return candidate

        # Prefer the latest validated dataset covering the request.
        latest = self._catalog.latest_validated(symbol, interval.value)
        if latest is not None and latest.covers_window(start, end):
            return latest

        # Any manifest that covers the request as a fallback.
        for candidate in self._catalog.find_coverage(
            symbol, interval.value, start, end
        ):
            return candidate
        return None


__all__ = [
    "CacheHit",
    "KNOWN_SOURCES",
    "ResearchCacheError",
    "SOURCE_MANUAL",
    "SOURCE_PROVIDER",
    "SOURCE_WAREHOUSE",
    "WarehouseReader",
]
