"""DuckDB analytical query layer for the Phase 5.6 warehouse.

Eighth Phase 5.6 implementation card ``t_phase56_duckdb_queries``.
Provides read-only analytical queries over the canonical Parquet
tree written by :mod:`strategy.warehouse.parquet_io`.  Uses embedded
DuckDB — no server, no ETL step — and normalizes outputs to
:class:`~strategy.market_data_provider.Bar` records so callers see
the same shape whether they read via the DuckDB layer or the
row-level parquet_io reader.

The layer is intentionally narrow:

* Range scans and per-symbol lookups.
* Coverage / latest-bar / row-count summaries.
* Basic OHLCV aggregates (min/max/mean per interval).

Not in scope for this card:

* Cross-provider comparison — planned follow-up.
* Real-time views / live-tail queries — out of scope for Phase 5.6.
* Query pushdown to remote catalogs — the warehouse is local.

Read-only guarantees inherited from every other Phase 5.6 module.
DuckDB is opened in-memory by default; callers may pin a file
database for repeated sessions.  No credential env vars are read.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
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

import duckdb

from strategy.local_warehouse import WarehouseIntegrityError, WarehouseLayout
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    MarketDataValidationError,
)
from strategy.warehouse.parquet_io import (
    CANONICAL_COLUMNS,
    scan_parquet_files,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DuckDBQueryError(WarehouseIntegrityError):
    """Raised when a DuckDB query cannot complete because the
    partition is missing, the query window is malformed, or the
    on-disk Parquet fails schema checks at query time.
    """


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageSummary:
    """Coverage / row-count summary for one dataset partition."""

    dataset_id: str
    asset_class: str
    interval: str
    symbols: Tuple[str, ...]
    first_timestamp: str
    last_timestamp: str
    row_count: int
    file_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "asset_class": self.asset_class,
            "interval": self.interval,
            "symbols": list(self.symbols),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "row_count": self.row_count,
            "file_count": self.file_count,
        }


@dataclass(frozen=True)
class OHLCVAggregate:
    """Aggregate OHLCV statistics for a symbol over a query window.

    ``mean_close`` / ``mean_volume`` are unweighted arithmetic means
    across the sampled rows — callers who need volume-weighted
    statistics should compute from the raw bars.
    """

    symbol: str
    row_count: int
    first_timestamp: str
    last_timestamp: str
    min_low: float
    max_high: float
    mean_close: float
    mean_volume: float
    total_volume: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "row_count": self.row_count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "min_low": self.min_low,
            "max_high": self.max_high,
            "mean_close": self.mean_close,
            "mean_volume": self.mean_volume,
            "total_volume": self.total_volume,
        }


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class WarehouseQueryReader:
    """Read-only DuckDB analytical layer over the Parquet warehouse.

    Instances hold an embedded DuckDB connection (in-memory by
    default).  Every query re-scans the on-disk Parquet files via
    ``read_parquet(...)`` — DuckDB caches the file metadata across
    calls so repeated queries in one session are cheap.

    ``file_path`` may be passed to persist the DuckDB catalog to
    disk for cross-session reuse; on next construction with the
    same path, DuckDB reopens the file.  ``None`` (default) uses
    an in-memory database that is discarded on close.
    """

    def __init__(
        self,
        layout: WarehouseLayout,
        db_path: Optional[Path] = None,
    ) -> None:
        self._layout = layout
        self._db_path = db_path
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(
            str(db_path) if db_path else ":memory:"
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "WarehouseQueryReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def layout(self) -> WarehouseLayout:
        return self._layout

    # ------------------------------------------------------------------
    # Range scans
    # ------------------------------------------------------------------

    def scan_bars(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        symbols: Sequence[str],
        start: str,
        end: str,
        dataset_id: str = "",
    ) -> List[Bar]:
        """Return every canonical :class:`Bar` matching
        ``symbols`` × ``[start, end]`` under the partition.

        Results are sorted by ``(symbol, timestamp)`` — the same
        contract :func:`strategy.warehouse.parquet_io.read_bars`
        exposes, so callers can swap implementations freely.
        """
        _require_window(start, end)
        paths = self._resolve_paths(asset_class, interval, dataset_id)
        if not paths:
            return []
        symbol_filter, symbol_params = _symbol_filter_sql(symbols)
        query = f"""
            SELECT {", ".join(CANONICAL_COLUMNS)}
            FROM read_parquet(?, union_by_name=true)
            WHERE timestamp >= ? AND timestamp <= ?
            {symbol_filter}
            ORDER BY symbol, timestamp
        """
        cursor = self._conn.execute(
            query, [paths, start, end, *symbol_params]
        )
        return [_row_to_bar(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Coverage / summaries
    # ------------------------------------------------------------------

    def coverage_summary(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        dataset_id: str,
    ) -> Optional[CoverageSummary]:
        """Return a :class:`CoverageSummary` describing the given
        dataset partition, or ``None`` if the partition has no
        files.
        """
        paths = self._resolve_paths(asset_class, interval, dataset_id)
        if not paths:
            return None
        query = """
            SELECT
              MIN(timestamp) AS first_ts,
              MAX(timestamp) AS last_ts,
              COUNT(*) AS row_count,
              LIST_SORT(LIST(DISTINCT symbol)) AS symbols
            FROM read_parquet(?, union_by_name=true)
        """
        row = self._conn.execute(query, [paths]).fetchone()
        if row is None or row[2] == 0:
            return None
        first_ts, last_ts, row_count, symbols = row
        return CoverageSummary(
            dataset_id=dataset_id,
            asset_class=asset_class.value,
            interval=interval.value,
            symbols=tuple(sorted(symbols or [])),
            first_timestamp=first_ts or "",
            last_timestamp=last_ts or "",
            row_count=int(row_count),
            file_count=len(paths),
        )

    def latest_bar(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        symbol: str,
        dataset_id: str = "",
    ) -> Optional[Bar]:
        """Return the most recent :class:`Bar` for ``symbol`` in
        the given partition, or ``None`` if the symbol has no rows.
        """
        if not symbol:
            raise DuckDBQueryError("symbol is required")
        paths = self._resolve_paths(asset_class, interval, dataset_id)
        if not paths:
            return None
        query = f"""
            SELECT {", ".join(CANONICAL_COLUMNS)}
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT 1
        """
        cursor = self._conn.execute(query, [paths, symbol])
        row = cursor.fetchone()
        return _row_to_bar(row) if row is not None else None

    def row_counts_by_symbol(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        dataset_id: str = "",
    ) -> Dict[str, int]:
        """Return ``{symbol: row_count}`` under the given partition.
        Empty dict when the partition has no files.
        """
        paths = self._resolve_paths(asset_class, interval, dataset_id)
        if not paths:
            return {}
        query = """
            SELECT symbol, COUNT(*) AS row_count
            FROM read_parquet(?, union_by_name=true)
            GROUP BY symbol
            ORDER BY symbol
        """
        return {
            row[0]: int(row[1])
            for row in self._conn.execute(query, [paths]).fetchall()
        }

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------

    def ohlcv_aggregate(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        symbols: Sequence[str],
        start: str,
        end: str,
        dataset_id: str = "",
    ) -> List[OHLCVAggregate]:
        """Return per-symbol OHLCV aggregates over ``[start, end]``.

        Sorted by ``symbol`` ascending.  Symbols with no rows in
        the window are omitted from the result (rather than
        returning zeroed aggregates).
        """
        _require_window(start, end)
        paths = self._resolve_paths(asset_class, interval, dataset_id)
        if not paths:
            return []
        symbol_filter, symbol_params = _symbol_filter_sql(symbols)
        query = f"""
            SELECT
              symbol,
              COUNT(*)             AS row_count,
              MIN(timestamp)       AS first_ts,
              MAX(timestamp)       AS last_ts,
              MIN(low)             AS min_low,
              MAX(high)            AS max_high,
              AVG(close)           AS mean_close,
              AVG(volume)          AS mean_volume,
              SUM(volume)          AS total_volume
            FROM read_parquet(?, union_by_name=true)
            WHERE timestamp >= ? AND timestamp <= ?
            {symbol_filter}
            GROUP BY symbol
            ORDER BY symbol
        """
        cursor = self._conn.execute(
            query, [paths, start, end, *symbol_params]
        )
        return [
            OHLCVAggregate(
                symbol=row[0],
                row_count=int(row[1]),
                first_timestamp=row[2] or "",
                last_timestamp=row[3] or "",
                min_low=float(row[4] or 0.0),
                max_high=float(row[5] or 0.0),
                mean_close=float(row[6] or 0.0),
                mean_volume=float(row[7] or 0.0),
                total_volume=float(row[8] or 0.0),
            )
            for row in cursor.fetchall()
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_paths(
        self,
        asset_class: AssetClass,
        interval: BarInterval,
        dataset_id: str,
    ) -> List[str]:
        rels = scan_parquet_files(
            self._layout, asset_class, interval, dataset_id=dataset_id
        )
        return [
            str((self._layout.root / rel).resolve()) for rel in rels
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_window(start: str, end: str) -> None:
    if not start or not end:
        raise DuckDBQueryError(
            "start and end are required for range queries"
        )
    if start > end:
        raise DuckDBQueryError("start must be <= end")


def _symbol_filter_sql(
    symbols: Sequence[str],
) -> Tuple[str, List[str]]:
    if not symbols:
        return "", []
    placeholders = ", ".join(["?"] * len(symbols))
    return f"AND symbol IN ({placeholders})", list(symbols)


def _row_to_bar(row: Sequence[Any]) -> Bar:
    (
        symbol,
        timestamp,
        open_,
        high,
        low,
        close,
        volume,
        vwap,
        trade_count,
        interval_value,
        adjustment_value,
        adjustment_version,
    ) = row
    try:
        interval = BarInterval(interval_value)
        adjustment = AdjustmentMode(adjustment_value)
        return Bar(
            symbol=symbol,
            timestamp=timestamp,
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume),
            interval=interval,
            adjustment_mode=adjustment,
            vwap=None if vwap is None else float(vwap),
            trade_count=None if trade_count is None else int(trade_count),
            adjustment_version=adjustment_version or "",
        )
    except (ValueError, MarketDataValidationError) as exc:
        raise DuckDBQueryError(
            f"cannot materialize Bar from row {row!r}: {exc}"
        ) from exc


@contextmanager
def open_reader(
    layout: WarehouseLayout, db_path: Optional[Path] = None
) -> Iterator[WarehouseQueryReader]:
    """Context-manager wrapper for
    :class:`WarehouseQueryReader` — automatically closes the
    DuckDB connection on exit.
    """
    reader = WarehouseQueryReader(layout=layout, db_path=db_path)
    try:
        yield reader
    finally:
        reader.close()


__all__ = [
    "CoverageSummary",
    "DuckDBQueryError",
    "OHLCVAggregate",
    "WarehouseQueryReader",
    "open_reader",
]
