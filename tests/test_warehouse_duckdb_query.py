"""Tests for strategy/warehouse/duckdb_query.py.

Eighth Phase 5.6 implementation card ``t_phase56_duckdb_queries``.
All filesystem work under pytest ``tmp_path``.

Coverage:

* ``scan_bars`` returns canonical Bar records sorted by
  (symbol, timestamp)
* Interval + asset-class routing (partition scoping)
* Symbol / start / end filtering
* Empty partition returns empty list without raising
* ``coverage_summary`` returns None for missing partition,
  populated summary otherwise
* ``latest_bar`` returns the most recent bar per symbol
* ``row_counts_by_symbol`` matches the on-disk row count
* ``ohlcv_aggregate`` matches hand-computed statistics
* Range validation (empty bounds / reversed bounds rejected)
* Context manager closes the connection
* File-backed DuckDB persists across sessions
* Source safety + feature-flag invariance
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
)
from strategy.warehouse.duckdb_query import (
    CoverageSummary,
    DuckDBQueryError,
    OHLCVAggregate,
    WarehouseQueryReader,
    open_reader,
)
from strategy.warehouse.parquet_io import write_bars


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bar(sym: str, ts: str, close: float = 100.5) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=ts,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000_000,
        interval=BarInterval.DAILY,
        adjustment_mode=AdjustmentMode.RAW,
    )


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    layout = WarehouseLayout(root=tmp_path / "wh")
    layout.create()
    return layout


@pytest.fixture
def seeded(layout: WarehouseLayout) -> WarehouseLayout:
    write_bars(
        layout,
        [
            _bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0),
            _bar("AAPL", "2020-05-02T14:30:00+00:00", 102.0),
            _bar("AAPL", "2020-05-03T14:30:00+00:00", 101.0),
            _bar("MSFT", "2020-05-01T14:30:00+00:00", 200.0),
            _bar("MSFT", "2020-05-02T14:30:00+00:00", 205.0),
        ],
        AssetClass.EQUITY,
        dataset_id="d",
    )
    return layout


# ---------------------------------------------------------------------------
# scan_bars
# ---------------------------------------------------------------------------


class TestScanBars:
    def test_returns_bars_in_sorted_order(self, seeded):
        with open_reader(seeded) as reader:
            bars = reader.scan_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL", "MSFT"],
                "2020-05-01", "2020-05-31",
                dataset_id="d",
            )
        assert len(bars) == 5
        pairs = [(b.symbol, b.timestamp) for b in bars]
        assert pairs == sorted(pairs)

    def test_filters_by_symbol(self, seeded):
        with open_reader(seeded) as reader:
            bars = reader.scan_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"],
                "2020-05-01", "2020-05-31",
                dataset_id="d",
            )
        assert {b.symbol for b in bars} == {"AAPL"}

    def test_filters_by_window(self, seeded):
        with open_reader(seeded) as reader:
            bars = reader.scan_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                [], "2020-05-02T00:00:00+00:00", "2020-05-02T23:59:59+00:00",
                dataset_id="d",
            )
        assert len(bars) == 2
        assert all(b.timestamp.startswith("2020-05-02") for b in bars)

    def test_returns_canonical_bar_objects(self, seeded):
        with open_reader(seeded) as reader:
            bars = reader.scan_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"],
                "2020-05-01T00:00:00+00:00",
                "2020-05-01T23:59:59+00:00",
                dataset_id="d",
            )
        assert len(bars) == 1
        b = bars[0]
        assert isinstance(b, Bar)
        assert b.interval is BarInterval.DAILY
        assert b.adjustment_mode is AdjustmentMode.RAW

    def test_empty_partition_returns_empty_list(self, layout):
        with open_reader(layout) as reader:
            bars = reader.scan_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-01", "2020-05-31",
                dataset_id="none",
            )
        assert bars == []

    def test_rejects_empty_bounds(self, seeded):
        with open_reader(seeded) as reader:
            with pytest.raises(DuckDBQueryError, match="required"):
                reader.scan_bars(
                    AssetClass.EQUITY, BarInterval.DAILY,
                    [], "", "2020-05-31", dataset_id="d",
                )

    def test_rejects_reversed_window(self, seeded):
        with open_reader(seeded) as reader:
            with pytest.raises(DuckDBQueryError, match="start"):
                reader.scan_bars(
                    AssetClass.EQUITY, BarInterval.DAILY,
                    [], "2020-05-31", "2020-05-01", dataset_id="d",
                )


# ---------------------------------------------------------------------------
# coverage_summary
# ---------------------------------------------------------------------------


class TestCoverageSummary:
    def test_returns_none_when_partition_missing(self, layout):
        with open_reader(layout) as reader:
            assert reader.coverage_summary(
                AssetClass.EQUITY, BarInterval.DAILY, "none"
            ) is None

    def test_populated_summary_matches_data(self, seeded):
        with open_reader(seeded) as reader:
            cov = reader.coverage_summary(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
        assert cov is not None
        assert cov.row_count == 5
        assert cov.symbols == ("AAPL", "MSFT")
        assert cov.first_timestamp.startswith("2020-05-01")
        assert cov.last_timestamp.startswith("2020-05-03")
        assert cov.file_count > 0
        assert cov.interval == "1Day"
        assert cov.asset_class == "equity"

    def test_to_dict_serializes(self, seeded):
        with open_reader(seeded) as reader:
            cov = reader.coverage_summary(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
        d = cov.to_dict()
        assert d["row_count"] == 5
        assert d["symbols"] == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# latest_bar
# ---------------------------------------------------------------------------


class TestLatestBar:
    def test_returns_most_recent_bar(self, seeded):
        with open_reader(seeded) as reader:
            latest = reader.latest_bar(
                AssetClass.EQUITY, BarInterval.DAILY, "AAPL",
                dataset_id="d",
            )
        assert latest is not None
        assert latest.timestamp.startswith("2020-05-03")
        assert latest.symbol == "AAPL"

    def test_returns_none_for_missing_symbol(self, seeded):
        with open_reader(seeded) as reader:
            assert reader.latest_bar(
                AssetClass.EQUITY, BarInterval.DAILY, "NVDA",
                dataset_id="d",
            ) is None

    def test_returns_none_for_missing_partition(self, layout):
        with open_reader(layout) as reader:
            assert reader.latest_bar(
                AssetClass.EQUITY, BarInterval.DAILY, "AAPL",
                dataset_id="none",
            ) is None

    def test_rejects_empty_symbol(self, seeded):
        with open_reader(seeded) as reader:
            with pytest.raises(DuckDBQueryError, match="symbol"):
                reader.latest_bar(
                    AssetClass.EQUITY, BarInterval.DAILY, "",
                    dataset_id="d",
                )


# ---------------------------------------------------------------------------
# row_counts_by_symbol
# ---------------------------------------------------------------------------


class TestRowCountsBySymbol:
    def test_matches_on_disk_counts(self, seeded):
        with open_reader(seeded) as reader:
            counts = reader.row_counts_by_symbol(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
        assert counts == {"AAPL": 3, "MSFT": 2}

    def test_empty_partition_returns_empty_dict(self, layout):
        with open_reader(layout) as reader:
            assert reader.row_counts_by_symbol(
                AssetClass.EQUITY, BarInterval.DAILY, "none"
            ) == {}


# ---------------------------------------------------------------------------
# ohlcv_aggregate
# ---------------------------------------------------------------------------


class TestOHLCVAggregate:
    def test_per_symbol_aggregates(self, seeded):
        with open_reader(seeded) as reader:
            aggs = reader.ohlcv_aggregate(
                AssetClass.EQUITY, BarInterval.DAILY,
                [], "2020-05-01", "2020-05-31",
                dataset_id="d",
            )
        assert [a.symbol for a in aggs] == ["AAPL", "MSFT"]
        aapl = aggs[0]
        # AAPL closes: 100, 102, 101 -> mean 101
        assert aapl.row_count == 3
        assert abs(aapl.mean_close - 101.0) < 0.01
        assert abs(aapl.total_volume - 3_000_000.0) < 0.01

    def test_filters_by_symbol_list(self, seeded):
        with open_reader(seeded) as reader:
            aggs = reader.ohlcv_aggregate(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-01", "2020-05-31",
                dataset_id="d",
            )
        assert [a.symbol for a in aggs] == ["AAPL"]

    def test_symbols_with_no_rows_in_window_omitted(self, seeded):
        with open_reader(seeded) as reader:
            aggs = reader.ohlcv_aggregate(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["NVDA"], "2020-05-01", "2020-05-31",
                dataset_id="d",
            )
        assert aggs == []

    def test_empty_partition_returns_empty_list(self, layout):
        with open_reader(layout) as reader:
            aggs = reader.ohlcv_aggregate(
                AssetClass.EQUITY, BarInterval.DAILY,
                [], "2020-05-01", "2020-05-31",
                dataset_id="none",
            )
        assert aggs == []


# ---------------------------------------------------------------------------
# Persistence and context manager
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    def test_context_manager_closes(self, layout):
        with open_reader(layout) as reader:
            pass  # closes automatically
        # A second query on a closed conn would fail; we can't
        # directly test the flag but repeating the context works
        with open_reader(layout) as reader:
            reader.row_counts_by_symbol(
                AssetClass.EQUITY, BarInterval.DAILY, "none"
            )

    def test_file_backed_db_reopens(self, seeded, tmp_path):
        db = tmp_path / "wh.duckdb"
        with open_reader(seeded, db_path=db) as reader:
            counts_a = reader.row_counts_by_symbol(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
        # Reopen and read again
        with open_reader(seeded, db_path=db) as reader:
            counts_b = reader.row_counts_by_symbol(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
        assert counts_a == counts_b


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.duckdb_query as module
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_or_live_runner_imports(self):
        source = _module_source()
        for token in (
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "TradingClient",
            "from trader import",
            "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_provider_credential_env_reads(self):
        source = _module_source()
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, source)

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled_after_queries(self, seeded):
        flags = reset_feature_flags()
        with open_reader(seeded) as reader:
            reader.scan_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-01", "2020-05-31", "d",
            )
            reader.coverage_summary(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
            reader.latest_bar(
                AssetClass.EQUITY, BarInterval.DAILY, "AAPL", "d"
            )
            reader.row_counts_by_symbol(
                AssetClass.EQUITY, BarInterval.DAILY, "d"
            )
            reader.ohlcv_aggregate(
                AssetClass.EQUITY, BarInterval.DAILY,
                [], "2020-05-01", "2020-05-31", "d",
            )
        assert flags.all_disabled is True
