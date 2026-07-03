"""Tests for strategy/stats_engine.py."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.backtest_lab import BacktestEvent, DeterministicReplayClock, StrategyEvaluation
from strategy.comparison_harness import ChampionChallengerComparison, ComparisonHarness
from strategy.config import reset_feature_flags
from strategy.stats_engine import (
    KNOWN_STATS_LABELS,
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    METRIC_DISAGREEMENT_RATE,
    METRIC_RANK_DELTA_MEAN,
    METRIC_SCORE_DELTA_MEAN,
    METRIC_SELECTION_AGREEMENT_RATE,
    METRIC_WF_DISAGREEMENT_RATE,
    METRIC_WF_SCORE_DELTA_MEAN,
    STATS_DEFAULT_CONFIDENCE_LEVEL,
    STATS_SAMPLE_SIZE_FLOOR,
    SUPPORTED_CONFIDENCE_LEVELS,
    StatisticalFinding,
    analyze_comparison,
    analyze_walk_forward,
    cohens_d_one_sample,
    cohens_d_two_sample,
    findings_stable_hash,
    normal_mean_ci,
    wilson_proportion_ci,
)
from strategy.walk_forward import (
    WalkForwardPipeline,
    generate_walk_forward_schedule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class ScriptedEvaluator:
    strategy_id: str
    script: Dict[str, StrategyEvaluation] = field(default_factory=dict)

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        base = self.script.get(event.timestamp)
        if base is None:
            return StrategyEvaluation(
                strategy_id=self.strategy_id,
                event_timestamp=event.timestamp,
                warnings=["no script entry"],
            )
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=dict(base.scores),
            rankings=[dict(entry) for entry in base.rankings],
            explanations=dict(base.explanations),
            warnings=list(base.warnings),
        )


def _eval(
    strategy_id: str,
    timestamp: str,
    scores: Dict[str, float],
    ranked: List[str],
) -> StrategyEvaluation:
    return StrategyEvaluation(
        strategy_id=strategy_id,
        event_timestamp=timestamp,
        scores=dict(scores),
        rankings=[{"symbol": s, "rank": i + 1} for i, s in enumerate(ranked)],
    )


def _event(date: str, sequence: int = 1) -> BacktestEvent:
    return BacktestEvent(
        timestamp=f"{date}T14:30:00+00:00",
        event_type="market_snapshot",
        sequence=sequence,
    )


def _build_comparison_two_symbols() -> ChampionChallengerComparison:
    ts = "2026-07-02T14:30:00+00:00"
    champ_script = {
        ts: _eval("champion-v0.4.0", ts, {"AAPL": 0.70, "MSFT": 0.60}, ["AAPL", "MSFT"])
    }
    chall_script = {
        ts: _eval(
            "challenger-v0.1.0", ts, {"AAPL": 0.60, "MSFT": 0.70}, ["MSFT", "AAPL"]
        )
    }
    harness = ComparisonHarness(
        ScriptedEvaluator("champion-v0.4.0", champ_script),
        ScriptedEvaluator("challenger-v0.1.0", chall_script),
        score_delta_threshold=0.01,
    )
    return harness.run(
        DeterministicReplayClock([BacktestEvent(ts, "market_snapshot", sequence=1)]),
        dataset_id="paper-2026-q3",
        generated_at="2026-07-02T12:00:00+00:00",
    )


def _build_large_comparison(n_events: int = 40) -> ChampionChallengerComparison:
    """Comparison with ``n_events`` rows so we can hit the sample-size floor."""
    champ_script = {}
    chall_script = {}
    events: List[BacktestEvent] = []
    for i in range(n_events):
        ts = f"2026-07-{(i % 28) + 1:02d}T{(i % 12) + 8:02d}:{(i % 60):02d}:00+00:00"
        champ_script[ts] = _eval(
            "champion-v0.4.0", ts, {"AAPL": 0.5 + 0.001 * i}, ["AAPL"]
        )
        chall_script[ts] = _eval(
            "challenger-v0.1.0", ts, {"AAPL": 0.5 + 0.001 * i + 0.05}, ["AAPL"]
        )
        events.append(BacktestEvent(ts, "market_snapshot", sequence=i + 1))
    harness = ComparisonHarness(
        ScriptedEvaluator("champion-v0.4.0", champ_script),
        ScriptedEvaluator("challenger-v0.1.0", chall_script),
        score_delta_threshold=0.01,
    )
    return harness.run(
        DeterministicReplayClock(events),
        dataset_id="paper-2026-q3",
        generated_at="2026-07-02T12:00:00+00:00",
    )


def _build_walk_forward():
    schedule = generate_walk_forward_schedule(
        start_date="2026-07-01",
        end_date="2026-07-31",
        in_sample_days=10,
        out_of_sample_days=5,
        step_days=15,
    )
    champ_script = {}
    chall_script = {}
    events: List[BacktestEvent] = []
    for date_str in ["2026-07-12", "2026-07-13", "2026-07-28"]:
        ts = f"{date_str}T14:30:00+00:00"
        champ_script[ts] = _eval("champion-v0.4.0", ts, {"AAPL": 0.70}, ["AAPL"])
        chall_script[ts] = _eval("challenger-v0.1.0", ts, {"AAPL": 0.85}, ["AAPL"])
        events.append(BacktestEvent(ts, "market_snapshot", sequence=1))
    pipeline = WalkForwardPipeline(
        champion=ScriptedEvaluator("champion-v0.4.0", champ_script),
        challenger=ScriptedEvaluator("challenger-v0.1.0", chall_script),
        score_delta_threshold=0.01,
    )
    return pipeline.run(
        schedule,
        events,
        dataset_id="paper-2026-q3",
        generated_at="2026-07-02T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Numerical helpers
# ---------------------------------------------------------------------------


class TestNormalMeanCI:
    def test_empty_returns_zero(self):
        mean, lo, hi, n = normal_mean_ci([])
        assert (mean, lo, hi, n) == (0.0, 0.0, 0.0, 0)

    def test_single_value_returns_no_ci(self):
        mean, lo, hi, n = normal_mean_ci([0.5])
        assert mean == pytest.approx(0.5)
        assert lo == pytest.approx(0.5)
        assert hi == pytest.approx(0.5)
        assert n == 1

    def test_known_mean_and_stdev(self):
        """SEM should be std/sqrt(n); CI = mean ± 1.96 * SEM at 95%."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        mean, lo, hi, n = normal_mean_ci(values, confidence_level=0.95)
        assert mean == pytest.approx(3.0)
        # std ≈ 1.5811 (sample), SE ≈ 0.7071, ±1.96*SE ≈ ±1.386
        assert mean - lo == pytest.approx(1.9600 * (1.5811388 / math.sqrt(5)), rel=1e-3)
        assert hi - mean == pytest.approx(1.9600 * (1.5811388 / math.sqrt(5)), rel=1e-3)
        assert n == 5

    def test_ci_widens_at_higher_confidence(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        _, lo_95, hi_95, _ = normal_mean_ci(values, confidence_level=0.95)
        _, lo_99, hi_99, _ = normal_mean_ci(values, confidence_level=0.99)
        assert lo_99 < lo_95
        assert hi_99 > hi_95

    def test_rejects_unsupported_confidence(self):
        with pytest.raises(ValueError, match="unsupported confidence_level"):
            normal_mean_ci([1.0, 2.0], confidence_level=0.8)


class TestWilsonProportionCI:
    def test_zero_trials_returns_zero(self):
        p, lo, hi, n = wilson_proportion_ci(0, 0)
        assert (p, lo, hi, n) == (0.0, 0.0, 0.0, 0)

    def test_all_success_ci_below_one(self):
        p, lo, hi, n = wilson_proportion_ci(10, 10)
        assert p == pytest.approx(1.0)
        assert lo < 1.0
        assert hi == pytest.approx(1.0)
        assert n == 10

    def test_all_failure_ci_above_zero(self):
        p, lo, hi, n = wilson_proportion_ci(0, 10)
        assert p == pytest.approx(0.0)
        assert lo == pytest.approx(0.0)
        assert hi > 0.0

    def test_half_success_center_near_half(self):
        p, lo, hi, n = wilson_proportion_ci(50, 100)
        assert p == pytest.approx(0.5)
        assert lo < 0.5 < hi

    def test_boundaries_clipped_to_unit_interval(self):
        _, lo, hi, _ = wilson_proportion_ci(1, 5)
        assert 0.0 <= lo <= 1.0
        assert 0.0 <= hi <= 1.0

    @pytest.mark.parametrize(
        "successes,trials",
        [(-1, 10), (5, -1), (11, 10)],
    )
    def test_invalid_inputs_rejected(self, successes, trials):
        with pytest.raises(ValueError):
            wilson_proportion_ci(successes, trials)


class TestCohensD:
    def test_one_sample_effect_size(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        # mean = 3.0, std = 1.5811, d = 3.0 / 1.5811 ≈ 1.897
        assert cohens_d_one_sample(values) == pytest.approx(3.0 / 1.5811388, rel=1e-3)

    def test_one_sample_zero_variance(self):
        assert cohens_d_one_sample([1.0, 1.0, 1.0]) == 0.0

    def test_one_sample_empty(self):
        assert cohens_d_one_sample([]) == 0.0

    def test_two_sample_positive_effect(self):
        d = cohens_d_two_sample([2.0, 3.0, 4.0], [1.0, 2.0, 3.0])
        assert d > 0.0

    def test_two_sample_symmetric(self):
        d1 = cohens_d_two_sample([2.0, 3.0, 4.0], [1.0, 2.0, 3.0])
        d2 = cohens_d_two_sample([1.0, 2.0, 3.0], [2.0, 3.0, 4.0])
        assert d1 == pytest.approx(-d2)

    def test_two_sample_zero_variance_returns_zero(self):
        assert cohens_d_two_sample([1.0, 1.0], [1.0, 1.0]) == 0.0


class TestSupportedConfidenceLevels:
    def test_expected_levels(self):
        assert 0.90 in SUPPORTED_CONFIDENCE_LEVELS
        assert 0.95 in SUPPORTED_CONFIDENCE_LEVELS
        assert 0.99 in SUPPORTED_CONFIDENCE_LEVELS


# ---------------------------------------------------------------------------
# StatisticalFinding
# ---------------------------------------------------------------------------


class TestStatisticalFinding:
    def _finding(self, **overrides) -> StatisticalFinding:
        base = dict(
            metric=METRIC_SCORE_DELTA_MEAN,
            effect_size=0.5,
            ci_low=0.1,
            ci_high=0.9,
            sample_size=42,
            methodology="normal approx",
            label=LABEL_VALIDATED,
            confidence_level=0.95,
            evidence_ids=("cc_test",),
        )
        base.update(overrides)
        return StatisticalFinding(**base)

    def test_to_dict_roundtrip(self):
        f = self._finding(detail="test")
        d = f.to_dict()
        assert d["metric"] == METRIC_SCORE_DELTA_MEAN
        assert d["evidence_ids"] == ["cc_test"]
        assert d["detail"] == "test"
        json.dumps(d)

    def test_is_significant_ci_excludes_zero(self):
        f = self._finding(ci_low=0.1, ci_high=0.9)
        assert f.is_significant() is True

    def test_is_significant_ci_spans_zero(self):
        f = self._finding(ci_low=-0.2, ci_high=0.3)
        assert f.is_significant() is False

    def test_is_significant_ci_touches_zero_is_not_significant(self):
        f = self._finding(ci_low=0.0, ci_high=0.5)
        assert f.is_significant() is False

    def test_is_significant_negative_only(self):
        f = self._finding(ci_low=-0.5, ci_high=-0.1)
        assert f.is_significant() is True

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"metric": ""}, "metric"),
            ({"methodology": ""}, "methodology"),
            ({"label": "mystery"}, "unknown label"),
            ({"confidence_level": 0.0}, "confidence_level"),
            ({"confidence_level": 1.0}, "confidence_level"),
            ({"sample_size": -1}, "sample_size"),
            ({"ci_low": 0.9, "ci_high": 0.1}, "ci_low"),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._finding(**overrides)

    def test_evidence_ids_normalized_to_tuple(self):
        f = self._finding(evidence_ids=["a", "b"])
        assert f.evidence_ids == ("a", "b")

    def test_known_labels(self):
        assert LABEL_HYPOTHESIS in KNOWN_STATS_LABELS
        assert LABEL_VALIDATED in KNOWN_STATS_LABELS


# ---------------------------------------------------------------------------
# analyze_comparison
# ---------------------------------------------------------------------------


class TestAnalyzeComparison:
    def test_produces_fixed_metric_ordering(self):
        findings = analyze_comparison(_build_comparison_two_symbols())
        assert [f.metric for f in findings] == [
            METRIC_SCORE_DELTA_MEAN,
            METRIC_RANK_DELTA_MEAN,
            METRIC_DISAGREEMENT_RATE,
            METRIC_SELECTION_AGREEMENT_RATE,
        ]

    def test_all_findings_labeled_hypothesis_below_floor(self):
        findings = analyze_comparison(_build_comparison_two_symbols())
        # Two rows: below the default floor of 30
        for finding in findings:
            assert finding.label == LABEL_HYPOTHESIS

    def test_findings_labeled_validated_when_sample_meets_floor(self):
        findings = analyze_comparison(_build_large_comparison(40))
        by_metric = {f.metric: f for f in findings}
        assert by_metric[METRIC_SCORE_DELTA_MEAN].label == LABEL_VALIDATED
        assert by_metric[METRIC_SCORE_DELTA_MEAN].sample_size == 40

    def test_score_delta_finding_records_positive_effect(self):
        findings = analyze_comparison(_build_large_comparison(40))
        by_metric = {f.metric: f for f in findings}
        # Challenger added +0.05 to every event: mean delta should be ~0.05.
        score = by_metric[METRIC_SCORE_DELTA_MEAN]
        assert score.detail.startswith("mean score_delta=")
        assert score.ci_low > 0.0  # CI excludes zero
        assert score.is_significant() is True

    def test_disagreement_rate_finding_from_two_row_comparison(self):
        # Two rows, both are ranking disagreements (AAPL and MSFT swap)
        findings = analyze_comparison(_build_comparison_two_symbols())
        by_metric = {f.metric: f for f in findings}
        rate = by_metric[METRIC_DISAGREEMENT_RATE]
        assert rate.sample_size == 2
        assert rate.effect_size == pytest.approx(1.0)  # 2/2 rows disagreed
        assert rate.ci_low <= 1.0
        assert rate.ci_high == pytest.approx(1.0)

    def test_selection_agreement_finding(self):
        # Both symbols selected on both sides → 100% agreement
        findings = analyze_comparison(_build_comparison_two_symbols())
        by_metric = {f.metric: f for f in findings}
        assert by_metric[METRIC_SELECTION_AGREEMENT_RATE].effect_size == pytest.approx(1.0)

    def test_empty_comparison_produces_zero_sample_findings(self):
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0"),
            ScriptedEvaluator("challenger-v0.1.0"),
        )
        empty = harness.run(
            DeterministicReplayClock([]),
            dataset_id="empty",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        findings = analyze_comparison(empty)
        for finding in findings:
            assert finding.sample_size == 0
            assert finding.label == LABEL_HYPOTHESIS

    def test_evidence_ids_reference_comparison_run_id(self):
        comparison = _build_comparison_two_symbols()
        findings = analyze_comparison(comparison)
        for finding in findings:
            assert comparison.metadata.run_id in finding.evidence_ids

    def test_confidence_level_propagates(self):
        findings = analyze_comparison(
            _build_large_comparison(40), confidence_level=0.99
        )
        assert all(f.confidence_level == 0.99 for f in findings)

    def test_custom_sample_size_floor(self):
        findings = analyze_comparison(
            _build_comparison_two_symbols(), sample_size_floor=1
        )
        # Now everything with sample_size >= 1 is validated
        for finding in findings:
            if finding.sample_size >= 1:
                assert finding.label == LABEL_VALIDATED
            else:
                assert finding.label == LABEL_HYPOTHESIS

    def test_negative_sample_size_floor_rejected(self):
        with pytest.raises(ValueError, match="sample_size_floor"):
            analyze_comparison(_build_comparison_two_symbols(), sample_size_floor=-1)


# ---------------------------------------------------------------------------
# analyze_walk_forward
# ---------------------------------------------------------------------------


class TestAnalyzeWalkForward:
    def test_metric_ordering(self):
        findings = analyze_walk_forward(_build_walk_forward())
        assert [f.metric for f in findings] == [
            METRIC_WF_SCORE_DELTA_MEAN,
            METRIC_WF_DISAGREEMENT_RATE,
        ]

    def test_evidence_ids_include_report_and_split_run_ids(self):
        wf = _build_walk_forward()
        findings = analyze_walk_forward(wf)
        expected_evidence = {wf.report_id}
        for split_result in wf.split_results:
            expected_evidence.add(split_result.comparison.metadata.run_id)
        for finding in findings:
            assert set(finding.evidence_ids) >= expected_evidence

    def test_score_delta_aggregates_all_oos_rows(self):
        wf = _build_walk_forward()
        findings = analyze_walk_forward(wf)
        by_metric = {f.metric: f for f in findings}
        # Fixture: 3 OOS events all with score_delta = 0.15
        assert by_metric[METRIC_WF_SCORE_DELTA_MEAN].sample_size == 3

    def test_disagreement_rate_aggregates_all_oos_rows(self):
        wf = _build_walk_forward()
        findings = analyze_walk_forward(wf)
        by_metric = {f.metric: f for f in findings}
        # 3 rows, all triggered a score_delta disagreement
        rate = by_metric[METRIC_WF_DISAGREEMENT_RATE]
        assert rate.sample_size == 3
        assert rate.effect_size == pytest.approx(1.0)

    def test_labels_hypothesis_when_below_floor(self):
        findings = analyze_walk_forward(_build_walk_forward())
        assert all(f.label == LABEL_HYPOTHESIS for f in findings)

    def test_negative_sample_size_floor_rejected(self):
        with pytest.raises(ValueError, match="sample_size_floor"):
            analyze_walk_forward(_build_walk_forward(), sample_size_floor=-1)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_repeat_analysis_produces_identical_findings(self):
        comparison = _build_comparison_two_symbols()
        first = analyze_comparison(comparison)
        second = analyze_comparison(comparison)
        assert [f.to_dict() for f in first] == [f.to_dict() for f in second]

    def test_findings_stable_hash_is_deterministic(self):
        comparison = _build_comparison_two_symbols()
        first = analyze_comparison(comparison)
        second = analyze_comparison(comparison)
        assert findings_stable_hash(first) == findings_stable_hash(second)
        assert len(findings_stable_hash(first)) == 64

    def test_findings_stable_hash_ignores_ordering(self):
        comparison = _build_comparison_two_symbols()
        findings = analyze_comparison(comparison)
        reversed_findings = list(reversed(findings))
        assert findings_stable_hash(findings) == findings_stable_hash(reversed_findings)


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.stats_engine as module

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
                f"stats_engine must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.stats_engine as module

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
        analyze_comparison(_build_comparison_two_symbols())
        analyze_walk_forward(_build_walk_forward())
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.stats_engine", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.stats_engine  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)

    def test_module_never_touches_config_module(self):
        """stats_engine must not import strategy.config directly — it
        analyzes evidence, it does not read or mutate feature flags.
        """
        import strategy.stats_engine as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source
