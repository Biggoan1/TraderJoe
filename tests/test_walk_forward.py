"""Tests for strategy/walk_forward.py."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.comparison_harness import (
    DISAGREEMENT_RANKING,
    DISAGREEMENT_SCORE,
    KNOWN_DISAGREEMENT_KINDS,
    ComparisonHarness,
)
from strategy.config import FeatureFlags, reset_feature_flags
from strategy.rs_challenger import RelativeStrengthChallenger, rs_provider_from_map
from strategy.walk_forward import (
    WalkForwardPipeline,
    WalkForwardReport,
    WalkForwardSchedule,
    WalkForwardSplit,
    WalkForwardSplitResult,
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


def _evaluation(
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


# ---------------------------------------------------------------------------
# WalkForwardSplit
# ---------------------------------------------------------------------------


class TestWalkForwardSplit:
    def _split(self, **overrides) -> WalkForwardSplit:
        base = dict(
            split_id="wf-000",
            index=0,
            in_sample_start="2026-07-01",
            in_sample_end="2026-07-05",
            out_of_sample_start="2026-07-06",
            out_of_sample_end="2026-07-10",
        )
        base.update(overrides)
        return WalkForwardSplit(**base)

    def test_roundtrip(self):
        split = self._split()
        assert split.to_dict() == {
            "split_id": "wf-000",
            "index": 0,
            "in_sample_start": "2026-07-01",
            "in_sample_end": "2026-07-05",
            "out_of_sample_start": "2026-07-06",
            "out_of_sample_end": "2026-07-10",
        }
        json.dumps(split.to_dict())

    def test_contains_out_of_sample(self):
        split = self._split()
        assert split.contains_out_of_sample("2026-07-06") is True
        assert split.contains_out_of_sample("2026-07-10") is True
        assert split.contains_out_of_sample("2026-07-05") is False
        assert split.contains_out_of_sample("2026-07-11") is False

    def test_contains_in_sample(self):
        split = self._split()
        assert split.contains_in_sample("2026-07-01") is True
        assert split.contains_in_sample("2026-07-05") is True
        assert split.contains_in_sample("2026-07-06") is False

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"split_id": ""}, "split_id"),
            ({"index": -1}, "index"),
            ({"in_sample_start": "2026-07-06"}, "in_sample_start"),
            ({"out_of_sample_start": "2026-07-11"}, "out_of_sample_start"),
            (
                {"out_of_sample_start": "2026-07-05"},
                "strictly after",
            ),
            (
                {
                    "out_of_sample_start": "2026-07-06",
                    "out_of_sample_end": "2026-07-05",
                    "in_sample_end": "2026-07-05",
                },
                "out_of_sample_start must be <= out_of_sample_end",
            ),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._split(**overrides)


# ---------------------------------------------------------------------------
# generate_walk_forward_schedule
# ---------------------------------------------------------------------------


class TestScheduleGenerator:
    def test_basic_generation(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=5,
            schedule_id="wf-basic",
        )
        assert schedule.schedule_id == "wf-basic"
        # 07-01..07-10 IS, 07-11..07-15 OOS, step=5 → next start 07-06
        assert schedule.splits[0].in_sample_start == "2026-07-01"
        assert schedule.splits[0].in_sample_end == "2026-07-10"
        assert schedule.splits[0].out_of_sample_start == "2026-07-11"
        assert schedule.splits[0].out_of_sample_end == "2026-07-15"
        assert schedule.splits[1].in_sample_start == "2026-07-06"

    def test_no_overlap_within_split(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-08-30",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=5,
        )
        for split in schedule.splits:
            assert split.out_of_sample_start > split.in_sample_end

    def test_splits_are_time_ordered(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-08-30",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=5,
        )
        oos_starts = [split.out_of_sample_start for split in schedule.splits]
        assert oos_starts == sorted(oos_starts)
        assert len(oos_starts) == len(set(oos_starts))

    def test_end_date_boundary_respected(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-15",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=5,
        )
        assert schedule.splits[-1].out_of_sample_end <= "2026-07-15"

    def test_returns_no_splits_when_window_too_small(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-05",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=5,
        )
        assert schedule.splits == ()

    def test_deterministic_output(self):
        first = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-08-15",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=7,
        )
        second = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-08-15",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=7,
        )
        assert first == second
        assert first.stable_hash() == second.stable_hash()

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"in_sample_days": 0}, "in_sample_days"),
            ({"out_of_sample_days": 0}, "out_of_sample_days"),
            ({"step_days": 0}, "step_days"),
            (
                {"start_date": "2026-08-01", "end_date": "2026-07-01"},
                "start_date must be <= end_date",
            ),
        ],
    )
    def test_validation_errors(self, overrides, error):
        base = dict(
            start_date="2026-07-01",
            end_date="2026-08-01",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=5,
        )
        base.update(overrides)
        with pytest.raises(ValueError, match=error):
            generate_walk_forward_schedule(**base)


# ---------------------------------------------------------------------------
# WalkForwardSchedule
# ---------------------------------------------------------------------------


class TestWalkForwardSchedule:
    def test_stable_hash_deterministic(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-08-15",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=7,
        )
        assert schedule.stable_hash() == schedule.stable_hash()
        assert len(schedule.stable_hash()) == 64
        json.dumps(schedule.to_dict())

    def test_stable_hash_changes_with_config(self):
        base = dict(
            start_date="2026-07-01",
            end_date="2026-08-15",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=7,
        )
        first = generate_walk_forward_schedule(**base)
        second = generate_walk_forward_schedule(**{**base, "step_days": 5})
        assert first.stable_hash() != second.stable_hash()

    def test_schedule_rejects_out_of_order_splits(self):
        good = WalkForwardSplit(
            split_id="a",
            index=0,
            in_sample_start="2026-07-01",
            in_sample_end="2026-07-05",
            out_of_sample_start="2026-07-06",
            out_of_sample_end="2026-07-10",
        )
        earlier = WalkForwardSplit(
            split_id="b",
            index=1,
            in_sample_start="2026-06-20",
            in_sample_end="2026-06-24",
            out_of_sample_start="2026-06-25",
            out_of_sample_end="2026-06-30",
        )
        with pytest.raises(ValueError, match="strictly time-ordered"):
            WalkForwardSchedule(
                schedule_id="broken",
                start_date="2026-06-20",
                end_date="2026-07-10",
                in_sample_days=5,
                out_of_sample_days=5,
                step_days=5,
                splits=(good, earlier),
            )


# ---------------------------------------------------------------------------
# WalkForwardPipeline
# ---------------------------------------------------------------------------


def _identical_scripts(
    champion_id: str,
    challenger_id: str,
    per_date: Dict[str, Dict[str, object]],
):
    champ = {}
    chall = {}
    for date_str, payload in per_date.items():
        ts = f"{date_str}T14:30:00+00:00"
        champ[ts] = _evaluation(
            champion_id, ts, payload["scores"], payload["ranked"]
        )
        chall[ts] = _evaluation(
            challenger_id, ts, payload["scores"], payload["ranked"]
        )
    return champ, chall


class TestPipelineLeakage:
    def test_in_sample_events_are_not_passed_to_harness(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-20",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        assert len(schedule.splits) == 1  # single split for a tight scenario

        per_date = {
            # In-sample: should NOT be evaluated by the harness
            "2026-07-05": {"scores": {"AAPL": 0.9}, "ranked": ["AAPL"]},
            # OOS: should be evaluated
            "2026-07-12": {"scores": {"AAPL": 0.9}, "ranked": ["AAPL"]},
        }
        champ_script, chall_script = _identical_scripts(
            "champion-v0.4.0", "challenger-v0.1.0", per_date
        )
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0", champ_script),
            challenger=ScriptedEvaluator("challenger-v0.1.0", chall_script),
        )

        events = [
            _event("2026-07-05"),
            _event("2026-07-12"),
        ]
        report = pipeline.run(
            schedule,
            events,
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        result = report.split_results[0]
        assert result.in_sample_event_count == 1
        assert result.out_of_sample_event_count == 1
        assert result.dropped_event_count == 1
        # The comparison must have processed only the OOS event.
        assert len(result.comparison.score_tables) == 1
        assert (
            result.comparison.score_tables[0].event_timestamp
            == "2026-07-12T14:30:00+00:00"
        )

    def test_events_outside_any_window_recorded_as_unassigned(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-20",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        # Event after end_date: outside every window
        outside_event = _event("2026-08-01")

        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0"),
            challenger=ScriptedEvaluator("challenger-v0.1.0"),
        )
        report = pipeline.run(
            schedule,
            [outside_event],
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        assert report.total_unassigned_events == 1
        assert any("1 event(s)" in w for w in report.warnings)


class TestPipelineAggregation:
    def test_metrics_per_split(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        assert len(schedule.splits) == 2

        # Distinct disagreements in each split's OOS window.
        # Split 0 OOS: 2026-07-11..2026-07-15 → ranking swap
        # Split 1 OOS: 2026-07-26..2026-07-30 → score delta only
        champ_script = {}
        chall_script = {}
        for date_str in ["2026-07-12"]:
            ts = f"{date_str}T14:30:00+00:00"
            champ_script[ts] = _evaluation(
                "champion-v0.4.0",
                ts,
                {"AAPL": 0.70, "MSFT": 0.60},
                ["AAPL", "MSFT"],
            )
            chall_script[ts] = _evaluation(
                "challenger-v0.1.0",
                ts,
                {"AAPL": 0.60, "MSFT": 0.70},
                ["MSFT", "AAPL"],
            )
        for date_str in ["2026-07-28"]:
            ts = f"{date_str}T14:30:00+00:00"
            champ_script[ts] = _evaluation(
                "champion-v0.4.0", ts, {"AAPL": 0.70}, ["AAPL"]
            )
            chall_script[ts] = _evaluation(
                "challenger-v0.1.0", ts, {"AAPL": 0.85}, ["AAPL"]
            )

        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0", champ_script),
            challenger=ScriptedEvaluator("challenger-v0.1.0", chall_script),
            score_delta_threshold=0.01,
        )
        events = [_event("2026-07-12"), _event("2026-07-28")]
        report = pipeline.run(
            schedule,
            events,
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        # Split 0: two ranking_only records (AAPL and MSFT swap)
        assert report.split_results[0].disagreement_counts[DISAGREEMENT_RANKING] == 2
        assert report.split_results[0].disagreement_counts[DISAGREEMENT_SCORE] == 0
        # Split 1: one score_delta record (AAPL only)
        assert report.split_results[1].disagreement_counts[DISAGREEMENT_SCORE] == 1
        assert report.split_results[1].disagreement_counts[DISAGREEMENT_RANKING] == 0
        # Totals sum across splits
        assert report.total_disagreement_counts[DISAGREEMENT_RANKING] == 2
        assert report.total_disagreement_counts[DISAGREEMENT_SCORE] == 1
        assert report.total_out_of_sample_events == 2

    def test_report_id_is_stable_across_generated_at(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0"),
            challenger=ScriptedEvaluator("challenger-v0.1.0"),
        )
        events = [_event("2026-07-12"), _event("2026-07-28")]
        first = pipeline.run(schedule, events, dataset_id="unit-test", generated_at="2026-07-02T12:00:00+00:00")
        second = pipeline.run(schedule, events, dataset_id="unit-test", generated_at="2027-01-01T00:00:00+00:00")
        assert first.report_id == second.report_id
        assert first.stable_hash() == second.stable_hash()

    def test_report_id_changes_with_dataset(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0"),
            challenger=ScriptedEvaluator("challenger-v0.1.0"),
        )
        events = [_event("2026-07-12"), _event("2026-07-28")]
        a = pipeline.run(schedule, events, dataset_id="alpha")
        b = pipeline.run(schedule, events, dataset_id="beta")
        assert a.report_id != b.report_id

    def test_report_serialization(self):
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0"),
            challenger=ScriptedEvaluator("challenger-v0.1.0"),
        )
        report = pipeline.run(schedule, [_event("2026-07-12"), _event("2026-07-28")])
        payload = json.loads(report.to_json())
        assert payload["report_id"] == report.report_id
        assert payload["schedule"]["schedule_id"] == schedule.schedule_id
        assert set(payload["total_disagreement_counts"]) == set(
            KNOWN_DISAGREEMENT_KINDS
        )
        assert len(payload["split_results"]) == len(schedule.splits)


class TestPipelineConstruction:
    def test_rejects_negative_threshold(self):
        with pytest.raises(ValueError, match="score_delta_threshold"):
            WalkForwardPipeline(
                champion=ScriptedEvaluator("a"),
                challenger=ScriptedEvaluator("b"),
                score_delta_threshold=-0.1,
            )

    def test_exposes_ids(self):
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0"),
            challenger=ScriptedEvaluator("challenger-v0.1.0"),
            score_delta_threshold=0.05,
        )
        assert pipeline.champion_id == "champion-v0.4.0"
        assert pipeline.challenger_id == "challenger-v0.1.0"
        assert pipeline.score_delta_threshold == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Wired into RS Challenger
# ---------------------------------------------------------------------------


class TestPipelineWithRSChallenger:
    def test_disabled_challenger_yields_zero_disagreements(self):
        flags = reset_feature_flags()
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        ts = "2026-07-12T14:30:00+00:00"
        champ_script = {
            ts: _evaluation(
                "champion-v0.4.0",
                ts,
                {"AAPL": 0.70, "MSFT": 0.60},
                ["AAPL", "MSFT"],
            )
        }
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0", champ_script),
            challenger=RelativeStrengthChallenger(
                base_evaluator=ScriptedEvaluator("champion-v0.4.0", champ_script),
                rs_provider=rs_provider_from_map({ts: {"AAPL": 0, "MSFT": 100}}),
                flags=FeatureFlags(enable_relative_strength=False),
            ),
        )
        report = pipeline.run(schedule, [_event("2026-07-12")])
        totals = report.total_disagreement_counts
        assert sum(totals.values()) == 0
        assert flags.all_disabled is True


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.walk_forward as module

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
                f"walk_forward must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.walk_forward as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        # The one allowed appearance is our own explanatory sentence.
        assert 'call this "training"' in source
        # Any other occurrence would be a violation.
        stripped = source.replace('We do not call this "training"', "")
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped, (
                f"walk_forward must not reference {token!r}"
            )

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.walk_forward", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.walk_forward  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden), (
            f"walk_forward must not import order-path modules: {added & forbidden}"
        )

    def test_feature_flags_remain_disabled_after_run(self):
        flags = reset_feature_flags()
        schedule = generate_walk_forward_schedule(
            start_date="2026-07-01",
            end_date="2026-07-31",
            in_sample_days=10,
            out_of_sample_days=5,
            step_days=15,
        )
        pipeline = WalkForwardPipeline(
            champion=ScriptedEvaluator("champion-v0.4.0"),
            challenger=ScriptedEvaluator("challenger-v0.1.0"),
        )
        pipeline.run(schedule, [_event("2026-07-12"), _event("2026-07-28")])

        assert flags.all_disabled is True
        assert flags.enabled_flags == []
