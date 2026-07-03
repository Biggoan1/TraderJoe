"""Tests for strategy/learning_reports.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from strategy.config import reset_feature_flags
from strategy.feature_importance import (
    FLAG_LOW_SAMPLE,
    METHOD_PEARSON,
    FeatureImportanceScore,
)
from strategy.learning_reports import (
    DEFAULT_LEARNING_REPORT_OUTPUT_DIR,
    LEARNING_REPORT_KIND,
    LearningReport,
    LearningReportPaths,
    render_learning_report,
)
from strategy.pattern_discovery import PatternHypothesis
from strategy.promotion_gates import STATE_BACKTEST, PromotionEntry
from strategy.stats_engine import (
    KNOWN_STATS_LABELS,
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    METRIC_SCORE_DELTA_MEAN,
    StatisticalFinding,
)
from strategy.weight_recommender import (
    RecommendationEnvelope,
    WeightRecommendation,
    envelope_for,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _finding(metric: str = METRIC_SCORE_DELTA_MEAN, effect: float = 0.5) -> StatisticalFinding:
    return StatisticalFinding(
        metric=metric,
        effect_size=effect,
        ci_low=0.2,
        ci_high=0.8,
        sample_size=40,
        methodology="normal approx",
        label=LABEL_VALIDATED,
        confidence_level=0.95,
        evidence_ids=("cc_test",),
    )


def _hypothesis(feature_values=("expanding", "bullish")) -> PatternHypothesis:
    return PatternHypothesis(
        pattern_id="pat_test",
        feature_keys=("breadth_regime", "regime"),
        feature_values=feature_values,
        description="test pattern",
        sample_size=12,
        win_rate=0.75,
        win_rate_ci_low=0.5,
        win_rate_ci_high=0.9,
        mean_return=2.0,
        return_ci_low=1.0,
        return_ci_high=3.0,
        effect_size=1.5,
        label=LABEL_HYPOTHESIS,
        confidence_level=0.95,
        supporting_evidence_ids=("t1", "t2"),
    )


def _importance_score(feature_name: str = "feature_a") -> FeatureImportanceScore:
    return FeatureImportanceScore(
        feature_name=feature_name,
        method=METHOD_PEARSON,
        score=0.6,
        sample_size=40,
        ci_low=0.3,
        ci_high=0.85,
        confidence_level=0.95,
        label=LABEL_VALIDATED,
        flagged=False,
        flag_reasons=(),
    )


def _recommendation() -> WeightRecommendation:
    return WeightRecommendation(
        recommendation_id="wr_test",
        target_config_key="feature_a",
        current_value=10.0,
        proposed_value=12.0,
        rationale="test",
        required_promotion_state=STATE_BACKTEST,
        confidence_level=0.95,
        label=LABEL_VALIDATED,
    )


# ---------------------------------------------------------------------------
# LearningReportPaths
# ---------------------------------------------------------------------------


class TestLearningReportPaths:
    def test_paths_derive_from_output_dir_and_report_id(self):
        paths = LearningReportPaths(output_dir="/tmp/out", report_id="lr_abcd")
        d = paths.to_dict()
        assert d["report_dir"].endswith("lr_abcd")
        assert d["markdown_path"].endswith("lr_abcd/report.md")
        assert d["json_path"].endswith("lr_abcd/report.json")
        assert d["recommendations_path"].endswith("lr_abcd/recommendations.json")
        assert d["manifest_path"].endswith("lr_abcd/manifest.json")
        json.dumps(d)

    def test_default_output_dir_constant(self):
        assert DEFAULT_LEARNING_REPORT_OUTPUT_DIR == "reports/learning"


# ---------------------------------------------------------------------------
# render_learning_report — structure and schema
# ---------------------------------------------------------------------------


class TestRenderLearningReport:
    def test_report_id_has_lr_prefix(self):
        report = render_learning_report(
            findings=[_finding()],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score()],
            recommendations=[_recommendation()],
        )
        assert report.report_id.startswith("lr_")
        assert len(report.report_id) == 3 + 12

    def test_json_payload_schema(self):
        report = render_learning_report(
            findings=[_finding()],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score()],
            recommendations=[_recommendation()],
        )
        payload = json.loads(report.to_json())
        assert payload["kind"] == LEARNING_REPORT_KIND
        assert payload["report_id"] == report.report_id
        for section in (
            "statistical_findings",
            "pattern_hypotheses",
            "feature_importance",
            "weight_recommendations",
            "recommendation_envelopes",
        ):
            assert section in payload

        summary = payload["summary"]
        for key in (
            "finding_count",
            "hypothesis_count",
            "importance_count",
            "recommendation_count",
            "finding_labels",
            "hypothesis_labels",
            "importance_labels",
            "recommendation_labels",
        ):
            assert key in summary
        assert set(summary["finding_labels"]) == set(KNOWN_STATS_LABELS)

    def test_summary_counts_match_input(self):
        report = render_learning_report(
            findings=[_finding(), _finding(metric="rank_delta_mean")],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score(), _importance_score("b")],
            recommendations=[_recommendation()],
        )
        summary = report.payload["summary"]
        assert summary["finding_count"] == 2
        assert summary["hypothesis_count"] == 1
        assert summary["importance_count"] == 2
        assert summary["recommendation_count"] == 1

    def test_markdown_contains_required_sections(self):
        report = render_learning_report(
            findings=[_finding()],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score()],
            recommendations=[_recommendation()],
        )
        md = report.to_markdown()
        assert "# Learning System Report" in md
        assert "## Summary" in md
        assert "## Statistical Findings" in md
        assert "## Pattern Hypotheses" in md
        assert "## Feature Importance" in md
        assert "## Weight Recommendations" in md
        assert "## Reproducibility" in md
        assert "Recommendations only" in md

    def test_markdown_flags_empty_sections(self):
        report = render_learning_report()
        md = report.to_markdown()
        assert "_No findings supplied._" in md
        assert "_No hypotheses supplied._" in md
        assert "_No importance scores supplied._" in md
        assert "_No weight recommendations supplied._" in md

    def test_custom_title(self):
        report = render_learning_report(title="Custom Title")
        assert report.title == "Custom Title"

    def test_default_title_includes_report_id(self):
        report = render_learning_report()
        assert report.report_id in report.title

    def test_envelopes_included_in_payload_and_markdown(self):
        rec = _recommendation()
        env = envelope_for(rec)
        report = render_learning_report(
            recommendations=[rec], envelopes=[env]
        )
        payload = json.loads(report.to_json())
        assert len(payload["recommendation_envelopes"]) == 1
        assert payload["recommendation_envelopes"][0]["envelope_id"] == env.envelope_id
        assert "## Recommendation Envelopes" in report.to_markdown()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_stable_hash_ignores_generated_at(self):
        first = render_learning_report(
            findings=[_finding()], generated_at="2026-07-02T12:00:00+00:00"
        )
        second = render_learning_report(
            findings=[_finding()], generated_at="2027-01-01T00:00:00+00:00"
        )
        assert first.stable_hash() == second.stable_hash()
        assert first.report_id == second.report_id

    def test_repeat_render_produces_byte_identical_outputs(self):
        first = render_learning_report(
            findings=[_finding()],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score()],
            recommendations=[_recommendation()],
            generated_at="2026-07-02T12:00:00+00:00",
        )
        second = render_learning_report(
            findings=[_finding()],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score()],
            recommendations=[_recommendation()],
            generated_at="2026-07-02T12:00:00+00:00",
        )
        assert first.to_json() == second.to_json()
        assert first.to_markdown() == second.to_markdown()
        assert first.recommendations_json() == second.recommendations_json()
        assert first.manifest_json() == second.manifest_json()

    def test_report_id_changes_with_findings(self):
        empty = render_learning_report()
        with_finding = render_learning_report(findings=[_finding()])
        assert empty.report_id != with_finding.report_id

    def test_report_id_changes_with_recommendations(self):
        rec_a = _recommendation()
        rec_b = WeightRecommendation(
            recommendation_id="wr_other",
            target_config_key="feature_b",
            current_value=1.0,
            proposed_value=1.2,
            rationale="test-b",
            required_promotion_state=STATE_BACKTEST,
            confidence_level=0.95,
            label=LABEL_VALIDATED,
        )
        assert render_learning_report(
            recommendations=[rec_a]
        ).report_id != render_learning_report(
            recommendations=[rec_b]
        ).report_id


# ---------------------------------------------------------------------------
# Write persistence
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_creates_all_four_files(self, tmp_path):
        report = render_learning_report(
            findings=[_finding()],
            recommendations=[_recommendation()],
            generated_at="2026-07-02T12:00:00+00:00",
        )
        paths = report.write(str(tmp_path))
        assert Path(paths.markdown_path).is_file()
        assert Path(paths.json_path).is_file()
        assert Path(paths.recommendations_path).is_file()
        assert Path(paths.manifest_path).is_file()

        # JSON files parse cleanly
        for p in (paths.json_path, paths.recommendations_path, paths.manifest_path):
            json.loads(Path(p).read_text(encoding="utf-8"))

    def test_write_lands_under_report_id_dir(self, tmp_path):
        report = render_learning_report()
        paths = report.write(str(tmp_path))
        assert Path(paths.report_dir).name == report.report_id

    def test_write_is_idempotent(self, tmp_path):
        report = render_learning_report(
            findings=[_finding()],
            generated_at="2026-07-02T12:00:00+00:00",
        )
        report.write(str(tmp_path))
        report.write(str(tmp_path))
        paths = report.paths_for(str(tmp_path))
        assert Path(paths.markdown_path).read_text(encoding="utf-8") == report.markdown

    def test_recommendations_persisted_separately(self, tmp_path):
        rec = _recommendation()
        report = render_learning_report(recommendations=[rec])
        paths = report.write(str(tmp_path))
        data = json.loads(Path(paths.recommendations_path).read_text(encoding="utf-8"))
        assert len(data["recommendations"]) == 1
        assert data["recommendations"][0]["target_config_key"] == "feature_a"

    def test_manifest_carries_source_hashes(self, tmp_path):
        report = render_learning_report(
            findings=[_finding()],
            hypotheses=[_hypothesis()],
            importance_scores=[_importance_score()],
            recommendations=[_recommendation()],
        )
        paths = report.write(str(tmp_path))
        manifest = json.loads(Path(paths.manifest_path).read_text(encoding="utf-8"))
        for key in (
            "findings_hash",
            "hypotheses_hash",
            "importance_hash",
            "recommendations_hash",
        ):
            assert key in manifest
            assert len(manifest[key]) == 64


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_report_still_produces_valid_bundle(self, tmp_path):
        report = render_learning_report()
        payload = json.loads(report.to_json())
        assert payload["summary"]["finding_count"] == 0
        assert payload["summary"]["hypothesis_count"] == 0
        report.write(str(tmp_path))

    def test_empty_report_id_is_stable(self):
        first = render_learning_report(generated_at="2026-07-02T12:00:00+00:00")
        second = render_learning_report(generated_at="2027-01-01T00:00:00+00:00")
        assert first.report_id == second.report_id


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.learning_reports as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in [
            "alpaca",
            "place_order",
            "submit_order",
            "TradingClient",
            "api_key",
            "yfinance",
        ]:
            assert token not in source, (
                f"learning_reports must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.learning_reports as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 4 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped

    def test_module_never_touches_global_feature_flags(self, tmp_path):
        flags = reset_feature_flags()
        report = render_learning_report(findings=[_finding()])
        report.write(str(tmp_path))
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_module_never_imports_config(self):
        import strategy.learning_reports as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.learning_reports", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.learning_reports  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)
