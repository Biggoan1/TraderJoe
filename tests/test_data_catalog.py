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
