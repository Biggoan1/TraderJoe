"""Research run manifest.

Ties every artifact produced by a single research run into one
JSON + Markdown pair, plus a ``reports/latest`` pointer for quick
operator access.  Deterministic except for its ``started_at`` /
``completed_at`` timestamps — everything else derives from the
run's inputs so a rerun with the same inputs produces the same
``stable_hash``.

Never carries credentials.  Never emits provider API keys or the
LLM endpoint URL beyond redaction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import stable_hash


DEFAULT_LATEST_SUBDIR = "latest"
_URL_PATTERN = re.compile(r"^(https?://)[^@\s/]+(@.*)?", re.IGNORECASE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _redact_url(url: str) -> str:
    """Best-effort URL redaction — strip user:password prefix if
    present, and any ``?key=`` / ``?apikey=`` / ``?secret=`` query
    parameters.
    """
    if not url:
        return ""
    without_creds = re.sub(
        r"(https?://)[^@/\s]+@", r"\1", url, count=1
    )
    without_creds = re.sub(
        r"([?&](?:api[_-]?key|secret|token)=)[^&\s]+",
        r"\1***",
        without_creds,
        flags=re.IGNORECASE,
    )
    return without_creds


@dataclass(frozen=True)
class ArtifactRef:
    """One artifact recorded in a run manifest.  ``path`` is
    relative to whatever root the caller passes at write time.
    """

    kind: str
    artifact_id: str
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "path": self.path,
        }


@dataclass(frozen=True)
class ResearchRunManifest:
    """Provenance manifest for a single research run."""

    run_id: str
    dataset_id: str
    dataset_version: str
    symbols: Tuple[str, ...]
    benchmarks: Tuple[str, ...]
    window_start: str
    window_end: str
    provenance_source: str  # warehouse | provider | fixture
    provenance_summary: str
    config_hash: str
    git_commit: str = ""
    model_id: str = ""
    llm_endpoint_redacted: str = ""
    feature_flags_all_disabled: bool = True
    feature_flags_enabled: Tuple[str, ...] = ()
    promotion_state: str = "disabled"
    promotion_approvals: int = 0
    comparison_artifact: Optional[ArtifactRef] = None
    walk_forward_artifact: Optional[ArtifactRef] = None
    walk_forward_research_artifact: Optional[ArtifactRef] = None
    learning_artifact: Optional[ArtifactRef] = None
    analyst_artifacts: Tuple[ArtifactRef, ...] = ()
    dataset_manifest_path: str = ""
    warnings: Tuple[str, ...] = ()
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "symbols": list(self.symbols),
            "benchmarks": list(self.benchmarks),
            "window_start": self.window_start,
            "window_end": self.window_end,
            "provenance_source": self.provenance_source,
            "provenance_summary": self.provenance_summary,
            "config_hash": self.config_hash,
            "git_commit": self.git_commit,
            "model_id": self.model_id,
            "llm_endpoint_redacted": self.llm_endpoint_redacted,
            "feature_flags_all_disabled": self.feature_flags_all_disabled,
            "feature_flags_enabled": list(self.feature_flags_enabled),
            "promotion_state": self.promotion_state,
            "promotion_approvals": self.promotion_approvals,
            "comparison_artifact": (
                self.comparison_artifact.to_dict()
                if self.comparison_artifact
                else None
            ),
            "walk_forward_artifact": (
                self.walk_forward_artifact.to_dict()
                if self.walk_forward_artifact
                else None
            ),
            "walk_forward_research_artifact": (
                self.walk_forward_research_artifact.to_dict()
                if self.walk_forward_research_artifact
                else None
            ),
            "learning_artifact": (
                self.learning_artifact.to_dict()
                if self.learning_artifact
                else None
            ),
            "analyst_artifacts": [
                a.to_dict() for a in self.analyst_artifacts
            ],
            "dataset_manifest_path": self.dataset_manifest_path,
            "warnings": list(self.warnings),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def stable_hash(self) -> str:
        """Deterministic hash independent of started_at /
        completed_at.
        """
        data = self.to_dict()
        data.pop("started_at", None)
        data.pop("completed_at", None)
        return stable_hash(data)

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append(f"# Research Run — {self.run_id}")
        lines.append("")
        lines.append(f"- **Dataset:** `{self.dataset_id}@{self.dataset_version}`")
        lines.append(
            f"- **Window:** `{self.window_start}` → `{self.window_end}`"
        )
        lines.append(f"- **Symbols:** {', '.join(self.symbols)}")
        lines.append(f"- **Benchmarks:** {', '.join(self.benchmarks)}")
        lines.append(
            f"- **Provenance source:** `{self.provenance_source}` — {self.provenance_summary}"
        )
        lines.append(f"- **Config hash:** `{self.config_hash}`")
        if self.git_commit:
            lines.append(f"- **Git commit:** `{self.git_commit}`")
        if self.model_id:
            lines.append(
                f"- **Model:** `{self.model_id}` "
                f"(endpoint: `{self.llm_endpoint_redacted}`)"
            )
        lines.append(
            "- **Feature flags:** "
            + (
                "all disabled"
                if self.feature_flags_all_disabled
                else f"enabled: {list(self.feature_flags_enabled)}"
            )
        )
        lines.append(
            f"- **Promotion state:** `{self.promotion_state}` "
            f"({self.promotion_approvals} approval records)"
        )
        lines.append("")
        lines.append("## Artifacts")
        for label, ref in (
            ("Comparison", self.comparison_artifact),
            ("Walk-forward", self.walk_forward_artifact),
            ("Walk-forward research report", self.walk_forward_research_artifact),
            ("Learning", self.learning_artifact),
        ):
            if ref:
                lines.append(
                    f"- **{label}:** `{ref.artifact_id}` — `{ref.path}`"
                )
        if self.analyst_artifacts:
            lines.append("- **Analyst narratives:**")
            for a in self.analyst_artifacts:
                lines.append(
                    f"  - `{a.artifact_id}` — `{a.path}`"
                )
        if self.warnings:
            lines.append("")
            lines.append("## Warnings")
            for w in self.warnings:
                lines.append(f"- {w}")
        lines.append("")
        lines.append(
            f"_Started: {self.started_at} • Completed: {self.completed_at}_"
        )
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_from_bundle(
    bundle: Any,
    run_id: str,
    started_at: str,
    completed_at: Optional[str] = None,
    git_commit: str = "",
    model_id: str = "",
    llm_endpoint: str = "",
    reports_root: str = "",
) -> ResearchRunManifest:
    """Assemble a manifest from a
    :class:`~strategy.historical_validation.HistoricalValidationBundle`.

    ``llm_endpoint`` is redacted before storage.  Artifact paths
    are relativised to ``reports_root`` when provided.
    """
    completed_at = completed_at or _utc_now_iso()

    def _rel(path: str) -> str:
        if not path or not reports_root:
            return path
        try:
            return str(Path(path).resolve().relative_to(
                Path(reports_root).resolve()
            ))
        except (ValueError, OSError):
            return path

    provenance = getattr(bundle, "dataset_provenance", {}) or {}
    source = provenance.get("source") or "fixture"
    datasets = provenance.get("datasets") or []
    dataset_id = ""
    dataset_version = ""
    if datasets:
        dataset_id = datasets[0].get("dataset_id", "")
        dataset_version = datasets[0].get("version", "")
    elif provenance.get("provider_name"):
        dataset_id = provenance.get("provider_name", "")
    provenance_summary = ", ".join(
        f"{d.get('dataset_id','')}@{d.get('version','')}"
        for d in datasets
    ) if datasets else source

    config = getattr(bundle, "config", None)
    symbols = tuple(getattr(config, "symbols", ()))
    benchmarks = tuple(getattr(config, "benchmarks", ()))
    window_start = getattr(config, "window_start", "") if config else ""
    window_end = getattr(config, "window_end", "") if config else ""
    config_hash = config.stable_hash() if config else ""

    comparison_ref: Optional[ArtifactRef] = None
    comparison_report = getattr(bundle, "comparison_report", None)
    if comparison_report is not None:
        paths = getattr(bundle, "comparison_report_paths", None)
        p = ""
        if paths is not None:
            output_dir = getattr(paths, "output_dir", "")
            report_id = getattr(paths, "report_id", "")
            p = _rel(str(Path(output_dir) / report_id)) if output_dir else ""
        comparison_ref = ArtifactRef(
            kind="comparison",
            artifact_id=comparison_report.report_id,
            path=p,
        )

    walk_forward_ref: Optional[ArtifactRef] = None
    walk_forward_report = getattr(bundle, "walk_forward_report", None)
    if walk_forward_report is not None:
        walk_forward_ref = ArtifactRef(
            kind="walk_forward",
            artifact_id=walk_forward_report.report_id,
            path="",
        )

    walk_forward_research_ref: Optional[ArtifactRef] = None
    walk_forward_research_report = getattr(
        bundle, "walk_forward_research_report", None
    )
    if walk_forward_research_report is not None:
        paths = getattr(bundle, "walk_forward_report_paths", None)
        p = ""
        if paths is not None:
            output_dir = getattr(paths, "output_dir", "")
            report_id = getattr(paths, "report_id", "")
            p = _rel(str(Path(output_dir) / report_id)) if output_dir else ""
        walk_forward_research_ref = ArtifactRef(
            kind="walk_forward_research",
            artifact_id=walk_forward_research_report.report_id,
            path=p,
        )

    learning_ref: Optional[ArtifactRef] = None
    learning_report = getattr(bundle, "learning_report", None)
    if learning_report is not None:
        paths = getattr(bundle, "learning_report_paths", None)
        p = ""
        if paths is not None:
            output_dir = getattr(paths, "output_dir", "")
            report_id = getattr(paths, "report_id", "")
            p = _rel(str(Path(output_dir) / report_id)) if output_dir else ""
        learning_ref = ArtifactRef(
            kind="learning",
            artifact_id=learning_report.report_id,
            path=p,
        )

    analyst_refs: List[ArtifactRef] = []
    analyst_reports = getattr(bundle, "analyst_reports", []) or []
    analyst_paths = getattr(bundle, "analyst_report_paths", []) or []
    for i, report in enumerate(analyst_reports):
        p = ""
        if i < len(analyst_paths):
            paths_obj = analyst_paths[i]
            output_dir = getattr(paths_obj, "output_dir", "")
            report_id = getattr(paths_obj, "report_id", "")
            p = _rel(str(Path(output_dir) / report_id)) if output_dir else ""
        analyst_refs.append(
            ArtifactRef(
                kind="analyst",
                artifact_id=report.report_id,
                path=p,
            )
        )

    promotion = getattr(bundle, "promotion_entry", None)
    promotion_state = (
        getattr(promotion, "current_state", "disabled")
        if promotion
        else "disabled"
    )
    promotion_approvals = len(
        getattr(promotion, "approvals", []) if promotion else []
    )

    from strategy.config import get_feature_flags

    flags = get_feature_flags()
    return ResearchRunManifest(
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        symbols=symbols,
        benchmarks=benchmarks,
        window_start=window_start,
        window_end=window_end,
        provenance_source=source,
        provenance_summary=provenance_summary,
        config_hash=config_hash,
        git_commit=git_commit,
        model_id=model_id,
        llm_endpoint_redacted=_redact_url(llm_endpoint),
        feature_flags_all_disabled=flags.all_disabled,
        feature_flags_enabled=tuple(flags.enabled_flags),
        promotion_state=promotion_state,
        promotion_approvals=promotion_approvals,
        comparison_artifact=comparison_ref,
        walk_forward_artifact=walk_forward_ref,
        walk_forward_research_artifact=walk_forward_research_ref,
        learning_artifact=learning_ref,
        analyst_artifacts=tuple(analyst_refs),
        dataset_manifest_path=getattr(bundle, "dataset_manifest_path", ""),
        warnings=tuple(getattr(bundle, "warnings", []) or []),
        started_at=started_at,
        completed_at=completed_at,
    )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def write(
    manifest: ResearchRunManifest,
    output_dir: Path,
    write_latest_pointer: bool = True,
) -> Dict[str, Path]:
    """Write the manifest JSON + Markdown to
    ``<output_dir>/<run_id>.json`` and ``<output_dir>/<run_id>.md``.

    When ``write_latest_pointer`` is True, also writes copies as
    ``<output_dir>/latest.json`` and ``<output_dir>/latest.md``
    (a plain copy — symlinks are avoided so Windows operators
    don't hit permission issues).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{manifest.run_id}.json"
    md_path = output_dir / f"{manifest.run_id}.md"
    json_path.write_text(manifest.to_json(), encoding="utf-8")
    md_path.write_text(manifest.to_markdown(), encoding="utf-8")
    result = {"json": json_path, "markdown": md_path}
    if write_latest_pointer:
        latest_json = output_dir / "latest.json"
        latest_md = output_dir / "latest.md"
        latest_json.write_text(manifest.to_json(), encoding="utf-8")
        latest_md.write_text(manifest.to_markdown(), encoding="utf-8")
        result["latest_json"] = latest_json
        result["latest_markdown"] = latest_md
    return result


__all__ = [
    "ArtifactRef",
    "DEFAULT_LATEST_SUBDIR",
    "ResearchRunManifest",
    "build_from_bundle",
    "write",
]
