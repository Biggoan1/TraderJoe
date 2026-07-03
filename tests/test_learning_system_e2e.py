"""End-to-end validation for the Phase 4 Learning System.

Exercises the full pipeline against a frozen fixture set of
Phase 3-shaped records.  Proves:

* byte-identical reruns with the same inputs and ``generated_at``
* empty input handled cleanly
* pipeline refuses to write outside its configured ``output_root``
* the global ``FeatureFlags`` singleton remains disabled after a run
* no Phase 4 module imports the order path
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

from strategy.config import reset_feature_flags
from strategy.feature_importance import (
    FeatureImportanceScore,
    METHOD_PEARSON,
)
from strategy.learning_pipeline import (
    LearningPipeline,
    LearningPipelineError,
)
from strategy.learning_reports import DEFAULT_LEARNING_REPORT_OUTPUT_DIR
from strategy.pattern_discovery import PatternHypothesis
from strategy.promotion_gates import STATE_BACKTEST, PromotionEntry
from strategy.stats_engine import (
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    METRIC_DISAGREEMENT_RATE,
    METRIC_SCORE_DELTA_MEAN,
    StatisticalFinding,
)
from strategy.weight_recommender import (
    WeightRecommendation,
    envelope_for,
)


# ---------------------------------------------------------------------------
# Frozen fixture set
# ---------------------------------------------------------------------------


FIXTURE_GENERATED_AT = "2026-07-02T12:00:00+00:00"


def _fixture_findings() -> List[StatisticalFinding]:
    return [
        StatisticalFinding(
            metric=METRIC_SCORE_DELTA_MEAN,
            effect_size=0.75,
            ci_low=0.2,
            ci_high=1.3,
            sample_size=40,
            methodology="normal approx",
            label=LABEL_VALIDATED,
            confidence_level=0.95,
            evidence_ids=("cc_fixture_a",),
        ),
        StatisticalFinding(
            metric=METRIC_DISAGREEMENT_RATE,
            effect_size=0.30,
            ci_low=0.15,
            ci_high=0.45,
            sample_size=40,
            methodology="Wilson score",
            label=LABEL_VALIDATED,
            confidence_level=0.95,
            evidence_ids=("cc_fixture_a",),
        ),
    ]


def _fixture_hypotheses() -> List[PatternHypothesis]:
    return [
        PatternHypothesis(
            pattern_id="pat_fixture",
            feature_keys=("breadth_regime", "regime"),
            feature_values=("expanding", "bullish"),
            description="fixture pattern",
            sample_size=15,
            win_rate=0.8,
            win_rate_ci_low=0.55,
            win_rate_ci_high=0.93,
            mean_return=1.5,
            return_ci_low=0.9,
            return_ci_high=2.1,
            effect_size=1.2,
            label=LABEL_HYPOTHESIS,
            confidence_level=0.95,
            supporting_evidence_ids=("t1", "t2", "t3"),
        )
    ]


def _fixture_importance_scores() -> List[FeatureImportanceScore]:
    return [
        FeatureImportanceScore(
            feature_name="feature_a",
            method=METHOD_PEARSON,
            score=0.65,
            sample_size=40,
            ci_low=0.35,
            ci_high=0.85,
            confidence_level=0.95,
            label=LABEL_VALIDATED,
            flagged=False,
            flag_reasons=(),
        ),
    ]


def _fixture_recommendations() -> List[WeightRecommendation]:
    return [
        WeightRecommendation(
            recommendation_id="wr_fixture",
            target_config_key="feature_a",
            current_value=10.0,
            proposed_value=12.0,
            rationale="fixture rationale",
            required_promotion_state=STATE_BACKTEST,
            confidence_level=0.95,
            label=LABEL_VALIDATED,
        ),
    ]


def _run(pipeline: LearningPipeline):
    return pipeline.run(
        findings=_fixture_findings(),
        hypotheses=_fixture_hypotheses(),
        importance_scores=_fixture_importance_scores(),
        recommendations=_fixture_recommendations(),
        envelopes=(envelope_for(_fixture_recommendations()[0]),),
        generated_at=FIXTURE_GENERATED_AT,
    )


# ---------------------------------------------------------------------------
# Constructor + config
# ---------------------------------------------------------------------------


class TestPipelineConstruction:
    def test_default_output_root_matches_module_constant(self):
        pipeline = LearningPipeline()
        assert pipeline.output_root == DEFAULT_LEARNING_REPORT_OUTPUT_DIR

    def test_empty_output_root_rejected(self):
        with pytest.raises(LearningPipelineError, match="output_root"):
            LearningPipeline(output_root="")


# ---------------------------------------------------------------------------
# Byte-identical reruns
# ---------------------------------------------------------------------------


class TestByteIdenticalReruns:
    def test_two_runs_produce_identical_files(self, tmp_path):
        pipeline_a = LearningPipeline(output_root=str(tmp_path / "run_a"))
        pipeline_b = LearningPipeline(output_root=str(tmp_path / "run_b"))
        report_a, paths_a = _run(pipeline_a)
        report_b, paths_b = _run(pipeline_b)

        assert report_a.report_id == report_b.report_id
        assert Path(paths_a.markdown_path).read_bytes() == Path(
            paths_b.markdown_path
        ).read_bytes()
        assert Path(paths_a.json_path).read_bytes() == Path(
            paths_b.json_path
        ).read_bytes()
        assert Path(paths_a.recommendations_path).read_bytes() == Path(
            paths_b.recommendations_path
        ).read_bytes()
        assert Path(paths_a.manifest_path).read_bytes() == Path(
            paths_b.manifest_path
        ).read_bytes()

    def test_stable_hash_ignores_generated_at(self, tmp_path):
        pipeline = LearningPipeline(output_root=str(tmp_path))
        report_a, _ = pipeline.run(
            findings=_fixture_findings(),
            generated_at="2026-07-02T12:00:00+00:00",
        )
        report_b, _ = pipeline.run(
            findings=_fixture_findings(),
            generated_at="2027-01-01T00:00:00+00:00",
        )
        assert report_a.stable_hash() == report_b.stable_hash()
        assert report_a.report_id == report_b.report_id


# ---------------------------------------------------------------------------
# Empty / missing input handling
# ---------------------------------------------------------------------------


class TestEmptyAndMissingInputs:
    def test_empty_inputs_produce_valid_bundle(self, tmp_path):
        pipeline = LearningPipeline(output_root=str(tmp_path))
        report, paths = pipeline.run(generated_at=FIXTURE_GENERATED_AT)
        assert report.payload["summary"]["finding_count"] == 0
        assert Path(paths.markdown_path).is_file()

    def test_partial_inputs_produce_findings_only_report(self, tmp_path):
        pipeline = LearningPipeline(output_root=str(tmp_path))
        report, _ = pipeline.run(
            findings=_fixture_findings(),
            generated_at=FIXTURE_GENERATED_AT,
        )
        summary = report.payload["summary"]
        assert summary["finding_count"] == 2
        assert summary["hypothesis_count"] == 0
        assert summary["importance_count"] == 0
        assert summary["recommendation_count"] == 0

    def test_output_root_is_created_if_missing(self, tmp_path):
        missing = tmp_path / "not_yet"
        assert not missing.exists()
        pipeline = LearningPipeline(output_root=str(missing))
        report, paths = _run(pipeline)
        assert Path(paths.markdown_path).is_file()


# ---------------------------------------------------------------------------
# Write-outside-root refusal
# ---------------------------------------------------------------------------


class TestWriteOutsideRootRefusal:
    def test_pipeline_only_writes_under_output_root(self, tmp_path):
        pipeline = LearningPipeline(output_root=str(tmp_path / "root"))
        report, paths = _run(pipeline)

        # All four files must land under the configured root
        root = (tmp_path / "root").resolve()
        for file_path in (
            paths.markdown_path,
            paths.json_path,
            paths.recommendations_path,
            paths.manifest_path,
        ):
            resolved = Path(file_path).resolve()
            assert resolved.is_relative_to(root), (
                f"{file_path!r} escapes {root!r}"
            )

    def test_no_stray_files_outside_root(self, tmp_path):
        pipeline = LearningPipeline(output_root=str(tmp_path / "root"))
        _run(pipeline)
        # Nothing exists at tmp_path other than the configured root
        # and its report_id subtree.
        top_level = {entry.name for entry in tmp_path.iterdir()}
        assert top_level == {"root"}

    def test_symlink_escape_refused(self, tmp_path):
        """A malicious ``output_root`` that resolves outside the caller's
        intended tree still lands under whatever the pipeline's
        ``output_root`` resolves to.  The guard is: nothing lands
        outside ``self.output_root`` after resolution.
        """
        # Create real root + a symlink pointing at it
        real_root = tmp_path / "real"
        real_root.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_root)
        pipeline = LearningPipeline(output_root=str(link))
        report, paths = _run(pipeline)

        real_target = Path(paths.report_dir).resolve()
        assert real_target.is_relative_to(real_root.resolve())


# ---------------------------------------------------------------------------
# Flag isolation + observational guarantees
# ---------------------------------------------------------------------------


class TestFlagIsolation:
    def test_global_feature_flags_remain_disabled_after_run(self, tmp_path):
        flags = reset_feature_flags()
        pipeline = LearningPipeline(output_root=str(tmp_path))
        _run(pipeline)
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_running_pipeline_does_not_mutate_strategy_config(self, tmp_path):
        config_path = Path("strategy/config.py")
        before = config_path.read_bytes()
        pipeline = LearningPipeline(output_root=str(tmp_path))
        _run(pipeline)
        after = config_path.read_bytes()
        assert before == after


# ---------------------------------------------------------------------------
# Forbidden imports across every Phase 4 module
# ---------------------------------------------------------------------------


PHASE_4_MODULES = (
    "strategy.stats_engine",
    "strategy.pattern_discovery",
    "strategy.feature_importance",
    "strategy.weight_recommender",
    "strategy.learning_reports",
    "strategy.learning_pipeline",
)

FORBIDDEN_ORDER_PATH_MODULES = {
    "trader_cli",
    "trader",
    "crypto_trader",
    "telegram_approvals",
}


class TestForbiddenImports:
    @pytest.mark.parametrize("module_name", PHASE_4_MODULES)
    def test_source_has_no_broker_or_order_path_references(self, module_name):
        module = sys.modules.get(module_name)
        if module is None:
            module = __import__(module_name, fromlist=[""])
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
                f"{module_name} must not reference {token!r}"
            )

    def test_no_phase4_module_imports_order_path_at_import_time(self):
        for module_name in PHASE_4_MODULES:
            for name in [module_name, "strategy"]:
                sys.modules.pop(name, None)
        before = set(sys.modules)
        for module_name in PHASE_4_MODULES:
            __import__(module_name, fromlist=[""])
        added = set(sys.modules) - before
        overlap = added & FORBIDDEN_ORDER_PATH_MODULES
        assert not overlap, (
            f"Phase 4 imports pulled in order-path modules: {overlap}"
        )

    @pytest.mark.parametrize("module_name", PHASE_4_MODULES)
    def test_terminology_avoids_training(self, module_name):
        module = sys.modules.get(module_name)
        if module is None:
            module = __import__(module_name, fromlist=[""])
        source = Path(module.__file__).read_text(encoding="utf-8")
        # Every module must contain the explicit terminology sentence;
        # everything else must be free of "training".
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 4 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped, (
                f"{module_name} contains a stray {token!r}"
            )


# ---------------------------------------------------------------------------
# Recommendation routing check
# ---------------------------------------------------------------------------


class TestRecommendationRouting:
    def test_envelope_can_still_apply_to_promotion_entry(self, tmp_path):
        pipeline = LearningPipeline(output_root=str(tmp_path))
        rec = _fixture_recommendations()[0]
        env = envelope_for(rec)
        entry = PromotionEntry(flag_name="enable_relative_strength")
        updated = env.apply_to_entry(entry)

        # Running the pipeline does not affect the routing behavior
        _run(pipeline)
        assert "weight_recommendation" in updated.evidence
        assert entry.evidence == {}
