"""Canonical Parquet bar storage for the Phase 5.6 warehouse.

Fourth Phase 5.6 implementation card ``t_phase56_parquet_storage``.
Ships the on-disk bar writer / reader every future warehouse fetch
and analytical query will pass through.  No DuckDB, no provider
plugins, no import pipeline — this module is exclusively about
converting :class:`~strategy.market_data_provider.Bar` instances to
and from Parquet files at deterministic paths under a
:class:`~strategy.local_warehouse.WarehouseLayout`.

Design deliverable in
``docs/architecture/phase-5-6-historical-warehouse.md``
(§Architecture.2 + §Storage Architecture Discussion).

Read-only guarantees enforced by tests in
``tests/test_warehouse_parquet_io.py``:

* Never places, submits, cancels, or replaces orders.
* Never imports ``trader``, ``crypto_trader``, ``trader_cli``,
  ``telegram_approvals``, or ``strategy.runner``.
* Never mutates :class:`~strategy.config.FeatureFlags`.
* Never constructs an ``ApprovalRecord``.
* Never advances ``PromotionEntry`` state.
* Never reads any provider credential env namespace.

Terminology: validation / replay / research / acquisition.  Never
"training".

Canonical schema
================

Every bar row carries the same 11 columns in the same order.
Consumers must not reorder columns on read — the schema is the
warehouse's contract with itself.

    0.  symbol              string, non-null
    1.  timestamp           string, non-null, ISO 8601
    2.  open                float64, non-null
    3.  high                float64, non-null
    4.  low                 float64, non-null
    5.  close               float64, non-null
    6.  volume              float64, non-null
    7.  vwap                float64, nullable
    8.  trade_count         int64,  nullable
    9.  interval            string, non-null (BarInterval.value)
    10. adjustment_mode     string, non-null (AdjustmentMode.value)
    11. adjustment_version  string, non-null (may be empty)

Rows are written sorted by ``(symbol, timestamp)`` so subsequent
range scans are cache-friendly.  Bars for multiple symbols may be
mixed within one write call — the partitioner routes each bar to
its own file.

Compression
===========

Zstandard (level 3) is applied at write time.  Deterministic:
identical input rows produce byte-identical output files at a
fixed pyarrow / libparquet version.  Consumers who care about
byte reproducibility across pyarrow upgrades should compare
row-level content, not file bytes.

Partition strategy
==================

Every bar routes to exactly one file, keyed by
``(asset_class, interval, symbol, time_bucket)`` where
``time_bucket`` derives from the bar's timestamp:

    daily             -> {yyyy}.parquet
    hourly            -> {yyyy}-{mm}.parquet
    minute / second   -> {yyyy}-{mm}-{dd}.parquet

The dataset directory under
:meth:`~strategy.local_warehouse.WarehouseLayout.dataset_dir` looks
like ``<layout.root>/<asset>/<interval>/<dataset_id>/``; the
per-symbol partition is ``<dataset_id>/<symbol>/<bucket>.parquet``.
Callers who want the same partition rule without the dataset scope
(e.g. for direct import) may pass ``dataset_id=""`` — the file is
still written under the appropriate asset/interval subtree.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
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

import pyarrow as pa
import pyarrow.parquet as pq

from strategy.local_warehouse import (
    WarehouseIntegrityError,
    WarehouseLayout,
    _asset_subdir,
    _interval_subdir,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    MarketDataValidationError,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_COMPRESSION = "zstd"
DEFAULT_COMPRESSION_LEVEL = 3

# Deterministic column order.  Consumers MUST NOT reorder these
# columns on read — the warehouse's schema is the contract with
# itself.
CANONICAL_COLUMNS: Tuple[str, ...] = (
    "symbol",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "trade_count",
    "interval",
    "adjustment_mode",
    "adjustment_version",
)

# Canonical pyarrow schema.  Every column but ``vwap`` and
# ``trade_count`` is non-null.  vwap is float64 nullable so
# providers that don't emit VWAP can still round-trip.
CANONICAL_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timestamp", pa.string(), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.float64(), nullable=False),
        pa.field("vwap", pa.float64(), nullable=True),
        pa.field("trade_count", pa.int64(), nullable=True),
        pa.field("interval", pa.string(), nullable=False),
        pa.field("adjustment_mode", pa.string(), nullable=False),
        pa.field("adjustment_version", pa.string(), nullable=False),
    ]
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ParquetStorageError(WarehouseIntegrityError):
    """Raised when a Parquet read / write cannot complete because
    the input mixes intervals or the on-disk file's schema does not
    match :data:`CANONICAL_SCHEMA`.
    """


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WrittenFile:
    """One Parquet file produced by :func:`write_bars`.

    All paths are relative to :attr:`WarehouseLayout.root` so the
    warehouse manifest can reference them portably.
    """

    relative_path: str
    absolute_path: str
    symbol: str
    time_bucket: str
    row_count: int
    size_bytes: int
    sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "symbol": self.symbol,
            "time_bucket": self.time_bucket,
            "row_count": self.row_count,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


# ---------------------------------------------------------------------------
# Partition helpers
# ---------------------------------------------------------------------------


_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _time_bucket_for(bar: Bar) -> str:
    """Derive the partition key from a bar's timestamp.

    Rules mirror the module docstring.  The timestamp MUST start
    with a full ``YYYY-MM-DD`` prefix — providers that emit shorter
    stamps must normalize before construction.
    """
    match = _DATE_PREFIX.match(bar.timestamp)
    if not match:
        raise ParquetStorageError(
            f"bar timestamp {bar.timestamp!r} does not start with YYYY-MM-DD; "
            "provider plugins are responsible for normalising to ISO 8601"
        )
    year, month, day = match.groups()
    interval = bar.interval
    if interval is BarInterval.DAILY:
        return year
    if interval is BarInterval.HOURLY:
        return f"{year}-{month}"
    # Sub-hourly (minute/second) -> per-day partition
    return f"{year}-{month}-{day}"


def _partition_dir(
    layout: WarehouseLayout,
    dataset_id: str,
    asset_class: AssetClass,
    interval: BarInterval,
    symbol: str,
) -> Path:
    """Compute the directory that holds this partition's parquet
    files.  Never creates the directory — callers create on write.
    """
    if not symbol:
        raise ParquetStorageError("symbol is required")
    asset = _asset_subdir(asset_class)
    interval_subdir = _interval_subdir(interval)
    root = layout.root / asset / interval_subdir
    if dataset_id:
        # Reuse the warehouse's dataset_id guard by asking the
        # layout to compute the dataset dir — it validates the id
        # for us and raises WarehouseIntegrityError on traversal.
        root = layout.dataset_dir(dataset_id, asset_class, interval)
    return root / symbol


def _relative_to_root(layout: WarehouseLayout, path: Path) -> str:
    return path.resolve().relative_to(layout.root.resolve()).as_posix()


# ---------------------------------------------------------------------------
# Bar <-> arrow conversion
# ---------------------------------------------------------------------------


def _sort_rows(bars: Sequence[Bar]) -> List[Bar]:
    return sorted(bars, key=lambda b: (b.symbol, b.timestamp))


def _bars_to_arrays(
    bars: Sequence[Bar],
) -> Dict[str, pa.Array]:
    """Convert a homogeneous batch of bars to Arrow columnar arrays.

    All bars in the batch must share the same ``interval`` and
    ``adjustment_mode`` — the file schema fixes both.  Rows are
    NOT reordered here; the caller sorts before batching.
    """
    if not bars:
        return {col: pa.array([], type=CANONICAL_SCHEMA.field(col).type) for col in CANONICAL_COLUMNS}
    symbols = [b.symbol for b in bars]
    timestamps = [b.timestamp for b in bars]
    opens = [float(b.open) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]
    vwaps = [None if b.vwap is None else float(b.vwap) for b in bars]
    trade_counts = [
        None if b.trade_count is None else int(b.trade_count) for b in bars
    ]
    intervals = [b.interval.value for b in bars]
    adjustments = [b.adjustment_mode.value for b in bars]
    adjustment_versions = [b.adjustment_version for b in bars]
    return {
        "symbol": pa.array(symbols, type=pa.string()),
        "timestamp": pa.array(timestamps, type=pa.string()),
        "open": pa.array(opens, type=pa.float64()),
        "high": pa.array(highs, type=pa.float64()),
        "low": pa.array(lows, type=pa.float64()),
        "close": pa.array(closes, type=pa.float64()),
        "volume": pa.array(volumes, type=pa.float64()),
        "vwap": pa.array(vwaps, type=pa.float64()),
        "trade_count": pa.array(trade_counts, type=pa.int64()),
        "interval": pa.array(intervals, type=pa.string()),
        "adjustment_mode": pa.array(adjustments, type=pa.string()),
        "adjustment_version": pa.array(adjustment_versions, type=pa.string()),
    }


def _rows_to_table(bars: Sequence[Bar]) -> pa.Table:
    arrays = _bars_to_arrays(bars)
    columns = [arrays[name] for name in CANONICAL_COLUMNS]
    return pa.Table.from_arrays(columns, schema=CANONICAL_SCHEMA)


def _row_to_bar(row: Mapping[str, Any]) -> Bar:
    interval_value = row["interval"]
    adjustment_value = row["adjustment_mode"]
    try:
        interval = BarInterval(interval_value)
    except ValueError as exc:
        raise ParquetStorageError(
            f"unknown interval on disk: {interval_value!r}"
        ) from exc
    try:
        adjustment = AdjustmentMode(adjustment_value)
    except ValueError as exc:
        raise ParquetStorageError(
            f"unknown adjustment_mode on disk: {adjustment_value!r}"
        ) from exc
    try:
        return Bar(
            symbol=row["symbol"],
            timestamp=row["timestamp"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            interval=interval,
            adjustment_mode=adjustment,
            vwap=row["vwap"],
            trade_count=row["trade_count"],
            adjustment_version=row["adjustment_version"] or "",
        )
    except MarketDataValidationError as exc:
        raise ParquetStorageError(
            f"invalid bar on disk: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Public writer
# ---------------------------------------------------------------------------


def _sha256_file(path: Path, chunk: int = 65536) -> Tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


def _atomic_write_table(table: pa.Table, target: Path) -> None:
    """Write ``table`` atomically to ``target``.

    Uses ``tempfile.mkstemp`` in the same directory + ``os.replace``
    so a crash mid-write cannot corrupt an existing file.  Cleans up
    the temp file best-effort on failure.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}-",
        suffix=".tmp",
    )
    os.close(tmp_fd)
    try:
        pq.write_table(
            table,
            tmp_name,
            compression=DEFAULT_COMPRESSION,
            compression_level=DEFAULT_COMPRESSION_LEVEL,
            use_dictionary=False,
            write_statistics=False,
        )
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_bars(
    layout: WarehouseLayout,
    bars: Sequence[Bar],
    asset_class: AssetClass,
    dataset_id: str = "",
) -> List[WrittenFile]:
    """Write a batch of bars to canonical Parquet files.

    Every bar must share the same ``interval`` and
    ``adjustment_mode`` — the file schema fixes both.  The
    partitioner groups bars by ``(symbol, time_bucket)`` and writes
    one Parquet file per group.  Bars are sorted by
    ``(symbol, timestamp)`` before writing so downstream range
    scans are cache-friendly.

    Files are written atomically (temp file + ``os.replace``).  A
    crash mid-write leaves the existing file intact.

    Returns one :class:`WrittenFile` per file produced, containing
    the relative + absolute paths, row count, byte count, and
    sha256 of the file.  Callers thread these into their manifest.
    """
    if not bars:
        return []
    intervals = {b.interval for b in bars}
    if len(intervals) != 1:
        raise ParquetStorageError(
            f"write_bars requires a single interval per call; got {intervals}"
        )
    adjustments = {b.adjustment_mode for b in bars}
    if len(adjustments) != 1:
        raise ParquetStorageError(
            f"write_bars requires a single adjustment_mode per call; got {adjustments}"
        )
    interval = intervals.pop()

    grouped: Dict[Tuple[str, str], List[Bar]] = defaultdict(list)
    for bar in _sort_rows(bars):
        bucket = _time_bucket_for(bar)
        grouped[(bar.symbol, bucket)].append(bar)

    written: List[WrittenFile] = []
    for (symbol, bucket), rows in sorted(grouped.items()):
        partition_dir = _partition_dir(
            layout, dataset_id, asset_class, interval, symbol
        )
        target = partition_dir / f"{bucket}.parquet"
        table = _rows_to_table(rows)
        _atomic_write_table(table, target)
        sha256, size_bytes = _sha256_file(target)
        written.append(
            WrittenFile(
                relative_path=_relative_to_root(layout, target),
                absolute_path=str(target),
                symbol=symbol,
                time_bucket=bucket,
                row_count=len(rows),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        )
    return written


# ---------------------------------------------------------------------------
# Public reader
# ---------------------------------------------------------------------------


def _validate_on_disk_schema(schema: pa.Schema, path: Path) -> None:
    expected = list(CANONICAL_COLUMNS)
    actual = [schema.field(i).name for i in range(len(schema))]
    if actual != expected:
        raise ParquetStorageError(
            f"schema mismatch in {path}: expected columns {expected}, got {actual}"
        )
    for expected_field in CANONICAL_SCHEMA:
        got = schema.field(expected_field.name)
        if got.type != expected_field.type:
            raise ParquetStorageError(
                f"schema mismatch in {path}: column {expected_field.name} "
                f"expected {expected_field.type}, got {got.type}"
            )


def read_bars(
    layout: WarehouseLayout,
    relative_paths: Sequence[str],
) -> List[Bar]:
    """Read Parquet files and return :class:`Bar` records in
    deterministic ``(symbol, timestamp)`` order.

    Every path must be relative to :attr:`WarehouseLayout.root`.
    Reader refuses to open a file whose schema does not match
    :data:`CANONICAL_SCHEMA` — the on-disk contract is enforced on
    read as well as write.
    """
    if not relative_paths:
        return []
    bars: List[Bar] = []
    for rel in relative_paths:
        target = layout.root / rel
        if not target.is_file():
            raise ParquetStorageError(f"missing Parquet file: {target}")
        pf = pq.ParquetFile(str(target))
        _validate_on_disk_schema(pf.schema_arrow, target)
        table = pf.read()
        for row in table.to_pylist():
            bars.append(_row_to_bar(row))
    bars.sort(key=lambda b: (b.symbol, b.timestamp))
    return bars


def scan_parquet_files(
    layout: WarehouseLayout,
    asset_class: AssetClass,
    interval: BarInterval,
    dataset_id: str = "",
    symbol: Optional[str] = None,
) -> List[str]:
    """Enumerate the Parquet files under a given
    ``(asset_class, interval, dataset_id, symbol?)`` partition.

    Returns relative paths (portable across warehouse roots),
    sorted lexicographically so callers get deterministic scan
    order.  Never touches file contents.
    """
    if symbol is not None and not symbol:
        raise ParquetStorageError("symbol must be non-empty when supplied")
    interval_root = layout.root / _asset_subdir(asset_class) / _interval_subdir(interval)
    if dataset_id:
        interval_root = layout.dataset_dir(dataset_id, asset_class, interval)
    if not interval_root.is_dir():
        return []
    if symbol is not None:
        candidates = sorted((interval_root / symbol).glob("*.parquet"))
    else:
        candidates = sorted(interval_root.glob("*/*.parquet"))
    return [_relative_to_root(layout, path) for path in candidates]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "CANONICAL_COLUMNS",
    "CANONICAL_SCHEMA",
    "DEFAULT_COMPRESSION",
    "DEFAULT_COMPRESSION_LEVEL",
    "ParquetStorageError",
    "WrittenFile",
    "read_bars",
    "scan_parquet_files",
    "write_bars",
]
