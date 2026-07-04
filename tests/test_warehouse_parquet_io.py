"""Tests for strategy/warehouse/parquet_io.py.

Fourth Phase 5.6 implementation card ``t_phase56_parquet_storage``.
All filesystem work runs under pytest ``tmp_path``.

Coverage:

* Canonical schema shape (column order + dtypes + null constraints)
* Write / read roundtrip for daily, hourly, minute, second bars
* Partition key derivation and file naming
* Deterministic sort order + rejection of mixed interval /
  adjustment_mode batches
* Atomic write leaves no stray temp files
* sha256 + size + row count on the returned ``WrittenFile`` match
  the on-disk file
* ``scan_parquet_files`` returns portable relative paths
* Schema mismatch on read is refused
* Warehouse integration: files land under
  ``dataset_dir`` from the layout module
* Feature-flag invariance
* Source-safety (no order path, no live-runner imports, no
  provider plugins, no credential env reads, no
  ``ApprovalRecord`` / ``PromotionEntry`` construction)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List

import pyarrow.parquet as pq
import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.local_warehouse import (
    WarehouseIntegrityError,
    WarehouseLayout,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
)
from strategy.warehouse.parquet_io import (
    CANONICAL_COLUMNS,
    CANONICAL_SCHEMA,
    DEFAULT_COMPRESSION,
    ParquetStorageError,
    WrittenFile,
    read_bars,
    scan_parquet_files,
    write_bars,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    layout = WarehouseLayout(root=tmp_path / "warehouse")
    layout.create()
    return layout


def _daily_bar(
    ts: str,
    symbol: str = "AAPL",
    close: float = 100.5,
    adjustment: AdjustmentMode = AdjustmentMode.RAW,
    vwap=None,
    trade_count=None,
    adjustment_version: str = "",
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=100.0,
        high=101.0,
        low=99.5,
        close=close,
        volume=1_000_000,
        interval=BarInterval.DAILY,
        adjustment_mode=adjustment,
        vwap=vwap,
        trade_count=trade_count,
        adjustment_version=adjustment_version,
    )


def _bar_at(
    ts: str, interval: BarInterval, symbol: str = "AAPL"
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=100.0,
        high=101.0,
        low=99.5,
        close=100.5,
        volume=1_000_000,
        interval=interval,
        adjustment_mode=AdjustmentMode.RAW,
    )


# ---------------------------------------------------------------------------
# Canonical schema
# ---------------------------------------------------------------------------


class TestCanonicalSchema:
    def test_column_order_and_length(self):
        assert CANONICAL_COLUMNS == (
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
        assert len(CANONICAL_COLUMNS) == 12

    def test_schema_matches_columns(self):
        assert [f.name for f in CANONICAL_SCHEMA] == list(CANONICAL_COLUMNS)

    def test_non_nullable_fields(self):
        for name in (
            "symbol",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "interval",
            "adjustment_mode",
            "adjustment_version",
        ):
            assert CANONICAL_SCHEMA.field(name).nullable is False, (
                f"{name} must be non-nullable"
            )

    def test_nullable_fields(self):
        assert CANONICAL_SCHEMA.field("vwap").nullable is True
        assert CANONICAL_SCHEMA.field("trade_count").nullable is True

    def test_compression_is_zstd(self):
        assert DEFAULT_COMPRESSION == "zstd"


# ---------------------------------------------------------------------------
# Writer basics
# ---------------------------------------------------------------------------


class TestWriteBarsBasics:
    def test_empty_input_returns_empty_list(self, layout):
        written = write_bars(layout, [], AssetClass.EQUITY, dataset_id="d")
        assert written == []

    def test_writes_one_file_per_symbol_and_bucket(self, layout):
        bars = [
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2020-05-02T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2021-01-15T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="MSFT"),
        ]
        written = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="aapl-msft"
        )
        assert len(written) == 3  # AAPL/2020, AAPL/2021, MSFT/2020
        rels = {w.relative_path for w in written}
        assert (
            "equities/daily/aapl-msft/AAPL/2020.parquet" in rels
        )
        assert (
            "equities/daily/aapl-msft/AAPL/2021.parquet" in rels
        )
        assert (
            "equities/daily/aapl-msft/MSFT/2020.parquet" in rels
        )

    def test_written_file_row_and_byte_counts_match_disk(self, layout):
        bars = [
            _daily_bar("2020-05-01T14:30:00+00:00"),
            _daily_bar("2020-05-02T14:30:00+00:00"),
        ]
        written = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="aapl"
        )
        assert len(written) == 1
        w = written[0]
        assert w.row_count == 2
        assert w.size_bytes == Path(w.absolute_path).stat().st_size

    def test_sha256_matches_disk(self, layout):
        bars = [_daily_bar("2020-05-01T14:30:00+00:00")]
        w = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="aapl"
        )[0]
        h = hashlib.sha256(Path(w.absolute_path).read_bytes()).hexdigest()
        assert w.sha256 == h

    def test_mixed_intervals_rejected(self, layout):
        bars = [
            _bar_at("2020-05-01T14:30:00+00:00", BarInterval.DAILY),
            _bar_at("2020-05-01T14:30:00+00:00", BarInterval.HOURLY),
        ]
        with pytest.raises(ParquetStorageError, match="single interval"):
            write_bars(layout, bars, AssetClass.EQUITY, dataset_id="d")

    def test_mixed_adjustment_modes_rejected(self, layout):
        bars = [
            _daily_bar(
                "2020-05-01T14:30:00+00:00", adjustment=AdjustmentMode.RAW
            ),
            _daily_bar(
                "2020-05-02T14:30:00+00:00",
                adjustment=AdjustmentMode.SPLIT_DIVIDEND,
            ),
        ]
        with pytest.raises(
            ParquetStorageError, match="single adjustment_mode"
        ):
            write_bars(layout, bars, AssetClass.EQUITY, dataset_id="d")

    def test_bad_timestamp_prefix_rejected(self, layout):
        # Timestamp missing YYYY-MM-DD prefix
        bar = Bar(
            symbol="AAPL",
            timestamp="20260501T14:30:00Z",  # compact form -> rejected
            open=100.0, high=101.0, low=99.5, close=100.5, volume=1,
            interval=BarInterval.DAILY,
            adjustment_mode=AdjustmentMode.RAW,
        )
        with pytest.raises(ParquetStorageError, match="YYYY-MM-DD"):
            write_bars(
                layout, [bar], AssetClass.EQUITY, dataset_id="d"
            )

    def test_atomic_write_leaves_no_stray_temp_files(self, layout):
        bars = [_daily_bar("2020-05-01T14:30:00+00:00")]
        written = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="d"
        )
        parent = Path(written[0].absolute_path).parent
        stray = [
            p for p in parent.iterdir()
            if p.name.startswith(".") or p.suffix == ".tmp"
        ]
        assert stray == []


# ---------------------------------------------------------------------------
# Partition strategy
# ---------------------------------------------------------------------------


class TestPartitionStrategy:
    def test_daily_bucket_is_year(self, layout):
        w = write_bars(
            layout,
            [_daily_bar("2020-05-01T14:30:00+00:00")],
            AssetClass.EQUITY,
            dataset_id="d",
        )
        assert w[0].time_bucket == "2020"
        assert w[0].relative_path.endswith("/2020.parquet")

    def test_hourly_bucket_is_year_month(self, layout):
        bar = _bar_at("2020-05-15T14:30:00+00:00", BarInterval.HOURLY)
        w = write_bars(layout, [bar], AssetClass.EQUITY, dataset_id="d")
        assert w[0].time_bucket == "2020-05"
        assert w[0].relative_path.endswith("/2020-05.parquet")

    def test_minute_bucket_is_year_month_day(self, layout):
        bar = _bar_at("2020-05-15T14:30:00+00:00", BarInterval.MINUTE_1)
        w = write_bars(layout, [bar], AssetClass.EQUITY, dataset_id="d")
        assert w[0].time_bucket == "2020-05-15"
        assert w[0].relative_path.endswith("/2020-05-15.parquet")

    def test_second_bucket_is_year_month_day(self, layout):
        bar = _bar_at("2020-05-15T14:30:00+00:00", BarInterval.SECOND_1)
        w = write_bars(layout, [bar], AssetClass.EQUITY, dataset_id="d")
        assert w[0].time_bucket == "2020-05-15"

    def test_all_minute_intervals_share_daily_partition(self, layout):
        for interval in (
            BarInterval.MINUTE_1,
            BarInterval.MINUTE_5,
            BarInterval.MINUTE_15,
            BarInterval.MINUTE_30,
        ):
            bar = _bar_at("2020-05-15T14:30:00+00:00", interval)
            w = write_bars(
                layout, [bar], AssetClass.EQUITY,
                dataset_id=f"d-{interval.name.lower()}",
            )
            assert w[0].time_bucket == "2020-05-15"

    def test_asset_class_routes_dataset_dir(self, layout):
        # ETF routes to equities/
        bar = _daily_bar("2020-05-01T14:30:00+00:00", symbol="SPY")
        w = write_bars(
            layout, [bar], AssetClass.ETF, dataset_id="spy"
        )
        assert w[0].relative_path.startswith("equities/daily/spy/")

        # Crypto routes to crypto/
        bar = _daily_bar("2020-05-01T14:30:00+00:00", symbol="BTCUSD")
        w = write_bars(
            layout, [bar], AssetClass.CRYPTO, dataset_id="btc"
        )
        assert w[0].relative_path.startswith("crypto/daily/btc/")

    def test_no_dataset_id_lands_under_asset_interval_root(self, layout):
        # ``dataset_id=""`` writes under
        # ``<asset>/<interval>/<symbol>/<bucket>.parquet``
        bar = _daily_bar("2020-05-01T14:30:00+00:00")
        w = write_bars(layout, [bar], AssetClass.EQUITY, dataset_id="")
        assert w[0].relative_path == "equities/daily/AAPL/2020.parquet"


# ---------------------------------------------------------------------------
# Reader roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_daily_roundtrip_preserves_row_content(self, layout):
        bars = [
            _daily_bar(
                "2020-05-01T14:30:00+00:00", vwap=100.25, trade_count=42,
                adjustment_version="alpaca:2026-07-15",
            ),
            _daily_bar(
                "2020-05-02T14:30:00+00:00", vwap=None, trade_count=None,
            ),
        ]
        written = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="d"
        )
        read_back = read_bars(layout, [w.relative_path for w in written])
        assert len(read_back) == 2
        # Sorted by (symbol, timestamp)
        assert read_back[0].timestamp == "2020-05-01T14:30:00+00:00"
        assert read_back[0].vwap == 100.25
        assert read_back[0].trade_count == 42
        assert read_back[0].adjustment_version == "alpaca:2026-07-15"
        assert read_back[1].vwap is None
        assert read_back[1].trade_count is None
        # Interval + adjustment_mode round-trip as enums
        assert read_back[0].interval is BarInterval.DAILY
        assert read_back[0].adjustment_mode is AdjustmentMode.RAW

    def test_read_bars_returns_symbol_then_timestamp_sorted(self, layout):
        bars = [
            _daily_bar("2020-05-02T14:30:00+00:00", symbol="MSFT"),
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="MSFT"),
            _daily_bar("2020-05-02T14:30:00+00:00", symbol="AAPL"),
        ]
        w = write_bars(layout, bars, AssetClass.EQUITY, dataset_id="d")
        read_back = read_bars(layout, [f.relative_path for f in w])
        pairs = [(b.symbol, b.timestamp) for b in read_back]
        assert pairs == sorted(pairs)

    def test_missing_file_raises(self, layout):
        with pytest.raises(ParquetStorageError, match="missing Parquet"):
            read_bars(layout, ["equities/daily/AAPL/nonexistent.parquet"])

    def test_empty_relative_paths_returns_empty_list(self, layout):
        assert read_bars(layout, []) == []

    def test_hourly_and_minute_roundtrip(self, layout):
        for interval in (
            BarInterval.HOURLY,
            BarInterval.MINUTE_1,
            BarInterval.MINUTE_15,
        ):
            bar = _bar_at("2020-05-15T14:30:00+00:00", interval)
            w = write_bars(
                layout, [bar], AssetClass.EQUITY,
                dataset_id=f"d-{interval.name.lower()}",
            )
            [restored] = read_bars(layout, [w[0].relative_path])
            assert restored.interval is interval


# ---------------------------------------------------------------------------
# Schema enforcement on read
# ---------------------------------------------------------------------------


class TestSchemaEnforcement:
    def test_reader_refuses_file_with_mismatched_schema(
        self, tmp_path, layout
    ):
        # Handcraft a parquet file with a different column set.
        import pyarrow as pa
        wrong = pa.Table.from_pydict(
            {
                "wrong_column": ["x"],
            }
        )
        target = layout.root / "equities" / "daily" / "d" / "AAPL"
        target.mkdir(parents=True, exist_ok=True)
        pq.write_table(wrong, str(target / "2020.parquet"))
        with pytest.raises(ParquetStorageError, match="schema mismatch"):
            read_bars(layout, ["equities/daily/d/AAPL/2020.parquet"])


# ---------------------------------------------------------------------------
# scan_parquet_files
# ---------------------------------------------------------------------------


class TestScanParquetFiles:
    def test_empty_partition_returns_empty_list(self, layout):
        assert scan_parquet_files(
            layout, AssetClass.EQUITY, BarInterval.DAILY, dataset_id="none"
        ) == []

    def test_lists_all_files_under_partition(self, layout):
        bars = [
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2021-05-01T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="MSFT"),
        ]
        write_bars(layout, bars, AssetClass.EQUITY, dataset_id="d")
        listed = scan_parquet_files(
            layout, AssetClass.EQUITY, BarInterval.DAILY, dataset_id="d"
        )
        assert listed == [
            "equities/daily/d/AAPL/2020.parquet",
            "equities/daily/d/AAPL/2021.parquet",
            "equities/daily/d/MSFT/2020.parquet",
        ]

    def test_symbol_filter_restricts_scan(self, layout):
        bars = [
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="AAPL"),
            _daily_bar("2020-05-01T14:30:00+00:00", symbol="MSFT"),
        ]
        write_bars(layout, bars, AssetClass.EQUITY, dataset_id="d")
        listed = scan_parquet_files(
            layout, AssetClass.EQUITY, BarInterval.DAILY,
            dataset_id="d", symbol="AAPL",
        )
        assert listed == ["equities/daily/d/AAPL/2020.parquet"]

    def test_empty_symbol_filter_rejected(self, layout):
        with pytest.raises(ParquetStorageError, match="symbol"):
            scan_parquet_files(
                layout, AssetClass.EQUITY, BarInterval.DAILY,
                dataset_id="d", symbol="",
            )


# ---------------------------------------------------------------------------
# Warehouse integration
# ---------------------------------------------------------------------------


class TestWarehouseIntegration:
    def test_files_land_under_dataset_dir_from_layout(self, layout):
        bars = [_daily_bar("2020-05-01T14:30:00+00:00")]
        w = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="aapl"
        )
        dataset_dir = layout.dataset_dir(
            "aapl", AssetClass.EQUITY, BarInterval.DAILY
        )
        assert Path(w[0].absolute_path).is_relative_to(dataset_dir)

    def test_dataset_id_traversal_rejected(self, layout):
        # WarehouseLayout guards against path-traversal ids
        bars = [_daily_bar("2020-05-01T14:30:00+00:00")]
        with pytest.raises(WarehouseIntegrityError):
            write_bars(
                layout, bars, AssetClass.EQUITY, dataset_id="../evil"
            )


# ---------------------------------------------------------------------------
# Feature-flag invariance
# ---------------------------------------------------------------------------


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled_after_write_and_read(self, layout):
        flags = reset_feature_flags()
        bars = [_daily_bar("2020-05-01T14:30:00+00:00")]
        w = write_bars(layout, bars, AssetClass.EQUITY, dataset_id="d")
        read_bars(layout, [f.relative_path for f in w])
        scan_parquet_files(
            layout, AssetClass.EQUITY, BarInterval.DAILY, dataset_id="d"
        )
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_flags_stay_disabled_after_module_import(self):
        flags = reset_feature_flags()
        import strategy.warehouse.parquet_io  # noqa: F401
        assert flags.all_disabled is True


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.parquet_io as module
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_references(self):
        source = _module_source()
        for token in [
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "close_all_positions",
            "create_order",
            "replace_order",
            "TradingClient",
        ]:
            assert token not in source, (
                f"parquet_io must not reference {token!r}"
            )

    def test_no_live_runner_imports(self):
        source = _module_source()
        for token in [
            "from trader import",
            "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "import trader_cli",
            "from telegram_approvals import",
            "import telegram_approvals",
            "from strategy.runner import",
            "import strategy.runner",
        ]:
            assert token not in source, (
                f"parquet_io must not import {token!r}"
            )

    def test_no_provider_credential_env_reads(self):
        source = _module_source()
        for pattern in [
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
            r'os\.environ\[\s*[\'"]CRYPTO_ALPACA_',
        ]:
            assert not re.search(pattern, source), (
                f"parquet_io must not read from provider credentials: {pattern!r}"
            )

    def test_no_yfinance_or_pandas(self):
        source = _module_source()
        assert "import yfinance" not in source
        assert "import pandas" not in source
        assert "yf.download" not in source

    def test_no_approval_record_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source

    def test_no_promotion_entry_construction(self):
        source = _module_source()
        assert "PromotionEntry(" not in source

    def test_no_provider_plugin_imports(self):
        source = _module_source()
        for token in [
            "from strategy.providers",
            "import strategy.providers",
            "alpaca_trade_api",
            "polygon",
            "databento",
            "tiingo",
        ]:
            assert token not in source, (
                f"parquet_io must not depend on provider plugin: {token!r}"
            )

    def test_import_does_not_pull_in_live_runner(self):
        for name in ("strategy.warehouse.parquet_io", "strategy.warehouse", "strategy"):
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.warehouse.parquet_io  # noqa: F401
        added = set(sys.modules) - before
        forbidden = {
            "trader",
            "trader_cli",
            "crypto_trader",
            "telegram_approvals",
        }
        assert not (added & forbidden), (
            f"forbidden imports pulled in: {added & forbidden}"
        )
