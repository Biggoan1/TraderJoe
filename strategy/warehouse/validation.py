"""Warehouse integrity validation.

Tenth Phase 5.6 implementation card ``t_phase56_validation``.
Validates the on-disk Parquet files + catalog manifests against
a set of integrity invariants and produces a
:class:`ValidationReport` — a research artifact suitable for
promotion evidence.

Invariants checked (this card):

* Manifest file present, valid JSON, satisfies
  :class:`~strategy.data_catalog.DatasetManifest.validate`
* Every ``DatasetFile`` entry exists on disk
* sha256 matches manifest declaration
* Row count matches manifest declaration
* Parquet schema matches
  :data:`strategy.warehouse.parquet_io.CANONICAL_SCHEMA`
* OHLCV invariants:
  * ``low <= open, close <= high`` (belt-and-braces vs Bar
    invariants at write time)
  * ``volume >= 0``
  * no NaN in required fields
* No duplicate ``(symbol, timestamp)`` within the dataset
* Timestamps parseable as ISO 8601
* Dataset lifecycle transitions: ``unvalidated → validated``
  or ``unvalidated → quarantined``.  This module never
  advances state on its own — it produces a report and lets
  the operator decide.

Not in scope for this card:

* Trading-calendar alignment / holiday mismatches / DST
  anomalies — those land in ``t_phase56_gap_detection``.
* Corporate-action cross-checks — need the splits.db /
  dividends.db metadata tables from a later card.
* Cross-dataset comparison — a separate cross-provider card.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from strategy.data_catalog import DatasetFile, DatasetManifest
from strategy.local_warehouse import (
    STATUS_QUARANTINED,
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    STATUS_VALIDATING,
    WarehouseIntegrityError,
    WarehouseLayout,
    read_manifest,
    write_manifest,
)
from strategy.warehouse.parquet_io import (
    CANONICAL_SCHEMA,
    read_bars,
)


# ---------------------------------------------------------------------------
# Errors + severities
# ---------------------------------------------------------------------------


class ValidationError(WarehouseIntegrityError):
    """Raised when validation cannot even begin (missing
    manifest, corrupt JSON, malformed dataset id).
    """


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

KNOWN_SEVERITIES: Tuple[str, ...] = (SEVERITY_ERROR, SEVERITY_WARNING)


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationFinding:
    """One issue surfaced during a validation pass."""

    kind: str
    severity: str
    message: str
    subject: str = ""  # e.g. relative file path or "manifest"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Aggregate report for one dataset validation pass.

    ``ok`` is ``True`` iff no findings had severity
    :data:`SEVERITY_ERROR`.  Warnings never fail a pass.
    """

    dataset_id: str
    validation_status_before: str
    ok: bool
    total_findings: int
    error_count: int
    warning_count: int
    files_checked: int
    rows_checked: int
    findings: Tuple[ValidationFinding, ...]
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "validation_status_before": self.validation_status_before,
            "ok": self.ok,
            "total_findings": self.total_findings,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "files_checked": self.files_checked,
            "rows_checked": self.rows_checked,
            "findings": [f.to_dict() for f in self.findings],
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _sha256_file(path: Path, chunk: int = 65536) -> Tuple[str, int]:
    import hashlib

    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_dataset(
    layout: WarehouseLayout,
    dataset_id: str,
) -> ValidationReport:
    """Validate one dataset and return a :class:`ValidationReport`.

    This function is pure — it never mutates the manifest or the
    dataset files.  Callers who want to promote the dataset's
    ``validation_status`` based on the report use
    :func:`apply_validation_report` explicitly.
    """
    findings: List[ValidationFinding] = []
    files_checked = 0
    rows_checked = 0
    status_before = ""

    try:
        payload = read_manifest(layout, dataset_id)
    except WarehouseIntegrityError as exc:
        raise ValidationError(str(exc)) from exc
    try:
        manifest = DatasetManifest.from_dict(payload)
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            f"manifest {dataset_id!r} failed schema validation: {exc}"
        ) from exc
    status_before = manifest.validation_status

    # ------------------------------------------------------------------
    # File-level checks
    # ------------------------------------------------------------------
    seen_keys: Dict[Tuple[str, str], str] = {}
    for entry in manifest.files:
        target = layout.root / entry.path
        if not target.is_file():
            findings.append(
                ValidationFinding(
                    kind="missing_file",
                    severity=SEVERITY_ERROR,
                    message=f"file {entry.path!r} is missing on disk",
                    subject=entry.path,
                )
            )
            continue
        files_checked += 1

        # sha256 + size
        actual_sha, actual_size = _sha256_file(target)
        if actual_sha != entry.sha256:
            findings.append(
                ValidationFinding(
                    kind="checksum_mismatch",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"sha256 mismatch: manifest={entry.sha256[:12]}..., "
                        f"disk={actual_sha[:12]}..."
                    ),
                    subject=entry.path,
                )
            )
        if actual_size != entry.size_bytes:
            findings.append(
                ValidationFinding(
                    kind="size_mismatch",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"size mismatch: manifest={entry.size_bytes}, "
                        f"disk={actual_size}"
                    ),
                    subject=entry.path,
                )
            )

        # Schema + row-level checks — only for Parquet files.
        if not target.suffix.lower() == ".parquet":
            continue
        schema_ok, schema_findings = _check_parquet_schema(target, entry.path)
        findings.extend(schema_findings)
        if not schema_ok:
            continue

        try:
            bars = read_bars(layout, [entry.path])
        except WarehouseIntegrityError as exc:
            findings.append(
                ValidationFinding(
                    kind="unreadable_parquet",
                    severity=SEVERITY_ERROR,
                    message=str(exc),
                    subject=entry.path,
                )
            )
            continue
        rows_checked += len(bars)
        if entry.row_count is not None and entry.row_count != len(bars):
            findings.append(
                ValidationFinding(
                    kind="row_count_mismatch",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"row_count mismatch: manifest={entry.row_count}, "
                        f"disk={len(bars)}"
                    ),
                    subject=entry.path,
                )
            )

        # Row-level OHLCV + duplicate + timestamp checks
        for bar in bars:
            _check_bar_invariants(bar, entry.path, findings)
            key = (bar.symbol, bar.timestamp)
            if key in seen_keys:
                findings.append(
                    ValidationFinding(
                        kind="duplicate_bar",
                        severity=SEVERITY_ERROR,
                        message=(
                            f"duplicate ({bar.symbol}, {bar.timestamp}) "
                            f"between {seen_keys[key]!r} and {entry.path!r}"
                        ),
                        subject=entry.path,
                    )
                )
            else:
                seen_keys[key] = entry.path

    error_count = sum(1 for f in findings if f.severity == SEVERITY_ERROR)
    warning_count = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
    ok = error_count == 0
    return ValidationReport(
        dataset_id=dataset_id,
        validation_status_before=status_before,
        ok=ok,
        total_findings=len(findings),
        error_count=error_count,
        warning_count=warning_count,
        files_checked=files_checked,
        rows_checked=rows_checked,
        findings=tuple(findings),
        generated_at=_utc_now_iso(),
    )


def _check_parquet_schema(
    target: Path, subject: str
) -> Tuple[bool, List[ValidationFinding]]:
    findings: List[ValidationFinding] = []
    try:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(str(target))
    except Exception as exc:  # noqa: BLE001 — malformed Parquet
        findings.append(
            ValidationFinding(
                kind="unreadable_parquet",
                severity=SEVERITY_ERROR,
                message=f"cannot open {target}: {exc}",
                subject=subject,
            )
        )
        return False, findings
    actual = [pf.schema_arrow.field(i).name for i in range(len(pf.schema_arrow))]
    expected = [f.name for f in CANONICAL_SCHEMA]
    if actual != expected:
        findings.append(
            ValidationFinding(
                kind="schema_mismatch",
                severity=SEVERITY_ERROR,
                message=(
                    f"expected {expected} columns; got {actual}"
                ),
                subject=subject,
            )
        )
        return False, findings
    for expected_field in CANONICAL_SCHEMA:
        got = pf.schema_arrow.field(expected_field.name)
        if got.type != expected_field.type:
            findings.append(
                ValidationFinding(
                    kind="schema_mismatch",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"{expected_field.name}: expected {expected_field.type}, "
                        f"got {got.type}"
                    ),
                    subject=subject,
                )
            )
    return True, findings


def _check_bar_invariants(bar, subject: str, findings: List[ValidationFinding]) -> None:
    # OHLC invariants — bar_write ensures these but re-check on
    # read for belt-and-braces safety.
    if bar.low > bar.high:
        findings.append(
            ValidationFinding(
                kind="ohlc_invariant",
                severity=SEVERITY_ERROR,
                message=f"low>{bar.high} for {bar.symbol}@{bar.timestamp}",
                subject=subject,
            )
        )
    for name, value in (("open", bar.open), ("close", bar.close)):
        if value < bar.low or value > bar.high:
            findings.append(
                ValidationFinding(
                    kind="ohlc_invariant",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"{name}={value} outside [low={bar.low},high={bar.high}] "
                        f"for {bar.symbol}@{bar.timestamp}"
                    ),
                    subject=subject,
                )
            )
    if bar.volume < 0:
        findings.append(
            ValidationFinding(
                kind="negative_volume",
                severity=SEVERITY_ERROR,
                message=(
                    f"volume={bar.volume} for {bar.symbol}@{bar.timestamp}"
                ),
                subject=subject,
            )
        )
    for name, value in (
        ("open", bar.open), ("high", bar.high), ("low", bar.low),
        ("close", bar.close), ("volume", bar.volume),
    ):
        if isinstance(value, float) and math.isnan(value):
            findings.append(
                ValidationFinding(
                    kind="nan_field",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"{name} is NaN for {bar.symbol}@{bar.timestamp}"
                    ),
                    subject=subject,
                )
            )
    if not _ISO_PREFIX.match(bar.timestamp):
        findings.append(
            ValidationFinding(
                kind="malformed_timestamp",
                severity=SEVERITY_ERROR,
                message=(
                    f"timestamp {bar.timestamp!r} does not start with YYYY-MM-DD"
                ),
                subject=subject,
            )
        )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_validation_report(
    layout: WarehouseLayout,
    report: ValidationReport,
) -> None:
    """Promote or quarantine the dataset based on ``report.ok``.

    * ``ok=True``  → status becomes ``validated``.
    * ``ok=False`` → status becomes ``quarantined``.

    Uses :func:`~strategy.local_warehouse.write_manifest` with
    ``force=True`` since the caller has explicitly asked for a
    lifecycle transition based on an integrity report.
    """
    payload = read_manifest(layout, report.dataset_id)
    payload["validation_status"] = (
        STATUS_VALIDATED if report.ok else STATUS_QUARANTINED
    )
    write_manifest(layout, report.dataset_id, payload, force=True)


def write_report(
    report: ValidationReport,
    output_dir: Path,
) -> Path:
    """Persist ``report`` under ``output_dir`` as
    ``<dataset_id>-<timestamp>.json`` and return the path.

    Filename includes the report's ``generated_at`` so multiple
    passes on the same dataset don't overwrite each other.  The
    directory is created if needed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = re.sub(r"[^0-9A-Za-z]", "-", report.generated_at)
    target = output_dir / f"{report.dataset_id}-{safe_ts}.json"
    target.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


__all__ = [
    "KNOWN_SEVERITIES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "ValidationError",
    "ValidationFinding",
    "ValidationReport",
    "apply_validation_report",
    "validate_dataset",
    "write_report",
]
