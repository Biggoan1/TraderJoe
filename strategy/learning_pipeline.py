"""Learning System End-to-End Pipeline.

Thin orchestration wrapper that runs a full Learning System render
against caller-supplied Phase 4 records and persists the resulting
:class:`~strategy.learning_reports.LearningReport` under a
statically-configured output root.

The pipeline is immutable with respect to its output root: it exposes
no per-run override, and every write lands under
``<output_root>/<report_id>/``.  A defensive check refuses to persist
if the computed report directory does not resolve inside the
configured root.

The module never enables feature flags, mutates production config,
places orders, imports order-path code, or wires the historical
validation paper account.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 4 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import (
    Optional,
    Sequence,
    Tuple,
)

from strategy.feature_importance import FeatureImportanceScore
from strategy.learning_reports import (
    DEFAULT_LEARNING_REPORT_OUTPUT_DIR,
    LearningReport,
    LearningReportPaths,
    render_learning_report,
)
from strategy.pattern_discovery import PatternHypothesis
from strategy.stats_engine import StatisticalFinding
from strategy.weight_recommender import (
    RecommendationEnvelope,
    WeightRecommendation,
)


class LearningPipelineError(ValueError):
    """Raised when the pipeline is asked to write outside its configured root."""


@dataclass(frozen=True)
class LearningPipeline:
    """Immutable end-to-end orchestrator for the Learning System.

    ``output_root`` is set at construction time and cannot be
    overridden per run.  Every :meth:`run` writes under
    ``<output_root>/<report_id>/``.
    """

    output_root: str = DEFAULT_LEARNING_REPORT_OUTPUT_DIR

    def __post_init__(self) -> None:
        if not self.output_root:
            raise LearningPipelineError(
                "LearningPipeline.output_root is required"
            )

    def run(
        self,
        findings: Sequence[StatisticalFinding] = (),
        hypotheses: Sequence[PatternHypothesis] = (),
        importance_scores: Sequence[FeatureImportanceScore] = (),
        recommendations: Sequence[WeightRecommendation] = (),
        envelopes: Sequence[RecommendationEnvelope] = (),
        title: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Tuple[LearningReport, LearningReportPaths]:
        """Render + persist a Learning Report under ``output_root``."""
        report = render_learning_report(
            findings=findings,
            hypotheses=hypotheses,
            importance_scores=importance_scores,
            recommendations=recommendations,
            envelopes=envelopes,
            title=title,
            generated_at=generated_at,
        )
        paths = report.paths_for(self.output_root)
        self._ensure_within_root(paths.report_dir)
        report.write(self.output_root)
        return report, paths

    def _ensure_within_root(self, report_dir: str) -> None:
        root = Path(self.output_root).resolve()
        target = Path(report_dir).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise LearningPipelineError(
                f"report directory {report_dir!r} escapes configured "
                f"output_root {self.output_root!r}"
            ) from exc
