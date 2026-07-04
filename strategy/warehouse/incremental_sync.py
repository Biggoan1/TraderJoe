"""Nightly append-only sync runner for the Phase 5.6 warehouse.

Seventh Phase 5.6 implementation card ``t_phase56_incremental_sync``.
Consults each dataset's catalog manifest for the latest observed
bar timestamp, asks the highest-priority provider for bars
*strictly newer* than that timestamp, and appends the result to
the existing partition tree via
:func:`strategy.warehouse.parquet_io.write_bars` — never
overwriting existing bars.  Corporate-action-driven revisions
(bars that changed because Alpaca published a late split) do NOT
run through this pathway; they require ``warehouse-revise``,
which writes to a new dataset version.

Not in scope for this card:

* Corporate-action revision replay (a follow-up card will run
  ``warehouse-revise`` against ``t_phase56_data_versioning``).
* Dry-run report rendering into a dashboard —
  ``t_phase55_dashboard_plan``'s territory.
* Automated cron scheduling — operator concern.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from strategy.data_catalog import DatasetFile, DatasetManifest
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    WarehouseIntegrityError,
    WarehouseLayout,
    read_manifest,
    write_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    MarketDataProvider,
    MarketDataRequestError,
)
from strategy.warehouse.parquet_io import (
    WrittenFile,
    read_bars,
    scan_parquet_files,
    write_bars,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IncrementalSyncError(WarehouseIntegrityError):
    """Raised when sync cannot proceed (missing manifest,
    non-matching interval/asset_class, provider failure, or an
    attempt to sync a dataset with no coverage anchor).
    """


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCall:
    """One provider fetch made during a sync run."""

    provider: str
    symbols: Tuple[str, ...]
    start: str
    end: str
    bars_returned: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "symbols": list(self.symbols),
            "start": self.start,
            "end": self.end,
            "bars_returned": self.bars_returned,
        }


@dataclass(frozen=True)
class SyncReport:
    """Summary of one :func:`sync_dataset` invocation."""

    dataset_id: str
    provider_name: str
    latest_before: str
    latest_after: str
    query_start: str
    query_end: str
    bars_appended: int
    files_appended: Tuple[WrittenFile, ...]
    dry_run: bool
    warnings: Tuple[str, ...]
    calls: Tuple[ProviderCall, ...]
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "provider_name": self.provider_name,
            "latest_before": self.latest_before,
            "latest_after": self.latest_after,
            "query_start": self.query_start,
            "query_end": self.query_end,
            "bars_appended": self.bars_appended,
            "files_appended": [f.to_dict() for f in self.files_appended],
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
            "calls": [c.to_dict() for c in self.calls],
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Provider ledger
# ---------------------------------------------------------------------------


@dataclass
class ProviderLedger:
    """Per-provider cost + rate-limit ledger.

    Aggregates provider usage across a sync session so operators
    can see how much they burned on each provider before writing
    the manifest.  The ledger is a research artifact; it does NOT
    influence which provider gets called (that's the priority list
    on :class:`SyncPolicy`).
    """

    calls: Dict[str, int] = field(default_factory=dict)
    bars: Dict[str, int] = field(default_factory=dict)

    def record(self, provider: str, bars_returned: int) -> None:
        self.calls[provider] = self.calls.get(provider, 0) + 1
        self.bars[provider] = self.bars.get(provider, 0) + bars_returned

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "bars": dict(self.bars),
        }


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncPolicy:
    """Configuration for a sync run.

    ``providers`` is the priority-ordered list of provider
    plugins the sync will try until one returns non-empty results.
    Providers earlier in the list have higher priority — the
    warehouse always prefers the first successful non-empty
    response.

    ``dry_run`` disables all writes; the report describes what
    the sync WOULD do.  Useful for a nightly preview under review
    before running a mutation.
    """

    providers: Tuple[MarketDataProvider, ...]
    dry_run: bool = False
    end: str = ""  # empty means "now"

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", tuple(self.providers))
        if not self.providers:
            raise IncrementalSyncError(
                "SyncPolicy requires at least one provider"
            )


# ---------------------------------------------------------------------------
# sync_dataset
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _load_dataset_manifest(
    layout: WarehouseLayout, dataset_id: str
) -> DatasetManifest:
    try:
        payload = read_manifest(layout, dataset_id)
    except WarehouseIntegrityError as exc:
        raise IncrementalSyncError(str(exc)) from exc
    manifest = DatasetManifest.from_dict(payload)
    if not manifest.interval or not manifest.asset_class:
        raise IncrementalSyncError(
            f"dataset {dataset_id!r} missing warehouse fields "
            "(interval / asset_class); not a warehouse-managed dataset"
        )
    return manifest


def _latest_timestamp(
    layout: WarehouseLayout,
    dataset_id: str,
    asset_class: AssetClass,
    interval: BarInterval,
) -> Optional[str]:
    """Scan the on-disk Parquet files and return the max bar
    timestamp observed.  ``None`` when the dataset has no files.
    """
    paths = scan_parquet_files(
        layout, asset_class, interval, dataset_id=dataset_id
    )
    if not paths:
        return None
    latest = ""
    for rel in paths:
        for bar in read_bars(layout, [rel]):
            if bar.timestamp > latest:
                latest = bar.timestamp
    return latest or None


def _fetch_new_bars(
    provider: MarketDataProvider,
    symbols: Sequence[str],
    start: str,
    end: str,
    interval: BarInterval,
    adjustment: AdjustmentMode,
) -> Tuple[List[Bar], List[str]]:
    warnings: List[str] = []
    if interval is BarInterval.DAILY:
        response = provider.fetch_daily_bars(
            symbols, start, end, adjustment
        )
    else:
        response = provider.fetch_intraday_bars(
            symbols, start, end, interval, adjustment
        )
    warnings.extend(response.warnings)
    return list(response.batch), warnings


def sync_dataset(
    layout: WarehouseLayout,
    dataset_id: str,
    policy: SyncPolicy,
    ledger: Optional[ProviderLedger] = None,
) -> SyncReport:
    """Append bars newer than the dataset's current latest bar.

    Reads the dataset manifest, scans the on-disk Parquet files to
    find the latest timestamp, then asks the highest-priority
    provider from ``policy.providers`` for bars strictly after
    that timestamp.  Falls through the priority list until one
    provider returns a non-empty batch.

    Never redownloads existing history.  Never overwrites existing
    Parquet files: appended bars land in new partition files (the
    partition key is time-bucketed, so an append on a new day
    always creates a new file).

    The manifest is updated in-place with the new file entries and
    the new ``end_date``.  Validated manifests are NOT overwritten
    without ``force=True``; sync refuses to touch a validated
    dataset and returns a warning.
    """
    if ledger is None:
        ledger = ProviderLedger()

    manifest = _load_dataset_manifest(layout, dataset_id)
    interval = BarInterval(manifest.interval)
    asset_class = AssetClass(manifest.asset_class)
    adjustment = (
        AdjustmentMode(manifest.adjustment_mode)
        if manifest.adjustment_mode
        else AdjustmentMode.RAW
    )
    symbols = list(manifest.symbols)
    if not symbols:
        raise IncrementalSyncError(
            f"dataset {dataset_id!r} has no symbols to sync"
        )

    latest_before = _latest_timestamp(
        layout, dataset_id, asset_class, interval
    )
    if latest_before is None:
        latest_before = manifest.start_date

    query_start = latest_before or manifest.start_date
    query_end = policy.end or _utc_now_iso()
    warnings: List[str] = []
    calls: List[ProviderCall] = []

    if manifest.validation_status == STATUS_VALIDATED and not policy.dry_run:
        warnings.append(
            f"dataset {dataset_id!r} is validated; sync refuses to mutate. "
            "Use warehouse-revise for corporate-action revisions."
        )
        return SyncReport(
            dataset_id=dataset_id,
            provider_name="",
            latest_before=latest_before or "",
            latest_after=latest_before or "",
            query_start=query_start,
            query_end=query_end,
            bars_appended=0,
            files_appended=(),
            dry_run=policy.dry_run,
            warnings=tuple(warnings),
            calls=(),
            generated_at=_utc_now_iso(),
        )

    # Try each provider in priority order until one returns bars.
    chosen_provider: Optional[MarketDataProvider] = None
    chosen_bars: List[Bar] = []
    for provider in policy.providers:
        try:
            bars, w = _fetch_new_bars(
                provider, symbols, query_start, query_end,
                interval, adjustment,
            )
        except MarketDataRequestError as exc:
            warnings.append(f"{provider.name}: {exc}")
            ledger.record(provider.name, 0)
            calls.append(
                ProviderCall(
                    provider=provider.name,
                    symbols=tuple(symbols),
                    start=query_start,
                    end=query_end,
                    bars_returned=0,
                )
            )
            continue
        warnings.extend(w)
        # Strictly newer than latest_before
        new_bars = [
            b for b in bars if (not latest_before) or b.timestamp > latest_before
        ]
        ledger.record(provider.name, len(new_bars))
        calls.append(
            ProviderCall(
                provider=provider.name,
                symbols=tuple(symbols),
                start=query_start,
                end=query_end,
                bars_returned=len(new_bars),
            )
        )
        if new_bars:
            chosen_provider = provider
            chosen_bars = new_bars
            break

    if not chosen_bars or chosen_provider is None:
        return SyncReport(
            dataset_id=dataset_id,
            provider_name="",
            latest_before=latest_before or "",
            latest_after=latest_before or "",
            query_start=query_start,
            query_end=query_end,
            bars_appended=0,
            files_appended=(),
            dry_run=policy.dry_run,
            warnings=tuple(warnings),
            calls=tuple(calls),
            generated_at=_utc_now_iso(),
        )

    files_appended: Tuple[WrittenFile, ...] = ()
    latest_after = latest_before or ""
    if not policy.dry_run:
        written = write_bars(
            layout, chosen_bars, asset_class, dataset_id=dataset_id
        )
        files_appended = tuple(written)
        for bar in chosen_bars:
            if bar.timestamp > latest_after:
                latest_after = bar.timestamp
        _append_to_manifest(
            layout, manifest, chosen_provider.name, chosen_bars, files_appended
        )
    else:
        # Dry run: still compute what would land.
        for bar in chosen_bars:
            if bar.timestamp > latest_after:
                latest_after = bar.timestamp

    return SyncReport(
        dataset_id=dataset_id,
        provider_name=chosen_provider.name,
        latest_before=latest_before or "",
        latest_after=latest_after,
        query_start=query_start,
        query_end=query_end,
        bars_appended=len(chosen_bars),
        files_appended=files_appended,
        dry_run=policy.dry_run,
        warnings=tuple(warnings),
        calls=tuple(calls),
        generated_at=_utc_now_iso(),
    )


def _append_to_manifest(
    layout: WarehouseLayout,
    manifest: DatasetManifest,
    provider_name: str,
    bars: Sequence[Bar],
    files: Sequence[WrittenFile],
) -> None:
    """Update ``manifest`` in place with the newly-appended files
    and the new ``end_date``.  Merges by path so re-appending the
    same partition file replaces the existing entry rather than
    duplicating it.
    """
    existing_files = {f.path: f for f in manifest.files}
    for w in files:
        existing_files[w.relative_path] = DatasetFile(
            path=w.relative_path,
            sha256=w.sha256,
            size_bytes=w.size_bytes,
            row_count=w.row_count,
        )
    new_end = manifest.end_date
    for bar in bars:
        if bar.timestamp > new_end:
            new_end = bar.timestamp
    updated = DatasetManifest.from_dict(
        {
            **manifest.to_dict(),
            "end_date": new_end,
            "imported_at": _utc_now_iso(),
            "files": [f.to_dict() for f in existing_files.values()],
            "validation_status": STATUS_UNVALIDATED,
        }
    )
    write_manifest(
        layout, manifest.dataset_id, updated.to_dict(), force=False
    )


def sync_all(
    layout: WarehouseLayout,
    dataset_ids: Sequence[str],
    policy: SyncPolicy,
    ledger: Optional[ProviderLedger] = None,
) -> List[SyncReport]:
    """Convenience wrapper: run :func:`sync_dataset` across a list
    of dataset ids and return the per-dataset reports.  A failure
    in one dataset does not stop the run — every dataset gets its
    own attempt and its own report entry with warnings.
    """
    reports: List[SyncReport] = []
    for dataset_id in dataset_ids:
        try:
            reports.append(sync_dataset(layout, dataset_id, policy, ledger))
        except IncrementalSyncError as exc:
            reports.append(
                SyncReport(
                    dataset_id=dataset_id,
                    provider_name="",
                    latest_before="",
                    latest_after="",
                    query_start="",
                    query_end="",
                    bars_appended=0,
                    files_appended=(),
                    dry_run=policy.dry_run,
                    warnings=(str(exc),),
                    calls=(),
                    generated_at=_utc_now_iso(),
                )
            )
    return reports


__all__ = [
    "IncrementalSyncError",
    "ProviderCall",
    "ProviderLedger",
    "SyncPolicy",
    "SyncReport",
    "sync_all",
    "sync_dataset",
]
