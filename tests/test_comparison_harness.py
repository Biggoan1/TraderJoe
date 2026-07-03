"""Tests for strategy/comparison_harness.py."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.backtest_lab import (
    BacktestEvent,
    DeterministicReplayClock,
    NoOpStrategyAdapter,
    StrategyEvaluation,
)
from strategy.comparison_harness import (
    CHALLENGER_ROLE,
    CHAMPION_ROLE,
    DISAGREEMENT_DATA,
    DISAGREEMENT_RANKING,
    DISAGREEMENT_SCORE,
    DISAGREEMENT_SELECTION,
    KNOWN_DISAGREEMENT_KINDS,
    ChampionChallengerComparison,
    ChampionChallengerRunMetadata,
    ComparisonEvaluator,
    ComparisonHarness,
    DisagreementRecord,
    ScoreRow,
    ScoreTable,
)
from strategy.config import CHALLENGER_NAME, CHAMPION_NAME, reset_feature_flags


# ---------------------------------------------------------------------------
# Fake evaluators
# ---------------------------------------------------------------------------


@dataclass
class ScriptedEvaluator:
    """Deterministic evaluator driven by a per-timestamp script."""

    strategy_id: str
    script: Dict[str, StrategyEvaluation] = field(default_factory=dict)
    calls: List[str] = field(default_factory=list)

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        self.calls.append(event.timestamp)
        evaluation = self.script.get(event.timestamp)
        if evaluation is None:
            return StrategyEvaluation(
                strategy_id=self.strategy_id,
                event_timestamp=event.timestamp,
                warnings=["no script entry"],
            )
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=dict(evaluation.scores),
            rankings=[dict(entry) for entry in evaluation.rankings],
            explanations=dict(evaluation.explanations),
            warnings=list(evaluation.warnings),
        )


def _eval(
    strategy_id: str,
    timestamp: str,
    scores: Dict[str, float],
    ranked: List[str],
    explanations: Dict[str, str] | None = None,
    warnings: List[str] | None = None,
) -> StrategyEvaluation:
    return StrategyEvaluation(
        strategy_id=strategy_id,
        event_timestamp=timestamp,
        scores=dict(scores),
        rankings=[{"symbol": symbol, "rank": idx + 1} for idx, symbol in enumerate(ranked)],
        explanations=dict(explanations or {}),
        warnings=list(warnings or []),
    )


def _clock(timestamps: List[str]) -> DeterministicReplayClock:
    return DeterministicReplayClock(
        [
            BacktestEvent(timestamp=ts, event_type="market_snapshot", sequence=idx + 1)
            for idx, ts in enumerate(timestamps)
        ]
    )


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    def test_score_row_to_dict_roundtrip(self):
        row = ScoreRow(
            symbol="AAPL",
            champion_score=0.7,
            challenger_score=0.9,
            champion_rank=2,
            challenger_rank=1,
            champion_selected=True,
            challenger_selected=True,
            score_delta=0.2,
            rank_delta=1,
            champion_explanation="champ",
            challenger_explanation="chall",
        )
        d = row.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["score_delta"] == pytest.approx(0.2)
        assert d["rank_delta"] == 1
        json.dumps(d)

    def test_disagreement_record_rejects_unknown_kind(self):
        with pytest.raises(ValueError, match="unknown disagreement kind"):
            DisagreementRecord(
                event_timestamp="2026-07-02T14:30:00+00:00",
                event_type="market_snapshot",
                symbol="AAPL",
                kind="not-a-kind",
                champion_id="champion-v0.4.0",
                challenger_id="rs-challenger-v0.1.0",
            )

    def test_score_table_to_dict(self):
        table = ScoreTable(
            event_timestamp="2026-07-02T14:30:00+00:00",
            event_type="market_snapshot",
            event_sequence=1,
            champion_id="champion-v0.4.0",
            challenger_id="rs-challenger-v0.1.0",
            rows=[ScoreRow(symbol="AAPL")],
            champion_warnings=["c"],
            challenger_warnings=["r"],
        )
        d = table.to_dict()
        assert d["rows"][0]["symbol"] == "AAPL"
        assert d["champion_warnings"] == ["c"]
        json.dumps(d)

    def test_known_disagreement_kinds(self):
        assert DISAGREEMENT_RANKING in KNOWN_DISAGREEMENT_KINDS
        assert DISAGREEMENT_SELECTION in KNOWN_DISAGREEMENT_KINDS
        assert DISAGREEMENT_SCORE in KNOWN_DISAGREEMENT_KINDS
        assert DISAGREEMENT_DATA in KNOWN_DISAGREEMENT_KINDS

    def test_role_aliases_track_config(self):
        assert CHAMPION_ROLE == CHAMPION_NAME
        assert CHALLENGER_ROLE == CHALLENGER_NAME


# ---------------------------------------------------------------------------
# Protocol / construction tests
# ---------------------------------------------------------------------------


class TestHarnessConstruction:
    def test_noop_adapter_satisfies_protocol(self):
        adapter = NoOpStrategyAdapter(strategy_id="noop")
        assert isinstance(adapter, ComparisonEvaluator)

    def test_rejects_negative_threshold(self):
        champion = NoOpStrategyAdapter(strategy_id="champion")
        challenger = NoOpStrategyAdapter(strategy_id="challenger")
        with pytest.raises(ValueError, match="score_delta_threshold"):
            ComparisonHarness(champion, challenger, score_delta_threshold=-0.1)

    def test_rejects_identical_ids(self):
        adapter = NoOpStrategyAdapter(strategy_id="same")
        other = NoOpStrategyAdapter(strategy_id="same")
        with pytest.raises(ValueError, match="distinct strategy_id"):
            ComparisonHarness(adapter, other)

    def test_exposes_identifiers(self):
        champion = NoOpStrategyAdapter(strategy_id="champion-v0.4.0")
        challenger = NoOpStrategyAdapter(strategy_id="rs-challenger-v0.1.0")
        harness = ComparisonHarness(champion, challenger, score_delta_threshold=0.05)
        assert harness.champion_id == "champion-v0.4.0"
        assert harness.challenger_id == "rs-challenger-v0.1.0"
        assert harness.score_delta_threshold == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Comparison behavior
# ---------------------------------------------------------------------------


class TestHarnessRun:
    def test_noop_run_produces_empty_tables_and_no_disagreements(self):
        champion = NoOpStrategyAdapter(strategy_id="champion")
        challenger = NoOpStrategyAdapter(strategy_id="challenger")
        harness = ComparisonHarness(champion, challenger)

        result = harness.run(_clock(["2026-07-02T14:30:00+00:00"]))

        assert result.disagreements == []
        assert len(result.score_tables) == 1
        assert result.score_tables[0].rows == []

    def test_identical_evaluations_produce_no_disagreements(self):
        ts = "2026-07-02T14:30:00+00:00"
        script = {
            ts: _eval(
                "champion",
                ts,
                scores={"AAPL": 0.9, "MSFT": 0.7},
                ranked=["AAPL", "MSFT"],
            ),
        }
        champion = ScriptedEvaluator("champion-v0.4.0", script)
        challenger = ScriptedEvaluator("challenger-v0.1.0", dict(script))
        harness = ComparisonHarness(champion, challenger)

        result = harness.run(_clock([ts]))

        assert result.disagreements == []
        assert [row.symbol for row in result.score_tables[0].rows] == ["AAPL", "MSFT"]

    def test_ranking_only_disagreement(self):
        ts = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts: _eval("champion", ts, {"AAPL": 0.9, "MSFT": 0.7}, ["AAPL", "MSFT"])
        }
        challenger_script = {
            ts: _eval("challenger", ts, {"AAPL": 0.9, "MSFT": 0.7}, ["MSFT", "AAPL"])
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
        )

        result = harness.run(_clock([ts]))

        by_kind = result.disagreements_by_kind()
        assert len(by_kind[DISAGREEMENT_RANKING]) == 2
        assert all(
            record.kind == DISAGREEMENT_RANKING
            for record in by_kind[DISAGREEMENT_RANKING]
        )
        symbols = sorted(record.symbol for record in by_kind[DISAGREEMENT_RANKING])
        assert symbols == ["AAPL", "MSFT"]
        aapl = next(r for r in by_kind[DISAGREEMENT_RANKING] if r.symbol == "AAPL")
        assert aapl.champion_rank == 1
        assert aapl.challenger_rank == 2
        assert aapl.rank_delta == -1  # champion(1) - challenger(2)

    def test_entry_selection_disagreement(self):
        ts = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts: _eval("champion", ts, {"AAPL": 0.9, "MSFT": 0.7}, ["AAPL", "MSFT"])
        }
        challenger_script = {
            ts: _eval(
                "challenger",
                ts,
                scores={"AAPL": 0.9, "MSFT": 0.7},
                ranked=["AAPL"],
            )
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
        )

        result = harness.run(_clock([ts]))

        selection_records = result.disagreements_by_kind()[DISAGREEMENT_SELECTION]
        assert [r.symbol for r in selection_records] == ["MSFT"]
        record = selection_records[0]
        assert record.champion_rank == 2
        assert record.challenger_rank is None
        assert "champion_selected=True" in record.detail
        assert "challenger_selected=False" in record.detail

    def test_data_unavailable_disagreement(self):
        ts = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts: _eval("champion", ts, {"AAPL": 0.9}, ["AAPL"])
        }
        challenger_script = {
            ts: _eval("challenger", ts, {"MSFT": 0.5}, ["MSFT"])
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
        )

        result = harness.run(_clock([ts]))

        data_records = result.disagreements_by_kind()[DISAGREEMENT_DATA]
        assert sorted(r.symbol for r in data_records) == ["AAPL", "MSFT"]
        aapl = next(r for r in data_records if r.symbol == "AAPL")
        assert aapl.champion_score == pytest.approx(0.9)
        assert aapl.challenger_score is None
        assert aapl.score_delta is None
        assert aapl.rank_delta is None

    def test_score_delta_disagreement_respects_threshold(self):
        ts = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts: _eval("champion", ts, {"AAPL": 0.60}, ["AAPL"])
        }
        challenger_script = {
            ts: _eval("challenger", ts, {"AAPL": 0.85}, ["AAPL"])
        }
        harness_strict = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
            score_delta_threshold=0.10,
        )
        harness_relaxed = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
            score_delta_threshold=0.50,
        )

        strict = harness_strict.run(_clock([ts]))
        relaxed = harness_relaxed.run(_clock([ts]))

        strict_records = strict.disagreements_by_kind()[DISAGREEMENT_SCORE]
        assert [r.symbol for r in strict_records] == ["AAPL"]
        assert strict_records[0].score_delta == pytest.approx(0.25)
        assert relaxed.disagreements_by_kind()[DISAGREEMENT_SCORE] == []

    def test_disagreements_sorted_deterministically(self):
        ts_a = "2026-07-02T14:30:00+00:00"
        ts_b = "2026-07-02T15:00:00+00:00"
        script_champ = {
            ts_a: _eval("champion", ts_a, {"AAPL": 0.9, "MSFT": 0.7}, ["AAPL", "MSFT"]),
            ts_b: _eval("champion", ts_b, {"NVDA": 0.8}, ["NVDA"]),
        }
        script_chall = {
            ts_a: _eval("challenger", ts_a, {"AAPL": 0.9, "MSFT": 0.7}, ["MSFT", "AAPL"]),
            ts_b: _eval("challenger", ts_b, {}, []),
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", script_champ),
            ScriptedEvaluator("challenger-v0.1.0", script_chall),
        )

        result = harness.run(_clock([ts_a, ts_b]))
        keys = [
            (r.event_timestamp, r.event_type, r.symbol, r.kind)
            for r in result.disagreements
        ]
        assert keys == sorted(keys)

    def test_score_table_rows_sorted_by_symbol(self):
        ts = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts: _eval("champion", ts, {"ZM": 0.5, "AAPL": 0.9}, ["AAPL", "ZM"])
        }
        challenger_script = {
            ts: _eval("challenger", ts, {"ZM": 0.5, "AAPL": 0.9}, ["AAPL", "ZM"])
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
        )

        result = harness.run(_clock([ts]))
        assert [row.symbol for row in result.score_tables[0].rows] == ["AAPL", "ZM"]

    def test_score_table_captures_event_metadata(self):
        ts = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts: _eval("champion", ts, {"AAPL": 0.9}, ["AAPL"], warnings=["c-warn"])
        }
        challenger_script = {
            ts: _eval("challenger", ts, {"AAPL": 0.9}, ["AAPL"], warnings=["r-warn"])
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
        )

        result = harness.run(_clock([ts]))
        table = result.score_tables[0]
        assert table.event_type == "market_snapshot"
        assert table.event_sequence == 1
        assert table.champion_warnings == ["c-warn"]
        assert table.challenger_warnings == ["r-warn"]
        assert result.warnings == ["c-warn", "r-warn"]


# ---------------------------------------------------------------------------
# Metadata + serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def _run(self, generated_at: str, seed: int = 0) -> ChampionChallengerComparison:
        ts_a = "2026-07-02T14:30:00+00:00"
        ts_b = "2026-07-02T15:00:00+00:00"
        champion_script = {
            ts_a: _eval("champion", ts_a, {"AAPL": 0.9, "MSFT": 0.7}, ["AAPL", "MSFT"]),
            ts_b: _eval("champion", ts_b, {"NVDA": 0.8}, ["NVDA"]),
        }
        challenger_script = {
            ts_a: _eval("challenger", ts_a, {"AAPL": 0.9, "MSFT": 0.7}, ["MSFT", "AAPL"]),
            ts_b: _eval("challenger", ts_b, {"NVDA": 0.85}, ["NVDA"]),
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
            score_delta_threshold=0.01,
        )
        return harness.run(
            _clock([ts_a, ts_b]),
            dataset_id="paper-2026-q3",
            seed=seed,
            generated_at=generated_at,
        )

    def test_run_id_derived_from_inputs(self):
        result = self._run("2026-07-02T12:00:00+00:00")
        assert isinstance(result.metadata, ChampionChallengerRunMetadata)
        assert result.metadata.run_id.startswith("cc_")
        assert result.metadata.event_count == 2
        assert result.metadata.dataset_id == "paper-2026-q3"

    def test_stable_hash_independent_of_generated_at(self):
        first = self._run("2026-07-02T12:00:00+00:00")
        second = self._run("2026-08-15T09:00:00+00:00")
        assert first.stable_hash() == second.stable_hash()
        assert first.metadata.run_id == second.metadata.run_id

    def test_run_id_changes_with_dataset(self):
        first = self._run("2026-07-02T12:00:00+00:00", seed=0)
        # Change dataset id via manual run
        ts_a = "2026-07-02T14:30:00+00:00"
        champion_script = {
            ts_a: _eval("champion", ts_a, {"AAPL": 0.9}, ["AAPL"]),
        }
        challenger_script = {
            ts_a: _eval("challenger", ts_a, {"AAPL": 0.9}, ["AAPL"]),
        }
        harness = ComparisonHarness(
            ScriptedEvaluator("champion-v0.4.0", champion_script),
            ScriptedEvaluator("challenger-v0.1.0", challenger_script),
        )
        alt = harness.run(_clock([ts_a]), dataset_id="different")
        assert alt.metadata.run_id != first.metadata.run_id

    def test_to_json_is_deterministic(self):
        first = self._run("2026-07-02T12:00:00+00:00")
        second = self._run("2026-07-02T12:00:00+00:00")

        # Drop the `generated_at` fields before comparing raw JSON — everything
        # else must be byte-identical for two runs with the same inputs.
        first_payload = json.loads(first.to_json())
        second_payload = json.loads(second.to_json())
        first_payload["metadata"].pop("generated_at")
        second_payload["metadata"].pop("generated_at")
        assert json.dumps(first_payload, sort_keys=True) == json.dumps(
            second_payload, sort_keys=True
        )

    def test_full_dict_json_serializable(self):
        result = self._run("2026-07-02T12:00:00+00:00")
        payload = result.to_dict()
        json.dumps(payload)
        assert payload["metadata"]["run_id"] == result.metadata.run_id
        assert len(payload["score_tables"]) == 2

    def test_disagreements_by_kind_partitions_records(self):
        result = self._run("2026-07-02T12:00:00+00:00")
        buckets = result.disagreements_by_kind()
        total = sum(len(records) for records in buckets.values())
        assert total == len(result.disagreements)
        assert set(buckets) == set(KNOWN_DISAGREEMENT_KINDS)


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.comparison_harness as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in ["alpaca", "place_order", "submit_order", "TradingClient"]:
            assert token not in source, (
                f"comparison_harness must not reference {token!r}"
            )

    def test_feature_flags_remain_disabled_after_run(self):
        flags = reset_feature_flags()
        champion = NoOpStrategyAdapter(strategy_id="champion")
        challenger = NoOpStrategyAdapter(strategy_id="challenger")
        harness = ComparisonHarness(champion, challenger)

        harness.run(_clock(["2026-07-02T14:30:00+00:00"]))

        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_run_does_not_import_runner(self):
        """Loading and using the harness must not import the trading runner
        or trader_cli order paths — those live outside the strategy research
        surface.
        """
        import sys

        # Fresh state: unload any modules that may already be imported.
        for name in ["strategy.comparison_harness", "strategy"]:
            sys.modules.pop(name, None)

        # Baseline snapshot of pre-existing modules.
        before = set(sys.modules)
        import strategy.comparison_harness as harness_module  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden), (
            f"comparison_harness must not import order-path modules: "
            f"{added & forbidden}"
        )
