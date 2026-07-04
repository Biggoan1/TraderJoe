"""Dataset versioning + lineage for the Phase 5.6 warehouse.

Ninth Phase 5.6 implementation card ``t_phase56_data_versioning``.
Extends the catalog's ``parent_dataset_id`` +
``corporate_action_version`` + ``adjustment_version`` scaffolding
with lineage traversal helpers, version-diff reports, and a
guarded ``derive_next_version`` that produces a new dataset id
from an existing one — never mutating the parent.

Read-only guarantees:

* Never overwrites a validated dataset — new versions live at a
  new ``dataset_id``.
* Never fetches from a provider.
* Never touches live-trading paths.

The revision workflow this module supports:

1. Alpaca publishes a late split for AAPL.
2. Operator fetches the revised bars into a new dataset
   (``aapl-2020-v2`` derived from ``aapl-2020``).
3. ``compare_versions(parent, child, layout)`` emits a
   :class:`VersionDiff` describing which bars changed value
   between the two versions.
4. Downstream research pins to whichever version its analysis
   used — the parent remains reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.data_catalog import DataCatalog, DatasetManifest
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    WarehouseIntegrityError,
    WarehouseLayout,
    read_manifest,
    write_manifest,
)
from strategy.market_data_provider import AssetClass, Bar, BarInterval
from strategy.warehouse.parquet_io import read_bars, scan_parquet_files


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VersioningError(WarehouseIntegrityError):
    """Raised when a versioning operation would violate lineage
    invariants (parent missing, would-mutate-validated dataset,
    circular lineage).
    """


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionNode:
    """One node in a dataset's version chain.

    Ordered by (root-first, corporate_action_version int,
    corporate_action_version str, dataset_id) — the same rule
    ``DataCatalog.versions`` applies.
    """

    dataset_id: str
    parent_dataset_id: str
    corporate_action_version: str
    adjustment_version: str
    validation_status: str
    imported_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "parent_dataset_id": self.parent_dataset_id,
            "corporate_action_version": self.corporate_action_version,
            "adjustment_version": self.adjustment_version,
            "validation_status": self.validation_status,
            "imported_at": self.imported_at,
        }


@dataclass(frozen=True)
class BarDiff:
    """One row-level difference between two dataset versions.

    ``old_bar`` / ``new_bar`` are the pre / post values.  ``kind``
    is ``"added"`` when the row exists only in the child,
    ``"removed"`` when only in the parent, and ``"changed"`` when
    both sides have the (symbol, timestamp) but with different
    values.
    """

    symbol: str
    timestamp: str
    kind: str  # "added" | "removed" | "changed"
    old_close: Optional[float]
    new_close: Optional[float]
    old_volume: Optional[float]
    new_volume: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "old_close": self.old_close,
            "new_close": self.new_close,
            "old_volume": self.old_volume,
            "new_volume": self.new_volume,
        }


@dataclass(frozen=True)
class VersionDiff:
    """Aggregate diff between two dataset versions."""

    parent_dataset_id: str
    child_dataset_id: str
    total_added: int
    total_removed: int
    total_changed: int
    changes: Tuple[BarDiff, ...]
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_dataset_id": self.parent_dataset_id,
            "child_dataset_id": self.child_dataset_id,
            "total_added": self.total_added,
            "total_removed": self.total_removed,
            "total_changed": self.total_changed,
            "changes": [c.to_dict() for c in self.changes],
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VERSION_SUFFIX = re.compile(r"^(.*?)-v(\d+)$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _version_sort_key(node: VersionNode) -> Tuple[int, int, str, str]:
    is_root = 0 if not node.parent_dataset_id else 1
    try:
        version_int = int(node.corporate_action_version)
    except (TypeError, ValueError):
        version_int = 0
    return (is_root, version_int, node.corporate_action_version or "", node.dataset_id)


def _to_node(manifest: DatasetManifest) -> VersionNode:
    return VersionNode(
        dataset_id=manifest.dataset_id,
        parent_dataset_id=manifest.parent_dataset_id,
        corporate_action_version=manifest.corporate_action_version,
        adjustment_version=manifest.adjustment_version,
        validation_status=manifest.validation_status,
        imported_at=manifest.imported_at,
    )


# ---------------------------------------------------------------------------
# Lineage traversal
# ---------------------------------------------------------------------------


def version_chain(catalog: DataCatalog, dataset_id: str) -> List[VersionNode]:
    """Return the ordered version chain rooted at ``dataset_id``.

    The chain includes the root (if present in the catalog) plus
    every manifest whose ``parent_dataset_id`` transitively resolves
    to ``dataset_id``.  Ordering matches
    :meth:`DataCatalog.versions`.
    """
    manifests = catalog.versions(dataset_id)
    return sorted((_to_node(m) for m in manifests), key=_version_sort_key)


def latest_version(
    catalog: DataCatalog, dataset_id: str
) -> Optional[VersionNode]:
    """Return the most recent version in the chain, or ``None``
    when no manifest matches.  "Most recent" = highest
    ``corporate_action_version`` (numeric ascending, string
    fallback, then dataset_id lexical).
    """
    chain = version_chain(catalog, dataset_id)
    return chain[-1] if chain else None


def parent_manifest(
    catalog: DataCatalog, dataset_id: str
) -> Optional[DatasetManifest]:
    """Return the parent manifest of ``dataset_id`` (its
    ``parent_dataset_id``'s manifest) or ``None`` when it's a root
    or the parent isn't in the catalog.
    """
    if not catalog.has(dataset_id):
        raise VersioningError(
            f"dataset {dataset_id!r} not found in catalog"
        )
    manifest = catalog.get(dataset_id)
    if not manifest.parent_dataset_id:
        return None
    if not catalog.has(manifest.parent_dataset_id):
        return None
    return catalog.get(manifest.parent_dataset_id)


# ---------------------------------------------------------------------------
# Derive next version
# ---------------------------------------------------------------------------


def derive_next_version(
    parent_manifest: DatasetManifest,
    revision_source: str = "",
    adjustment_version: str = "",
    corporate_action_version: str = "",
) -> Dict[str, Any]:
    """Derive a fresh manifest payload for the next version of a
    dataset.

    Returns a manifest dict (ready to pass to
    :func:`strategy.local_warehouse.write_manifest`) that:

    * inherits every field from the parent manifest
    * points ``parent_dataset_id`` at the parent
    * bumps ``corporate_action_version`` (parse-as-int if the
      parent's is int-shaped, otherwise use the caller-supplied
      value)
    * sets ``validation_status`` to ``unvalidated`` — a new
      version starts fresh
    * clears ``files`` so the caller can attach new file entries
      after writing the revised bars

    The new ``dataset_id`` is derived by appending ``-v{next}``
    unless the parent already carries a ``-vN`` suffix, in which
    case the suffix is incremented.

    NEVER modifies the parent's manifest.  Never touches the
    filesystem; the caller writes the returned payload.
    """
    if not parent_manifest.dataset_id:
        raise VersioningError("parent manifest missing dataset_id")

    parent_id = parent_manifest.dataset_id
    parent_ca_version = parent_manifest.corporate_action_version or "0"
    try:
        parent_int = int(parent_ca_version)
    except (TypeError, ValueError):
        parent_int = 0

    if corporate_action_version:
        new_ca_version = corporate_action_version
    else:
        new_ca_version = str(parent_int + 1)

    # Derive the new dataset id: root-v2 if parent already had -vN,
    # otherwise append -v{new_int}.
    match = _VERSION_SUFFIX.match(parent_id)
    if match:
        root_stem, existing = match.groups()
        try:
            existing_int = int(existing)
            next_int = existing_int + 1
        except ValueError:
            next_int = parent_int + 1
        try:
            new_dataset_id = f"{root_stem}-v{int(new_ca_version)}"
        except ValueError:
            new_dataset_id = f"{root_stem}-v{next_int}"
        actual_parent = parent_id
    else:
        try:
            new_dataset_id = f"{parent_id}-v{int(new_ca_version)}"
        except ValueError:
            new_dataset_id = f"{parent_id}-v{parent_int + 1}"
        actual_parent = parent_id

    payload = parent_manifest.to_dict()
    payload["dataset_id"] = new_dataset_id
    payload["parent_dataset_id"] = actual_parent
    payload["corporate_action_version"] = new_ca_version
    payload["validation_status"] = STATUS_UNVALIDATED
    payload["imported_at"] = _utc_now_iso()
    payload["files"] = []
    if adjustment_version:
        payload["adjustment_version"] = adjustment_version
    if revision_source:
        payload["notes"] = revision_source
    return payload


# ---------------------------------------------------------------------------
# Version diff
# ---------------------------------------------------------------------------


def _load_all_bars(
    layout: WarehouseLayout,
    manifest: DatasetManifest,
) -> Dict[Tuple[str, str], Bar]:
    interval = BarInterval(manifest.interval)
    asset_class = AssetClass(manifest.asset_class)
    paths = scan_parquet_files(
        layout, asset_class, interval, dataset_id=manifest.dataset_id
    )
    result: Dict[Tuple[str, str], Bar] = {}
    for rel in paths:
        for bar in read_bars(layout, [rel]):
            result[(bar.symbol, bar.timestamp)] = bar
    return result


def compare_versions(
    layout: WarehouseLayout,
    catalog: DataCatalog,
    parent_dataset_id: str,
    child_dataset_id: str,
) -> VersionDiff:
    """Compute a :class:`VersionDiff` between two catalog datasets.

    Reads both sides through
    :func:`strategy.warehouse.parquet_io.read_bars`, compares by
    ``(symbol, timestamp)``, and records ``added`` / ``removed`` /
    ``changed`` rows.  A ``changed`` row is one where the same
    key exists in both but ``close`` or ``volume`` differs.

    Ordering of the returned ``changes`` tuple is deterministic:
    ``(symbol, timestamp, kind)`` ascending.
    """
    if not catalog.has(parent_dataset_id):
        raise VersioningError(
            f"parent {parent_dataset_id!r} not in catalog"
        )
    if not catalog.has(child_dataset_id):
        raise VersioningError(
            f"child {child_dataset_id!r} not in catalog"
        )

    parent_manifest = catalog.get(parent_dataset_id)
    child_manifest = catalog.get(child_dataset_id)

    if parent_manifest.interval != child_manifest.interval:
        raise VersioningError(
            f"interval mismatch: parent={parent_manifest.interval!r} "
            f"child={child_manifest.interval!r}"
        )
    if parent_manifest.asset_class != child_manifest.asset_class:
        raise VersioningError(
            f"asset_class mismatch: parent={parent_manifest.asset_class!r} "
            f"child={child_manifest.asset_class!r}"
        )

    parent_bars = _load_all_bars(layout, parent_manifest)
    child_bars = _load_all_bars(layout, child_manifest)

    keys = sorted(set(parent_bars) | set(child_bars))
    changes: List[BarDiff] = []
    added = removed = changed = 0
    for key in keys:
        p_bar = parent_bars.get(key)
        c_bar = child_bars.get(key)
        symbol, timestamp = key
        if p_bar is None and c_bar is not None:
            added += 1
            changes.append(
                BarDiff(
                    symbol=symbol, timestamp=timestamp, kind="added",
                    old_close=None, new_close=c_bar.close,
                    old_volume=None, new_volume=c_bar.volume,
                )
            )
        elif c_bar is None and p_bar is not None:
            removed += 1
            changes.append(
                BarDiff(
                    symbol=symbol, timestamp=timestamp, kind="removed",
                    old_close=p_bar.close, new_close=None,
                    old_volume=p_bar.volume, new_volume=None,
                )
            )
        else:
            assert p_bar is not None and c_bar is not None
            if p_bar.close != c_bar.close or p_bar.volume != c_bar.volume:
                changed += 1
                changes.append(
                    BarDiff(
                        symbol=symbol, timestamp=timestamp, kind="changed",
                        old_close=p_bar.close, new_close=c_bar.close,
                        old_volume=p_bar.volume, new_volume=c_bar.volume,
                    )
                )

    return VersionDiff(
        parent_dataset_id=parent_dataset_id,
        child_dataset_id=child_dataset_id,
        total_added=added,
        total_removed=removed,
        total_changed=changed,
        changes=tuple(changes),
        generated_at=_utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# Refuse to mutate validated datasets
# ---------------------------------------------------------------------------


def refuse_if_validated(
    layout: WarehouseLayout,
    dataset_id: str,
) -> None:
    """Raise :class:`VersioningError` if ``dataset_id`` is
    validated.  Called by any workflow that would attempt to
    mutate the dataset (e.g. a revision that overwrites the
    same id instead of writing a new version).
    """
    try:
        payload = read_manifest(layout, dataset_id)
    except WarehouseIntegrityError:
        return
    if payload.get("validation_status") == STATUS_VALIDATED:
        raise VersioningError(
            f"dataset {dataset_id!r} is validated; write a new version "
            "via derive_next_version instead of mutating in place"
        )


__all__ = [
    "BarDiff",
    "VersionDiff",
    "VersionNode",
    "VersioningError",
    "compare_versions",
    "derive_next_version",
    "latest_version",
    "parent_manifest",
    "refuse_if_validated",
    "version_chain",
]
