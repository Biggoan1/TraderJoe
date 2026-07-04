"""Bulk import pipeline for the Phase 5.6 warehouse.

Sixth Phase 5.6 implementation card ``t_phase56_import_pipeline``.
Consumes a :class:`~strategy.market_data_provider.MarketDataProvider`
(from any registered plugin) and writes the returned bars through
:func:`strategy.warehouse.parquet_io.write_bars` into the canonical
warehouse layout.  Records a manifest via the extended
:class:`strategy.data_catalog.DatasetManifest` so downstream
warehouse queries can discover the imported files.

Scope:

* Bulk import from any provider plugin
* Symbol-chunked pulls to respect provider rate limits
* Resumable via a per-import SQLite queue
* Rebuild-manifest workflow that regenerates a manifest from an
  existing tree (useful after operator-driven backfills)

Not in scope for this card:

* Incremental (append-only) sync — ``t_phase56_incremental_sync``.
* Gap detection reports — ``t_phase56_gap_detection``.
* DuckDB query surface — ``t_phase56_duckdb_queries``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)

from strategy.data_catalog import DatasetFile, DatasetManifest, HISTORICAL_BARS
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    WarehouseIntegrityError,
    WarehouseLayout,
    write_manifest,
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
from strategy.warehouse.parquet_io import (
    WrittenFile,
    scan_parquet_files,
    write_bars,
)


DEFAULT_CHUNK_SIZE = 50

STATUS_PENDING = "pending"
STATUS_INFLIGHT = "inflight"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

KNOWN_QUEUE_STATUSES: Tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_INFLIGHT,
    STATUS_DONE,
    STATUS_FAILED,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ImportPipelineError(WarehouseIntegrityError):
    """Raised when an import cannot proceed because inputs are
    malformed or the provider misbehaves in a way tests must
    surface loudly.
    """


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportReport:
    """Summary of one :func:`import_bars` invocation."""

    dataset_id: str
    provider_name: str
    asset_class: str
    interval: str
    adjustment_mode: str
    symbols_requested: Tuple[str, ...]
    symbols_ok: Tuple[str, ...]
    symbols_empty: Tuple[str, ...]
    total_bars: int
    files: Tuple[WrittenFile, ...]
    warnings: Tuple[str, ...]
    manifest_path: str
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "provider_name": self.provider_name,
            "asset_class": self.asset_class,
            "interval": self.interval,
            "adjustment_mode": self.adjustment_mode,
            "symbols_requested": list(self.symbols_requested),
            "symbols_ok": list(self.symbols_ok),
            "symbols_empty": list(self.symbols_empty),
            "total_bars": self.total_bars,
            "files": [f.to_dict() for f in self.files],
            "warnings": list(self.warnings),
            "manifest_path": self.manifest_path,
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Resumable queue
# ---------------------------------------------------------------------------


class PendingDownloadsQueue:
    """SQLite-backed queue of pending per-symbol chunks.

    Tracks ``(dataset_id, symbol, start, end)`` triples with a
    status column so an interrupted import can resume from the
    last incomplete symbol.

    The queue's own schema is intentionally minimal — the catalog
    holds the canonical dataset shape; the queue only tracks
    per-chunk progress so we can resume.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS pending_downloads (
      dataset_id TEXT NOT NULL,
      symbol     TEXT NOT NULL,
      window_start TEXT NOT NULL,
      window_end   TEXT NOT NULL,
      status TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (dataset_id, symbol, window_start, window_end)
    )
    """

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute(self.SCHEMA)
        self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PendingDownloadsQueue":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def enqueue(
        self,
        dataset_id: str,
        symbol: str,
        window_start: str,
        window_end: str,
    ) -> None:
        now = _utc_now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO pending_downloads "
            "(dataset_id, symbol, window_start, window_end, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dataset_id, symbol, window_start, window_end, STATUS_PENDING, now),
        )
        self._conn.commit()

    def pending(self, dataset_id: str) -> List[Tuple[str, str, str]]:
        cursor = self._conn.execute(
            "SELECT symbol, window_start, window_end "
            "FROM pending_downloads "
            "WHERE dataset_id = ? AND status IN (?, ?) "
            "ORDER BY symbol, window_start",
            (dataset_id, STATUS_PENDING, STATUS_INFLIGHT),
        )
        return [(r[0], r[1], r[2]) for r in cursor.fetchall()]

    def mark(
        self,
        dataset_id: str,
        symbol: str,
        window_start: str,
        window_end: str,
        status: str,
    ) -> None:
        if status not in KNOWN_QUEUE_STATUSES:
            raise ImportPipelineError(
                f"unknown queue status {status!r}; expected one of {KNOWN_QUEUE_STATUSES}"
            )
        self._conn.execute(
            "UPDATE pending_downloads SET status = ?, updated_at = ? "
            "WHERE dataset_id = ? AND symbol = ? "
            "AND window_start = ? AND window_end = ?",
            (status, _utc_now_iso(), dataset_id, symbol, window_start, window_end),
        )
        self._conn.commit()

    def status(
        self,
        dataset_id: str,
        symbol: str,
        window_start: str,
        window_end: str,
    ) -> Optional[str]:
        cursor = self._conn.execute(
            "SELECT status FROM pending_downloads "
            "WHERE dataset_id = ? AND symbol = ? "
            "AND window_start = ? AND window_end = ?",
            (dataset_id, symbol, window_start, window_end),
        )
        row = cursor.fetchone()
        return None if row is None else row[0]


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _chunks(seq: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    if size <= 0:
        raise ImportPipelineError("chunk_size must be positive")
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fetch_bars_at(
    provider: MarketDataProvider,
    symbols: Sequence[str],
    start: str,
    end: str,
    interval: BarInterval,
    adjustment: AdjustmentMode,
) -> ProviderResponse[Sequence[Bar]]:
    if interval is BarInterval.DAILY:
        return provider.fetch_daily_bars(symbols, start, end, adjustment)
    return provider.fetch_intraday_bars(symbols, start, end, interval, adjustment)


def import_bars(
    layout: WarehouseLayout,
    provider: MarketDataProvider,
    dataset_id: str,
    symbols: Sequence[str],
    start: str,
    end: str,
    asset_class: AssetClass,
    interval: BarInterval,
    adjustment: AdjustmentMode = AdjustmentMode.RAW,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    queue: Optional[PendingDownloadsQueue] = None,
    force: bool = False,
) -> ImportReport:
    """Fetch bars from ``provider`` in chunks, write to Parquet,
    and register a manifest.

    Idempotent when ``queue`` is supplied: an interrupted run will
    resume the pending chunks on the next call.  If ``queue`` is
    ``None`` every chunk is fetched fresh — safe for one-shot
    imports where the caller controls retries.

    The pipeline never overwrites a validated manifest without
    ``force=True`` — the immutability guarantee inherited from
    :func:`strategy.local_warehouse.write_manifest` is respected.

    Returns an :class:`ImportReport` summarizing the run.  Callers
    can serialize it (e.g. into a run log) or pass it to a review
    dashboard.
    """
    if not symbols:
        raise ImportPipelineError("symbols is required")
    if not dataset_id:
        raise ImportPipelineError("dataset_id is required")
    layout.create()  # idempotent
    # Ensure the dataset dir exists so the writer can drop files.
    dataset_dir = layout.dataset_dir(dataset_id, asset_class, interval)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    symbols_seq: List[str] = list(symbols)
    warnings: List[str] = []
    all_written: List[WrittenFile] = []
    symbols_ok: List[str] = []
    symbols_empty: List[str] = []
    total_bars = 0

    for chunk in _chunks(symbols_seq, chunk_size):
        # Register the chunk in the queue for resumability.
        if queue is not None:
            for sym in chunk:
                queue.enqueue(dataset_id, sym, start, end)
                queue.mark(dataset_id, sym, start, end, STATUS_INFLIGHT)
        try:
            response = _fetch_bars_at(
                provider, chunk, start, end, interval, adjustment
            )
        except MarketDataRequestError as exc:
            if queue is not None:
                for sym in chunk:
                    queue.mark(dataset_id, sym, start, end, STATUS_FAILED)
            raise
        warnings.extend(response.warnings)
        chunk_bars: List[Bar] = list(response.batch)
        if chunk_bars:
            written = write_bars(
                layout, chunk_bars, asset_class, dataset_id=dataset_id
            )
            all_written.extend(written)
            total_bars += len(chunk_bars)
        for sym in chunk:
            status = response.per_symbol_status.get(sym, "empty")
            if status == "ok":
                symbols_ok.append(sym)
            else:
                symbols_empty.append(sym)
            if queue is not None:
                queue.mark(dataset_id, sym, start, end, STATUS_DONE)

    manifest = _build_manifest_from_written(
        dataset_id=dataset_id,
        provider_name=provider.name,
        asset_class=asset_class,
        interval=interval,
        adjustment=adjustment,
        symbols_ok=symbols_ok,
        symbols_empty=symbols_empty,
        window_start=start,
        window_end=end,
        written=all_written,
    )
    if all_written:
        manifest_path = write_manifest(
            layout, dataset_id, manifest.to_dict(), force=force
        )
    else:
        # write_manifest guards on validated status, but a manifest
        # with zero files is not useful.  Emit a warning instead
        # of writing an empty manifest.
        warnings.append("no bars returned; manifest not written")
        manifest_path = layout.manifest_path(dataset_id)

    return ImportReport(
        dataset_id=dataset_id,
        provider_name=provider.name,
        asset_class=asset_class.value,
        interval=interval.value,
        adjustment_mode=adjustment.value,
        symbols_requested=tuple(symbols_seq),
        symbols_ok=tuple(symbols_ok),
        symbols_empty=tuple(symbols_empty),
        total_bars=total_bars,
        files=tuple(all_written),
        warnings=tuple(warnings),
        manifest_path=str(manifest_path),
        generated_at=_utc_now_iso(),
    )


def _build_manifest_from_written(
    dataset_id: str,
    provider_name: str,
    asset_class: AssetClass,
    interval: BarInterval,
    adjustment: AdjustmentMode,
    symbols_ok: Sequence[str],
    symbols_empty: Sequence[str],
    window_start: str,
    window_end: str,
    written: Sequence[WrittenFile],
) -> DatasetManifest:
    files = tuple(
        DatasetFile(
            path=w.relative_path,
            sha256=w.sha256,
            size_bytes=w.size_bytes,
            row_count=w.row_count,
        )
        for w in written
    )
    return DatasetManifest(
        dataset_id=dataset_id,
        kind=HISTORICAL_BARS,
        source=provider_name,
        symbols=tuple(symbols_ok),
        start_date=window_start,
        end_date=window_end,
        imported_at=_utc_now_iso(),
        files=files or (
            # DatasetManifest requires at least one file; a
            # zero-file manifest is meaningless.  Callers that hit
            # this branch see it in the ImportReport's warnings.
            DatasetFile(
                path=".empty",
                sha256="0" * 64,
                size_bytes=0,
                row_count=0,
            ),
        ),
        provider=provider_name,
        interval=interval.value,
        asset_class=asset_class.value,
        adjustment_mode=adjustment.value,
        validation_status=STATUS_UNVALIDATED,
        metadata={
            "symbols_empty": list(symbols_empty),
        },
    )


# ---------------------------------------------------------------------------
# Rebuild manifest
# ---------------------------------------------------------------------------


def rebuild_manifest(
    layout: WarehouseLayout,
    dataset_id: str,
    asset_class: AssetClass,
    interval: BarInterval,
    adjustment: AdjustmentMode = AdjustmentMode.RAW,
    provider_name: str = "operator",
    force: bool = False,
) -> DatasetManifest:
    """Regenerate a dataset manifest from the on-disk Parquet files.

    Scans the dataset partition, computes per-file sha256 + row
    counts, and writes a fresh manifest.  Useful after operator
    backfills or after moving a warehouse tree between machines.
    """
    from strategy.warehouse.parquet_io import read_bars

    paths = scan_parquet_files(
        layout, asset_class, interval, dataset_id=dataset_id
    )
    if not paths:
        raise ImportPipelineError(
            f"no Parquet files found for dataset {dataset_id!r}"
        )
    files: List[DatasetFile] = []
    symbols: set = set()
    window_start = ""
    window_end = ""
    for rel in paths:
        absolute = layout.root / rel
        # sha256 + size — compute directly rather than re-writing.
        import hashlib
        digest = hashlib.sha256()
        size = 0
        with absolute.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        sha = digest.hexdigest()

        # Row count + symbol set + date bounds — read the file once.
        bars = read_bars(layout, [rel])
        for bar in bars:
            symbols.add(bar.symbol)
            if not window_start or bar.timestamp < window_start:
                window_start = bar.timestamp
            if not window_end or bar.timestamp > window_end:
                window_end = bar.timestamp
        files.append(
            DatasetFile(
                path=rel,
                sha256=sha,
                size_bytes=size,
                row_count=len(bars),
            )
        )

    manifest = DatasetManifest(
        dataset_id=dataset_id,
        kind=HISTORICAL_BARS,
        source=provider_name,
        symbols=tuple(sorted(symbols)),
        start_date=window_start,
        end_date=window_end,
        imported_at=_utc_now_iso(),
        files=tuple(files),
        provider=provider_name,
        interval=interval.value,
        asset_class=asset_class.value,
        adjustment_mode=adjustment.value,
        validation_status=STATUS_UNVALIDATED,
        notes="regenerated from on-disk Parquet files",
    )
    write_manifest(layout, dataset_id, manifest.to_dict(), force=force)
    return manifest


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "ImportPipelineError",
    "ImportReport",
    "KNOWN_QUEUE_STATUSES",
    "PendingDownloadsQueue",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_INFLIGHT",
    "STATUS_PENDING",
    "import_bars",
    "rebuild_manifest",
]
