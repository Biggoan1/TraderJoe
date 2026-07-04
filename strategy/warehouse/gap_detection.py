"""Coverage-gap detection for the Phase 5.6 warehouse.

Eleventh Phase 5.6 implementation card
``t_phase56_gap_detection``.  Runs a deterministic scanner across
a dataset's on-disk Parquet files and produces a
:class:`GapReport` describing what's missing relative to an
expected coverage window.

Gap kinds this card detects (day-level granularity for daily
datasets; sub-day intervals are treated as calendar-day gaps
between the first and last bar of each expected day):

* ``missing_trading_day`` — expected trading day per calendar
  has no bars for a given symbol.
* ``missing_symbol`` — symbol expected but absent from the
  dataset entirely.
* ``partial_day`` — a sub-daily dataset covers a trading day but
  emits fewer bars than a lower-bound threshold (default: 1).
* ``holiday_bars_present`` — bars present on a declared holiday
  when a calendar is supplied.
* ``weekend_bars_present`` — bars present on a weekend day when
  no calendar is supplied and ``exclude_weekends=True``.
* ``no_coverage`` — the dataset has no bars for a requested
  symbol at all.

Notes:

* Trading-day calendar is optional.  Callers who pass a
  ``TradingCalendar`` get holiday-aware detection; callers who
  don't get a simple weekday scan (Mon-Fri).
* Corporate-action mismatch checks (e.g. a split recorded but
  bar values unadjusted) are the concern of the versioning
  card's ``compare_versions`` — not this scanner.
* DST anomalies are not currently scanned; the design doc
  reserves them as a follow-up.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from strategy.data_catalog import DataCatalog, DatasetManifest
from strategy.local_warehouse import (
    WarehouseIntegrityError,
    WarehouseLayout,
    read_manifest,
)
from strategy.market_data_provider import AssetClass, BarInterval, CalendarDay, CalendarSessionKind
from strategy.warehouse.parquet_io import read_bars, scan_parquet_files


class GapDetectionError(WarehouseIntegrityError):
    """Raised when gap detection cannot proceed — malformed
    dataset id, missing partition, malformed window bounds.
    """


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    """One coverage gap surfaced by the scanner."""

    kind: str
    symbol: str
    date: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "date": self.date,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GapReport:
    """Aggregate coverage-gap report for one dataset scan."""

    dataset_id: str
    asset_class: str
    interval: str
    window_start: str
    window_end: str
    expected_symbols: Tuple[str, ...]
    observed_symbols: Tuple[str, ...]
    total_expected_trading_days: int
    total_gaps: int
    gap_counts: Dict[str, int]
    gaps: Tuple[Gap, ...]
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "asset_class": self.asset_class,
            "interval": self.interval,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "expected_symbols": list(self.expected_symbols),
            "observed_symbols": list(self.observed_symbols),
            "total_expected_trading_days": self.total_expected_trading_days,
            "total_gaps": self.total_gaps,
            "gap_counts": dict(self.gap_counts),
            "gaps": [g.to_dict() for g in self.gaps],
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _iso_date(iso: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", iso)
    if not match:
        raise GapDetectionError(
            f"cannot parse date from timestamp {iso!r}"
        )
    return match.group(1)


def _daterange(start_iso: str, end_iso: str) -> Iterable[date]:
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------


def detect_gaps(
    layout: WarehouseLayout,
    dataset_id: str,
    window_start: str,
    window_end: str,
    calendar: Optional[Sequence[CalendarDay]] = None,
    exclude_weekends: bool = True,
    expected_symbols: Optional[Sequence[str]] = None,
    minimum_bars_per_day: int = 1,
) -> GapReport:
    """Scan a dataset for coverage gaps.

    * ``window_start`` / ``window_end`` are inclusive ISO 8601
      date strings (``YYYY-MM-DD``).  Empty bounds raise.
    * ``calendar`` — optional list of
      :class:`~strategy.market_data_provider.CalendarDay`.  When
      supplied, holidays flagged in the calendar are excluded
      from the expected trading days; bars present on holidays
      surface as ``holiday_bars_present``.
    * ``exclude_weekends`` — when no calendar is supplied,
      weekends are excluded from expected trading days by
      default.  Set ``False`` to enable weekend-bars-present
      checks against a 7-day/week schedule.
    * ``expected_symbols`` — the symbol universe to expect.
      When ``None``, the manifest's ``symbols`` field is used.
    * ``minimum_bars_per_day`` — for sub-daily intervals, a
      trading day with fewer than this many bars is flagged as
      ``partial_day``.  Daily intervals expect exactly 1.
    """
    if not window_start or not window_end:
        raise GapDetectionError(
            "window_start and window_end are required"
        )
    if window_start > window_end:
        raise GapDetectionError("window_start must be <= window_end")

    # Load manifest for provenance + declared symbols
    try:
        payload = read_manifest(layout, dataset_id)
    except WarehouseIntegrityError as exc:
        raise GapDetectionError(str(exc)) from exc
    manifest = DatasetManifest.from_dict(payload)
    if not manifest.interval or not manifest.asset_class:
        raise GapDetectionError(
            f"dataset {dataset_id!r} missing warehouse fields"
        )
    interval = BarInterval(manifest.interval)
    asset_class = AssetClass(manifest.asset_class)
    if expected_symbols is None:
        expected_symbols = manifest.symbols
    expected_symbols = tuple(expected_symbols)

    # Load bars across all files
    paths = scan_parquet_files(
        layout, asset_class, interval, dataset_id=dataset_id
    )
    bars_by_symbol_day: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    observed_symbols: Set[str] = set()
    for rel in paths:
        for bar in read_bars(layout, [rel]):
            observed_symbols.add(bar.symbol)
            day = _iso_date(bar.timestamp)
            bars_by_symbol_day[bar.symbol][day] += 1

    # Determine expected trading days for the window
    if calendar is not None:
        holidays: Set[str] = {
            c.date for c in calendar if c.kind is CalendarSessionKind.HOLIDAY
        }
        # If a full calendar is supplied, expected trading days
        # are its FULL_TRADING and HALF_TRADING entries within
        # window; but we also honor the plain iso range.
        cal_trading_days: Set[str] = {
            c.date for c in calendar
            if c.kind is not CalendarSessionKind.HOLIDAY
            and window_start <= c.date <= window_end
        }
        expected_days = tuple(sorted(cal_trading_days))
    else:
        holidays = set()
        expected_days = tuple(
            d.isoformat()
            for d in _daterange(window_start, window_end)
            if not (exclude_weekends and _is_weekend(d))
        )

    gaps: List[Gap] = []
    for symbol in expected_symbols:
        symbol_days = bars_by_symbol_day.get(symbol, {})
        if not symbol_days:
            gaps.append(
                Gap(
                    kind="no_coverage",
                    symbol=symbol,
                    date="",
                    detail="no bars for symbol in dataset",
                )
            )
            continue
        for day in expected_days:
            count = symbol_days.get(day, 0)
            if count == 0:
                gaps.append(
                    Gap(
                        kind="missing_trading_day",
                        symbol=symbol,
                        date=day,
                    )
                )
            elif interval is not BarInterval.DAILY and count < minimum_bars_per_day:
                gaps.append(
                    Gap(
                        kind="partial_day",
                        symbol=symbol,
                        date=day,
                        detail=(
                            f"observed {count} bars, expected >= "
                            f"{minimum_bars_per_day}"
                        ),
                    )
                )
        # Holiday / weekend bars-present checks
        for day, count in symbol_days.items():
            if day < window_start or day > window_end:
                continue
            if calendar is not None and day in holidays:
                gaps.append(
                    Gap(
                        kind="holiday_bars_present",
                        symbol=symbol,
                        date=day,
                        detail=f"{count} bars on declared holiday",
                    )
                )
            if calendar is None and exclude_weekends:
                try:
                    day_date = date.fromisoformat(day)
                except ValueError:
                    continue
                if _is_weekend(day_date):
                    gaps.append(
                        Gap(
                            kind="weekend_bars_present",
                            symbol=symbol,
                            date=day,
                            detail=f"{count} bars on weekend",
                        )
                    )

    # Missing symbols
    for symbol in expected_symbols:
        if symbol not in observed_symbols and not any(
            g.kind == "no_coverage" and g.symbol == symbol
            for g in gaps
        ):
            gaps.append(
                Gap(kind="missing_symbol", symbol=symbol, date="")
            )

    # Deterministic ordering
    gaps.sort(key=lambda g: (g.symbol, g.date, g.kind))
    gap_counts: Dict[str, int] = defaultdict(int)
    for gap in gaps:
        gap_counts[gap.kind] += 1

    return GapReport(
        dataset_id=dataset_id,
        asset_class=asset_class.value,
        interval=interval.value,
        window_start=window_start,
        window_end=window_end,
        expected_symbols=tuple(expected_symbols),
        observed_symbols=tuple(sorted(observed_symbols)),
        total_expected_trading_days=len(expected_days),
        total_gaps=len(gaps),
        gap_counts=dict(gap_counts),
        gaps=tuple(gaps),
        generated_at=_utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_report(report: GapReport, output_dir: Path) -> Path:
    """Persist ``report`` under ``output_dir`` and return the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = re.sub(r"[^0-9A-Za-z]", "-", report.generated_at)
    target = output_dir / f"{report.dataset_id}-{safe_ts}.json"
    target.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


__all__ = [
    "Gap",
    "GapDetectionError",
    "GapReport",
    "detect_gaps",
    "write_report",
]
