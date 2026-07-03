"""Research Data Catalog.

Read-only registry of research datasets used by the Backtest Lab.

The catalog does not fetch data, download files, mutate manifests, place
orders, call broker APIs, or enable feature flags. It only lists,
validates, and re-checksums locally-stored datasets so backtest and
walk-forward runs can be reproduced from immutable inputs.

Storage layout:

    research_data/
        manifests/<dataset_id>.json    -- immutable dataset manifests
        <dataset_id>/*                 -- referenced data files

Manifest kinds (see ``KNOWN_DATASET_KINDS``):

    historical_bars    -- OHLCV bars for candidate symbols
    benchmark          -- SPY / QQQ / sector ETF bars used for context
    paper_log          -- exports of paper-trading logs
    research_context   -- watchlists, digest exports, sector/breadth snapshots

Example:

    from strategy.data_catalog import DataCatalog

    catalog = DataCatalog.from_directory("research_data")
    for dataset in catalog.list(kind="historical_bars"):
        result = catalog.validate(dataset.dataset_id)
        assert result.ok
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from strategy.backtest_lab import stable_hash


DEFAULT_CATALOG_ROOT = "research_data"
DEFAULT_MANIFESTS_SUBDIR = "manifests"
DEFAULT_MANIFEST_VERSION = "1"

HISTORICAL_BARS = "historical_bars"
BENCHMARK = "benchmark"
PAPER_LOG = "paper_log"
RESEARCH_CONTEXT = "research_context"

KNOWN_DATASET_KINDS: Tuple[str, ...] = (
    HISTORICAL_BARS,
    BENCHMARK,
    PAPER_LOG,
    RESEARCH_CONTEXT,
)

_DATASET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-.]{1,63}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _read_chunks(path: Path, chunk_size: int = 65536) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


def sha256_file(path: str) -> str:
    """Return SHA-256 hex digest for the file at ``path``."""
    digest = hashlib.sha256()
    for chunk in _read_chunks(Path(path)):
        digest.update(chunk)
    return digest.hexdigest()


def _validate_dataset_id(dataset_id: str) -> None:
    if not dataset_id or not _DATASET_ID_PATTERN.match(dataset_id):
        raise ValueError(
            "dataset_id must be lowercase alphanumeric with '_', '-', or '.'"
            f" (got {dataset_id!r})"
        )


def _validate_sha256(sha256: str) -> None:
    if not sha256 or not _HEX64_PATTERN.match(sha256):
        raise ValueError(f"sha256 must be a 64-character hex digest (got {sha256!r})")


def _read_csv_header(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return next(reader)
        except StopIteration:
            return []


def _read_json_keys(path: Path) -> Optional[List[str]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return list(data.keys())
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return list(data[0].keys())
    return None


@dataclass(frozen=True)
class DatasetFile:
    """A single file referenced by a dataset manifest."""

    path: str
    sha256: str
    size_bytes: int
    row_count: Optional[int] = None
    schema: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", tuple(self.schema))
        self.validate()

    def validate(self) -> None:
        if not self.path:
            raise ValueError("DatasetFile.path is required")
        if Path(self.path).is_absolute():
            raise ValueError(
                f"DatasetFile.path must be relative to the dataset root (got {self.path!r})"
            )
        _validate_sha256(self.sha256)
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if self.row_count is not None and self.row_count < 0:
            raise ValueError("row_count cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "row_count": self.row_count,
            "schema": list(self.schema),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DatasetFile":
        return cls(
            path=data["path"],
            sha256=data["sha256"],
            size_bytes=int(data["size_bytes"]),
            row_count=(
                int(data["row_count"]) if data.get("row_count") is not None else None
            ),
            schema=tuple(data.get("schema") or ()),
        )


@dataclass(frozen=True)
class DatasetManifest:
    """Immutable description of a research dataset."""

    dataset_id: str
    kind: str
    description: str = ""
    source: str = ""
    symbols: Tuple[str, ...] = ()
    benchmarks: Tuple[str, ...] = ()
    start_date: str = ""
    end_date: str = ""
    imported_at: str = ""
    version: str = DEFAULT_MANIFEST_VERSION
    files: Tuple[DatasetFile, ...] = ()
    notes: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "benchmarks", tuple(self.benchmarks))
        object.__setattr__(self, "files", tuple(self.files))
        self.validate()

    def validate(self) -> None:
        _validate_dataset_id(self.dataset_id)
        if self.kind not in KNOWN_DATASET_KINDS:
            raise ValueError(
                f"kind must be one of {KNOWN_DATASET_KINDS} (got {self.kind!r})"
            )
        if not self.files:
            raise ValueError("manifest must reference at least one file")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before or equal to end_date")
        seen: set[str] = set()
        for entry in self.files:
            if entry.path in seen:
                raise ValueError(f"duplicate file path in manifest: {entry.path}")
            seen.add(entry.path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "description": self.description,
            "source": self.source,
            "symbols": list(self.symbols),
            "benchmarks": list(self.benchmarks),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "imported_at": self.imported_at,
            "version": self.version,
            "files": [entry.to_dict() for entry in self.files],
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DatasetManifest":
        return cls(
            dataset_id=data["dataset_id"],
            kind=data["kind"],
            description=data.get("description", ""),
            source=data.get("source", ""),
            symbols=tuple(data.get("symbols") or ()),
            benchmarks=tuple(data.get("benchmarks") or ()),
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
            imported_at=data.get("imported_at", ""),
            version=data.get("version", DEFAULT_MANIFEST_VERSION),
            files=tuple(
                DatasetFile.from_dict(entry) for entry in (data.get("files") or ())
            ),
            notes=data.get("notes", ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def stable_hash(self) -> str:
        """Deterministic hash independent of import timestamp."""
        data = self.to_dict()
        data.pop("imported_at", None)
        return stable_hash(data)


@dataclass
class DatasetValidationResult:
    """Outcome of validating a dataset against the local filesystem."""

    dataset_id: str
    root: str
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checked_files: int = 0

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "root": self.root,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checked_files": self.checked_files,
        }


class DataCatalog:
    """Read-only registry of dataset manifests.

    The catalog never writes to dataset files or manifests. Operators
    produce manifests offline (see :func:`build_dataset_manifest`) and the
    catalog only reads them.
    """

    def __init__(
        self,
        root: str,
        manifests: Dict[str, DatasetManifest],
        manifests_dir: Optional[str] = None,
    ):
        self._root = str(Path(root))
        self._manifests_dir = str(
            Path(manifests_dir) if manifests_dir else Path(root) / DEFAULT_MANIFESTS_SUBDIR
        )
        self._manifests: Dict[str, DatasetManifest] = dict(manifests)

    @property
    def root(self) -> str:
        return self._root

    @property
    def manifests_dir(self) -> str:
        return self._manifests_dir

    @classmethod
    def from_directory(
        cls,
        root: str = DEFAULT_CATALOG_ROOT,
        manifests_subdir: str = DEFAULT_MANIFESTS_SUBDIR,
    ) -> "DataCatalog":
        root_path = Path(root)
        manifests_path = root_path / manifests_subdir
        manifests: Dict[str, DatasetManifest] = {}
        if manifests_path.is_dir():
            for entry in sorted(manifests_path.glob("*.json")):
                with entry.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                manifest = DatasetManifest.from_dict(payload)
                if manifest.dataset_id in manifests:
                    raise ValueError(
                        f"duplicate dataset_id in catalog: {manifest.dataset_id}"
                    )
                manifests[manifest.dataset_id] = manifest
        return cls(
            root=str(root_path),
            manifests=manifests,
            manifests_dir=str(manifests_path),
        )

    def list(self, kind: Optional[str] = None) -> List[DatasetManifest]:
        if kind is not None and kind not in KNOWN_DATASET_KINDS:
            raise ValueError(
                f"unknown dataset kind: {kind!r} (expected one of {KNOWN_DATASET_KINDS})"
            )
        items = [
            manifest
            for manifest in self._manifests.values()
            if kind is None or manifest.kind == kind
        ]
        items.sort(key=lambda manifest: manifest.dataset_id)
        return items

    def has(self, dataset_id: str) -> bool:
        return dataset_id in self._manifests

    def get(self, dataset_id: str) -> DatasetManifest:
        if dataset_id not in self._manifests:
            raise KeyError(f"dataset not found in catalog: {dataset_id}")
        return self._manifests[dataset_id]

    def checksum_file(self, dataset_id: str, relative_path: str) -> str:
        """Return the on-disk SHA-256 for a file referenced by a dataset."""
        manifest = self.get(dataset_id)
        for entry in manifest.files:
            if entry.path == relative_path:
                return sha256_file(str(Path(self._root) / entry.path))
        raise KeyError(
            f"file {relative_path!r} is not referenced by dataset {dataset_id!r}"
        )

    def reproducibility_metadata(self, dataset_id: str) -> Dict[str, Any]:
        manifest = self.get(dataset_id)
        return {
            "dataset_id": manifest.dataset_id,
            "manifest_hash": manifest.stable_hash(),
            "manifest_version": manifest.version,
            "kind": manifest.kind,
            "source": manifest.source,
            "imported_at": manifest.imported_at,
            "start_date": manifest.start_date,
            "end_date": manifest.end_date,
            "symbols": list(manifest.symbols),
            "benchmarks": list(manifest.benchmarks),
            "file_checksums": {
                entry.path: entry.sha256 for entry in manifest.files
            },
            "root": self._root,
        }

    def validate(self, dataset_id: str) -> DatasetValidationResult:
        manifest = self.get(dataset_id)
        result = DatasetValidationResult(dataset_id=dataset_id, root=self._root)
        root = Path(self._root)
        for entry in manifest.files:
            file_path = root / entry.path
            if not file_path.is_file():
                result.add_error(f"missing file: {entry.path}")
                continue
            actual_size = file_path.stat().st_size
            if actual_size != entry.size_bytes:
                result.add_error(
                    f"size mismatch for {entry.path}: "
                    f"manifest={entry.size_bytes}, disk={actual_size}"
                )
            actual_sha = sha256_file(str(file_path))
            if actual_sha != entry.sha256:
                result.add_error(
                    f"checksum mismatch for {entry.path}: "
                    f"manifest={entry.sha256[:12]}..., disk={actual_sha[:12]}..."
                )
            if entry.schema:
                self._validate_schema(entry, file_path, result)
            else:
                result.add_warning(f"no schema declared for {entry.path}")
            result.checked_files += 1
        return result

    def validate_all(self) -> List[DatasetValidationResult]:
        return [self.validate(dataset_id) for dataset_id in sorted(self._manifests)]

    def _validate_schema(
        self,
        entry: DatasetFile,
        file_path: Path,
        result: DatasetValidationResult,
    ) -> None:
        suffix = file_path.suffix.lower()
        expected = list(entry.schema)
        if suffix == ".csv":
            try:
                header = _read_csv_header(file_path)
            except (OSError, UnicodeDecodeError) as exc:
                result.add_error(f"could not read CSV header for {entry.path}: {exc}")
                return
            if header != expected:
                result.add_error(
                    f"schema mismatch for {entry.path}: "
                    f"expected={expected}, header={header}"
                )
        elif suffix == ".json":
            try:
                keys = _read_json_keys(file_path)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                result.add_error(f"could not read JSON schema for {entry.path}: {exc}")
                return
            if keys is None:
                result.add_warning(
                    f"schema declared but {entry.path} is not a JSON object or record list"
                )
                return
            missing = [key for key in expected if key not in keys]
            if missing:
                result.add_error(
                    f"schema mismatch for {entry.path}: missing keys {missing}"
                )
        else:
            result.add_warning(
                f"schema declared but {entry.path} has unsupported extension for schema check"
            )


def build_dataset_manifest(
    root: str,
    dataset_id: str,
    kind: str,
    files: List[Dict[str, Any]],
    description: str = "",
    source: str = "",
    symbols: Iterable[str] = (),
    benchmarks: Iterable[str] = (),
    start_date: str = "",
    end_date: str = "",
    imported_at: str = "",
    version: str = DEFAULT_MANIFEST_VERSION,
    notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> DatasetManifest:
    """Compute a manifest by hashing local files.

    Operator utility used to prepare immutable manifests before writing
    them to disk. Not called by :class:`DataCatalog` reads.
    """
    root_path = Path(root)
    dataset_files: List[DatasetFile] = []
    for entry in files:
        relative_path = entry["path"]
        file_path = root_path / relative_path
        if not file_path.is_file():
            raise FileNotFoundError(f"file not found while building manifest: {file_path}")
        dataset_files.append(
            DatasetFile(
                path=relative_path,
                sha256=sha256_file(str(file_path)),
                size_bytes=file_path.stat().st_size,
                row_count=entry.get("row_count"),
                schema=tuple(entry.get("schema") or ()),
            )
        )
    return DatasetManifest(
        dataset_id=dataset_id,
        kind=kind,
        description=description,
        source=source,
        symbols=tuple(symbols),
        benchmarks=tuple(benchmarks),
        start_date=start_date,
        end_date=end_date,
        imported_at=imported_at,
        version=version,
        files=tuple(dataset_files),
        notes=notes,
        metadata=dict(metadata or {}),
    )
