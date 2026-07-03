"""Research Report Generation.

Read-only renderers that turn Champion/Challenger comparison output and
walk-forward reports into Markdown + JSON research artifacts suitable
for the ``reports/backtests/`` tree.

Every renderer produces a :class:`ResearchReport` bundle containing:

* ``markdown`` — human-readable summary for reviewers
* ``payload``  — machine-readable JSON of the underlying object plus a
  precomputed summary
* ``disagreements`` — flattened, deterministically-ordered list of the
  per-symbol disagreement records
* ``manifest``  — reproducibility metadata (report id, source hashes,
  configuration, generated_at)

Reports do not place orders, fetch live data, mutate feature flags,
or write to any directory the caller has not explicitly named.
Persistence is opt-in via :meth:`ResearchReport.write`.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 3 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from strategy.backtest_lab import stable_hash, stable_json
from strategy.comparison_harness import (
    KNOWN_DISAGREEMENT_KINDS,
    ChampionChallengerComparison,
)
from strategy.walk_forward import WalkForwardReport


REPORT_ID_PREFIX = "rr"
REPORT_FILENAME_MARKDOWN = "report.md"
REPORT_FILENAME_JSON = "report.json"
REPORT_FILENAME_DISAGREEMENTS = "disagreements.json"
REPORT_FILENAME_MANIFEST = "manifest.json"

REPORT_KIND_COMPARISON = "champion_challenger_comparison"
REPORT_KIND_WALK_FORWARD = "walk_forward"

KNOWN_REPORT_KINDS = (REPORT_KIND_COMPARISON, REPORT_KIND_WALK_FORWARD)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ResearchReportPaths:
    """Deterministic locations for a persisted research report."""

    output_dir: str
    report_id: str

    @property
    def report_dir(self) -> str:
        return str(Path(self.output_dir) / self.report_id)

    @property
    def markdown_path(self) -> str:
        return str(Path(self.report_dir) / REPORT_FILENAME_MARKDOWN)

    @property
    def json_path(self) -> str:
        return str(Path(self.report_dir) / REPORT_FILENAME_JSON)

    @property
    def disagreements_path(self) -> str:
        return str(Path(self.report_dir) / REPORT_FILENAME_DISAGREEMENTS)

    @property
    def manifest_path(self) -> str:
        return str(Path(self.report_dir) / REPORT_FILENAME_MANIFEST)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "report_id": self.report_id,
            "report_dir": self.report_dir,
            "markdown_path": self.markdown_path,
            "json_path": self.json_path,
            "disagreements_path": self.disagreements_path,
            "manifest_path": self.manifest_path,
        }


@dataclass
class ResearchReport:
    """Bundle of Markdown + JSON research artifacts for one source object."""

    report_id: str
    kind: str
    title: str
    markdown: str
    payload: Dict[str, Any]
    disagreements: List[Dict[str, Any]]
    manifest: Dict[str, Any]
    generated_at: str

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_REPORT_KINDS:
            raise ValueError(
                f"unknown report kind: {self.kind!r} "
                f"(expected one of {KNOWN_REPORT_KINDS})"
            )

    def to_markdown(self) -> str:
        return self.markdown

    def to_json(self) -> str:
        return stable_json(self.payload)

    def disagreements_json(self) -> str:
        return stable_json({"disagreements": list(self.disagreements)})

    def manifest_json(self) -> str:
        return stable_json(self.manifest)

    def stable_hash(self) -> str:
        """Reproducibility hash: excludes ``generated_at``."""
        data = {
            "kind": self.kind,
            "title": self.title,
            "payload": self.payload,
            "disagreements": list(self.disagreements),
            "manifest": {
                key: value
                for key, value in self.manifest.items()
                if key != "generated_at"
            },
        }
        return stable_hash(data)

    def paths_for(self, output_dir: str) -> ResearchReportPaths:
        return ResearchReportPaths(output_dir=output_dir, report_id=self.report_id)

    def write(self, output_dir: str) -> ResearchReportPaths:
        paths = self.paths_for(output_dir)
        Path(paths.report_dir).mkdir(parents=True, exist_ok=True)
        Path(paths.markdown_path).write_text(self.markdown, encoding="utf-8")
        Path(paths.json_path).write_text(
            json.dumps(self.payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        Path(paths.disagreements_path).write_text(
            json.dumps(
                {"disagreements": list(self.disagreements)},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        Path(paths.manifest_path).write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return paths


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


def _empty_disagreement_counts() -> Dict[str, int]:
    return {kind: 0 for kind in KNOWN_DISAGREEMENT_KINDS}


def _count_by_kind(records: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = _empty_disagreement_counts()
    for record in records:
        kind = record.get("kind")
        if kind in counts:
            counts[kind] = counts[kind] + 1
        elif kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _daily_summary(comparison: ChampionChallengerComparison) -> List[Dict[str, Any]]:
    """One row per event timestamp with per-kind disagreement counts."""
    rows_by_ts: Dict[str, Dict[str, Any]] = {}
    for table in comparison.score_tables:
        rows_by_ts[table.event_timestamp] = {
            "event_timestamp": table.event_timestamp,
            "event_type": table.event_type,
            "event_sequence": table.event_sequence,
            "symbol_count": len(table.rows),
            "disagreement_counts": _empty_disagreement_counts(),
        }
    for record in comparison.disagreements:
        entry = rows_by_ts.setdefault(
            record.event_timestamp,
            {
                "event_timestamp": record.event_timestamp,
                "event_type": record.event_type,
                "event_sequence": 0,
                "symbol_count": 0,
                "disagreement_counts": _empty_disagreement_counts(),
            },
        )
        counts = entry["disagreement_counts"]
        counts[record.kind] = counts.get(record.kind, 0) + 1
    return sorted(rows_by_ts.values(), key=lambda row: row["event_timestamp"])


def _flattened_disagreements(
    comparison: ChampionChallengerComparison,
) -> List[Dict[str, Any]]:
    records = [record.to_dict() for record in comparison.disagreements]
    records.sort(
        key=lambda record: (
            record.get("event_timestamp", ""),
            record.get("event_type", ""),
            record.get("symbol", ""),
            record.get("kind", ""),
        )
    )
    return records


def _comparison_summary(
    comparison: ChampionChallengerComparison,
    disagreements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "champion_id": comparison.metadata.champion_id,
        "challenger_id": comparison.metadata.challenger_id,
        "dataset_id": comparison.metadata.dataset_id,
        "event_count": comparison.metadata.event_count,
        "score_delta_threshold": comparison.metadata.score_delta_threshold,
        "seed": comparison.metadata.seed,
        "total_disagreements": len(disagreements),
        "disagreement_counts": _count_by_kind(disagreements),
        "warnings": list(comparison.warnings),
    }


def _render_comparison_markdown(
    comparison: ChampionChallengerComparison,
    daily_rows: List[Dict[str, Any]],
    disagreements: List[Dict[str, Any]],
    summary: Dict[str, Any],
    report_id: str,
    title: str,
    generated_at: str,
) -> str:
    lines: List[str] = [
        f"# {title}",
        "",
        f"_Report ID: `{report_id}`_",
        f"_Generated: {generated_at}_",
        "",
        "Observational research only. Does not place trades or alter strategy behavior.",
        "",
        "## Summary",
        f"- Champion: `{summary['champion_id']}`",
        f"- Challenger: `{summary['challenger_id']}`",
        f"- Dataset: `{summary['dataset_id']}`",
        f"- Events evaluated: {summary['event_count']}",
        f"- Score-delta threshold: {summary['score_delta_threshold']}",
        f"- Seed: {summary['seed']}",
        f"- Total disagreements: {summary['total_disagreements']}",
    ]
    counts = summary["disagreement_counts"]
    for kind in KNOWN_DISAGREEMENT_KINDS:
        lines.append(f"  - `{kind}`: {counts.get(kind, 0)}")

    lines.extend(["", "## Daily Comparison"])
    if not daily_rows:
        lines.append("_No events in this comparison._")
    else:
        lines.append(
            "| Event Timestamp | Type | Seq | Symbols | "
            + " | ".join(f"`{kind}`" for kind in KNOWN_DISAGREEMENT_KINDS)
            + " |"
        )
        lines.append(
            "|---|---|---|---|"
            + "|".join("---" for _ in KNOWN_DISAGREEMENT_KINDS)
            + "|"
        )
        for row in daily_rows:
            cells = [
                row["event_timestamp"],
                row["event_type"],
                str(row["event_sequence"]),
                str(row["symbol_count"]),
            ]
            for kind in KNOWN_DISAGREEMENT_KINDS:
                cells.append(str(row["disagreement_counts"].get(kind, 0)))
            lines.append("| " + " | ".join(cells) + " |")

    lines.extend(["", "## Disagreements by Kind"])
    grouped: Dict[str, List[Dict[str, Any]]] = {
        kind: [] for kind in KNOWN_DISAGREEMENT_KINDS
    }
    for record in disagreements:
        grouped.setdefault(record["kind"], []).append(record)
    for kind in KNOWN_DISAGREEMENT_KINDS:
        records = grouped.get(kind, [])
        lines.append(f"### `{kind}` ({len(records)})")
        if not records:
            lines.append("_No disagreements._")
            continue
        for record in records:
            lines.append(
                "- "
                f"{record.get('event_timestamp')} "
                f"`{record.get('symbol')}` "
                f"champion_rank={record.get('champion_rank')} "
                f"challenger_rank={record.get('challenger_rank')} "
                f"score_delta={record.get('score_delta')}"
            )

    if summary["warnings"]:
        lines.extend(["", "## Data-Quality Notes"])
        for warning in summary["warnings"]:
            lines.append(f"- {warning}")
    else:
        lines.extend(["", "## Data-Quality Notes", "_None._"])

    lines.extend(
        [
            "",
            "## Reproducibility",
            f"- Report ID: `{report_id}`",
            f"- Comparison run ID: `{comparison.metadata.run_id}`",
            f"- Comparison stable hash: `{comparison.stable_hash()}`",
        ]
    )
    return "\n".join(lines)


def _report_id_from_source_hash(source_hash: str, kind: str) -> str:
    digest = hashlib.sha256(f"{kind}:{source_hash}".encode("utf-8")).hexdigest()
    return f"{REPORT_ID_PREFIX}_{digest[:12]}"


def render_comparison_report(
    comparison: ChampionChallengerComparison,
    title: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> ResearchReport:
    """Render a research report from a Champion/Challenger comparison."""
    generated = generated_at or _utc_now_iso()
    disagreements = _flattened_disagreements(comparison)
    daily_rows = _daily_summary(comparison)
    summary = _comparison_summary(comparison, disagreements)
    source_hash = comparison.stable_hash()
    report_id = _report_id_from_source_hash(source_hash, REPORT_KIND_COMPARISON)
    resolved_title = title or (
        f"Champion vs Challenger Comparison — {comparison.metadata.run_id}"
    )

    payload = {
        "report_id": report_id,
        "kind": REPORT_KIND_COMPARISON,
        "title": resolved_title,
        "summary": summary,
        "daily_summary": daily_rows,
        "comparison": comparison.to_dict(),
    }
    manifest = {
        "report_id": report_id,
        "kind": REPORT_KIND_COMPARISON,
        "source_run_id": comparison.metadata.run_id,
        "source_hash": source_hash,
        "champion_id": comparison.metadata.champion_id,
        "challenger_id": comparison.metadata.challenger_id,
        "dataset_id": comparison.metadata.dataset_id,
        "event_count": comparison.metadata.event_count,
        "seed": comparison.metadata.seed,
        "score_delta_threshold": comparison.metadata.score_delta_threshold,
        "disagreement_counts": summary["disagreement_counts"],
        "generated_at": generated,
    }
    markdown = _render_comparison_markdown(
        comparison=comparison,
        daily_rows=daily_rows,
        disagreements=disagreements,
        summary=summary,
        report_id=report_id,
        title=resolved_title,
        generated_at=generated,
    )
    return ResearchReport(
        report_id=report_id,
        kind=REPORT_KIND_COMPARISON,
        title=resolved_title,
        markdown=markdown,
        payload=payload,
        disagreements=disagreements,
        manifest=manifest,
        generated_at=generated,
    )


# ---------------------------------------------------------------------------
# Walk-forward report
# ---------------------------------------------------------------------------


def _walk_forward_disagreements(wf_report: WalkForwardReport) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for split_result in wf_report.split_results:
        for record in split_result.comparison.disagreements:
            payload = record.to_dict()
            payload["split_id"] = split_result.split.split_id
            payload["split_index"] = split_result.split.index
            records.append(payload)
    records.sort(
        key=lambda record: (
            record.get("split_index", 0),
            record.get("event_timestamp", ""),
            record.get("event_type", ""),
            record.get("symbol", ""),
            record.get("kind", ""),
        )
    )
    return records


def _walk_forward_summary(
    wf_report: WalkForwardReport,
    disagreements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "champion_id": wf_report.champion_id,
        "challenger_id": wf_report.challenger_id,
        "dataset_id": wf_report.dataset_id,
        "seed": wf_report.seed,
        "score_delta_threshold": wf_report.score_delta_threshold,
        "schedule_id": wf_report.schedule.schedule_id,
        "in_sample_days": wf_report.schedule.in_sample_days,
        "out_of_sample_days": wf_report.schedule.out_of_sample_days,
        "step_days": wf_report.schedule.step_days,
        "split_count": len(wf_report.schedule.splits),
        "total_events": wf_report.total_events,
        "total_out_of_sample_events": wf_report.total_out_of_sample_events,
        "total_unassigned_events": wf_report.total_unassigned_events,
        "total_disagreements": len(disagreements),
        "disagreement_counts": _count_by_kind(disagreements),
        "warnings": list(wf_report.warnings),
    }


def _render_walk_forward_markdown(
    wf_report: WalkForwardReport,
    disagreements: List[Dict[str, Any]],
    summary: Dict[str, Any],
    report_id: str,
    title: str,
    generated_at: str,
) -> str:
    lines: List[str] = [
        f"# {title}",
        "",
        f"_Report ID: `{report_id}`_",
        f"_Generated: {generated_at}_",
        "",
        "Observational research only. Does not place trades or alter strategy behavior.",
        "",
        "## Summary",
        f"- Champion: `{summary['champion_id']}`",
        f"- Challenger: `{summary['challenger_id']}`",
        f"- Dataset: `{summary['dataset_id']}`",
        f"- Schedule: `{summary['schedule_id']}`",
        f"  - In-sample days: {summary['in_sample_days']}",
        f"  - Out-of-sample days: {summary['out_of_sample_days']}",
        f"  - Step days: {summary['step_days']}",
        f"  - Splits: {summary['split_count']}",
        f"- Total events: {summary['total_events']}",
        f"- Out-of-sample events: {summary['total_out_of_sample_events']}",
        f"- Unassigned events: {summary['total_unassigned_events']}",
        f"- Score-delta threshold: {summary['score_delta_threshold']}",
        f"- Seed: {summary['seed']}",
        f"- Total disagreements: {summary['total_disagreements']}",
    ]
    counts = summary["disagreement_counts"]
    for kind in KNOWN_DISAGREEMENT_KINDS:
        lines.append(f"  - `{kind}`: {counts.get(kind, 0)}")

    lines.extend(["", "## Split Results"])
    if not wf_report.split_results:
        lines.append("_No splits configured._")
    else:
        header = (
            "| Split | IS Window | OOS Window | Events | "
            + " | ".join(f"`{kind}`" for kind in KNOWN_DISAGREEMENT_KINDS)
            + " |"
        )
        lines.append(header)
        lines.append(
            "|---|---|---|---|"
            + "|".join("---" for _ in KNOWN_DISAGREEMENT_KINDS)
            + "|"
        )
        for split_result in wf_report.split_results:
            split = split_result.split
            cells = [
                f"`{split.split_id}`",
                f"{split.in_sample_start} → {split.in_sample_end}",
                f"{split.out_of_sample_start} → {split.out_of_sample_end}",
                str(split_result.out_of_sample_event_count),
            ]
            for kind in KNOWN_DISAGREEMENT_KINDS:
                cells.append(str(split_result.disagreement_counts.get(kind, 0)))
            lines.append("| " + " | ".join(cells) + " |")

    if summary["warnings"]:
        lines.extend(["", "## Data-Quality Notes"])
        for warning in summary["warnings"]:
            lines.append(f"- {warning}")
    else:
        lines.extend(["", "## Data-Quality Notes", "_None._"])

    lines.extend(
        [
            "",
            "## Reproducibility",
            f"- Report ID: `{report_id}`",
            f"- Walk-forward report ID: `{wf_report.report_id}`",
            f"- Walk-forward stable hash: `{wf_report.stable_hash()}`",
            f"- Schedule stable hash: `{wf_report.schedule.stable_hash()}`",
        ]
    )
    return "\n".join(lines)


def render_walk_forward_report(
    wf_report: WalkForwardReport,
    title: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> ResearchReport:
    """Render a research report from a :class:`WalkForwardReport`."""
    generated = generated_at or _utc_now_iso()
    disagreements = _walk_forward_disagreements(wf_report)
    summary = _walk_forward_summary(wf_report, disagreements)
    source_hash = wf_report.stable_hash()
    report_id = _report_id_from_source_hash(source_hash, REPORT_KIND_WALK_FORWARD)
    resolved_title = title or (
        f"Walk-Forward Comparison — {wf_report.report_id}"
    )

    payload = {
        "report_id": report_id,
        "kind": REPORT_KIND_WALK_FORWARD,
        "title": resolved_title,
        "summary": summary,
        "walk_forward": wf_report.to_dict(),
    }
    # Strip the nested generated_at fields from the payload copy so the
    # resulting stable_hash reflects only the deterministic content.
    payload_walk = payload["walk_forward"]
    payload_walk.pop("generated_at", None)
    for split_payload in payload_walk.get("split_results", []):
        comparison = split_payload.get("comparison", {})
        metadata = comparison.get("metadata", {})
        metadata.pop("generated_at", None)

    manifest = {
        "report_id": report_id,
        "kind": REPORT_KIND_WALK_FORWARD,
        "source_report_id": wf_report.report_id,
        "source_hash": source_hash,
        "schedule_id": wf_report.schedule.schedule_id,
        "schedule_hash": wf_report.schedule.stable_hash(),
        "champion_id": wf_report.champion_id,
        "challenger_id": wf_report.challenger_id,
        "dataset_id": wf_report.dataset_id,
        "seed": wf_report.seed,
        "score_delta_threshold": wf_report.score_delta_threshold,
        "split_count": len(wf_report.schedule.splits),
        "total_events": wf_report.total_events,
        "total_out_of_sample_events": wf_report.total_out_of_sample_events,
        "total_unassigned_events": wf_report.total_unassigned_events,
        "disagreement_counts": summary["disagreement_counts"],
        "generated_at": generated,
    }
    markdown = _render_walk_forward_markdown(
        wf_report=wf_report,
        disagreements=disagreements,
        summary=summary,
        report_id=report_id,
        title=resolved_title,
        generated_at=generated,
    )
    return ResearchReport(
        report_id=report_id,
        kind=REPORT_KIND_WALK_FORWARD,
        title=resolved_title,
        markdown=markdown,
        payload=payload,
        disagreements=disagreements,
        manifest=manifest,
        generated_at=generated,
    )
