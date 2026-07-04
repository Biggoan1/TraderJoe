"""Tests for strategy/warehouse/versioning.py.

Ninth Phase 5.6 card ``t_phase56_data_versioning``.

Coverage:

* Lineage traversal: version_chain, latest_version,
  parent_manifest across roots and children
* derive_next_version: increments corporate_action_version,
  sets parent, clears files, resets validation_status,
  preserves other fields
* Suffix rules for dataset_id derivation
* compare_versions: added / removed / changed classification,
  deterministic ordering
* Interval / asset_class mismatch rejected
* refuse_if_validated raises for validated datasets
* Never mutates parent manifest
* Source safety + feature-flag invariance
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.data_catalog import DataCatalog, DatasetFile, DatasetManifest
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    WarehouseLayout,
    write_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
)
from strategy.warehouse.parquet_io import write_bars
from strategy.warehouse.versioning import (
    BarDiff,
    VersionDiff,
    VersionNode,
    VersioningError,
    compare_versions,
    derive_next_version,
    latest_version,
    parent_manifest,
    refuse_if_validated,
    version_chain,
)


def _bar(sym: str, ts: str, close: float = 100.0) -> Bar:
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


def _write_dataset(
    layout: WarehouseLayout,
    dataset_id: str,
    bars: List[Bar],
    parent_dataset_id: str = "",
    corporate_action_version: str = "1",
    validation_status: str = STATUS_UNVALIDATED,
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
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        kind="historical_bars",
        symbols=tuple(sorted({b.symbol for b in bars})),
        start_date=min(b.timestamp for b in bars),
        end_date=max(b.timestamp for b in bars),
        files=files,
        provider="alpaca",
        interval="1Day",
        asset_class="equity",
        adjustment_mode="raw",
        adjustment_version="alpaca:v1",
        corporate_action_version=corporate_action_version,
        parent_dataset_id=parent_dataset_id,
        validation_status=validation_status,
    )
    write_manifest(layout, dataset_id, manifest.to_dict(), force=True)
    return manifest


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    layout = WarehouseLayout(root=tmp_path / "wh")
    layout.create()
    return layout


@pytest.fixture
def three_versions(layout: WarehouseLayout) -> DataCatalog:
    _write_dataset(
        layout, "aapl-2020",
        [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
        corporate_action_version="1",
        validation_status=STATUS_VALIDATED,
    )
    _write_dataset(
        layout, "aapl-2020-v2",
        [_bar("AAPL", "2020-05-01T14:30:00+00:00", 25.0)],
        parent_dataset_id="aapl-2020",
        corporate_action_version="2",
    )
    _write_dataset(
        layout, "aapl-2020-v3",
        [_bar("AAPL", "2020-05-01T14:30:00+00:00", 12.5)],
        parent_dataset_id="aapl-2020",
        corporate_action_version="3",
    )
    return DataCatalog.from_warehouse_layout(layout)


# ---------------------------------------------------------------------------
# Lineage traversal
# ---------------------------------------------------------------------------


class TestLineageTraversal:
    def test_version_chain_orders_by_version(self, three_versions):
        chain = version_chain(three_versions, "aapl-2020")
        assert [n.dataset_id for n in chain] == [
            "aapl-2020", "aapl-2020-v2", "aapl-2020-v3",
        ]

    def test_latest_version_returns_v3(self, three_versions):
        latest = latest_version(three_versions, "aapl-2020")
        assert latest is not None
        assert latest.dataset_id == "aapl-2020-v3"
        assert latest.corporate_action_version == "3"

    def test_latest_version_none_when_missing(self, three_versions):
        assert latest_version(three_versions, "no-such") is None

    def test_parent_manifest_returns_root(self, three_versions):
        parent = parent_manifest(three_versions, "aapl-2020-v2")
        assert parent is not None
        assert parent.dataset_id == "aapl-2020"

    def test_parent_manifest_root_has_no_parent(self, three_versions):
        assert parent_manifest(three_versions, "aapl-2020") is None

    def test_parent_manifest_missing_dataset_raises(self, three_versions):
        with pytest.raises(VersioningError, match="not found"):
            parent_manifest(three_versions, "no-such-dataset")


# ---------------------------------------------------------------------------
# derive_next_version
# ---------------------------------------------------------------------------


class TestDeriveNextVersion:
    def test_bumps_version_from_root(self, layout):
        root = _write_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00")],
            corporate_action_version="1",
        )
        payload = derive_next_version(root)
        assert payload["dataset_id"] == "aapl-2020-v2"
        assert payload["parent_dataset_id"] == "aapl-2020"
        assert payload["corporate_action_version"] == "2"
        assert payload["validation_status"] == STATUS_UNVALIDATED
        assert payload["files"] == []

    def test_bumps_from_existing_v_suffix(self, layout):
        v2 = _write_dataset(
            layout, "aapl-2020-v2",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00")],
            parent_dataset_id="aapl-2020",
            corporate_action_version="2",
        )
        payload = derive_next_version(v2)
        assert payload["dataset_id"] == "aapl-2020-v3"
        assert payload["parent_dataset_id"] == "aapl-2020-v2"

    def test_carries_over_provider_fields(self, layout):
        root = _write_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00")],
            corporate_action_version="1",
        )
        payload = derive_next_version(
            root, adjustment_version="alpaca:v2",
        )
        assert payload["provider"] == "alpaca"
        assert payload["asset_class"] == "equity"
        assert payload["interval"] == "1Day"
        assert payload["adjustment_version"] == "alpaca:v2"

    def test_explicit_corporate_action_version_used(self, layout):
        root = _write_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00")],
            corporate_action_version="1",
        )
        payload = derive_next_version(
            root, corporate_action_version="7",
        )
        assert payload["corporate_action_version"] == "7"
        assert payload["dataset_id"] == "aapl-2020-v7"

    def test_never_mutates_parent(self, layout):
        root = _write_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00")],
            corporate_action_version="1",
        )
        before = root.to_dict()
        derive_next_version(root)
        after = root.to_dict()
        assert before == after

    def test_notes_from_revision_source(self, layout):
        root = _write_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00")],
            corporate_action_version="1",
        )
        payload = derive_next_version(
            root, revision_source="alpaca late split 2020-06-01",
        )
        assert "late split" in payload["notes"]


# ---------------------------------------------------------------------------
# compare_versions
# ---------------------------------------------------------------------------


class TestCompareVersions:
    def test_identifies_changed_bar(self, three_versions, layout):
        diff = compare_versions(
            layout, three_versions, "aapl-2020", "aapl-2020-v2"
        )
        assert diff.total_changed == 1
        assert diff.total_added == 0
        assert diff.total_removed == 0
        [change] = diff.changes
        assert change.kind == "changed"
        assert change.old_close == 100.0
        assert change.new_close == 25.0

    def test_identifies_added_row(self, layout):
        _write_dataset(
            layout, "root",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
            corporate_action_version="1",
        )
        _write_dataset(
            layout, "root-v2",
            [
                _bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0),
                _bar("AAPL", "2020-05-02T14:30:00+00:00", 101.0),
            ],
            parent_dataset_id="root",
            corporate_action_version="2",
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        diff = compare_versions(layout, catalog, "root", "root-v2")
        assert diff.total_added == 1
        assert diff.changes[0].kind == "added"
        assert diff.changes[0].timestamp.startswith("2020-05-02")

    def test_identifies_removed_row(self, layout):
        _write_dataset(
            layout, "root",
            [
                _bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0),
                _bar("AAPL", "2020-05-02T14:30:00+00:00", 101.0),
            ],
            corporate_action_version="1",
        )
        _write_dataset(
            layout, "root-v2",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
            parent_dataset_id="root",
            corporate_action_version="2",
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        diff = compare_versions(layout, catalog, "root", "root-v2")
        assert diff.total_removed == 1
        [c] = diff.changes
        assert c.kind == "removed"

    def test_no_diff_when_identical(self, layout):
        _write_dataset(
            layout, "dataset-a",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
        )
        _write_dataset(
            layout, "dataset-b",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        diff = compare_versions(layout, catalog, "dataset-a", "dataset-b")
        assert diff.total_changed == 0
        assert diff.total_added == 0
        assert diff.total_removed == 0
        assert diff.changes == ()

    def test_interval_mismatch_rejected(self, layout):
        _write_dataset(
            layout, "daily",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
        )
        # Write a bogus manifest with mismatched interval
        payload = json.loads(
            layout.manifest_path("daily").read_text()
        )
        payload_hourly = dict(payload)
        payload_hourly["dataset_id"] = "hourly"
        payload_hourly["interval"] = "1Hour"
        # For a new dataset_id we need to write it via the layout
        # but keep the bars valid; we don't need real bars for the
        # rejection check — just a manifest whose files list is
        # non-empty.  Reuse the daily files.
        write_manifest(layout, "hourly", payload_hourly, force=True)
        catalog = DataCatalog.from_warehouse_layout(layout)
        with pytest.raises(VersioningError, match="interval mismatch"):
            compare_versions(layout, catalog, "daily", "hourly")

    def test_missing_dataset_rejected(self, layout):
        _write_dataset(
            layout, "dataset-a",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        with pytest.raises(VersioningError, match="not in catalog"):
            compare_versions(layout, catalog, "dataset-a", "no-such")

    def test_diff_ordering_deterministic(self, layout):
        _write_dataset(
            layout, "dataset-a",
            [
                _bar("MSFT", "2020-05-01T14:30:00+00:00", 200.0),
                _bar("AAPL", "2020-05-02T14:30:00+00:00", 100.0),
            ],
        )
        _write_dataset(
            layout, "dataset-b",
            [
                _bar("MSFT", "2020-05-01T14:30:00+00:00", 250.0),
                _bar("AAPL", "2020-05-02T14:30:00+00:00", 150.0),
            ],
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        diff = compare_versions(layout, catalog, "dataset-a", "dataset-b")
        pairs = [(c.symbol, c.timestamp) for c in diff.changes]
        assert pairs == sorted(pairs)


# ---------------------------------------------------------------------------
# refuse_if_validated
# ---------------------------------------------------------------------------


class TestRefuseIfValidated:
    def test_raises_for_validated(self, layout):
        _write_dataset(
            layout, "dataset-a",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
            validation_status=STATUS_VALIDATED,
        )
        with pytest.raises(VersioningError, match="validated"):
            refuse_if_validated(layout, "dataset-a")

    def test_passes_for_unvalidated(self, layout):
        _write_dataset(
            layout, "dataset-a",
            [_bar("AAPL", "2020-05-01T14:30:00+00:00", 100.0)],
        )
        refuse_if_validated(layout, "dataset-a")  # no raise

    def test_passes_for_missing_dataset(self, layout):
        refuse_if_validated(layout, "no-such")  # no raise


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.versioning as module
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
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, source)

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled(self, three_versions, layout):
        flags = reset_feature_flags()
        version_chain(three_versions, "aapl-2020")
        latest_version(three_versions, "aapl-2020")
        compare_versions(
            layout, three_versions, "aapl-2020", "aapl-2020-v2"
        )
        assert flags.all_disabled is True
