"""Tests for strategy/data_catalog.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy.config import reset_feature_flags
from strategy.data_catalog import (
    BENCHMARK,
    DEFAULT_MANIFESTS_SUBDIR,
    HISTORICAL_BARS,
    KNOWN_DATASET_KINDS,
    PAPER_LOG,
    RESEARCH_CONTEXT,
    DataCatalog,
    DatasetFile,
    DatasetManifest,
    DatasetValidationResult,
    build_dataset_manifest,
    sha256_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _seed_bars_dataset(root: Path) -> DatasetManifest:
    data_path = root / "aapl-2026-q3" / "AAPL.csv"
    _write_csv(
        data_path,
        ["date", "open", "high", "low", "close", "volume"],
        [
            ["2026-07-01", "200.0", "203.0", "199.5", "202.5", "1000000"],
            ["2026-07-02", "202.6", "205.0", "201.0", "204.0", "1200000"],
        ],
    )
    manifest = build_dataset_manifest(
        root=str(root),
        dataset_id="aapl-2026-q3",
        kind=HISTORICAL_BARS,
        description="AAPL daily bars for 2026 Q3",
        source="unit-test",
        symbols=["AAPL"],
        start_date="2026-07-01",
        end_date="2026-09-30",
        imported_at="2026-07-02T12:00:00+00:00",
        notes="fixture",
        files=[
            {
                "path": "aapl-2026-q3/AAPL.csv",
                "row_count": 2,
                "schema": ["date", "open", "high", "low", "close", "volume"],
            }
        ],
    )
    manifests_dir = root / DEFAULT_MANIFESTS_SUBDIR
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{manifest.dataset_id}.json").write_text(
        manifest.to_json(), encoding="utf-8"
    )
    return manifest


def _seed_benchmark_dataset(root: Path) -> DatasetManifest:
    data_path = root / "spy-2026-q3" / "SPY.csv"
    _write_csv(
        data_path,
        ["date", "close"],
        [["2026-07-01", "540.0"], ["2026-07-02", "541.5"]],
    )
    manifest = build_dataset_manifest(
        root=str(root),
        dataset_id="spy-2026-q3",
        kind=BENCHMARK,
        description="SPY benchmark closes",
        source="unit-test",
        benchmarks=["SPY"],
        start_date="2026-07-01",
        end_date="2026-09-30",
        imported_at="2026-07-02T12:00:00+00:00",
        files=[
            {
                "path": "spy-2026-q3/SPY.csv",
                "row_count": 2,
                "schema": ["date", "close"],
            }
        ],
    )
    manifests_dir = root / DEFAULT_MANIFESTS_SUBDIR
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{manifest.dataset_id}.json").write_text(
        manifest.to_json(), encoding="utf-8"
    )
    return manifest


def _seed_research_context_dataset(root: Path) -> DatasetManifest:
    data_path = root / "watchlist-2026-07-01" / "watchlist.json"
    _write_json(
        data_path,
        {"date": "2026-07-01", "symbols": ["AAPL", "MSFT"], "notes": "trending"},
    )
    manifest = build_dataset_manifest(
        root=str(root),
        dataset_id="watchlist-2026-07-01",
        kind=RESEARCH_CONTEXT,
        description="Trending watchlist snapshot",
        source="unit-test",
        imported_at="2026-07-02T12:00:00+00:00",
        files=[
            {
                "path": "watchlist-2026-07-01/watchlist.json",
                "schema": ["date", "symbols"],
            }
        ],
    )
    manifests_dir = root / DEFAULT_MANIFESTS_SUBDIR
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{manifest.dataset_id}.json").write_text(
        manifest.to_json(), encoding="utf-8"
    )
    return manifest


# ---------------------------------------------------------------------------
# Dataset kinds
# ---------------------------------------------------------------------------


class TestKnownDatasetKinds:
    def test_known_kinds_include_expected_values(self):
        assert HISTORICAL_BARS in KNOWN_DATASET_KINDS
        assert BENCHMARK in KNOWN_DATASET_KINDS
        assert PAPER_LOG in KNOWN_DATASET_KINDS
        assert RESEARCH_CONTEXT in KNOWN_DATASET_KINDS


# ---------------------------------------------------------------------------
# DatasetFile
# ---------------------------------------------------------------------------


class TestDatasetFile:
    def test_to_dict_roundtrip(self):
        entry = DatasetFile(
            path="foo/bar.csv",
            sha256="a" * 64,
            size_bytes=128,
            row_count=10,
            schema=("date", "close"),
        )
        restored = DatasetFile.from_dict(entry.to_dict())
        assert restored == entry
        json.dumps(entry.to_dict())

    def test_schema_is_tuple(self):
        entry = DatasetFile(
            path="foo/bar.csv",
            sha256="a" * 64,
            size_bytes=1,
            schema=["date", "close"],
        )
        assert entry.schema == ("date", "close")

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"path": ""}, "path is required"),
            ({"path": "/absolute/foo.csv"}, "must be relative"),
            ({"sha256": ""}, "sha256"),
            ({"sha256": "not-a-hex"}, "sha256"),
            ({"size_bytes": -1}, "size_bytes"),
            ({"row_count": -1}, "row_count"),
        ],
    )
    def test_validation_errors(self, overrides, error):
        base = {
            "path": "foo/bar.csv",
            "sha256": "a" * 64,
            "size_bytes": 128,
            "row_count": 10,
        }
        base.update(overrides)
        with pytest.raises(ValueError, match=error):
            DatasetFile(**base)


# ---------------------------------------------------------------------------
# DatasetManifest
# ---------------------------------------------------------------------------


class TestDatasetManifest:
    def _make(self, **overrides) -> DatasetManifest:
        base = dict(
            dataset_id="paper-2026-q3",
            kind=PAPER_LOG,
            description="paper trading log",
            source="test",
            symbols=["AAPL"],
            benchmarks=["SPY"],
            start_date="2026-07-01",
            end_date="2026-07-31",
            imported_at="2026-07-02T12:00:00+00:00",
            files=(
                DatasetFile(
                    path="paper-2026-q3/trades.csv",
                    sha256="b" * 64,
                    size_bytes=42,
                    row_count=3,
                    schema=("id", "symbol"),
                ),
            ),
        )
        base.update(overrides)
        return DatasetManifest(**base)

    def test_to_dict_roundtrip(self):
        manifest = self._make()
        restored = DatasetManifest.from_dict(manifest.to_dict())
        assert restored == manifest
        json.dumps(manifest.to_dict())

    def test_stable_hash_ignores_imported_at(self):
        first = self._make(imported_at="2026-07-02T12:00:00+00:00")
        second = self._make(imported_at="2026-08-15T09:00:00+00:00")
        assert first.stable_hash() == second.stable_hash()

    def test_stable_hash_changes_with_content(self):
        first = self._make()
        second = self._make(description="different")
        assert first.stable_hash() != second.stable_hash()

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"dataset_id": ""}, "dataset_id"),
            ({"dataset_id": "Bad Id!"}, "dataset_id"),
            ({"kind": "unknown"}, "kind must be one of"),
            ({"files": ()}, "at least one file"),
            (
                {"start_date": "2026-08-01", "end_date": "2026-07-01"},
                "start_date must be before or equal to end_date",
            ),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._make(**overrides)

    def test_duplicate_file_paths_rejected(self):
        entry = DatasetFile(
            path="paper-2026-q3/trades.csv",
            sha256="b" * 64,
            size_bytes=42,
        )
        with pytest.raises(ValueError, match="duplicate file path"):
            self._make(files=(entry, entry))


# ---------------------------------------------------------------------------
# build_dataset_manifest + sha256_file
# ---------------------------------------------------------------------------


class TestBuildDatasetManifest:
    def test_build_computes_checksums(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))
        manifest = catalog.get("aapl-2026-q3")
        file_entry = manifest.files[0]

        expected = sha256_file(str(tmp_path / file_entry.path))
        assert file_entry.sha256 == expected
        assert file_entry.size_bytes == (tmp_path / file_entry.path).stat().st_size
        assert file_entry.schema == (
            "date", "open", "high", "low", "close", "volume"
        )

    def test_build_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_dataset_manifest(
                root=str(tmp_path),
                dataset_id="missing",
                kind=HISTORICAL_BARS,
                files=[{"path": "nowhere/nothing.csv"}],
            )

    def test_sha256_file_matches_hashlib(self, tmp_path):
        target = tmp_path / "sample.txt"
        target.write_bytes(b"hello backtest lab")
        import hashlib as _hashlib

        assert sha256_file(str(target)) == _hashlib.sha256(
            b"hello backtest lab"
        ).hexdigest()


# ---------------------------------------------------------------------------
# DataCatalog listing and lookup
# ---------------------------------------------------------------------------


class TestDataCatalogListing:
    def test_from_directory_loads_manifests(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        _seed_benchmark_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        assert catalog.has("aapl-2026-q3")
        assert catalog.has("spy-2026-q3")
        assert not catalog.has("nope")

    def test_from_directory_empty_when_missing(self, tmp_path):
        catalog = DataCatalog.from_directory(str(tmp_path / "does-not-exist"))
        assert catalog.list() == []

    def test_list_sorted_by_dataset_id(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        _seed_benchmark_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        ids = [m.dataset_id for m in catalog.list()]
        assert ids == sorted(ids)

    def test_list_filters_by_kind(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        _seed_benchmark_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        bars = catalog.list(kind=HISTORICAL_BARS)
        assert [m.dataset_id for m in bars] == ["aapl-2026-q3"]

    def test_list_unknown_kind_raises(self, tmp_path):
        catalog = DataCatalog.from_directory(str(tmp_path))
        with pytest.raises(ValueError, match="unknown dataset kind"):
            catalog.list(kind="not-a-kind")

    def test_get_missing_raises(self, tmp_path):
        catalog = DataCatalog.from_directory(str(tmp_path))
        with pytest.raises(KeyError, match="not found"):
            catalog.get("missing")

    def test_from_directory_rejects_duplicate_ids(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        duplicate = tmp_path / DEFAULT_MANIFESTS_SUBDIR / "duplicate.json"
        manifest = json.loads(
            (tmp_path / DEFAULT_MANIFESTS_SUBDIR / "aapl-2026-q3.json").read_text()
        )
        duplicate.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ValueError, match="duplicate dataset_id"):
            DataCatalog.from_directory(str(tmp_path))


# ---------------------------------------------------------------------------
# DataCatalog validation
# ---------------------------------------------------------------------------


class TestDataCatalogValidation:
    def test_validate_ok(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("aapl-2026-q3")
        assert result.ok is True
        assert result.errors == []
        assert result.checked_files == 1

    def test_validate_missing_file(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        (tmp_path / "aapl-2026-q3" / "AAPL.csv").unlink()
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("aapl-2026-q3")
        assert result.ok is False
        assert any("missing file" in err for err in result.errors)

    def test_validate_checksum_mismatch(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        target = tmp_path / "aapl-2026-q3" / "AAPL.csv"
        original = target.read_text()
        # keep the same length so we isolate a checksum-only mismatch
        assert original.endswith("\n")
        tampered = original[:-1] + "X"
        target.write_text(tampered, encoding="utf-8")
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("aapl-2026-q3")
        assert result.ok is False
        assert any("checksum mismatch" in err for err in result.errors)
        assert not any("size mismatch" in err for err in result.errors)

    def test_validate_size_mismatch(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        target = tmp_path / "aapl-2026-q3" / "AAPL.csv"
        target.write_text(target.read_text() + "extra bytes\n", encoding="utf-8")
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("aapl-2026-q3")
        assert result.ok is False
        assert any("size mismatch" in err for err in result.errors)

    def test_validate_csv_schema_mismatch(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))
        manifest = catalog.get("aapl-2026-q3")
        # Rewrite the manifest with a wrong schema on disk
        wrong = manifest.to_dict()
        wrong["files"][0]["schema"] = ["date", "close"]
        (tmp_path / DEFAULT_MANIFESTS_SUBDIR / "aapl-2026-q3.json").write_text(
            json.dumps(wrong), encoding="utf-8"
        )
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("aapl-2026-q3")
        assert result.ok is False
        assert any("schema mismatch" in err for err in result.errors)

    def test_validate_json_schema_ok(self, tmp_path):
        _seed_research_context_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("watchlist-2026-07-01")
        assert result.ok is True
        assert result.errors == []

    def test_validate_json_schema_missing_key(self, tmp_path):
        _seed_research_context_dataset(tmp_path)
        # Rewrite the JSON payload to drop the "symbols" key so it doesn't
        # match the manifest schema. Also update the manifest checksum/size
        # so this test isolates the schema mismatch.
        payload_path = tmp_path / "watchlist-2026-07-01" / "watchlist.json"
        _write_json(payload_path, {"date": "2026-07-01", "notes": "trending"})
        rebuilt = build_dataset_manifest(
            root=str(tmp_path),
            dataset_id="watchlist-2026-07-01",
            kind=RESEARCH_CONTEXT,
            description="Trending watchlist snapshot",
            source="unit-test",
            imported_at="2026-07-02T12:00:00+00:00",
            files=[
                {
                    "path": "watchlist-2026-07-01/watchlist.json",
                    "schema": ["date", "symbols"],
                }
            ],
        )
        (tmp_path / DEFAULT_MANIFESTS_SUBDIR / "watchlist-2026-07-01.json").write_text(
            rebuilt.to_json(), encoding="utf-8"
        )
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("watchlist-2026-07-01")
        assert result.ok is False
        assert any("schema mismatch" in err for err in result.errors)
        assert any("symbols" in err for err in result.errors)

    def test_validate_warns_without_schema(self, tmp_path):
        manifest = _seed_bars_dataset(tmp_path)
        # Rewrite manifest with schema stripped so we can prove the warning
        stripped = manifest.to_dict()
        stripped["files"][0]["schema"] = []
        (tmp_path / DEFAULT_MANIFESTS_SUBDIR / "aapl-2026-q3.json").write_text(
            json.dumps(stripped), encoding="utf-8"
        )
        catalog = DataCatalog.from_directory(str(tmp_path))

        result = catalog.validate("aapl-2026-q3")
        assert result.ok is True
        assert any("no schema declared" in warning for warning in result.warnings)

    def test_validate_all_returns_result_per_dataset(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        _seed_benchmark_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        results = catalog.validate_all()
        assert len(results) == 2
        assert all(isinstance(r, DatasetValidationResult) for r in results)
        assert [r.dataset_id for r in results] == ["aapl-2026-q3", "spy-2026-q3"]
        assert all(r.ok for r in results)

    def test_validate_does_not_mutate_files(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))
        target = tmp_path / "aapl-2026-q3" / "AAPL.csv"
        manifest_path = tmp_path / DEFAULT_MANIFESTS_SUBDIR / "aapl-2026-q3.json"
        before_data = target.read_bytes()
        before_manifest = manifest_path.read_bytes()

        catalog.validate("aapl-2026-q3")
        catalog.validate_all()

        assert target.read_bytes() == before_data
        assert manifest_path.read_bytes() == before_manifest

    def test_validation_result_to_dict(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))
        result = catalog.validate("aapl-2026-q3")

        d = result.to_dict()
        assert d["dataset_id"] == "aapl-2026-q3"
        assert d["ok"] is True
        assert d["checked_files"] == 1
        json.dumps(d)


# ---------------------------------------------------------------------------
# Checksum + reproducibility metadata
# ---------------------------------------------------------------------------


class TestChecksumAndReproducibility:
    def test_checksum_file_matches_disk(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        actual = catalog.checksum_file("aapl-2026-q3", "aapl-2026-q3/AAPL.csv")
        assert actual == sha256_file(str(tmp_path / "aapl-2026-q3" / "AAPL.csv"))

    def test_checksum_file_missing_path_raises(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))
        with pytest.raises(KeyError):
            catalog.checksum_file("aapl-2026-q3", "not/registered.csv")

    def test_reproducibility_metadata_contains_expected_fields(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        metadata = catalog.reproducibility_metadata("aapl-2026-q3")
        assert metadata["dataset_id"] == "aapl-2026-q3"
        assert metadata["manifest_hash"] == catalog.get(
            "aapl-2026-q3"
        ).stable_hash()
        assert metadata["kind"] == HISTORICAL_BARS
        assert metadata["source"] == "unit-test"
        assert metadata["imported_at"] == "2026-07-02T12:00:00+00:00"
        assert metadata["start_date"] == "2026-07-01"
        assert metadata["end_date"] == "2026-09-30"
        assert metadata["symbols"] == ["AAPL"]
        assert metadata["file_checksums"] == {
            "aapl-2026-q3/AAPL.csv": catalog.get(
                "aapl-2026-q3"
            ).files[0].sha256
        }
        json.dumps(metadata)

    def test_reproducibility_metadata_is_deterministic(self, tmp_path):
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))

        first = catalog.reproducibility_metadata("aapl-2026-q3")
        second = catalog.reproducibility_metadata("aapl-2026-q3")
        assert first == second


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_does_not_import_order_paths(self):
        import strategy.data_catalog as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        forbidden = [
            "alpaca",
            "place_order",
            "submit_order",
            "TradingClient",
        ]
        for token in forbidden:
            assert token not in source, f"data_catalog must not reference {token!r}"

    def test_feature_flags_remain_disabled(self, tmp_path):
        flags = reset_feature_flags()
        _seed_bars_dataset(tmp_path)
        catalog = DataCatalog.from_directory(str(tmp_path))
        catalog.validate_all()
        catalog.reproducibility_metadata("aapl-2026-q3")

        assert flags.all_disabled is True
        assert flags.enabled_flags == []


# ---------------------------------------------------------------------------
# Phase 5.6 warehouse extension — t_phase56_catalog
# ---------------------------------------------------------------------------


from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    WarehouseLayout,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    BarInterval,
)


def _wh_file(name: str = "a.csv") -> DatasetFile:
    return DatasetFile(path=name, sha256="0" * 64, size_bytes=1)


def _mkm(**over) -> DatasetManifest:
    base = dict(
        dataset_id="dataset-a",
        kind=HISTORICAL_BARS,
        files=(_wh_file(),),
    )
    base.update(over)
    return DatasetManifest(**base)


class TestWarehouseFieldsDefaultToEmpty:
    """A manifest constructed with only the Phase 3 fields must round-trip
    byte-identically through ``to_dict`` / ``from_dict``.  The warehouse
    fields are emitted only when populated.
    """

    def test_legacy_manifest_to_dict_omits_warehouse_fields(self):
        m = _mkm(symbols=("AAPL",))
        d = m.to_dict()
        for key in (
            "provider",
            "provider_request_id",
            "interval",
            "asset_class",
            "adjustment_mode",
            "adjustment_version",
            "corporate_action_version",
            "timezone",
            "validation_status",
            "parent_dataset_id",
            "provider_capabilities_snapshot",
            "warehouse_paths",
        ):
            assert key not in d, (
                f"legacy manifest must not emit {key!r}"
            )

    def test_legacy_manifest_stable_hash_unaffected(self):
        # Two manifests differing only in fields the warehouse
        # extension added — but neither populates them.  Their
        # stable hashes must be equal to prove the extension didn't
        # inflate the hashable payload.
        a = _mkm(symbols=("AAPL",), imported_at="2026-01-01")
        b = _mkm(symbols=("AAPL",), imported_at="2026-02-01")
        assert a.stable_hash() == b.stable_hash()

    def test_legacy_roundtrip_byte_identical(self):
        m = _mkm(
            symbols=("AAPL",),
            start_date="2020-01-01",
            end_date="2020-12-31",
        )
        d = m.to_dict()
        restored = DatasetManifest.from_dict(d)
        assert restored == m
        assert restored.to_dict() == d


class TestWarehouseFieldsPopulated:
    def test_all_warehouse_fields_serialize_when_populated(self):
        m = _mkm(
            symbols=("AAPL",),
            start_date="2015-01-02",
            end_date="2026-07-14",
            provider="fake",
            provider_request_id="trace-xyz",
            interval=BarInterval.DAILY.value,
            asset_class=AssetClass.EQUITY.value,
            adjustment_mode=AdjustmentMode.SPLIT_DIVIDEND.value,
            adjustment_version="fake:2026-07-15",
            corporate_action_version="3",
            timezone="America/New_York",
            validation_status=STATUS_VALIDATED,
            provider_capabilities_snapshot={
                "supported_intervals": ["1Day"]
            },
            warehouse_paths={"dataset_dir": "equities/daily/aapl"},
        )
        d = m.to_dict()
        assert d["provider"] == "fake"
        assert d["provider_request_id"] == "trace-xyz"
        assert d["interval"] == "1Day"
        assert d["asset_class"] == "equity"
        assert d["adjustment_mode"] == "split_dividend"
        assert d["adjustment_version"] == "fake:2026-07-15"
        assert d["corporate_action_version"] == "3"
        assert d["timezone"] == "America/New_York"
        assert d["validation_status"] == STATUS_VALIDATED
        assert d["provider_capabilities_snapshot"] == {
            "supported_intervals": ["1Day"]
        }
        assert d["warehouse_paths"] == {"dataset_dir": "equities/daily/aapl"}

    def test_warehouse_manifest_roundtrip(self):
        m = _mkm(
            symbols=("AAPL",),
            provider="fake",
            interval=BarInterval.HOURLY.value,
            asset_class=AssetClass.ETF.value,
            adjustment_mode=AdjustmentMode.RAW.value,
            validation_status=STATUS_UNVALIDATED,
            parent_dataset_id="aapl-root",
            corporate_action_version="2",
        )
        restored = DatasetManifest.from_dict(m.to_dict())
        assert restored == m

    def test_stable_hash_reflects_warehouse_content(self):
        a = _mkm(symbols=("AAPL",), provider="one")
        b = _mkm(symbols=("AAPL",), provider="two")
        assert a.stable_hash() != b.stable_hash()

    def test_covers_symbol_and_covers_window(self):
        m = _mkm(
            symbols=("AAPL",),
            benchmarks=("SPY",),
            start_date="2020-01-01",
            end_date="2020-12-31",
        )
        assert m.covers_symbol("AAPL") is True
        assert m.covers_symbol("SPY") is True
        assert m.covers_symbol("NVDA") is False
        assert m.covers_window("2020-06-01", "2020-06-30") is True
        assert m.covers_window("2019-01-01", "2019-12-31") is False
        assert m.covers_window("2021-01-01", "2021-12-31") is False
        # Empty declared bounds -> open-ended
        m2 = _mkm(symbols=("AAPL",))
        assert m2.covers_window("2020-01-01", "2020-12-31") is True


class TestWarehouseFieldValidation:
    @pytest.mark.parametrize(
        "override,match",
        [
            ({"interval": "bogus"}, "interval"),
            ({"asset_class": "bogus"}, "asset_class"),
            ({"adjustment_mode": "bogus"}, "adjustment_mode"),
            ({"validation_status": "bogus"}, "validation_status"),
        ],
    )
    def test_unknown_enum_value_rejected(self, override, match):
        with pytest.raises(ValueError, match=match):
            _mkm(symbols=("AAPL",), **override)

    def test_parent_dataset_id_pattern_enforced(self):
        with pytest.raises(ValueError, match="dataset_id"):
            _mkm(symbols=("AAPL",), parent_dataset_id="Bad Parent!")

    def test_parent_cannot_equal_own_id(self):
        with pytest.raises(ValueError, match="parent_dataset_id"):
            _mkm(
                dataset_id="aapl",
                symbols=("AAPL",),
                parent_dataset_id="aapl",
            )

    def test_empty_warehouse_fields_are_valid(self):
        # Legacy path — every warehouse field left empty.  Must not raise.
        _mkm(symbols=("AAPL",))


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _seed_catalog(**variants) -> DataCatalog:
    """Build a DataCatalog from a dict of manifest overrides."""
    manifests = {}
    for dataset_id, over in variants.items():
        payload = dict(over)
        payload.setdefault("kind", HISTORICAL_BARS)
        payload.setdefault("files", (_wh_file(f"{dataset_id}.csv"),))
        payload["dataset_id"] = dataset_id
        manifests[dataset_id] = DatasetManifest(**payload)
    return DataCatalog(root=".", manifests=manifests)


class TestFindCoverage:
    def test_matches_symbol_interval_window(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",),
                    start_date="2020-01-01",
                    end_date="2020-12-31",
                    interval="1Day",
                ),
                "msft-2020": dict(
                    symbols=("MSFT",),
                    start_date="2020-01-01",
                    end_date="2020-12-31",
                    interval="1Day",
                ),
            }
        )
        hits = catalog.find_coverage(
            "AAPL", "1Day", "2020-06-01", "2020-06-30"
        )
        assert [m.dataset_id for m in hits] == ["aapl-2020"]

    def test_matches_benchmark_symbol(self):
        catalog = _seed_catalog(
            **{
                "spy-2020": dict(
                    benchmarks=("SPY",),
                    start_date="2020-01-01",
                    end_date="2020-12-31",
                    interval="1Day",
                ),
            }
        )
        hits = catalog.find_coverage(
            "SPY", "1Day", "2020-06-01", "2020-06-30"
        )
        assert [m.dataset_id for m in hits] == ["spy-2020"]

    def test_interval_mismatch_excludes(self):
        catalog = _seed_catalog(
            **{
                "aapl-daily": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-12-31",
                ),
                "aapl-hourly": dict(
                    symbols=("AAPL",), interval="1Hour",
                    start_date="2020-01-01", end_date="2020-12-31",
                ),
            }
        )
        hits = catalog.find_coverage(
            "AAPL", "1Hour", "2020-01-01", "2020-12-31"
        )
        assert [m.dataset_id for m in hits] == ["aapl-hourly"]

    def test_non_overlapping_window_excluded(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-12-31",
                ),
            }
        )
        hits = catalog.find_coverage(
            "AAPL", "1Day", "2022-01-01", "2022-12-31"
        )
        assert hits == []

    def test_sorted_by_start_then_dataset_id(self):
        catalog = _seed_catalog(
            **{
                "aapl-b-2020": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-06-30",
                ),
                "aapl-a-2020": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-06-30",
                ),
                "aapl-2021": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2021-01-01", end_date="2021-06-30",
                ),
            }
        )
        hits = catalog.find_coverage(
            "AAPL", "1Day", "2020-01-01", "2021-12-31"
        )
        assert [m.dataset_id for m in hits] == [
            "aapl-a-2020",
            "aapl-b-2020",
            "aapl-2021",
        ]


class TestGaps:
    def _catalog_with(self, windows: list[tuple[str, str, str]]) -> DataCatalog:
        variants = {
            f"aapl-{i}": dict(
                symbols=("AAPL",), interval="1Day",
                start_date=start, end_date=end,
            )
            for i, (dataset_id, start, end) in enumerate(windows)
        }
        return _seed_catalog(**variants)

    def test_no_gap_when_fully_covered(self):
        catalog = self._catalog_with(
            [("m1", "2020-01-01", "2020-12-31")]
        )
        assert catalog.gaps(
            "AAPL", "1Day", "2020-06-01", "2020-06-30"
        ) == []

    def test_full_gap_when_no_coverage(self):
        catalog = self._catalog_with([])
        assert catalog.gaps(
            "AAPL", "1Day", "2020-01-01", "2020-12-31"
        ) == [("2020-01-01", "2020-12-31")]

    def test_gap_between_two_windows(self):
        catalog = self._catalog_with(
            [
                ("a", "2020-01-01", "2020-06-30"),
                ("b", "2020-09-01", "2020-12-31"),
            ]
        )
        gaps = catalog.gaps(
            "AAPL", "1Day", "2020-01-01", "2020-12-31"
        )
        assert gaps == [("2020-06-30", "2020-09-01")]

    def test_gap_at_end_of_range(self):
        catalog = self._catalog_with(
            [("a", "2020-01-01", "2020-06-30")]
        )
        assert catalog.gaps(
            "AAPL", "1Day", "2020-01-01", "2020-12-31"
        ) == [("2020-06-30", "2020-12-31")]

    def test_gap_at_start_of_range(self):
        catalog = self._catalog_with(
            [("a", "2020-06-01", "2020-12-31")]
        )
        assert catalog.gaps(
            "AAPL", "1Day", "2020-01-01", "2020-12-31"
        ) == [("2020-01-01", "2020-06-01")]

    def test_overlapping_windows_merged(self):
        catalog = self._catalog_with(
            [
                ("a", "2020-01-01", "2020-08-31"),
                ("b", "2020-06-01", "2020-12-31"),
            ]
        )
        # Fully covered — no gaps
        assert catalog.gaps(
            "AAPL", "1Day", "2020-02-01", "2020-11-30"
        ) == []

    def test_gaps_requires_explicit_bounds(self):
        catalog = self._catalog_with([])
        with pytest.raises(ValueError, match="bounds"):
            catalog.gaps("AAPL", "1Day", "", "2020-12-31")
        with pytest.raises(ValueError, match="bounds"):
            catalog.gaps("AAPL", "1Day", "2020-01-01", "")

    def test_gaps_rejects_reversed_range(self):
        catalog = self._catalog_with([])
        with pytest.raises(ValueError, match="start"):
            catalog.gaps(
                "AAPL", "1Day", "2020-12-31", "2020-01-01"
            )


class TestLatestValidated:
    def test_returns_none_when_no_validated_dataset(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-12-31",
                    validation_status=STATUS_UNVALIDATED,
                ),
            }
        )
        assert catalog.latest_validated("AAPL", "1Day") is None

    def test_returns_the_latest_validated_by_end_date(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-12-31",
                    validation_status=STATUS_VALIDATED,
                ),
                "aapl-2021": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2021-01-01", end_date="2021-12-31",
                    validation_status=STATUS_VALIDATED,
                ),
            }
        )
        latest = catalog.latest_validated("AAPL", "1Day")
        assert latest is not None
        assert latest.dataset_id == "aapl-2021"

    def test_filters_by_interval(self):
        catalog = _seed_catalog(
            **{
                "aapl-hourly": dict(
                    symbols=("AAPL",), interval="1Hour",
                    start_date="2020-01-01", end_date="2020-12-31",
                    validation_status=STATUS_VALIDATED,
                ),
                "aapl-daily": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-06-30",
                    validation_status=STATUS_VALIDATED,
                ),
            }
        )
        assert (
            catalog.latest_validated("AAPL", "1Day").dataset_id
            == "aapl-daily"
        )
        assert (
            catalog.latest_validated("AAPL", "1Hour").dataset_id
            == "aapl-hourly"
        )

    def test_filters_by_symbol(self):
        catalog = _seed_catalog(
            **{
                "msft-2020": dict(
                    symbols=("MSFT",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-12-31",
                    validation_status=STATUS_VALIDATED,
                ),
            }
        )
        assert catalog.latest_validated("AAPL", "1Day") is None

    def test_ignores_quarantined_datasets(self):
        catalog = _seed_catalog(
            **{
                "aapl-good": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-01-01", end_date="2020-06-30",
                    validation_status=STATUS_VALIDATED,
                ),
                "aapl-bad": dict(
                    symbols=("AAPL",), interval="1Day",
                    start_date="2020-07-01", end_date="2020-12-31",
                    validation_status="quarantined",
                ),
            }
        )
        latest = catalog.latest_validated("AAPL", "1Day")
        assert latest is not None
        assert latest.dataset_id == "aapl-good"


class TestVersions:
    def test_versions_returns_root_alone_when_no_children(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",),
                    corporate_action_version="1",
                ),
            }
        )
        chain = catalog.versions("aapl-2020")
        assert [m.dataset_id for m in chain] == ["aapl-2020"]

    def test_versions_returns_chain_ordered_by_version(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",),
                    corporate_action_version="1",
                ),
                "aapl-2020-v3": dict(
                    symbols=("AAPL",),
                    parent_dataset_id="aapl-2020",
                    corporate_action_version="3",
                ),
                "aapl-2020-v2": dict(
                    symbols=("AAPL",),
                    parent_dataset_id="aapl-2020",
                    corporate_action_version="2",
                ),
            }
        )
        chain = catalog.versions("aapl-2020")
        assert [m.dataset_id for m in chain] == [
            "aapl-2020",
            "aapl-2020-v2",
            "aapl-2020-v3",
        ]

    def test_versions_empty_when_nothing_matches(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",),
                    corporate_action_version="1",
                ),
            }
        )
        assert catalog.versions("no-such-id") == []

    def test_versions_returns_children_even_when_root_missing(self):
        # A revision whose parent is not in the catalog is still
        # discoverable through the children lookup.
        catalog = _seed_catalog(
            **{
                "aapl-2020-v2": dict(
                    symbols=("AAPL",),
                    parent_dataset_id="aapl-2020",
                    corporate_action_version="2",
                ),
            }
        )
        chain = catalog.versions("aapl-2020")
        assert [m.dataset_id for m in chain] == ["aapl-2020-v2"]

    def test_versions_rejects_invalid_dataset_id(self):
        catalog = _seed_catalog(
            **{
                "aapl-2020": dict(
                    symbols=("AAPL",),
                ),
            }
        )
        with pytest.raises(ValueError, match="dataset_id"):
            catalog.versions("Bad Id!")


# ---------------------------------------------------------------------------
# from_warehouse_layout
# ---------------------------------------------------------------------------


class TestFromWarehouseLayout:
    def test_reads_manifests_from_warehouse_layout(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "warehouse")
        layout.create()
        manifest = _mkm(
            dataset_id="aapl-2020",
            symbols=("AAPL",),
            start_date="2020-01-01",
            end_date="2020-12-31",
            provider="fake",
            interval="1Day",
            asset_class="equity",
            adjustment_mode="split_dividend",
            validation_status=STATUS_VALIDATED,
        )
        (layout.manifests_dir / f"{manifest.dataset_id}.json").write_text(
            manifest.to_json(),
            encoding="utf-8",
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        assert catalog.has("aapl-2020")
        loaded = catalog.get("aapl-2020")
        assert loaded.provider == "fake"
        assert loaded.validation_status == STATUS_VALIDATED

    def test_empty_warehouse_layout_yields_empty_catalog(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "warehouse")
        layout.create()
        catalog = DataCatalog.from_warehouse_layout(layout)
        assert catalog.list() == []


# ---------------------------------------------------------------------------
# Feature-flag invariance for the new surface
# ---------------------------------------------------------------------------


class TestWarehouseExtensionFeatureFlagInvariance:
    def test_flags_stay_disabled_after_queries(self, tmp_path):
        flags = reset_feature_flags()
        layout = WarehouseLayout(root=tmp_path / "warehouse")
        layout.create()
        m = _mkm(
            dataset_id="aapl",
            symbols=("AAPL",),
            start_date="2020-01-01",
            end_date="2020-12-31",
            interval="1Day",
            validation_status=STATUS_VALIDATED,
        )
        (layout.manifests_dir / "aapl.json").write_text(
            m.to_json(), encoding="utf-8"
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        catalog.find_coverage("AAPL", "1Day", "2020-01-01", "2020-12-31")
        catalog.gaps("AAPL", "1Day", "2020-01-01", "2020-12-31")
        catalog.latest_validated("AAPL", "1Day")
        catalog.versions("aapl")
        assert flags.all_disabled is True
        assert flags.enabled_flags == []
