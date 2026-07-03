"""Learning Report Generation.

Aggregates Phase 4 analysis records (statistical findings, pattern
hypotheses, feature-importance scores, weight recommendations) into a
:class:`LearningReport` bundle that mirrors the Phase 3
``ResearchReport`` layout.  Each bundle carries a Markdown body, a
JSON payload with a summary + every source record, a flattened
recommendations list, and a reproducibility manifest.  Persistence is
opt-in and writes four artifact files under
``reports/learning/<report_id>/``.

The module never enables feature flags, mutates production config,
places orders, imports order-path code, or wires the historical
validation paper account.  Reports are pure data — promotion decisions
still require the full Phase 3 gate process and explicit
:class:`~strategy.promotion_gates.ApprovalRecord` entries.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 4 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import stable_hash, stable_json
from strategy.feature_importance import (
    FeatureImportanceScore,
    importance_stable_hash,
)
from strategy.pattern_discovery import (
    PatternHypothesis,
    hypotheses_stable_hash,
)
from strategy.stats_engine import (
    KNOWN_STATS_LABELS,
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    StatisticalFinding,
    findings_stable_hash,
)
from strategy.weight_recommender import (
    RecommendationEnvelope,
    WeightRecommendation,
    recommendations_stable_hash,
)


LEARNING_REPORT_ID_PREFIX = "lr"
LEARNING_REPORT_KIND = "learning_report"

LEARNING_REPORT_FILENAME_MARKDOWN = "report.md"
LEARNING_REPORT_FILENAME_JSON = "report.json"
LEARNING_REPORT_FILENAME_RECOMMENDATIONS = "recommendations.json"
LEARNING_REPORT_FILENAME_MANIFEST = "manifest.json"

DEFAULT_LEARNING_REPORT_OUTPUT_DIR = "reports/learning"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningReportPaths:
    """Deterministic locations for a persisted Learning Report."""

    output_dir: str
    report_id: str

    @property
    def report_dir(self) -> str:
        return str(Path(self.output_dir) / self.report_id)

    @property
    def markdown_path(self) -> str:
        return str(Path(self.report_dir) / LEARNING_REPORT_FILENAME_MARKDOWN)

    @property
    def json_path(self) -> str:
        return str(Path(self.report_dir) / LEARNING_REPORT_FILENAME_JSON)

    @property
    def recommendations_path(self) -> str:
        return str(
            Path(self.report_dir) / LEARNING_REPORT_FILENAME_RECOMMENDATIONS
        )

    @property
    def manifest_path(self) -> str:
        return str(Path(self.report_dir) / LEARNING_REPORT_FILENAME_MANIFEST)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "report_id": self.report_id,
            "report_dir": self.report_dir,
            "markdown_path": self.markdown_path,
            "json_path": self.json_path,
            "recommendations_path": self.recommendations_path,
            "manifest_path": self.manifest_path,
        }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class LearningReport:
    """Bundle of Markdown + JSON learning-system artifacts."""

    report_id: str
    title: str
    markdown: str
    payload: Dict[str, Any]
    recommendations: List[Dict[str, Any]]
    manifest: Dict[str, Any]
    generated_at: str

    def to_markdown(self) -> str:
        return self.markdown

    def to_json(self) -> str:
        return stable_json(self.payload)

    def recommendations_json(self) -> str:
        return stable_json({"recommendations": list(self.recommendations)})

    def manifest_json(self) -> str:
        return stable_json(self.manifest)

    def stable_hash(self) -> str:
        """Reproducibility hash: excludes ``generated_at``."""
        data = {
            "title": self.title,
            "payload": self.payload,
            "recommendations": list(self.recommendations),
            "manifest": {
                key: value
                for key, value in self.manifest.items()
                if key != "generated_at"
            },
        }
        return stable_hash(data)

    def paths_for(self, output_dir: str) -> LearningReportPaths:
        return LearningReportPaths(
            output_dir=output_dir, report_id=self.report_id
        )

    def write(self, output_dir: str) -> LearningReportPaths:
        paths = self.paths_for(output_dir)
        Path(paths.report_dir).mkdir(parents=True, exist_ok=True)
        Path(paths.markdown_path).write_text(self.markdown, encoding="utf-8")
        Path(paths.json_path).write_text(
            json.dumps(self.payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        Path(paths.recommendations_path).write_text(
            json.dumps(
                {"recommendations": list(self.recommendations)},
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
# Render helpers
# ---------------------------------------------------------------------------


def _label_bucket(records: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {label: 0 for label in KNOWN_STATS_LABELS}
    for record in records:
        label = record.get("label", "")
        if label in counts:
            counts[label] += 1
    return counts


def _sorted_findings(
    findings: Iterable[StatisticalFinding],
) -> List[Dict[str, Any]]:
    return sorted(
        (f.to_dict() for f in findings),
        key=lambda entry: (entry["metric"], entry["sample_size"]),
    )


def _sorted_hypotheses(
    hypotheses: Iterable[PatternHypothesis],
) -> List[Dict[str, Any]]:
    return sorted(
        (h.to_dict() for h in hypotheses),
        key=lambda entry: (
            entry["feature_keys"],
            entry["feature_values"],
            entry["pattern_id"],
        ),
    )


def _sorted_importance(
    scores: Iterable[FeatureImportanceScore],
) -> List[Dict[str, Any]]:
    return sorted(
        (s.to_dict() for s in scores),
        key=lambda entry: (-abs(entry["score"]), entry["feature_name"]),
    )


def _sorted_recommendations(
    recommendations: Iterable[WeightRecommendation],
) -> List[Dict[str, Any]]:
    return sorted(
        (rec.to_dict() for rec in recommendations),
        key=lambda entry: (
            entry["target_config_key"],
            entry["recommendation_id"],
        ),
    )


def _build_summary(
    findings_payload: Sequence[Mapping[str, Any]],
    hypotheses_payload: Sequence[Mapping[str, Any]],
    importance_payload: Sequence[Mapping[str, Any]],
    recommendations_payload: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    return {
        "finding_count": len(findings_payload),
        "hypothesis_count": len(hypotheses_payload),
        "importance_count": len(importance_payload),
        "recommendation_count": len(recommendations_payload),
        "finding_labels": _label_bucket(findings_payload),
        "hypothesis_labels": _label_bucket(hypotheses_payload),
        "importance_labels": _label_bucket(importance_payload),
        "recommendation_labels": _label_bucket(recommendations_payload),
    }


def _render_markdown(
    report_id: str,
    title: str,
    generated_at: str,
    summary: Mapping[str, Any],
    findings_payload: Sequence[Mapping[str, Any]],
    hypotheses_payload: Sequence[Mapping[str, Any]],
    importance_payload: Sequence[Mapping[str, Any]],
    recommendations_payload: Sequence[Mapping[str, Any]],
    envelopes_payload: Sequence[Mapping[str, Any]],
) -> str:
    lines: List[str] = [
        f"# {title}",
        "",
        f"_Report ID: `{report_id}`_",
        f"_Generated: {generated_at}_",
        "",
        "Recommendations only. Does not enable feature flags, mutate production config, or advance promotion state.",
        "",
        "## Summary",
        f"- Statistical findings: {summary['finding_count']}",
        f"- Pattern hypotheses: {summary['hypothesis_count']}",
        f"- Feature importance scores: {summary['importance_count']}",
        f"- Weight recommendations: {summary['recommendation_count']}",
    ]

    lines.extend(["", "## Statistical Findings"])
    if not findings_payload:
        lines.append("_No findings supplied._")
    else:
        for finding in findings_payload:
            lines.append(
                f"- `{finding['metric']}` "
                f"effect_size={finding['effect_size']} "
                f"CI=[{finding['ci_low']}, {finding['ci_high']}] "
                f"n={finding['sample_size']} "
                f"label=`{finding['label']}`"
            )

    lines.extend(["", "## Pattern Hypotheses"])
    if not hypotheses_payload:
        lines.append("_No hypotheses supplied._")
    else:
        for hypothesis in hypotheses_payload:
            feature_pairs = ", ".join(
                f"{key}={value}"
                for key, value in zip(
                    hypothesis["feature_keys"], hypothesis["feature_values"]
                )
            )
            lines.append(
                f"- `{hypothesis['pattern_id']}` "
                f"({feature_pairs}) "
                f"n={hypothesis['sample_size']} "
                f"win_rate={hypothesis['win_rate']} "
                f"mean_return={hypothesis['mean_return']} "
                f"label=`{hypothesis['label']}`"
            )

    lines.extend(["", "## Feature Importance"])
    if not importance_payload:
        lines.append("_No importance scores supplied._")
    else:
        for score in importance_payload:
            flag_note = (
                f" flags=[{', '.join(score['flag_reasons'])}]"
                if score["flag_reasons"]
                else ""
            )
            lines.append(
                f"- `{score['feature_name']}` "
                f"score={score['score']} "
                f"CI=[{score['ci_low']}, {score['ci_high']}] "
                f"n={score['sample_size']} "
                f"label=`{score['label']}`{flag_note}"
            )

    lines.extend(["", "## Weight Recommendations"])
    if not recommendations_payload:
        lines.append("_No weight recommendations supplied._")
    else:
        for rec in recommendations_payload:
            lines.append(
                f"- `{rec['target_config_key']}` "
                f"current={rec['current_value']} "
                f"proposed={rec['proposed_value']} "
                f"delta={rec['delta']} "
                f"required_state=`{rec['required_promotion_state']}` "
                f"label=`{rec['label']}`"
            )

    if envelopes_payload:
        lines.extend(["", "## Recommendation Envelopes"])
        for env in envelopes_payload:
            lines.append(
                f"- `{env['envelope_id']}` → "
                f"evidence_key=`{env['evidence_key']}` "
                f"(recommendation=`{env['recommendation']['recommendation_id']}`)"
            )

    lines.extend(
        [
            "",
            "## Reproducibility",
            f"- Report ID: `{report_id}`",
        ]
    )
    return "\n".join(lines)


def _learning_report_id(
    findings_hash: str,
    hypotheses_hash: str,
    importance_hash: str,
    recommendations_hash: str,
) -> str:
    identity = {
        "findings_hash": findings_hash,
        "hypotheses_hash": hypotheses_hash,
        "importance_hash": importance_hash,
        "recommendations_hash": recommendations_hash,
    }
    digest = hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()
    return f"{LEARNING_REPORT_ID_PREFIX}_{digest[:12]}"


def render_learning_report(
    findings: Sequence[StatisticalFinding] = (),
    hypotheses: Sequence[PatternHypothesis] = (),
    importance_scores: Sequence[FeatureImportanceScore] = (),
    recommendations: Sequence[WeightRecommendation] = (),
    envelopes: Sequence[RecommendationEnvelope] = (),
    title: Optional[str] = None,
    generated_at: Optional[str] = None,
    explanation_summary: Optional[Mapping[str, Any]] = None,
) -> LearningReport:
    """Aggregate Phase 4 records into a :class:`LearningReport`."""
    generated = generated_at or _utc_now_iso()

    findings_payload = _sorted_findings(findings)
    hypotheses_payload = _sorted_hypotheses(hypotheses)
    importance_payload = _sorted_importance(importance_scores)
    recommendations_payload = _sorted_recommendations(recommendations)
    envelopes_payload = sorted(
        (envelope.to_dict() for envelope in envelopes),
        key=lambda entry: (entry["evidence_key"], entry["envelope_id"]),
    )

    summary = _build_summary(
        findings_payload,
        hypotheses_payload,
        importance_payload,
        recommendations_payload,
    )

    findings_hash = findings_stable_hash(findings)
    hypotheses_hash = hypotheses_stable_hash(hypotheses)
    importance_hash = importance_stable_hash(importance_scores)
    recommendations_hash = recommendations_stable_hash(recommendations)

    report_id = _learning_report_id(
        findings_hash,
        hypotheses_hash,
        importance_hash,
        recommendations_hash,
    )
    resolved_title = title or f"Learning System Report — {report_id}"

    payload = {
        "report_id": report_id,
        "kind": LEARNING_REPORT_KIND,
        "title": resolved_title,
        "summary": summary,
        "statistical_findings": findings_payload,
        "pattern_hypotheses": hypotheses_payload,
        "feature_importance": importance_payload,
        "weight_recommendations": recommendations_payload,
        "recommendation_envelopes": envelopes_payload,
    }
    if explanation_summary is not None:
        payload["explanation_summary"] = dict(explanation_summary)
    manifest = {
        "report_id": report_id,
        "kind": LEARNING_REPORT_KIND,
        "findings_hash": findings_hash,
        "hypotheses_hash": hypotheses_hash,
        "importance_hash": importance_hash,
        "recommendations_hash": recommendations_hash,
        "finding_count": summary["finding_count"],
        "hypothesis_count": summary["hypothesis_count"],
        "importance_count": summary["importance_count"],
        "recommendation_count": summary["recommendation_count"],
        "generated_at": generated,
    }
    markdown = _render_markdown(
        report_id=report_id,
        title=resolved_title,
        generated_at=generated,
        summary=summary,
        findings_payload=findings_payload,
        hypotheses_payload=hypotheses_payload,
        importance_payload=importance_payload,
        recommendations_payload=recommendations_payload,
        envelopes_payload=envelopes_payload,
    )

    return LearningReport(
        report_id=report_id,
        title=resolved_title,
        markdown=markdown,
        payload=payload,
        recommendations=recommendations_payload,
        manifest=manifest,
        generated_at=generated,
    )
