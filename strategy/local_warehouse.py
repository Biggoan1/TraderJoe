"""Local Historical Warehouse — layout and integrity.

Phase 5.6 foundation card ``t_phase56_local_warehouse``.  Ships the
canonical on-disk directory tree for the Historical Data Warehouse
and enforces immutability of validated datasets.

This module is intentionally narrow:

* Directory layout (constants + a frozen ``WarehouseLayout`` model)
* Deterministic path helpers for datasets / manifests / versions
* Layout verification against a well-formed tree
* Immutable-manifest guarantee — a validated manifest is never
  overwritten unless the caller explicitly passes ``force=True``

It ships **no bar I/O**, **no Parquet** support, **no DuckDB**
query engine, **no provider** implementations, and **no import
pipeline**.  Those land in later Phase 5.6 cards (in dependency
order: ``t_phase56_catalog`` → ``t_phase56_parquet_storage`` →
``t_phase56_duckdb_queries`` → ``t_phase56_provider_plugins`` → …).

Design doc:
``docs/architecture/phase-5-6-historical-warehouse.md``
(§Architecture.2 + §Architecture.8).

Read-only guarantees (enforced by tests in
``tests/test_local_warehouse.py``):

* Never places, submits, cancels, or replaces orders.
* Never imports ``trader``, ``crypto_trader``, ``trader_cli``,
  ``telegram_approvals``, or ``strategy.runner``.
* Never mutates the global :class:`~strategy.config.FeatureFlags`.
* Never constructs an ``ApprovalRecord``.
* Never advances ``PromotionEntry`` state.
* Reads only from the ``WAREHOUSE_*`` env-var namespace — disjoint
  from ``ALPACA_*``, ``APCA_*``, ``RESEARCH_ALPACA_*``,
  ``CRYPTO_ALPACA_*``.

Terminology: validation, replay, research, acquisition.  Never
"training".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from strategy.market_data_provider import AssetClass, BarInterval


# ---------------------------------------------------------------------------
# Env namespace
# ---------------------------------------------------------------------------


WAREHOUSE_ROOT_ENV = "WAREHOUSE_ROOT"
DEFAULT_WAREHOUSE_ROOT = "market_data"


# ---------------------------------------------------------------------------
# Directory tree constants
# ---------------------------------------------------------------------------


_EQUITIES = "equities"
_CRYPTO = "crypto"
_OPTIONS = "options"
_METADATA = "metadata"
_MANIFESTS = "manifests"
_VERSIONS = "versions"

_DAILY = "daily"
_HOURLY = "hourly"
_MINUTE = "minute"

# Directories the canonical warehouse tree MUST contain.  ``create()``
# builds them; ``verify()`` refuses to run against a tree missing any
# of them.  Kept in insertion order so error messages read
# predictably.
REQUIRED_SUBDIRS: Tuple[str, ...] = (
    f"{_EQUITIES}/{_DAILY}",
    f"{_EQUITIES}/{_HOURLY}",
    f"{_EQUITIES}/{_MINUTE}",
    f"{_CRYPTO}",
    f"{_OPTIONS}",
    f"{_METADATA}",
    f"{_MANIFESTS}",
    f"{_VERSIONS}",
)


# ---------------------------------------------------------------------------
# Validation status vocabulary
# ---------------------------------------------------------------------------


STATUS_UNVALIDATED = "unvalidated"
STATUS_VALIDATING = "validating"
STATUS_VALIDATED = "validated"
STATUS_QUARANTINED = "quarantined"

KNOWN_STATUSES: Tuple[str, ...] = (
    STATUS_UNVALIDATED,
    STATUS_VALIDATING,
    STATUS_VALIDATED,
    STATUS_QUARANTINED,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WarehouseIntegrityError(RuntimeError):
    """Raised when a warehouse operation would violate the
    integrity or immutability guarantees:

    * a required directory is missing or is not actually a directory
    * an attempt to overwrite a validated manifest without ``force``
    * a malformed dataset id or interval / asset-class combination
      with no defined partition
    * a manifest file that is not valid JSON
    """


# ---------------------------------------------------------------------------
# Partition helpers
# ---------------------------------------------------------------------------


def _asset_subdir(asset_class: AssetClass) -> str:
    if asset_class in (AssetClass.EQUITY, AssetClass.ETF):
        return _EQUITIES
    if asset_class is AssetClass.CRYPTO:
        return _CRYPTO
    if asset_class is AssetClass.OPTION:
        return _OPTIONS
    raise WarehouseIntegrityError(
        f"asset_class {asset_class.name!r} has no warehouse partition yet"
    )


def _interval_subdir(interval: BarInterval) -> str:
    if interval is BarInterval.DAILY:
        return _DAILY
    if interval is BarInterval.HOURLY:
        return _HOURLY
    if interval in (
        BarInterval.MINUTE_1,
        BarInterval.MINUTE_5,
        BarInterval.MINUTE_15,
        BarInterval.MINUTE_30,
        BarInterval.SECOND_1,
    ):
        return _MINUTE
    raise WarehouseIntegrityError(
        f"interval {interval.name!r} has no warehouse partition yet"
    )


# ---------------------------------------------------------------------------
# Layout model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WarehouseLayout:
    """Frozen model of the on-disk warehouse layout.

    ``root`` may point at a scratch directory during tests or at a
    long-lived location in production.  Every downstream path is
    derived from ``root`` — the model itself never reads or writes
    the filesystem outside of :meth:`create`, :meth:`verify`, and
    the manifest helpers.

    Two instances with the same ``root`` are equal and hash the same,
    so the model is safe as a dict key in caches.
    """

    root: Path

    @classmethod
    def default(cls) -> "WarehouseLayout":
        """Layout rooted at :data:`DEFAULT_WAREHOUSE_ROOT`."""
        return cls(root=Path(DEFAULT_WAREHOUSE_ROOT))

    @classmethod
    def from_env(
        cls, env: Optional[Mapping[str, str]] = None
    ) -> "WarehouseLayout":
        """Layout rooted at ``WAREHOUSE_ROOT`` (or the default if
        unset).  Reads only from the ``WAREHOUSE_*`` env namespace —
        never touches provider credential vars.
        """
        source = env if env is not None else os.environ
        root_str = source.get(WAREHOUSE_ROOT_ENV, DEFAULT_WAREHOUSE_ROOT)
        if not root_str:
            root_str = DEFAULT_WAREHOUSE_ROOT
        return cls(root=Path(root_str))

    # -- canonical subdirectory accessors --------------------------------

    @property
    def equities_daily_dir(self) -> Path:
        return self.root / _EQUITIES / _DAILY

    @property
    def equities_hourly_dir(self) -> Path:
        return self.root / _EQUITIES / _HOURLY

    @property
    def equities_minute_dir(self) -> Path:
        return self.root / _EQUITIES / _MINUTE

    @property
    def crypto_dir(self) -> Path:
        return self.root / _CRYPTO

    @property
    def options_dir(self) -> Path:
        return self.root / _OPTIONS

    @property
    def metadata_dir(self) -> Path:
        return self.root / _METADATA

    @property
    def manifests_dir(self) -> Path:
        return self.root / _MANIFESTS

    @property
    def versions_dir(self) -> Path:
        return self.root / _VERSIONS

    # -- tree lifecycle --------------------------------------------------

    def create(self) -> None:
        """Create every directory in :data:`REQUIRED_SUBDIRS`.

        Idempotent — calling repeatedly during setup is safe and
        never rewrites existing files.  Uses ``mkdir(parents=True,
        exist_ok=True)`` so a partially-populated tree is completed
        rather than rejected.
        """
        for sub in REQUIRED_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def verify(self) -> None:
        """Verify every required directory exists and is a directory.

        Raises :class:`WarehouseIntegrityError` describing the
        offending paths.  Never mutates the tree.
        """
        missing: List[str] = []
        wrong_kind: List[str] = []
        for sub in REQUIRED_SUBDIRS:
            path = self.root / sub
            if not path.exists():
                missing.append(sub)
            elif not path.is_dir():
                wrong_kind.append(sub)
        problems: List[str] = []
        if missing:
            problems.append(
                f"missing required directories: {missing}"
            )
        if wrong_kind:
            problems.append(
                f"required paths that are not directories: {wrong_kind}"
            )
        if problems:
            raise WarehouseIntegrityError(
                f"warehouse layout at {self.root} failed verification: "
                + "; ".join(problems)
            )

    # -- deterministic path helpers --------------------------------------

    def dataset_dir(
        self,
        dataset_id: str,
        asset_class: AssetClass,
        interval: BarInterval,
    ) -> Path:
        """Deterministic per-dataset directory path.

        Layout: ``<root>/<asset_subdir>/<interval_subdir>/<dataset_id>/``.

        The returned path is a pure computation — the directory is
        NOT created here.  Callers create it on write.
        """
        _require_dataset_id(dataset_id)
        asset_subdir = _asset_subdir(asset_class)
        interval_subdir = _interval_subdir(interval)
        return self.root / asset_subdir / interval_subdir / dataset_id

    def manifest_path(self, dataset_id: str) -> Path:
        """Deterministic manifest path.

        Layout: ``<root>/manifests/<dataset_id>.json``.
        """
        _require_dataset_id(dataset_id)
        return self.manifests_dir / f"{dataset_id}.json"

    def version_dir(self, dataset_id: str, version: str) -> Path:
        """Deterministic per-version directory for corporate-action
        revisions.

        Layout: ``<root>/versions/<dataset_id>/<version>/``.  ``version``
        is provider-opaque (``v1``, ``v2``, ``alpaca-2026-07-15``, …)
        but must sort lexicographically for lineage queries.
        """
        _require_dataset_id(dataset_id)
        if not version:
            raise WarehouseIntegrityError("version is required")
        return self.versions_dir / dataset_id / version


# ---------------------------------------------------------------------------
# Manifest I/O — immutable by default
# ---------------------------------------------------------------------------


def _require_dataset_id(dataset_id: str) -> None:
    if not dataset_id:
        raise WarehouseIntegrityError("dataset_id is required")
    # Guard against obvious path traversal in a dataset_id — the
    # catalog card will enforce a stricter regex; keep this narrow
    # for now so tests are portable.
    if (
        os.sep in dataset_id
        or "/" in dataset_id
        or "\\" in dataset_id
        or dataset_id in {".", ".."}
    ):
        raise WarehouseIntegrityError(
            f"dataset_id {dataset_id!r} must not contain path separators"
        )


def read_manifest(layout: WarehouseLayout, dataset_id: str) -> Dict[str, Any]:
    """Read a manifest.  Raises
    :class:`WarehouseIntegrityError` if the manifest is missing or
    not valid JSON.
    """
    path = layout.manifest_path(dataset_id)
    if not path.exists():
        raise WarehouseIntegrityError(
            f"manifest for dataset {dataset_id!r} not found at {path}"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WarehouseIntegrityError(
            f"manifest {path} is not valid JSON: {exc}"
        ) from exc


def is_validated(layout: WarehouseLayout, dataset_id: str) -> bool:
    """Return ``True`` iff the dataset has a manifest whose
    ``validation_status`` is :data:`STATUS_VALIDATED`.

    A missing manifest, unreadable manifest, or manifest without a
    status field all return ``False`` — this predicate never raises.
    """
    path = layout.manifest_path(dataset_id)
    if not path.exists():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(manifest, dict):
        return False
    return manifest.get("validation_status") == STATUS_VALIDATED


def refuse_overwrite_of_validated(
    layout: WarehouseLayout,
    dataset_id: str,
    force: bool = False,
) -> None:
    """Immutability guardrail — call before any operation that would
    overwrite dataset data or its manifest.

    Raises :class:`WarehouseIntegrityError` iff the dataset is
    validated and ``force`` is not set.  Passing ``force=True``
    bypasses the check — operators must set it explicitly, and the
    catalog card records the override.
    """
    if force:
        return
    if is_validated(layout, dataset_id):
        raise WarehouseIntegrityError(
            f"dataset {dataset_id!r} is validated; refusing to overwrite. "
            "Pass force=True to override (used for corporate-action "
            "revisions that write a new dataset version)."
        )


def write_manifest(
    layout: WarehouseLayout,
    dataset_id: str,
    manifest: Mapping[str, Any],
    force: bool = False,
) -> Path:
    """Write a manifest atomically.

    By default this call refuses to overwrite an existing manifest
    whose ``validation_status`` is :data:`STATUS_VALIDATED`.  Passing
    ``force=True`` bypasses the check — corporate-action revisions
    that write a new dataset version rely on it.

    The write itself is atomic (temp file + ``os.replace``) so a
    process crash mid-write cannot corrupt an existing manifest.  If
    the manifest already exists and force is set, the previous file
    is atomically replaced.
    """
    _require_dataset_id(dataset_id)
    if not isinstance(manifest, Mapping):
        raise WarehouseIntegrityError(
            "manifest must be a Mapping[str, Any]"
        )
    status = manifest.get("validation_status")
    if status is not None and status not in KNOWN_STATUSES:
        raise WarehouseIntegrityError(
            f"validation_status {status!r} is not one of {KNOWN_STATUSES}"
        )
    refuse_overwrite_of_validated(layout, dataset_id, force=force)

    layout.manifests_dir.mkdir(parents=True, exist_ok=True)
    path = layout.manifest_path(dataset_id)
    # Atomic write: temp file in the same directory + os.replace so
    # concurrent readers see either the old file or the new file,
    # never a partial write.
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(layout.manifests_dir),
        prefix=f".{dataset_id}-",
        suffix=".json.tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(dict(manifest), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, str(path))
    except Exception:
        # Best-effort cleanup of the temp file.  We do not raise a
        # secondary error if unlink fails — the primary error is more
        # useful to the caller.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_WAREHOUSE_ROOT",
    "KNOWN_STATUSES",
    "REQUIRED_SUBDIRS",
    "STATUS_QUARANTINED",
    "STATUS_UNVALIDATED",
    "STATUS_VALIDATED",
    "STATUS_VALIDATING",
    "WAREHOUSE_ROOT_ENV",
    "WarehouseIntegrityError",
    "WarehouseLayout",
    "is_validated",
    "read_manifest",
    "refuse_overwrite_of_validated",
    "write_manifest",
]
