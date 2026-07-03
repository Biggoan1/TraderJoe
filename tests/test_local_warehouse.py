"""Tests for strategy/local_warehouse.py.

Foundation card ``t_phase56_local_warehouse``.  All filesystem
operations run under pytest's ``tmp_path`` fixture so the tests
never touch the repository's ``market_data/`` tree.

Coverage:

* ``WarehouseLayout`` accessors — every canonical subdirectory
  path is derived from ``root``
* ``create()`` builds every :data:`REQUIRED_SUBDIRS` entry and is
  idempotent
* ``verify()`` accepts a well-formed tree and rejects a broken one
  (missing dir, or a file where a directory belongs)
* Path helpers (``dataset_dir`` / ``manifest_path`` /
  ``version_dir``) are deterministic
* Manifest writes are atomic and refuse to overwrite validated
  datasets without ``force=True``
* ``is_validated`` correctly reads the manifest status field
* ``refuse_overwrite_of_validated`` gates every write path
* Source-safety scan for order-path tokens, live-runner imports,
  provider credential env reads, ``ApprovalRecord`` construction,
  ``PromotionEntry`` construction
* Global :class:`~strategy.config.FeatureFlags` untouched after
  every operation
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.local_warehouse import (
    DEFAULT_WAREHOUSE_ROOT,
    KNOWN_STATUSES,
    REQUIRED_SUBDIRS,
    STATUS_QUARANTINED,
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    STATUS_VALIDATING,
    WAREHOUSE_ROOT_ENV,
    WarehouseIntegrityError,
    WarehouseLayout,
    is_validated,
    read_manifest,
    refuse_overwrite_of_validated,
    write_manifest,
)
from strategy.market_data_provider import AssetClass, BarInterval


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    return WarehouseLayout(root=tmp_path / "warehouse")


@pytest.fixture
def prepared(layout: WarehouseLayout) -> WarehouseLayout:
    layout.create()
    return layout


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_required_subdirs_covers_the_card_list(self):
        # The card lists the required directories explicitly; assert
        # each one is present.
        expected = {
            "equities/daily",
            "equities/hourly",
            "equities/minute",
            "crypto",
            "options",
            "metadata",
            "manifests",
            "versions",
        }
        assert set(REQUIRED_SUBDIRS) == expected

    def test_known_statuses_include_the_four_lifecycle_states(self):
        assert set(KNOWN_STATUSES) == {
            STATUS_UNVALIDATED,
            STATUS_VALIDATING,
            STATUS_VALIDATED,
            STATUS_QUARANTINED,
        }

    def test_default_root_is_market_data(self):
        assert DEFAULT_WAREHOUSE_ROOT == "market_data"

    def test_env_name_uses_warehouse_prefix(self):
        # Namespace disjoint from ALPACA_*, APCA_*, RESEARCH_ALPACA_*,
        # CRYPTO_ALPACA_*.
        assert WAREHOUSE_ROOT_ENV.startswith("WAREHOUSE_")


# ---------------------------------------------------------------------------
# Layout construction
# ---------------------------------------------------------------------------


class TestWarehouseLayout:
    def test_default_uses_default_root(self):
        assert WarehouseLayout.default().root == Path(DEFAULT_WAREHOUSE_ROOT)

    def test_from_env_reads_warehouse_root(self):
        layout = WarehouseLayout.from_env(
            env={WAREHOUSE_ROOT_ENV: "/tmp/custom-warehouse"}
        )
        assert layout.root == Path("/tmp/custom-warehouse")

    def test_from_env_falls_back_to_default_when_unset(self):
        layout = WarehouseLayout.from_env(env={})
        assert layout.root == Path(DEFAULT_WAREHOUSE_ROOT)

    def test_from_env_falls_back_to_default_on_empty_value(self):
        layout = WarehouseLayout.from_env(env={WAREHOUSE_ROOT_ENV: ""})
        assert layout.root == Path(DEFAULT_WAREHOUSE_ROOT)

    def test_frozen_dataclass_rejects_mutation(self, layout):
        with pytest.raises(Exception):
            layout.root = Path("/other")  # type: ignore[misc]

    def test_two_instances_with_same_root_are_equal(self, tmp_path):
        a = WarehouseLayout(root=tmp_path / "w")
        b = WarehouseLayout(root=tmp_path / "w")
        assert a == b
        # Safe as a dict key
        _ = {a: 1}


class TestSubdirAccessors:
    def test_all_accessors_derive_from_root(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "w")
        r = layout.root
        assert layout.equities_daily_dir == r / "equities" / "daily"
        assert layout.equities_hourly_dir == r / "equities" / "hourly"
        assert layout.equities_minute_dir == r / "equities" / "minute"
        assert layout.crypto_dir == r / "crypto"
        assert layout.options_dir == r / "options"
        assert layout.metadata_dir == r / "metadata"
        assert layout.manifests_dir == r / "manifests"
        assert layout.versions_dir == r / "versions"


# ---------------------------------------------------------------------------
# create() / verify()
# ---------------------------------------------------------------------------


class TestCreateAndVerify:
    def test_create_builds_every_required_subdir(self, layout):
        layout.create()
        for sub in REQUIRED_SUBDIRS:
            assert (layout.root / sub).is_dir()

    def test_create_is_idempotent(self, layout):
        layout.create()
        layout.create()  # must not raise
        # Sample a file inside a subdir survives a second create()
        marker = layout.metadata_dir / "keep.txt"
        marker.write_text("sentinel", encoding="utf-8")
        layout.create()
        assert marker.read_text(encoding="utf-8") == "sentinel"

    def test_verify_passes_after_create(self, prepared):
        prepared.verify()  # must not raise

    def test_verify_reports_missing_directory(self, prepared):
        # Simulate the versions dir being removed after setup
        import shutil
        shutil.rmtree(prepared.versions_dir)
        with pytest.raises(WarehouseIntegrityError, match="missing"):
            prepared.verify()

    def test_verify_reports_file_where_directory_expected(self, layout):
        # Create the tree but replace one subdir with a plain file
        layout.create()
        target = layout.crypto_dir
        import shutil
        shutil.rmtree(target)
        target.write_text("not a directory", encoding="utf-8")
        with pytest.raises(WarehouseIntegrityError, match="not directories"):
            layout.verify()

    def test_verify_names_the_offending_paths(self, prepared):
        import shutil
        shutil.rmtree(prepared.equities_hourly_dir)
        shutil.rmtree(prepared.options_dir)
        with pytest.raises(WarehouseIntegrityError) as excinfo:
            prepared.verify()
        message = str(excinfo.value)
        assert "equities/hourly" in message
        assert "options" in message


# ---------------------------------------------------------------------------
# Deterministic path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_dataset_dir_shape_for_equity_daily(self, layout):
        path = layout.dataset_dir(
            "aapl-daily-2015-2026",
            AssetClass.EQUITY,
            BarInterval.DAILY,
        )
        assert path == layout.equities_daily_dir / "aapl-daily-2015-2026"

    def test_dataset_dir_routes_etf_into_equities(self, layout):
        path = layout.dataset_dir(
            "spy-daily", AssetClass.ETF, BarInterval.DAILY
        )
        assert layout.equities_daily_dir in path.parents

    def test_dataset_dir_routes_crypto_into_crypto(self, layout):
        path = layout.dataset_dir(
            "btcusd-daily", AssetClass.CRYPTO, BarInterval.DAILY
        )
        assert layout.crypto_dir in path.parents

    def test_dataset_dir_routes_minute_intervals_into_minute(self, layout):
        for interval in (
            BarInterval.MINUTE_1,
            BarInterval.MINUTE_5,
            BarInterval.MINUTE_15,
            BarInterval.MINUTE_30,
            BarInterval.SECOND_1,
        ):
            path = layout.dataset_dir(
                "aapl-fine", AssetClass.EQUITY, interval
            )
            assert layout.equities_minute_dir in path.parents

    def test_dataset_dir_deterministic(self, layout):
        a = layout.dataset_dir("aapl", AssetClass.EQUITY, BarInterval.DAILY)
        b = layout.dataset_dir("aapl", AssetClass.EQUITY, BarInterval.DAILY)
        assert a == b

    def test_dataset_dir_does_not_create_the_directory(self, layout):
        # Purely computational — the directory must not exist yet
        path = layout.dataset_dir(
            "notyet", AssetClass.EQUITY, BarInterval.DAILY
        )
        assert not path.exists()

    def test_manifest_path_shape(self, layout):
        path = layout.manifest_path("aapl-daily-2015-2026")
        assert path == layout.manifests_dir / "aapl-daily-2015-2026.json"

    def test_manifest_path_deterministic(self, layout):
        a = layout.manifest_path("x")
        b = layout.manifest_path("x")
        assert a == b

    def test_version_dir_shape(self, layout):
        path = layout.version_dir("aapl-daily", "v2")
        assert path == layout.versions_dir / "aapl-daily" / "v2"

    def test_version_dir_requires_version(self, layout):
        with pytest.raises(WarehouseIntegrityError, match="version"):
            layout.version_dir("aapl", "")


class TestDatasetIdValidation:
    def test_empty_dataset_id_rejected(self, layout):
        with pytest.raises(WarehouseIntegrityError, match="dataset_id"):
            layout.dataset_dir("", AssetClass.EQUITY, BarInterval.DAILY)
        with pytest.raises(WarehouseIntegrityError, match="dataset_id"):
            layout.manifest_path("")
        with pytest.raises(WarehouseIntegrityError, match="dataset_id"):
            layout.version_dir("", "v1")

    @pytest.mark.parametrize(
        "bad_id",
        ["a/b", "..", ".", "a\\b"],
    )
    def test_path_traversal_rejected(self, layout, bad_id):
        with pytest.raises(WarehouseIntegrityError):
            layout.dataset_dir(bad_id, AssetClass.EQUITY, BarInterval.DAILY)
        with pytest.raises(WarehouseIntegrityError):
            layout.manifest_path(bad_id)

    def test_unsupported_asset_class_rejected(self, layout):
        with pytest.raises(WarehouseIntegrityError, match="asset_class"):
            layout.dataset_dir(
                "futures-data", AssetClass.FUTURE, BarInterval.DAILY
            )


# ---------------------------------------------------------------------------
# Manifest writes — immutable by default
# ---------------------------------------------------------------------------


def _valid_manifest(**overrides) -> Dict:
    base = {
        "dataset_id": "aapl-daily-2015-2026",
        "provider": "alpaca",
        "validation_status": STATUS_UNVALIDATED,
        "coverage": {"start": "2015-01-02", "end": "2026-07-14"},
    }
    base.update(overrides)
    return base


class TestManifestWriteAndRead:
    def test_write_then_read_round_trip(self, prepared):
        write_manifest(prepared, "aapl", _valid_manifest())
        payload = read_manifest(prepared, "aapl")
        assert payload["dataset_id"] == "aapl-daily-2015-2026"
        assert payload["validation_status"] == STATUS_UNVALIDATED

    def test_write_creates_the_manifests_dir_if_missing(self, layout):
        # No layout.create() first — write_manifest should still work
        # by creating the manifests directory on demand.
        assert not layout.manifests_dir.exists()
        write_manifest(layout, "aapl", _valid_manifest())
        assert layout.manifests_dir.is_dir()

    def test_write_is_deterministic(self, prepared):
        # Same manifest -> byte-identical file (sort_keys=True + trailing \n).
        p1 = write_manifest(
            prepared, "x", _valid_manifest(dataset_id="x")
        )
        first = p1.read_bytes()
        # Force overwrite (still unvalidated so allowed without force)
        p2 = write_manifest(
            prepared, "x", _valid_manifest(dataset_id="x")
        )
        second = p2.read_bytes()
        assert first == second

    def test_read_missing_manifest_raises(self, prepared):
        with pytest.raises(WarehouseIntegrityError, match="not found"):
            read_manifest(prepared, "no-such-dataset")

    def test_read_corrupted_manifest_raises(self, prepared):
        path = prepared.manifest_path("broken")
        prepared.manifests_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(WarehouseIntegrityError, match="JSON"):
            read_manifest(prepared, "broken")

    def test_unknown_status_rejected(self, prepared):
        with pytest.raises(WarehouseIntegrityError, match="validation_status"):
            write_manifest(
                prepared, "x",
                _valid_manifest(validation_status="bogus-status"),
            )

    def test_non_mapping_manifest_rejected(self, prepared):
        with pytest.raises(WarehouseIntegrityError, match="Mapping"):
            write_manifest(prepared, "x", [1, 2, 3])  # type: ignore[arg-type]


class TestImmutability:
    def test_validated_manifest_cannot_be_overwritten_by_default(
        self, prepared
    ):
        write_manifest(
            prepared, "aapl",
            _valid_manifest(validation_status=STATUS_VALIDATED),
        )
        with pytest.raises(
            WarehouseIntegrityError, match="validated"
        ):
            write_manifest(prepared, "aapl", _valid_manifest())

    def test_validated_manifest_can_be_overwritten_with_force(
        self, prepared
    ):
        write_manifest(
            prepared, "aapl",
            _valid_manifest(validation_status=STATUS_VALIDATED),
        )
        write_manifest(
            prepared, "aapl",
            _valid_manifest(validation_status=STATUS_QUARANTINED),
            force=True,
        )
        assert (
            read_manifest(prepared, "aapl")["validation_status"]
            == STATUS_QUARANTINED
        )

    def test_unvalidated_manifest_can_be_updated_without_force(
        self, prepared
    ):
        write_manifest(
            prepared, "aapl",
            _valid_manifest(validation_status=STATUS_UNVALIDATED),
        )
        # Second write while still unvalidated must succeed
        write_manifest(
            prepared, "aapl",
            _valid_manifest(
                validation_status=STATUS_VALIDATING,
                coverage={"start": "2015-01-02", "end": "2026-07-15"},
            ),
        )
        payload = read_manifest(prepared, "aapl")
        assert payload["validation_status"] == STATUS_VALIDATING
        assert payload["coverage"]["end"] == "2026-07-15"

    def test_quarantined_manifest_can_be_overwritten_without_force(
        self, prepared
    ):
        # A quarantined dataset is retained but not authoritative;
        # revisions should not require force.
        write_manifest(
            prepared, "aapl",
            _valid_manifest(validation_status=STATUS_QUARANTINED),
        )
        write_manifest(
            prepared, "aapl",
            _valid_manifest(validation_status=STATUS_UNVALIDATED),
        )

    def test_write_is_atomic_no_temp_file_left(self, prepared):
        write_manifest(prepared, "aapl", _valid_manifest())
        stray = [
            p for p in prepared.manifests_dir.iterdir()
            if p.suffix == ".tmp" or p.name.startswith(".")
        ]
        assert stray == []


class TestIsValidated:
    def test_missing_manifest_returns_false(self, prepared):
        assert is_validated(prepared, "no-such-dataset") is False

    def test_unvalidated_returns_false(self, prepared):
        write_manifest(
            prepared, "x",
            _valid_manifest(validation_status=STATUS_UNVALIDATED),
        )
        assert is_validated(prepared, "x") is False

    def test_validated_returns_true(self, prepared):
        write_manifest(
            prepared, "x",
            _valid_manifest(validation_status=STATUS_VALIDATED),
        )
        assert is_validated(prepared, "x") is True

    def test_corrupted_manifest_returns_false(self, prepared):
        prepared.manifests_dir.mkdir(parents=True, exist_ok=True)
        prepared.manifest_path("x").write_text("not json", encoding="utf-8")
        assert is_validated(prepared, "x") is False


class TestRefuseOverwriteHelper:
    def test_raises_when_dataset_validated_and_no_force(self, prepared):
        write_manifest(
            prepared, "x",
            _valid_manifest(validation_status=STATUS_VALIDATED),
        )
        with pytest.raises(WarehouseIntegrityError, match="validated"):
            refuse_overwrite_of_validated(prepared, "x")

    def test_allows_when_dataset_validated_and_force_set(self, prepared):
        write_manifest(
            prepared, "x",
            _valid_manifest(validation_status=STATUS_VALIDATED),
        )
        # Must not raise
        refuse_overwrite_of_validated(prepared, "x", force=True)

    def test_allows_when_dataset_unvalidated(self, prepared):
        write_manifest(
            prepared, "x",
            _valid_manifest(validation_status=STATUS_UNVALIDATED),
        )
        refuse_overwrite_of_validated(prepared, "x")

    def test_allows_when_dataset_missing(self, prepared):
        refuse_overwrite_of_validated(prepared, "no-such-dataset")


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.local_warehouse as module
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
                f"local_warehouse must not reference {token!r}"
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
                f"local_warehouse must not import {token!r}"
            )

    def test_no_provider_credential_env_reads(self):
        source = _module_source()
        # Credential-carrying env namespaces the warehouse MUST NOT
        # read.  The WAREHOUSE_* namespace is the only permitted one
        # for this layer.
        for pattern in [
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\.get\(\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\.get\(\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
            r'os\.environ\[\s*[\'"]CRYPTO_ALPACA_',
        ]:
            assert not re.search(pattern, source), (
                f"local_warehouse must not read from provider "
                f"credential env vars: pattern {pattern!r}"
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
        # Plugins land in a follow-up card; the layout module must
        # not depend on any of them.
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
                f"local_warehouse must not depend on provider plugin: {token!r}"
            )

    def test_terminology_avoids_training(self):
        source = _module_source()
        stripped = source.replace('Never\n"training".', "")
        stripped = stripped.replace('Never "training".', "")
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped, (
                f"local_warehouse must use validation / replay / research / "
                f"acquisition rather than {token!r}"
            )

    def test_import_does_not_pull_in_live_runner(self):
        for name in ("strategy.local_warehouse", "strategy"):
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.local_warehouse  # noqa: F401
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


# ---------------------------------------------------------------------------
# Global state invariants
# ---------------------------------------------------------------------------


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled_after_layout_operations(self, layout):
        flags = reset_feature_flags()
        layout.create()
        layout.verify()
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_flags_stay_disabled_after_manifest_write(self, prepared):
        flags = reset_feature_flags()
        write_manifest(prepared, "x", _valid_manifest())
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_flags_stay_disabled_after_module_import(self):
        flags = reset_feature_flags()
        import strategy.local_warehouse  # noqa: F401
        assert flags.all_disabled is True
