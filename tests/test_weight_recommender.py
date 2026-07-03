"""Tests for strategy/weight_recommender.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from strategy.config import reset_feature_flags
from strategy.feature_importance import (
    FLAG_CI_SPANS_ZERO,
    FLAG_LOW_SAMPLE,
    FLAG_ZERO_VARIANCE,
    METHOD_PEARSON,
    FeatureImportanceScore,
)
from strategy.promotion_gates import (
    STATE_APPROVED,
    STATE_BACKTEST,
    STATE_CANDIDATE,
    STATE_DISABLED,
    STATE_PAPER_TRADING,
    STATE_PRODUCTION,
    STATE_WALK_FORWARD,
    PromotionEntry,
)
from strategy.stats_engine import LABEL_HYPOTHESIS, LABEL_VALIDATED
from strategy.weight_recommender import (
    ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER,
    DEFAULT_EVIDENCE_KEY,
    FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER,
    MAX_ALLOWED_PROMOTION_STATE,
    RecommendationEnvelope,
    WeightRecommendation,
    build_envelope_id,
    build_recommendation_id,
    envelope_for,
    recommend_weights,
    recommendations_stable_hash,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _validated_score(
    feature_name: str,
    score: float = 0.5,
    ci_low: float = 0.3,
    ci_high: float = 0.7,
    sample_size: int = 40,
) -> FeatureImportanceScore:
    return FeatureImportanceScore(
        feature_name=feature_name,
        method=METHOD_PEARSON,
        score=score,
        sample_size=sample_size,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence_level=0.95,
        label=LABEL_VALIDATED,
        flagged=False,
        flag_reasons=(),
    )


def _hypothesis_score(
    feature_name: str, score: float = 0.5
) -> FeatureImportanceScore:
    return FeatureImportanceScore(
        feature_name=feature_name,
        method=METHOD_PEARSON,
        score=score,
        sample_size=5,
        ci_low=-0.5,
        ci_high=0.9,
        confidence_level=0.95,
        label=LABEL_HYPOTHESIS,
        flagged=True,
        flag_reasons=(FLAG_LOW_SAMPLE,),
    )


# ---------------------------------------------------------------------------
# WeightRecommendation validation
# ---------------------------------------------------------------------------


class TestWeightRecommendationValidation:
    def _rec(self, **overrides) -> WeightRecommendation:
        base = dict(
            recommendation_id="wr_test",
            target_config_key="feature_a",
            current_value=1.0,
            proposed_value=1.2,
            rationale="test",
            required_promotion_state=STATE_BACKTEST,
            confidence_level=0.95,
            label=LABEL_HYPOTHESIS,
        )
        base.update(overrides)
        return WeightRecommendation(**base)

    def test_roundtrip(self):
        r = self._rec()
        d = r.to_dict()
        assert d["target_config_key"] == "feature_a"
        assert d["delta"] == pytest.approx(0.2)
        json.dumps(d)

    @pytest.mark.parametrize(
        "state", [STATE_DISABLED, STATE_BACKTEST, STATE_WALK_FORWARD, STATE_PAPER_TRADING]
    )
    def test_allowed_promotion_states(self, state):
        self._rec(required_promotion_state=state)  # no exception

    @pytest.mark.parametrize(
        "state", [STATE_CANDIDATE, STATE_APPROVED, STATE_PRODUCTION]
    )
    def test_forbidden_promotion_states_rejected(self, state):
        with pytest.raises(ValueError, match="exceeds MAX_ALLOWED_PROMOTION_STATE"):
            self._rec(required_promotion_state=state)

    def test_unknown_promotion_state_rejected(self):
        with pytest.raises(ValueError, match="unknown required_promotion_state"):
            self._rec(required_promotion_state="mystery")

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"recommendation_id": ""}, "recommendation_id"),
            ({"target_config_key": ""}, "target_config_key"),
            ({"rationale": ""}, "rationale"),
            ({"confidence_level": 0.0}, "confidence_level"),
            ({"confidence_level": 1.0}, "confidence_level"),
            ({"label": "mystery"}, "unknown label"),
        ],
    )
    def test_field_validation(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._rec(**overrides)


class TestConstants:
    def test_max_allowed_promotion_state_is_paper_trading(self):
        assert MAX_ALLOWED_PROMOTION_STATE == STATE_PAPER_TRADING

    def test_allowed_and_forbidden_sets_partition_all_states(self):
        all_seen = set(ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER) | set(
            FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER
        )
        # All seven promotion states must be classified
        assert {
            STATE_DISABLED,
            STATE_BACKTEST,
            STATE_WALK_FORWARD,
            STATE_PAPER_TRADING,
            STATE_CANDIDATE,
            STATE_APPROVED,
            STATE_PRODUCTION,
        } == all_seen
        # No overlap
        assert not (
            set(ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER)
            & set(FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER)
        )


# ---------------------------------------------------------------------------
# RecommendationEnvelope
# ---------------------------------------------------------------------------


class TestRecommendationEnvelope:
    def _rec(self) -> WeightRecommendation:
        return WeightRecommendation(
            recommendation_id="wr_test",
            target_config_key="feature_a",
            current_value=1.0,
            proposed_value=1.2,
            rationale="test",
            required_promotion_state=STATE_BACKTEST,
            confidence_level=0.95,
            label=LABEL_HYPOTHESIS,
        )

    def test_envelope_requires_ids(self):
        with pytest.raises(ValueError, match="envelope_id"):
            RecommendationEnvelope(
                envelope_id="",
                recommendation=self._rec(),
                evidence_key=DEFAULT_EVIDENCE_KEY,
            )

    def test_envelope_requires_evidence_key(self):
        with pytest.raises(ValueError, match="evidence_key"):
            RecommendationEnvelope(
                envelope_id="we_test",
                recommendation=self._rec(),
                evidence_key="",
            )

    def test_to_evidence_value_is_deterministic_json(self):
        env = envelope_for(self._rec())
        assert env.to_evidence_value() == env.to_evidence_value()
        payload = json.loads(env.to_evidence_value())
        assert payload["target_config_key"] == "feature_a"

    def test_apply_to_entry_returns_new_entry_with_evidence(self):
        entry = PromotionEntry(flag_name="enable_relative_strength")
        env = envelope_for(self._rec())
        updated = env.apply_to_entry(entry)

        # Original entry unchanged
        assert entry.evidence == {}
        # New entry carries the serialized recommendation
        assert DEFAULT_EVIDENCE_KEY in updated.evidence
        assert updated.flag_name == entry.flag_name

    def test_apply_to_entry_supports_custom_evidence_key(self):
        entry = PromotionEntry(flag_name="enable_relative_strength")
        env = envelope_for(self._rec(), evidence_key="rs_weight_rec")
        updated = env.apply_to_entry(entry)
        assert "rs_weight_rec" in updated.evidence
        assert DEFAULT_EVIDENCE_KEY not in updated.evidence

    def test_apply_to_entry_preserves_existing_evidence(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            evidence={"existing": "value"},
        )
        env = envelope_for(self._rec())
        updated = env.apply_to_entry(entry)
        assert updated.evidence["existing"] == "value"
        assert DEFAULT_EVIDENCE_KEY in updated.evidence

    def test_to_dict_json_serializable(self):
        env = envelope_for(self._rec())
        d = env.to_dict()
        assert d["envelope_id"] == env.envelope_id
        assert d["recommendation"]["target_config_key"] == "feature_a"
        json.dumps(d)


# ---------------------------------------------------------------------------
# recommend_weights
# ---------------------------------------------------------------------------


class TestRecommendWeights:
    def test_positive_score_scales_weight_up(self):
        recs = recommend_weights(
            [_validated_score("feature_a", score=0.6)],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
            max_change_pct=0.20,
        )
        assert len(recs) == 1
        assert recs[0].proposed_value == pytest.approx(12.0)

    def test_negative_score_scales_weight_down(self):
        recs = recommend_weights(
            [_validated_score("feature_a", score=-0.6, ci_low=-0.7, ci_high=-0.3)],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
            max_change_pct=0.20,
        )
        assert recs[0].proposed_value == pytest.approx(8.0)

    def test_hypothesis_scores_skipped_when_require_validated_true(self):
        recs = recommend_weights(
            [_hypothesis_score("feature_a")],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recs == []

    def test_hypothesis_scores_included_when_require_validated_false(self):
        score = FeatureImportanceScore(
            feature_name="feature_a",
            method=METHOD_PEARSON,
            score=0.6,
            sample_size=5,
            ci_low=0.3,
            ci_high=0.9,
            confidence_level=0.95,
            label=LABEL_HYPOTHESIS,
            flagged=False,  # not flagged low_sample for this artificial fixture
            flag_reasons=(),
        )
        recs = recommend_weights(
            [score],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
            require_validated=False,
        )
        assert len(recs) == 1
        assert recs[0].label == LABEL_HYPOTHESIS

    def test_features_not_in_current_weights_skipped(self):
        recs = recommend_weights(
            [_validated_score("unknown_feature", score=0.6)],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recs == []

    def test_ci_spanning_zero_skipped_when_require_significant(self):
        score = FeatureImportanceScore(
            feature_name="feature_a",
            method=METHOD_PEARSON,
            score=0.3,
            sample_size=40,
            ci_low=-0.1,
            ci_high=0.5,
            confidence_level=0.95,
            label=LABEL_VALIDATED,
            flagged=True,
            flag_reasons=(FLAG_CI_SPANS_ZERO,),
        )
        recs = recommend_weights(
            [score],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recs == []

    def test_low_sample_flag_skips_feature(self):
        score = _validated_score("feature_a")
        # Rebuild score with a low_sample flag
        flagged_score = FeatureImportanceScore(
            feature_name=score.feature_name,
            method=score.method,
            score=score.score,
            sample_size=score.sample_size,
            ci_low=score.ci_low,
            ci_high=score.ci_high,
            confidence_level=score.confidence_level,
            label=score.label,
            flagged=True,
            flag_reasons=(FLAG_LOW_SAMPLE,),
        )
        recs = recommend_weights(
            [flagged_score],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recs == []

    def test_zero_variance_flag_skips_feature(self):
        flagged = FeatureImportanceScore(
            feature_name="feature_a",
            method=METHOD_PEARSON,
            score=0.6,
            sample_size=40,
            ci_low=0.3,
            ci_high=0.9,
            confidence_level=0.95,
            label=LABEL_VALIDATED,
            flagged=True,
            flag_reasons=(FLAG_ZERO_VARIANCE,),
        )
        recs = recommend_weights(
            [flagged],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recs == []

    def test_below_min_feature_score_skipped(self):
        recs = recommend_weights(
            [_validated_score("feature_a", score=0.05, ci_low=0.01, ci_high=0.1)],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
            min_feature_score=0.10,
        )
        assert recs == []

    @pytest.mark.parametrize(
        "state", [STATE_CANDIDATE, STATE_APPROVED, STATE_PRODUCTION]
    )
    def test_refuses_to_target_forbidden_states(self, state):
        with pytest.raises(ValueError, match="forbidden"):
            recommend_weights(
                [_validated_score("feature_a", score=0.6)],
                current_weights={"feature_a": 10.0},
                required_promotion_state=state,
            )

    def test_refuses_unknown_state(self):
        with pytest.raises(ValueError, match="unknown required_promotion_state"):
            recommend_weights(
                [_validated_score("feature_a", score=0.6)],
                current_weights={"feature_a": 10.0},
                required_promotion_state="mystery",
            )

    def test_refuses_non_positive_change_pct(self):
        with pytest.raises(ValueError, match="max_change_pct"):
            recommend_weights(
                [_validated_score("feature_a", score=0.6)],
                current_weights={"feature_a": 10.0},
                required_promotion_state=STATE_BACKTEST,
                max_change_pct=0.0,
            )

    def test_refuses_change_pct_above_one(self):
        with pytest.raises(ValueError, match="max_change_pct"):
            recommend_weights(
                [_validated_score("feature_a", score=0.6)],
                current_weights={"feature_a": 10.0},
                required_promotion_state=STATE_BACKTEST,
                max_change_pct=1.5,
            )

    def test_refuses_negative_min_feature_score(self):
        with pytest.raises(ValueError, match="min_feature_score"):
            recommend_weights(
                [_validated_score("feature_a", score=0.6)],
                current_weights={"feature_a": 10.0},
                required_promotion_state=STATE_BACKTEST,
                min_feature_score=-0.1,
            )

    def test_recommendations_sorted_deterministically(self):
        scores = [
            _validated_score("zeta", score=0.6),
            _validated_score("alpha", score=0.6),
            _validated_score("mu", score=0.6),
        ]
        recs = recommend_weights(
            scores,
            current_weights={"zeta": 1.0, "alpha": 1.0, "mu": 1.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert [r.target_config_key for r in recs] == ["alpha", "mu", "zeta"]

    def test_labels_validated_recommendation(self):
        recs = recommend_weights(
            [_validated_score("feature_a", score=0.6)],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recs[0].label == LABEL_VALIDATED


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def _scores(self) -> List[FeatureImportanceScore]:
        return [
            _validated_score("feature_a", score=0.6),
            _validated_score("feature_b", score=-0.4, ci_low=-0.7, ci_high=-0.2),
        ]

    def test_repeat_recommendations_identical(self):
        first = recommend_weights(
            self._scores(),
            current_weights={"feature_a": 5.0, "feature_b": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        second = recommend_weights(
            self._scores(),
            current_weights={"feature_a": 5.0, "feature_b": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert [r.to_dict() for r in first] == [r.to_dict() for r in second]

    def test_recommendations_stable_hash_order_independent(self):
        recs = recommend_weights(
            self._scores(),
            current_weights={"feature_a": 5.0, "feature_b": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        assert recommendations_stable_hash(recs) == recommendations_stable_hash(
            list(reversed(recs))
        )

    def test_build_recommendation_id_deterministic(self):
        a = build_recommendation_id("feature_a", 1.0, 1.2, STATE_BACKTEST)
        b = build_recommendation_id("feature_a", 1.0, 1.2, STATE_BACKTEST)
        assert a == b

    def test_build_envelope_id_changes_with_evidence_key(self):
        rec = self._scores()[0]
        recs = recommend_weights(
            [rec],
            current_weights={"feature_a": 5.0},
            required_promotion_state=STATE_BACKTEST,
        )
        rec_obj = recs[0]
        id_a = build_envelope_id(rec_obj, "key_a")
        id_b = build_envelope_id(rec_obj, "key_b")
        assert id_a != id_b


# ---------------------------------------------------------------------------
# Safety: never writes to strategy/config.py
# ---------------------------------------------------------------------------


class TestConfigImmutability:
    def test_module_source_never_opens_config_for_writing(self):
        import strategy.weight_recommender as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        # Forbidden write patterns
        assert 'open(strategy/config.py' not in source
        assert "config_path" not in source
        assert '"w"' not in source
        assert "'w'" not in source

    def test_module_never_imports_strategy_config(self):
        import strategy.weight_recommender as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source

    def test_running_recommender_does_not_mutate_config_file(self, tmp_path):
        """Read the config.py bytes before and after a recommender run
        and prove they are identical.
        """
        config_path = Path("strategy/config.py")
        before = config_path.read_bytes()

        scores = [_validated_score("feature_a", score=0.6)]
        recs = recommend_weights(
            scores,
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        env = envelope_for(recs[0])
        entry = PromotionEntry(flag_name="enable_relative_strength")
        env.apply_to_entry(entry)

        after = config_path.read_bytes()
        assert before == after


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.weight_recommender as module

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
                f"weight_recommender must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.weight_recommender as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 4 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped

    def test_module_never_touches_global_feature_flags(self):
        flags = reset_feature_flags()
        recs = recommend_weights(
            [_validated_score("feature_a", score=0.6)],
            current_weights={"feature_a": 10.0},
            required_promotion_state=STATE_BACKTEST,
        )
        env = envelope_for(recs[0])
        entry = PromotionEntry(flag_name="enable_relative_strength")
        env.apply_to_entry(entry)
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.weight_recommender", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.weight_recommender  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)
