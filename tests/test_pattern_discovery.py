"""Tests for strategy/pattern_discovery.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Sequence

import pytest

from strategy.config import reset_feature_flags
from strategy.pattern_discovery import (
    OUTCOME_KEY_RETURN_PCT,
    OUTCOME_KEY_WIN,
    PATTERN_MIN_SAMPLE_SIZE,
    PatternHypothesis,
    PatternObservation,
    discover_patterns,
    hypotheses_stable_hash,
    validate_patterns_out_of_sample,
)
from strategy.stats_engine import (
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    SUPPORTED_CONFIDENCE_LEVELS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _obs(
    trade_id: str,
    regime: str = "bullish",
    breadth_regime: str = "expanding",
    return_pct: float = 1.0,
    win: float = 1.0,
    entry: str = "2026-07-01T14:30:00+00:00",
    exit_: str = "2026-07-05T20:00:00+00:00",
) -> PatternObservation:
    return PatternObservation(
        trade_id=trade_id,
        entry_timestamp=entry,
        exit_timestamp=exit_,
        context={"regime": regime, "breadth_regime": breadth_regime},
        outcome={OUTCOME_KEY_WIN: win, OUTCOME_KEY_RETURN_PCT: return_pct},
    )


def _observations_bullish_expanding_positive(n: int = 12) -> List[PatternObservation]:
    return [
        _obs(f"t{i}", regime="bullish", breadth_regime="expanding", return_pct=2.0, win=1.0)
        for i in range(n)
    ]


def _observations_bearish_contracting_negative(n: int = 12) -> List[PatternObservation]:
    return [
        _obs(f"tb{i}", regime="bearish", breadth_regime="contracting", return_pct=-1.5, win=0.0)
        for i in range(n)
    ]


def _mixed_observations() -> List[PatternObservation]:
    return (
        _observations_bullish_expanding_positive(15)
        + _observations_bearish_contracting_negative(12)
        + [_obs("small1", regime="neutral", breadth_regime="expanding", return_pct=0.0, win=0.0)]
    )


# ---------------------------------------------------------------------------
# PatternObservation
# ---------------------------------------------------------------------------


class TestPatternObservation:
    def test_to_dict_roundtrip(self):
        o = _obs("t1")
        d = o.to_dict()
        assert d["trade_id"] == "t1"
        assert d["context"]["regime"] == "bullish"
        assert d["outcome"][OUTCOME_KEY_RETURN_PCT] == 1.0
        json.dumps(d)

    def test_has_context_keys_true_when_all_present(self):
        assert _obs("t1").has_context_keys(["regime", "breadth_regime"]) is True

    def test_has_context_keys_false_when_missing(self):
        assert _obs("t1").has_context_keys(["regime", "sector"]) is False

    def test_has_outcomes_true_when_both_present(self):
        assert _obs("t1").has_outcomes() is True

    def test_has_outcomes_false_when_missing(self):
        o = PatternObservation(
            trade_id="t1",
            entry_timestamp="2026-07-01T14:30:00+00:00",
            exit_timestamp="2026-07-05T20:00:00+00:00",
            context={"regime": "bullish"},
            outcome={OUTCOME_KEY_WIN: 1.0},  # missing return_pct
        )
        assert o.has_outcomes() is False

    def test_missing_trade_id_rejected(self):
        with pytest.raises(ValueError, match="trade_id"):
            PatternObservation(
                trade_id="",
                entry_timestamp="2026-07-01T14:30:00+00:00",
                exit_timestamp="2026-07-05T20:00:00+00:00",
            )

    def test_missing_entry_rejected(self):
        with pytest.raises(ValueError, match="entry_timestamp"):
            PatternObservation(
                trade_id="t1",
                entry_timestamp="",
                exit_timestamp="2026-07-05T20:00:00+00:00",
            )

    def test_exit_before_entry_rejected(self):
        with pytest.raises(ValueError, match="precedes"):
            PatternObservation(
                trade_id="t1",
                entry_timestamp="2026-07-05T20:00:00+00:00",
                exit_timestamp="2026-07-01T14:30:00+00:00",
            )


# ---------------------------------------------------------------------------
# PatternHypothesis
# ---------------------------------------------------------------------------


class TestPatternHypothesis:
    def _hypothesis(self, **overrides) -> PatternHypothesis:
        base = dict(
            pattern_id="pat_abc",
            feature_keys=("regime", "breadth_regime"),
            feature_values=("bullish", "expanding"),
            description="test",
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
        )
        base.update(overrides)
        return PatternHypothesis(**base)

    def test_to_dict_roundtrip(self):
        h = self._hypothesis()
        d = h.to_dict()
        assert d["pattern_id"] == "pat_abc"
        assert d["feature_keys"] == ["regime", "breadth_regime"]
        assert d["feature_values"] == ["bullish", "expanding"]
        json.dumps(d)

    def test_features_helper(self):
        h = self._hypothesis()
        assert h.features() == {"regime": "bullish", "breadth_regime": "expanding"}

    def test_is_significant_return_true_when_ci_excludes_zero(self):
        assert self._hypothesis().is_significant_return() is True

    def test_is_significant_return_false_when_ci_touches_zero(self):
        assert self._hypothesis(return_ci_low=0.0).is_significant_return() is False

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"pattern_id": ""}, "pattern_id"),
            (
                {
                    "feature_keys": ("regime",),
                    "feature_values": ("expanding", "bullish"),
                },
                "same length",
            ),
            ({"label": "mystery"}, "unknown label"),
            ({"confidence_level": 0.0}, "confidence_level"),
            ({"confidence_level": 1.0}, "confidence_level"),
            ({"sample_size": -1}, "sample_size"),
            (
                {"win_rate_ci_low": 0.9, "win_rate_ci_high": 0.5},
                "win_rate_ci_low",
            ),
            (
                {"return_ci_low": 3.0, "return_ci_high": 1.0},
                "return_ci_low",
            ),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._hypothesis(**overrides)


# ---------------------------------------------------------------------------
# discover_patterns
# ---------------------------------------------------------------------------


class TestDiscoverPatterns:
    def test_all_patterns_labeled_hypothesis(self):
        observations = _mixed_observations()
        patterns = discover_patterns(
            observations,
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        assert all(p.label == LABEL_HYPOTHESIS for p in patterns)

    def test_returns_pattern_per_feature_combination(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        feature_values = {p.feature_values for p in patterns}
        assert ("expanding", "bullish") in feature_values
        assert ("contracting", "bearish") in feature_values
        # neutral,expanding only has one observation → below floor → skipped

    def test_bullish_expanding_stats(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        by_values = {p.feature_values: p for p in patterns}
        p = by_values[("expanding", "bullish")]
        assert p.sample_size == 15
        assert p.win_rate == pytest.approx(1.0)
        assert p.mean_return == pytest.approx(2.0)
        assert p.return_ci_low == pytest.approx(2.0)
        assert p.return_ci_high == pytest.approx(2.0)

    def test_bearish_contracting_stats(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        by_values = {p.feature_values: p for p in patterns}
        p = by_values[("contracting", "bearish")]
        assert p.sample_size == 12
        assert p.win_rate == pytest.approx(0.0)
        assert p.mean_return == pytest.approx(-1.5)

    def test_below_min_sample_size_skipped(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        # neutral,expanding has 1 observation → skipped
        assert ("expanding", "neutral") not in {p.feature_values for p in patterns}

    def test_evidence_ids_are_sorted_and_reference_trade_ids(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        by_values = {p.feature_values: p for p in patterns}
        bullish = by_values[("expanding", "bullish")]
        assert list(bullish.supporting_evidence_ids) == sorted(
            bullish.supporting_evidence_ids
        )
        assert set(bullish.supporting_evidence_ids) == {f"t{i}" for i in range(15)}

    def test_deterministic_ordering(self):
        first = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        # Shuffle the input; expected result unchanged since discovery
        # sorts by feature keys/values.
        shuffled = list(reversed(_mixed_observations()))
        second = discover_patterns(
            shuffled,
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        assert [p.to_dict() for p in first] == [p.to_dict() for p in second]

    def test_single_key_grouping(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime",)],
            min_sample_size=10,
        )
        feature_values = {p.feature_values for p in patterns}
        assert ("bullish",) in feature_values
        assert ("bearish",) in feature_values

    def test_empty_feature_key_tuple_skipped(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [()],
            min_sample_size=1,
        )
        assert patterns == []

    def test_observations_missing_context_skipped(self):
        observations = _observations_bullish_expanding_positive(12) + [
            PatternObservation(
                trade_id="no_ctx",
                entry_timestamp="2026-07-01T14:30:00+00:00",
                exit_timestamp="2026-07-05T20:00:00+00:00",
                context={"regime": "bullish"},  # missing breadth_regime
                outcome={OUTCOME_KEY_WIN: 1.0, OUTCOME_KEY_RETURN_PCT: 5.0},
            )
        ]
        patterns = discover_patterns(
            observations,
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        by_values = {p.feature_values: p for p in patterns}
        assert by_values[("expanding", "bullish")].sample_size == 12

    def test_observations_missing_outcome_skipped(self):
        observations = _observations_bullish_expanding_positive(12) + [
            PatternObservation(
                trade_id="no_outcome",
                entry_timestamp="2026-07-01T14:30:00+00:00",
                exit_timestamp="2026-07-05T20:00:00+00:00",
                context={"regime": "bullish", "breadth_regime": "expanding"},
                outcome={},
            )
        ]
        patterns = discover_patterns(
            observations,
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        by_values = {p.feature_values: p for p in patterns}
        assert by_values[("expanding", "bullish")].sample_size == 12

    def test_negative_min_sample_size_rejected(self):
        with pytest.raises(ValueError, match="min_sample_size"):
            discover_patterns(
                _mixed_observations(),
                [("regime",)],
                min_sample_size=0,
            )

    def test_unsupported_confidence_rejected(self):
        with pytest.raises(ValueError, match="unsupported confidence_level"):
            discover_patterns(
                _mixed_observations(),
                [("regime",)],
                confidence_level=0.8,
            )

    def test_confidence_level_propagates(self):
        for level in SUPPORTED_CONFIDENCE_LEVELS:
            patterns = discover_patterns(
                _mixed_observations(),
                [("regime", "breadth_regime")],
                min_sample_size=10,
                confidence_level=level,
            )
            assert all(p.confidence_level == level for p in patterns)


# ---------------------------------------------------------------------------
# validate_patterns_out_of_sample
# ---------------------------------------------------------------------------


class TestValidatePatternsOutOfSample:
    def _in_sample(self) -> List[PatternHypothesis]:
        return discover_patterns(
            _observations_bullish_expanding_positive(15),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )

    def test_promotes_to_validated_when_directions_match(self):
        in_sample = self._in_sample()
        oos = [
            _obs(f"oos{i}", regime="bullish", breadth_regime="expanding", return_pct=1.5, win=1.0)
            for i in range(12)
        ]
        validated = validate_patterns_out_of_sample(
            in_sample, oos, min_sample_size=10
        )
        assert all(p.label == LABEL_VALIDATED for p in validated)
        assert "oos_check: pass" in validated[0].detail

    def test_stays_hypothesis_when_direction_flips(self):
        in_sample = self._in_sample()
        oos = [
            _obs(f"oos{i}", regime="bullish", breadth_regime="expanding", return_pct=-1.5, win=0.0)
            for i in range(12)
        ]
        validated = validate_patterns_out_of_sample(
            in_sample, oos, min_sample_size=10
        )
        assert all(p.label == LABEL_HYPOTHESIS for p in validated)
        assert "oos_check: fail" in validated[0].detail
        assert "return direction mismatch" in validated[0].detail

    def test_stays_hypothesis_when_oos_sample_below_floor(self):
        in_sample = self._in_sample()
        oos = [
            _obs(f"oos{i}", regime="bullish", breadth_regime="expanding", return_pct=1.5, win=1.0)
            for i in range(5)
        ]
        validated = validate_patterns_out_of_sample(
            in_sample, oos, min_sample_size=10
        )
        assert all(p.label == LABEL_HYPOTHESIS for p in validated)
        assert "insufficient OOS sample" in validated[0].detail

    def test_preserves_pattern_id_across_validation(self):
        in_sample = self._in_sample()
        oos = [
            _obs(f"oos{i}", regime="bullish", breadth_regime="expanding", return_pct=1.5, win=1.0)
            for i in range(12)
        ]
        validated = validate_patterns_out_of_sample(
            in_sample, oos, min_sample_size=10
        )
        assert validated[0].pattern_id == in_sample[0].pattern_id

    def test_win_rate_direction_mismatch_stays_hypothesis(self):
        in_sample = self._in_sample()
        # OOS with matching return direction but low win rate (positive average from
        # a few strong wins, but most trades are losses)
        oos = (
            [_obs(f"oos_w{i}", regime="bullish", breadth_regime="expanding", return_pct=20.0, win=1.0) for i in range(2)]
            + [_obs(f"oos_l{i}", regime="bullish", breadth_regime="expanding", return_pct=-1.0, win=0.0) for i in range(10)]
        )
        validated = validate_patterns_out_of_sample(
            in_sample, oos, min_sample_size=10
        )
        assert all(p.label == LABEL_HYPOTHESIS for p in validated)
        assert "win-rate direction mismatch" in validated[0].detail

    def test_negative_min_sample_size_rejected(self):
        with pytest.raises(ValueError, match="min_sample_size"):
            validate_patterns_out_of_sample(
                self._in_sample(), [], min_sample_size=0
            )


# ---------------------------------------------------------------------------
# Stable hash + determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_hypotheses_stable_hash_deterministic(self):
        observations = _mixed_observations()
        first = discover_patterns(
            observations, [("regime", "breadth_regime")], min_sample_size=10
        )
        second = discover_patterns(
            observations, [("regime", "breadth_regime")], min_sample_size=10
        )
        assert hypotheses_stable_hash(first) == hypotheses_stable_hash(second)
        assert len(hypotheses_stable_hash(first)) == 64

    def test_hypotheses_stable_hash_order_independent(self):
        patterns = discover_patterns(
            _mixed_observations(),
            [("regime", "breadth_regime")],
            min_sample_size=10,
        )
        assert hypotheses_stable_hash(patterns) == hypotheses_stable_hash(
            list(reversed(patterns))
        )


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.pattern_discovery as module

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
                f"pattern_discovery must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.pattern_discovery as module

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
        observations = _mixed_observations()
        patterns = discover_patterns(
            observations, [("regime", "breadth_regime")], min_sample_size=10
        )
        validate_patterns_out_of_sample(
            patterns, observations, min_sample_size=10
        )
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_module_never_imports_config(self):
        import strategy.pattern_discovery as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.pattern_discovery", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.pattern_discovery  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)
