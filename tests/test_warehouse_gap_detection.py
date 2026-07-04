"""Tests for strategy/warehouse/gap_detection.py.

Eleventh Phase 5.6 card ``t_phase56_gap_detection``.

Coverage:

* Fully-covered dataset -> zero gaps
* Missing trading days flagged
* Weekends excluded by default (no false positives)
* Weekend bars flagged when weekend_bars_present enabled
* Holiday awareness via CalendarDay input
* Missing symbol / no coverage
* Partial day for sub-daily intervals
* Deterministic ordering
* Serialization
* Report writes to output_dir
* Source safety + feature-flag invariance
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.data_catalog import DatasetFile, DatasetManifest
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    WarehouseLayout,
    write_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    CalendarDay,
    CalendarSessionKind,
)
from strategy.warehouse.gap_detection import (
    Gap,
    GapDetectionError,
    GapReport,
    detect_gaps,
    write_report,
)
from strategy.warehouse.parquet_io import write_bars


def _bar(sym: str, ts: str, close: float = 100.0, interval: BarInterval = BarInterval.DAILY) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=ts,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000_000,
        interval=interval,
        adjustment_mode=AdjustmentMode.RAW,
    )


def _seed(
    layout: WarehouseLayout,
    dataset_id: str,
    bars: List[Bar],
    interval: str = "1Day",
    symbols: Tuple[str, ...] = ("AAPL",),
) -> DatasetManifest:
    written = write_bars(
        layout, bars, AssetClass.EQUITY, dataset_id=dataset_id
    )
    files = tuple(
        DatasetFile(
            path=w.relative_path,
            sha256=w.sha256,
            size_bytes=w.size_bytes,
            row_count=w.row_count,
        )
        for w in written
    )
    m = DatasetManifest(
        dataset_id=dataset_id,
        kind="historical_bars",
        symbols=symbols,
        start_date=min(b.timestamp for b in bars),
        end_date=max(b.timestamp for b in bars),
        files=files,
        provider="alpaca",
        interval=interval,
        asset_class="equity",
        adjustment_mode="raw",
        validation_status=STATUS_UNVALIDATED,
    )
    write_manifest(layout, dataset_id, m.to_dict())
    return m


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    layout = WarehouseLayout(root=tmp_path / "wh")
    layout.create()
    return layout


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestFullCoverage:
    def test_no_gaps_when_fully_covered(self, layout):
        # 5 weekdays 2020-05-04 (Mon) through 2020-05-08 (Fri)
        bars = [
            _bar("AAPL", "2020-05-04T14:30:00+00:00"),
            _bar("AAPL", "2020-05-05T14:30:00+00:00"),
            _bar("AAPL", "2020-05-06T14:30:00+00:00"),
            _bar("AAPL", "2020-05-07T14:30:00+00:00"),
            _bar("AAPL", "2020-05-08T14:30:00+00:00"),
        ]
        _seed(layout, "dset", bars)
        r = detect_gaps(layout, "dset", "2020-05-04", "2020-05-08")
        assert r.total_gaps == 0
        assert r.total_expected_trading_days == 5


# ---------------------------------------------------------------------------
# Missing days
# ---------------------------------------------------------------------------


class TestMissingDays:
    def test_flags_missing_weekdays(self, layout):
        # Cover Mon-Wed only; expect Thu+Fri to be flagged
        bars = [
            _bar("AAPL", "2020-05-04T14:30:00+00:00"),
            _bar("AAPL", "2020-05-05T14:30:00+00:00"),
            _bar("AAPL", "2020-05-06T14:30:00+00:00"),
        ]
        _seed(layout, "dset", bars)
        r = detect_gaps(layout, "dset", "2020-05-04", "2020-05-08")
        missing = [g for g in r.gaps if g.kind == "missing_trading_day"]
        assert {g.date for g in missing} == {"2020-05-07", "2020-05-08"}

    def test_weekends_excluded_by_default(self, layout):
        # Sat/Sun in the window should not be flagged
        bars = [
            _bar("AAPL", "2020-05-08T14:30:00+00:00"),  # Fri
            _bar("AAPL", "2020-05-11T14:30:00+00:00"),  # Mon
        ]
        _seed(layout, "dset", bars)
        r = detect_gaps(layout, "dset", "2020-05-08", "2020-05-11")
        # Sat 5-9 and Sun 5-10 should not be flagged
        missing_dates = {
            g.date for g in r.gaps if g.kind == "missing_trading_day"
        }
        assert "2020-05-09" not in missing_dates
        assert "2020-05-10" not in missing_dates


# ---------------------------------------------------------------------------
# Weekend / holiday bars present
# ---------------------------------------------------------------------------


class TestUnexpectedBars:
    def test_weekend_bars_flagged_when_no_calendar(self, layout):
        bars = [
            _bar("AAPL", "2020-05-09T14:30:00+00:00"),  # Sat
            _bar("AAPL", "2020-05-11T14:30:00+00:00"),  # Mon
        ]
        _seed(layout, "dset", bars)
        r = detect_gaps(layout, "dset", "2020-05-09", "2020-05-11")
        weekend = [g for g in r.gaps if g.kind == "weekend_bars_present"]
        assert len(weekend) == 1
        assert weekend[0].date == "2020-05-09"

    def test_holiday_bars_flagged_when_calendar_supplied(self, layout):
        bars = [
            _bar("AAPL", "2020-07-03T14:30:00+00:00"),  # Fri (holiday)
            _bar("AAPL", "2020-07-06T14:30:00+00:00"),  # Mon
        ]
        _seed(layout, "dset", bars)
        calendar = [
            CalendarDay(
                exchange="XNYS", date="2020-07-03",
                kind=CalendarSessionKind.HOLIDAY,
            ),
            CalendarDay(
                exchange="XNYS", date="2020-07-06",
                kind=CalendarSessionKind.FULL_TRADING,
                open_time="09:30", close_time="16:00",
                timezone="America/New_York",
            ),
        ]
        r = detect_gaps(
            layout, "dset", "2020-07-03", "2020-07-06",
            calendar=calendar,
        )
        holiday = [g for g in r.gaps if g.kind == "holiday_bars_present"]
        assert len(holiday) == 1
        assert holiday[0].date == "2020-07-03"


# ---------------------------------------------------------------------------
# Missing / partial symbols
# ---------------------------------------------------------------------------


class TestMissingSymbols:
    def test_symbol_with_no_bars_flagged_no_coverage(self, layout):
        bars = [_bar("AAPL", "2020-05-04T14:30:00+00:00")]
        _seed(layout, "dset", bars, symbols=("AAPL", "MSFT"))
        r = detect_gaps(layout, "dset", "2020-05-04", "2020-05-04")
        gaps_no_cov = [g for g in r.gaps if g.kind == "no_coverage"]
        assert [g.symbol for g in gaps_no_cov] == ["MSFT"]

    def test_expected_symbols_override_manifest(self, layout):
        bars = [_bar("AAPL", "2020-05-04T14:30:00+00:00")]
        _seed(layout, "dset", bars, symbols=("AAPL",))
        r = detect_gaps(
            layout, "dset", "2020-05-04", "2020-05-04",
            expected_symbols=["AAPL", "NVDA"],
        )
        assert any(g.symbol == "NVDA" and g.kind == "no_coverage" for g in r.gaps)


# ---------------------------------------------------------------------------
# Partial-day (sub-daily)
# ---------------------------------------------------------------------------


class TestPartialDay:
    def test_hourly_dataset_partial_day_flagged(self, layout):
        # 2 hourly bars on Mon, expected >= 3 per day
        bars = [
            _bar("AAPL", "2020-05-04T09:30:00+00:00", interval=BarInterval.HOURLY),
            _bar("AAPL", "2020-05-04T10:30:00+00:00", interval=BarInterval.HOURLY),
        ]
        _seed(layout, "dset", bars, interval="1Hour")
        r = detect_gaps(
            layout, "dset", "2020-05-04", "2020-05-04",
            minimum_bars_per_day=3,
        )
        partial = [g for g in r.gaps if g.kind == "partial_day"]
        assert len(partial) == 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestGapDetectionErrors:
    def test_empty_bounds_rejected(self, layout):
        _seed(layout, "dset", [_bar("AAPL", "2020-05-04T14:30:00+00:00")])
        with pytest.raises(GapDetectionError, match="required"):
            detect_gaps(layout, "dset", "", "2020-05-08")

    def test_reversed_bounds_rejected(self, layout):
        _seed(layout, "dset", [_bar("AAPL", "2020-05-04T14:30:00+00:00")])
        with pytest.raises(GapDetectionError, match="window_start"):
            detect_gaps(layout, "dset", "2020-05-08", "2020-05-04")

    def test_missing_dataset_raises(self, layout):
        with pytest.raises(GapDetectionError, match="not found"):
            detect_gaps(
                layout, "no-such-dataset", "2020-05-04", "2020-05-08"
            )


# ---------------------------------------------------------------------------
# Ordering and serialization
# ---------------------------------------------------------------------------


class TestReportShape:
    def test_gaps_sorted_deterministically(self, layout):
        bars = [_bar("AAPL", "2020-05-04T14:30:00+00:00")]
        _seed(layout, "dset", bars, symbols=("AAPL", "MSFT", "NVDA"))
        r = detect_gaps(layout, "dset", "2020-05-04", "2020-05-05")
        pairs = [(g.symbol, g.date, g.kind) for g in r.gaps]
        assert pairs == sorted(pairs)

    def test_report_serializes(self, layout):
        _seed(layout, "dset", [_bar("AAPL", "2020-05-04T14:30:00+00:00")])
        r = detect_gaps(layout, "dset", "2020-05-04", "2020-05-08")
        d = r.to_dict()
        assert d["dataset_id"] == "dset"
        assert isinstance(d["gaps"], list)


class TestWriteReport:
    def test_lands_under_output_dir(self, layout, tmp_path):
        _seed(layout, "dset", [_bar("AAPL", "2020-05-04T14:30:00+00:00")])
        r = detect_gaps(layout, "dset", "2020-05-04", "2020-05-08")
        path = write_report(r, tmp_path / "gap-reports")
        assert path.is_file()
        assert (tmp_path / "gap-reports").is_dir()


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.gap_detection as module
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_or_live_runner_imports(self):
        source = _module_source()
        for token in (
            "submit_order", "place_order", "cancel_order",
            "close_position", "TradingClient",
            "from trader import", "import trader\n",
            "from crypto_trader import", "import crypto_trader",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_credential_env_reads(self):
        source = _module_source()
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
        ):
            assert not re.search(pattern, source)

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled(self, layout):
        flags = reset_feature_flags()
        _seed(layout, "dset", [_bar("AAPL", "2020-05-04T14:30:00+00:00")])
        detect_gaps(layout, "dset", "2020-05-04", "2020-05-08")
        assert flags.all_disabled is True
