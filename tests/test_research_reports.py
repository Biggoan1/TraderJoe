"""Tests for strategy/research_reports.py."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.backtest_lab import BacktestEvent, DeterministicReplayClock, StrategyEvaluation
from strategy.comparison_harness import (
    DISAGREEMENT_RANKING,
    DISAGREEMENT_SCORE,
    KNOWN_DISAGREEMENT_KINDS,
    ChampionChallengerComparison,
    ComparisonHarness,
)
from strategy.config import FeatureFlags, reset_feature_flags
from strategy.research_reports import (
    KNOWN_REPORT_KINDS,
    REPORT_KIND_COMPARISON,
    REPORT_KIND_WALK_FORWARD,
    ResearchReport,
    ResearchReportPaths,
    render_comparison_report,
    render_walk_forward_report,
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
    warnings: List[str] | None = None,
) -> StrategyEvaluation:
    return StrategyEvaluation(
        strategy_id=strategy_id,
        event_timestamp=timestamp,
        scores=dict(scores),
        rankings=[{"symbol": s, "rank": i + 1} for i, s in enumerate(ranked)],
        warnings=list(warnings or []),
    )


def _event(date: str, sequence: int = 1) -> BacktestEvent:
    return BacktestEvent(
        timestamp=f"{date}T14:30:00+00:00",
        event_type="market_snapshot",
        sequence=sequence,
    )


def _build_comparison(*, with_data_warning: bool = False) -> ChampionChallengerComparison:
    ts = "2026-07-02T14:30:00+00:00"
    champ_script = {
        ts: _eval(
            "champion-v0.4.0",
            ts,
            {"AAPL": 0.70, "MSFT": 0.60},
            ["AAPL", "MSFT"],
            warnings=["champ-warn"] if with_data_warning else [],
        )
    }
    chall_script = {
        ts: _eval(
            "challenger-v0.1.0",
            ts,
            {"AAPL": 0.60, "MSFT": 0.70},
            ["MSFT", "AAPL"],
        )
    }
    harness = ComparisonHarness(
        champion=ScriptedEvaluator("champion-v0.4.0", champ_script),
        challenger=ScriptedEvaluator("challenger-v0.1.0", chall_script),
        score_delta_threshold=0.01,
    )
    return harness.run(
        DeterministicReplayClock(
            [BacktestEvent(timestamp=ts, event_type="market_snapshot", sequence=1)]
        ),
        dataset_id="paper-2026-q3",
        seed=0,
        generated_at="2026-07-02T12:00:00+00:00",
    )


def _build_empty_comparison() -> ChampionChallengerComparison:
    harness = ComparisonHarness(
        champion=ScriptedEvaluator("champion-v0.4.0"),
        challenger=ScriptedEvaluator("challenger-v0.1.0"),
    )
    return harness.run(
        DeterministicReplayClock([]),
        dataset_id="empty",
        seed=0,
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
    for date_str in ["2026-07-12"]:
        ts = f"{date_str}T14:30:00+00:00"
        champ_script[ts] = _eval(
            "champion-v0.4.0", ts, {"AAPL": 0.70, "MSFT": 0.60}, ["AAPL", "MSFT"]
        )
        chall_script[ts] = _eval(
            "challenger-v0.1.0", ts, {"AAPL": 0.60, "MSFT": 0.70}, ["MSFT", "AAPL"]
        )
    for date_str in ["2026-07-28"]:
        ts = f"{date_str}T14:30:00+00:00"
        champ_script[ts] = _eval("champion-v0.4.0", ts, {"AAPL": 0.70}, ["AAPL"])
        chall_script[ts] = _eval("challenger-v0.1.0", ts, {"AAPL": 0.85}, ["AAPL"])

    pipeline = WalkForwardPipeline(
        champion=ScriptedEvaluator("champion-v0.4.0", champ_script),
        challenger=ScriptedEvaluator("challenger-v0.1.0", chall_script),
        score_delta_threshold=0.01,
    )
    return pipeline.run(
        schedule,
        [_event("2026-07-12"), _event("2026-07-28")],
        dataset_id="paper-2026-q3",
        seed=0,
        generated_at="2026-07-02T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestResearchReportModel:
    def test_report_rejects_unknown_kind(self):
        with pytest.raises(ValueError, match="unknown report kind"):
            ResearchReport(
                report_id="rr_test",
                kind="mystery",
                title="t",
                markdown="",
                payload={},
                disagreements=[],
                manifest={},
                generated_at="2026-07-02T12:00:00+00:00",
            )

    def test_known_report_kinds_contain_expected_values(self):
        assert REPORT_KIND_COMPARISON in KNOWN_REPORT_KINDS
        assert REPORT_KIND_WALK_FORWARD in KNOWN_REPORT_KINDS

    def test_paths_derive_from_output_dir_and_report_id(self):
        paths = ResearchReportPaths(output_dir="/tmp/out", report_id="rr_abcd")
        d = paths.to_dict()
        assert d["report_dir"].endswith("rr_abcd")
        assert d["markdown_path"].endswith("rr_abcd/report.md")
        assert d["json_path"].endswith("rr_abcd/report.json")
        assert d["disagreements_path"].endswith("rr_abcd/disagreements.json")
        assert d["manifest_path"].endswith("rr_abcd/manifest.json")
        json.dumps(d)


# ---------------------------------------------------------------------------
# Comparison report structure (snapshot-style)
# ---------------------------------------------------------------------------


class TestComparisonReport:
    def test_report_id_prefix_and_length(self):
        report = render_comparison_report(_build_comparison())
        assert report.report_id.startswith("rr_")
        assert len(report.report_id) == 3 + 12  # prefix + digest

    def test_report_title_default_matches_run_id(self):
        comparison = _build_comparison()
        report = render_comparison_report(comparison)
        assert comparison.metadata.run_id in report.title

    def test_report_custom_title(self):
        report = render_comparison_report(
            _build_comparison(), title="Custom Title"
        )
        assert report.title == "Custom Title"

    def test_json_payload_schema(self):
        report = render_comparison_report(_build_comparison())
        payload = json.loads(report.to_json())
        assert payload["report_id"] == report.report_id
        assert payload["kind"] == REPORT_KIND_COMPARISON
        assert "summary" in payload
        assert "daily_summary" in payload
        assert "comparison" in payload
        summary = payload["summary"]
        for key in (
            "champion_id",
            "challenger_id",
            "dataset_id",
            "event_count",
            "score_delta_threshold",
            "seed",
            "total_disagreements",
            "disagreement_counts",
            "warnings",
        ):
            assert key in summary
        assert set(summary["disagreement_counts"]) == set(KNOWN_DISAGREEMENT_KINDS)

    def test_disagreement_counts_match_actual_records(self):
        report = render_comparison_report(_build_comparison())
        counts = report.manifest["disagreement_counts"]
        # AAPL + MSFT swap → two ranking_only disagreements
        assert counts[DISAGREEMENT_RANKING] == 2
        assert counts[DISAGREEMENT_SCORE] == 0
        assert len(report.disagreements) == 2

    def test_disagreements_sorted_deterministically(self):
        report = render_comparison_report(_build_comparison())
        keys = [
            (
                record["event_timestamp"],
                record["event_type"],
                record["symbol"],
                record["kind"],
            )
            for record in report.disagreements
        ]
        assert keys == sorted(keys)

    def test_daily_summary_present_for_each_event(self):
        comparison = _build_comparison()
        report = render_comparison_report(comparison)
        payload = json.loads(report.to_json())
        assert len(payload["daily_summary"]) == len(comparison.score_tables)
        row = payload["daily_summary"][0]
        assert row["event_timestamp"] == "2026-07-02T14:30:00+00:00"
        assert row["disagreement_counts"][DISAGREEMENT_RANKING] == 2

    def test_manifest_carries_reproducibility_metadata(self):
        comparison = _build_comparison()
        report = render_comparison_report(comparison)
        manifest = json.loads(report.manifest_json())
        assert manifest["source_run_id"] == comparison.metadata.run_id
        assert manifest["source_hash"] == comparison.stable_hash()
        assert manifest["champion_id"] == "champion-v0.4.0"
        assert manifest["challenger_id"] == "challenger-v0.1.0"
        assert manifest["kind"] == REPORT_KIND_COMPARISON

    def test_markdown_contains_key_sections_and_ids(self):
        report = render_comparison_report(_build_comparison())
        md = report.to_markdown()
        assert "# Champion vs Challenger Comparison" in md
        assert "## Summary" in md
        assert "## Daily Comparison" in md
        assert "## Disagreements by Kind" in md
        assert "## Reproducibility" in md
        assert report.report_id in md
        for kind in KNOWN_DISAGREEMENT_KINDS:
            assert f"`{kind}`" in md
        assert "Observational research only" in md

    def test_markdown_flags_no_events_case(self):
        report = render_comparison_report(_build_empty_comparison())
        md = report.to_markdown()
        assert "_No events in this comparison._" in md

    def test_data_quality_notes_include_warnings(self):
        comparison = _build_comparison(with_data_warning=True)
        report = render_comparison_report(comparison)
        md = report.to_markdown()
        assert "## Data-Quality Notes" in md
        assert "champ-warn" in md
        payload = json.loads(report.to_json())
        assert "champ-warn" in payload["summary"]["warnings"]


# ---------------------------------------------------------------------------
# Walk-forward report structure (snapshot-style)
# ---------------------------------------------------------------------------


class TestWalkForwardReport:
    def test_report_id_prefix(self):
        report = render_walk_forward_report(_build_walk_forward())
        assert report.report_id.startswith("rr_")

    def test_json_payload_schema(self):
        report = render_walk_forward_report(_build_walk_forward())
        payload = json.loads(report.to_json())
        assert payload["kind"] == REPORT_KIND_WALK_FORWARD
        summary = payload["summary"]
        for key in (
            "champion_id",
            "challenger_id",
            "dataset_id",
            "schedule_id",
            "in_sample_days",
            "out_of_sample_days",
            "step_days",
            "split_count",
            "total_events",
            "total_out_of_sample_events",
            "total_unassigned_events",
            "total_disagreements",
            "disagreement_counts",
            "warnings",
        ):
            assert key in summary
        assert set(summary["disagreement_counts"]) == set(KNOWN_DISAGREEMENT_KINDS)
        assert "walk_forward" in payload
        assert "split_results" in payload["walk_forward"]

    def test_disagreements_carry_split_metadata(self):
        wf = _build_walk_forward()
        report = render_walk_forward_report(wf)
        assert report.disagreements  # non-empty
        for record in report.disagreements:
            assert "split_id" in record
            assert "split_index" in record

    def test_disagreements_sorted_by_split_then_event(self):
        report = render_walk_forward_report(_build_walk_forward())
        keys = [
            (
                record["split_index"],
                record["event_timestamp"],
                record["event_type"],
                record["symbol"],
                record["kind"],
            )
            for record in report.disagreements
        ]
        assert keys == sorted(keys)

    def test_markdown_contains_key_sections(self):
        report = render_walk_forward_report(_build_walk_forward())
        md = report.to_markdown()
        assert "# Walk-Forward Comparison" in md
        assert "## Summary" in md
        assert "## Split Results" in md
        assert "## Reproducibility" in md
        assert "In-sample days" in md
        assert "Out-of-sample days" in md
        assert "Observational research only" in md

    def test_markdown_flags_no_splits_case(self):
        empty_wf = _build_walk_forward()
        # Overwrite split_results to simulate an empty schedule
        empty_wf.split_results = []
        empty_wf.total_out_of_sample_events = 0
        report = render_walk_forward_report(empty_wf)
        assert "_No splits configured._" in report.to_markdown()

    def test_manifest_carries_reproducibility_metadata(self):
        wf = _build_walk_forward()
        report = render_walk_forward_report(wf)
        manifest = json.loads(report.manifest_json())
        assert manifest["source_report_id"] == wf.report_id
        assert manifest["source_hash"] == wf.stable_hash()
        assert manifest["schedule_hash"] == wf.schedule.stable_hash()
        assert manifest["kind"] == REPORT_KIND_WALK_FORWARD

    def test_payload_walk_forward_strips_generated_at(self):
        wf = _build_walk_forward()
        report = render_walk_forward_report(wf)
        payload = json.loads(report.to_json())
        walk = payload["walk_forward"]
        assert "generated_at" not in walk
        for split in walk["split_results"]:
            assert "generated_at" not in split["comparison"]["metadata"]


# ---------------------------------------------------------------------------
# Determinism + write
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_stable_hash_ignores_generated_at(self):
        comparison = _build_comparison()
        first = render_comparison_report(
            comparison, generated_at="2026-07-02T12:00:00+00:00"
        )
        second = render_comparison_report(
            comparison, generated_at="2027-01-01T00:00:00+00:00"
        )
        assert first.stable_hash() == second.stable_hash()
        assert first.report_id == second.report_id

    def test_repeat_render_produces_identical_byte_output(self):
        comparison = _build_comparison()
        first = render_comparison_report(
            comparison, generated_at="2026-07-02T12:00:00+00:00"
        )
        second = render_comparison_report(
            comparison, generated_at="2026-07-02T12:00:00+00:00"
        )
        assert first.to_json() == second.to_json()
        assert first.to_markdown() == second.to_markdown()
        assert first.disagreements_json() == second.disagreements_json()
        assert first.manifest_json() == second.manifest_json()

    def test_walk_forward_stable_hash_ignores_generated_at(self):
        wf = _build_walk_forward()
        first = render_walk_forward_report(
            wf, generated_at="2026-07-02T12:00:00+00:00"
        )
        second = render_walk_forward_report(
            wf, generated_at="2027-01-01T00:00:00+00:00"
        )
        assert first.stable_hash() == second.stable_hash()
        assert first.report_id == second.report_id

    def test_write_persists_all_four_files(self, tmp_path):
        report = render_comparison_report(
            _build_comparison(), generated_at="2026-07-02T12:00:00+00:00"
        )
        paths = report.write(str(tmp_path))

        assert Path(paths.markdown_path).is_file()
        assert Path(paths.json_path).is_file()
        assert Path(paths.disagreements_path).is_file()
        assert Path(paths.manifest_path).is_file()

        # Content sanity: JSON files are valid JSON
        for path in (paths.json_path, paths.disagreements_path, paths.manifest_path):
            json.loads(Path(path).read_text(encoding="utf-8"))

    def test_write_is_idempotent(self, tmp_path):
        report = render_comparison_report(
            _build_comparison(), generated_at="2026-07-02T12:00:00+00:00"
        )
        report.write(str(tmp_path))
        report.write(str(tmp_path))

        paths = report.paths_for(str(tmp_path))
        assert Path(paths.markdown_path).read_text(encoding="utf-8") == report.markdown

    def test_write_places_files_under_report_id_dir(self, tmp_path):
        report = render_comparison_report(_build_comparison())
        paths = report.write(str(tmp_path))
        assert Path(paths.report_dir).name == report.report_id


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.research_reports as module

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
                f"research_reports must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.research_reports as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        # The one allowed appearance is the explicit terminology sentence.
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 3 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped, (
                f"research_reports must not reference {token!r}"
            )

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.research_reports", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.research_reports  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden), (
            f"research_reports must not import order-path modules: {added & forbidden}"
        )

    def test_feature_flags_remain_disabled_after_render(self, tmp_path):
        flags = reset_feature_flags()
        report = render_comparison_report(_build_comparison())
        report.write(str(tmp_path))

        assert flags.all_disabled is True
        assert flags.enabled_flags == []
