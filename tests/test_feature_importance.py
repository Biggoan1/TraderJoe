"""Tests for strategy/feature_importance.py."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.config import reset_feature_flags
from strategy.feature_importance import (
    FI_SAMPLE_SIZE_FLOOR,
    FLAG_CI_SPANS_ZERO,
    FLAG_LOW_SAMPLE,
    FLAG_ZERO_VARIANCE,
    METHOD_PEARSON,
    FeatureImportanceScore,
    FeatureObservation,
    analyze_feature_importance,
    filter_lookahead_observations,
    fisher_z_ci,
    importance_stable_hash,
    pearson_r,
)
from strategy.stats_engine import LABEL_HYPOTHESIS, LABEL_VALIDATED


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _obs(
    trade_id: str,
    x: float,
    outcome: float,
    feature_ts: str = "2026-07-01T14:30:00+00:00",
    outcome_ts: str = "2026-07-05T20:00:00+00:00",
    features: Dict[str, float] | None = None,
) -> FeatureObservation:
    return FeatureObservation(
        trade_id=trade_id,
        feature_timestamp=feature_ts,
        outcome_timestamp=outcome_ts,
        features=features if features is not None else {"x": x},
        outcome=outcome,
    )


def _linear_positive(n: int = 40) -> List[FeatureObservation]:
    """Feature ``x`` and outcome perfectly linearly related with a slope."""
    return [
        _obs(f"t{i}", x=float(i), outcome=2.0 * i + 1.0)
        for i in range(n)
    ]


def _linear_negative(n: int = 40) -> List[FeatureObservation]:
    return [
        _obs(f"t{i}", x=float(i), outcome=-1.5 * i + 3.0)
        for i in range(n)
    ]


def _uncorrelated(n: int = 40) -> List[FeatureObservation]:
    values = [1.0, -1.0] * (n // 2)
    outcomes = [0.5, 0.5] * (n // 2)
    return [
        _obs(f"t{i}", x=values[i], outcome=outcomes[i])
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# FeatureObservation
# ---------------------------------------------------------------------------


class TestFeatureObservation:
    def test_roundtrip(self):
        obs = _obs("t1", 1.0, 2.0)
        d = obs.to_dict()
        assert d["trade_id"] == "t1"
        assert d["outcome"] == 2.0
        json.dumps(d)

    def test_has_lookahead_true_when_feature_at_outcome(self):
        obs = _obs(
            "t1",
            1.0,
            2.0,
            feature_ts="2026-07-05T20:00:00+00:00",
            outcome_ts="2026-07-05T20:00:00+00:00",
        )
        assert obs.has_lookahead() is True

    def test_has_lookahead_true_when_feature_after_outcome(self):
        obs = _obs(
            "t1",
            1.0,
            2.0,
            feature_ts="2026-07-06T00:00:00+00:00",
            outcome_ts="2026-07-05T20:00:00+00:00",
        )
        assert obs.has_lookahead() is True

    def test_has_lookahead_false_when_feature_strictly_before(self):
        obs = _obs("t1", 1.0, 2.0)  # defaults keep feature < outcome
        assert obs.has_lookahead() is False

    @pytest.mark.parametrize(
        "field,value,error",
        [
            ("trade_id", "", "trade_id"),
            ("feature_timestamp", "", "feature_timestamp"),
            ("outcome_timestamp", "", "outcome_timestamp"),
        ],
    )
    def test_missing_required_fields(self, field, value, error):
        base = dict(
            trade_id="t1",
            feature_timestamp="2026-07-01T14:30:00+00:00",
            outcome_timestamp="2026-07-05T20:00:00+00:00",
            features={"x": 1.0},
            outcome=1.0,
        )
        base[field] = value
        with pytest.raises(ValueError, match=error):
            FeatureObservation(**base)


# ---------------------------------------------------------------------------
# FeatureImportanceScore
# ---------------------------------------------------------------------------


class TestFeatureImportanceScore:
    def _score(self, **overrides) -> FeatureImportanceScore:
        base = dict(
            feature_name="x",
            method=METHOD_PEARSON,
            score=0.7,
            sample_size=40,
            ci_low=0.5,
            ci_high=0.85,
            confidence_level=0.95,
            label=LABEL_VALIDATED,
            flagged=False,
        )
        base.update(overrides)
        return FeatureImportanceScore(**base)

    def test_roundtrip(self):
        s = self._score(flag_reasons=(FLAG_LOW_SAMPLE,))
        d = s.to_dict()
        assert d["feature_name"] == "x"
        assert d["flag_reasons"] == [FLAG_LOW_SAMPLE]
        json.dumps(d)

    def test_is_significant_true_when_ci_excludes_zero(self):
        assert self._score(ci_low=0.5, ci_high=0.85).is_significant() is True

    def test_is_significant_false_when_ci_spans_zero(self):
        assert self._score(ci_low=-0.2, ci_high=0.3).is_significant() is False

    def test_is_significant_false_when_ci_touches_zero(self):
        assert self._score(ci_low=0.0, ci_high=0.5).is_significant() is False

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"feature_name": ""}, "feature_name"),
            ({"method": ""}, "method"),
            ({"label": "mystery"}, "unknown label"),
            ({"confidence_level": 0.0}, "confidence_level"),
            ({"confidence_level": 1.0}, "confidence_level"),
            ({"sample_size": -1}, "sample_size"),
            ({"ci_low": 0.9, "ci_high": 0.5}, "ci_low"),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._score(**overrides)


# ---------------------------------------------------------------------------
# pearson_r + fisher_z_ci helpers
# ---------------------------------------------------------------------------


class TestPearsonR:
    def test_perfect_positive_correlation(self):
        assert pearson_r([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self):
        assert pearson_r([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == pytest.approx(-1.0)

    def test_zero_variance_returns_zero(self):
        assert pearson_r([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0

    def test_empty_returns_zero(self):
        assert pearson_r([], []) == 0.0

    def test_single_value_returns_zero(self):
        assert pearson_r([1.0], [2.0]) == 0.0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="equal-length"):
            pearson_r([1.0, 2.0], [1.0])


class TestFisherZ:
    def test_symmetric_around_zero(self):
        lo, hi = fisher_z_ci(0.0, 100)
        assert lo == pytest.approx(-hi, rel=1e-6)

    def test_ci_widens_with_higher_confidence(self):
        lo95, hi95 = fisher_z_ci(0.5, 30, confidence_level=0.95)
        lo99, hi99 = fisher_z_ci(0.5, 30, confidence_level=0.99)
        assert lo99 < lo95
        assert hi99 > hi95

    def test_ci_shrinks_with_larger_sample(self):
        lo_small, hi_small = fisher_z_ci(0.5, 10)
        lo_large, hi_large = fisher_z_ci(0.5, 100)
        assert hi_large - lo_large < hi_small - lo_small

    def test_perfect_correlation_ci_bounded(self):
        # Clipped so tanh remains finite
        lo, hi = fisher_z_ci(1.0, 30)
        assert -1.0 <= lo <= 1.0
        assert -1.0 <= hi <= 1.0

    def test_small_sample_returns_flat_interval(self):
        lo, hi = fisher_z_ci(0.7, 3)
        assert lo == pytest.approx(0.7)
        assert hi == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# filter_lookahead_observations
# ---------------------------------------------------------------------------


class TestFilterLookahead:
    def test_partitions_by_lookahead_flag(self):
        good = _obs("good", 1.0, 2.0)
        bad = _obs(
            "bad",
            1.0,
            2.0,
            feature_ts="2026-07-06T00:00:00+00:00",
            outcome_ts="2026-07-05T20:00:00+00:00",
        )
        kept, dropped = filter_lookahead_observations([good, bad])
        assert kept == [good]
        assert dropped == [bad]

    def test_empty_input(self):
        kept, dropped = filter_lookahead_observations([])
        assert kept == [] and dropped == []


# ---------------------------------------------------------------------------
# analyze_feature_importance
# ---------------------------------------------------------------------------


class TestAnalyzeFeatureImportance:
    def test_perfect_positive_scores_correlation_one(self):
        scores = analyze_feature_importance(
            _linear_positive(40),
            feature_names=["x"],
            min_sample_size=30,
        )
        assert len(scores) == 1
        assert scores[0].score == pytest.approx(1.0)
        assert scores[0].label == LABEL_VALIDATED
        assert scores[0].flagged is False
        assert scores[0].method == METHOD_PEARSON

    def test_perfect_negative_scores_correlation_negative_one(self):
        scores = analyze_feature_importance(
            _linear_negative(40),
            feature_names=["x"],
            min_sample_size=30,
        )
        assert scores[0].score == pytest.approx(-1.0)

    def test_low_sample_flagged_and_labeled_hypothesis(self):
        # Well below the default 30 floor
        scores = analyze_feature_importance(
            _linear_positive(5),
            feature_names=["x"],
            min_sample_size=30,
        )
        assert scores[0].label == LABEL_HYPOTHESIS
        assert scores[0].flagged is True
        assert FLAG_LOW_SAMPLE in scores[0].flag_reasons

    def test_zero_variance_flagged(self):
        obs = [
            FeatureObservation(
                trade_id=f"t{i}",
                feature_timestamp="2026-07-01T14:30:00+00:00",
                outcome_timestamp="2026-07-05T20:00:00+00:00",
                features={"x": 1.0},
                outcome=float(i),
            )
            for i in range(40)
        ]
        scores = analyze_feature_importance(
            obs, feature_names=["x"], min_sample_size=30
        )
        assert FLAG_ZERO_VARIANCE in scores[0].flag_reasons
        assert scores[0].flagged is True

    def test_ci_spans_zero_flagged_for_uncorrelated_data(self):
        scores = analyze_feature_importance(
            _uncorrelated(40),
            feature_names=["x"],
            min_sample_size=30,
        )
        assert FLAG_CI_SPANS_ZERO in scores[0].flag_reasons
        assert scores[0].flagged is True

    def test_ranking_by_absolute_score(self):
        # Two features: x is perfect positive, y is uncorrelated
        obs: List[FeatureObservation] = []
        for i in range(40):
            obs.append(
                FeatureObservation(
                    trade_id=f"t{i}",
                    feature_timestamp="2026-07-01T14:30:00+00:00",
                    outcome_timestamp="2026-07-05T20:00:00+00:00",
                    features={"x": float(i), "y": ((-1) ** i)},
                    outcome=2.0 * i,
                )
            )
        scores = analyze_feature_importance(
            obs, feature_names=["y", "x"], min_sample_size=30
        )
        assert [s.feature_name for s in scores] == ["x", "y"]

    def test_missing_feature_reported_with_zero_sample(self):
        scores = analyze_feature_importance(
            _linear_positive(40),
            feature_names=["x", "missing"],
            min_sample_size=30,
        )
        by_name = {s.feature_name: s for s in scores}
        assert by_name["missing"].sample_size == 0
        assert by_name["missing"].label == LABEL_HYPOTHESIS
        assert by_name["missing"].flagged is True

    def test_lookahead_rejected_by_default(self):
        good = _linear_positive(20)
        bad = [_obs(
            "bad",
            1.0,
            2.0,
            feature_ts="2026-07-06T00:00:00+00:00",
            outcome_ts="2026-07-05T20:00:00+00:00",
        )]
        with pytest.raises(ValueError, match="look-ahead"):
            analyze_feature_importance(
                good + bad, feature_names=["x"], min_sample_size=10
            )

    def test_lookahead_accepted_when_reject_false(self):
        good = _linear_positive(20)
        bad = [_obs(
            "bad",
            999.0,  # extreme value to shift correlation if included
            -999.0,
            feature_ts="2026-07-06T00:00:00+00:00",
            outcome_ts="2026-07-05T20:00:00+00:00",
        )]
        scores = analyze_feature_importance(
            good + bad,
            feature_names=["x"],
            min_sample_size=10,
            reject_lookahead=False,
        )
        # No exception raised; bad observation counted
        assert len(scores) == 1

    def test_duplicate_feature_names_deduplicated(self):
        scores = analyze_feature_importance(
            _linear_positive(40),
            feature_names=["x", "x"],
            min_sample_size=30,
        )
        assert len(scores) == 1

    def test_deterministic_output(self):
        first = analyze_feature_importance(
            _linear_positive(40), feature_names=["x"], min_sample_size=30
        )
        second = analyze_feature_importance(
            _linear_positive(40), feature_names=["x"], min_sample_size=30
        )
        assert [s.to_dict() for s in first] == [s.to_dict() for s in second]

    def test_confidence_level_propagates(self):
        for level in (0.90, 0.95, 0.99):
            scores = analyze_feature_importance(
                _linear_positive(40),
                feature_names=["x"],
                min_sample_size=30,
                confidence_level=level,
            )
            assert scores[0].confidence_level == level

    def test_unsupported_confidence_rejected(self):
        with pytest.raises(ValueError, match="unsupported confidence_level"):
            analyze_feature_importance(
                _linear_positive(40),
                feature_names=["x"],
                confidence_level=0.8,
            )

    def test_negative_min_sample_rejected(self):
        with pytest.raises(ValueError, match="min_sample_size"):
            analyze_feature_importance(
                _linear_positive(10),
                feature_names=["x"],
                min_sample_size=0,
            )


# ---------------------------------------------------------------------------
# importance_stable_hash
# ---------------------------------------------------------------------------


class TestImportanceStableHash:
    def test_deterministic(self):
        first = analyze_feature_importance(
            _linear_positive(40), feature_names=["x"], min_sample_size=30
        )
        second = analyze_feature_importance(
            _linear_positive(40), feature_names=["x"], min_sample_size=30
        )
        assert importance_stable_hash(first) == importance_stable_hash(second)
        assert len(importance_stable_hash(first)) == 64

    def test_order_independent(self):
        scores = analyze_feature_importance(
            _linear_positive(40), feature_names=["x"], min_sample_size=30
        )
        assert importance_stable_hash(scores) == importance_stable_hash(
            list(reversed(scores))
        )


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.feature_importance as module

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
                f"feature_importance must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.feature_importance as module

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
        analyze_feature_importance(
            _linear_positive(40), feature_names=["x"], min_sample_size=30
        )
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_module_never_imports_config(self):
        import strategy.feature_importance as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.feature_importance", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.feature_importance  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)
