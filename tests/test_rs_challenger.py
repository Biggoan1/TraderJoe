"""Tests for strategy/rs_challenger.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from strategy.backtest_lab import (
    BacktestEvent,
    DeterministicReplayClock,
    StrategyEvaluation,
)
from strategy.comparison_harness import (
    DISAGREEMENT_DATA,
    DISAGREEMENT_RANKING,
    DISAGREEMENT_SCORE,
    ComparisonEvaluator,
    ComparisonHarness,
)
from strategy.config import FeatureFlags, reset_feature_flags
from strategy.rs_challenger import (
    DEFAULT_RS_NEUTRAL_SCORE,
    DEFAULT_RS_OVERLAY_WEIGHT,
    DEFAULT_RS_SCORE_RANGE,
    RS_CHALLENGER_FLAG_NAME,
    RS_CHALLENGER_STRATEGY_ID,
    RelativeStrengthChallenger,
    rs_provider_from_map,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class ScriptedBase:
    """Deterministic Champion stand-in driven by a per-timestamp script."""

    strategy_id: str
    script: Dict[str, StrategyEvaluation] = field(default_factory=dict)
    call_count: int = 0

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        self.call_count += 1
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


def _base_evaluation(
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
        rankings=[{"symbol": s, "rank": i + 1} for i, s in enumerate(ranked)],
        explanations=dict(explanations or {}),
        warnings=list(warnings or []),
    )


def _event(timestamp: str, sequence: int = 1) -> BacktestEvent:
    return BacktestEvent(
        timestamp=timestamp,
        event_type="market_snapshot",
        sequence=sequence,
    )


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_rejects_negative_weight(self):
        base = ScriptedBase("champion-v0.4.0")
        with pytest.raises(ValueError, match="overlay_weight"):
            RelativeStrengthChallenger(
                base, rs_provider_from_map({}), overlay_weight=-0.1
            )

    def test_rejects_non_positive_range(self):
        base = ScriptedBase("champion-v0.4.0")
        with pytest.raises(ValueError, match="score_range"):
            RelativeStrengthChallenger(
                base, rs_provider_from_map({}), score_range=0.0
            )

    def test_rejects_empty_strategy_id(self):
        base = ScriptedBase("champion-v0.4.0")
        with pytest.raises(ValueError, match="strategy_id"):
            RelativeStrengthChallenger(
                base, rs_provider_from_map({}), strategy_id=""
            )

    def test_defaults_match_module_constants(self):
        base = ScriptedBase("champion-v0.4.0")
        challenger = RelativeStrengthChallenger(base, rs_provider_from_map({}))
        assert challenger.strategy_id == RS_CHALLENGER_STRATEGY_ID
        assert challenger.overlay_weight == DEFAULT_RS_OVERLAY_WEIGHT
        assert challenger.neutral_score == DEFAULT_RS_NEUTRAL_SCORE
        assert challenger.score_range == DEFAULT_RS_SCORE_RANGE
        assert challenger.base_strategy_id == "champion-v0.4.0"

    def test_satisfies_comparison_evaluator_protocol(self):
        base = ScriptedBase("champion-v0.4.0")
        challenger = RelativeStrengthChallenger(base, rs_provider_from_map({}))
        assert isinstance(challenger, ComparisonEvaluator)


# ---------------------------------------------------------------------------
# Flag defaults + Champion parity when disabled
# ---------------------------------------------------------------------------


class TestDisabledByDefault:
    def test_is_disabled_with_global_defaults(self):
        flags = reset_feature_flags()
        base = ScriptedBase("champion-v0.4.0")
        challenger = RelativeStrengthChallenger(base, rs_provider_from_map({}))
        assert challenger.is_enabled is False
        assert getattr(flags, RS_CHALLENGER_FLAG_NAME) is False

    def test_disabled_evaluation_is_champion_parity(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.7, "MSFT": 0.6},
                    ["AAPL", "MSFT"],
                    explanations={"AAPL": "champ-note"},
                    warnings=["champ-warning"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 90, "MSFT": 10}}),
            flags=FeatureFlags(enable_relative_strength=False),
        )

        result = challenger.evaluate(_event(ts))

        assert result.strategy_id == RS_CHALLENGER_STRATEGY_ID
        assert result.scores == {"AAPL": 0.7, "MSFT": 0.6}
        assert result.rankings == [
            {"symbol": "AAPL", "rank": 1},
            {"symbol": "MSFT", "rank": 2},
        ]
        assert result.explanations == {"AAPL": "champ-note"}
        assert result.warnings == ["champ-warning"]

    def test_disabled_harness_run_has_no_disagreements(self):
        ts = "2026-07-02T14:30:00+00:00"
        script = {
            ts: _base_evaluation(
                "champion-v0.4.0",
                ts,
                {"AAPL": 0.7, "MSFT": 0.6},
                ["AAPL", "MSFT"],
            )
        }
        harness = ComparisonHarness(
            champion=ScriptedBase("champion-v0.4.0", script),
            challenger=RelativeStrengthChallenger(
                base_evaluator=ScriptedBase("champion-v0.4.0", script),
                rs_provider=rs_provider_from_map({ts: {"AAPL": 100, "MSFT": 0}}),
                flags=FeatureFlags(enable_relative_strength=False),
            ),
        )

        result = harness.run(
            DeterministicReplayClock([_event(ts)]),
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        assert result.disagreements == []

    def test_disabled_returns_deep_copies(self):
        ts = "2026-07-02T14:30:00+00:00"
        base_eval = _base_evaluation(
            "champion-v0.4.0",
            ts,
            {"AAPL": 0.7},
            ["AAPL"],
            explanations={"AAPL": "note"},
        )
        base = ScriptedBase("champion-v0.4.0", {ts: base_eval})
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({}),
            flags=FeatureFlags(enable_relative_strength=False),
        )

        result = challenger.evaluate(_event(ts))

        # Mutating the challenger output must not affect subsequent base
        # evaluations of the same event.
        result.scores["AAPL"] = 99.0
        result.rankings[0]["rank"] = 999
        result.explanations["AAPL"] = "tampered"

        fresh = base.evaluate(_event(ts))
        assert fresh.scores == {"AAPL": 0.7}
        assert fresh.rankings == [{"symbol": "AAPL", "rank": 1}]
        assert fresh.explanations == {"AAPL": "note"}


# ---------------------------------------------------------------------------
# Enabled overlay behavior
# ---------------------------------------------------------------------------


class TestEnabledOverlay:
    def _harness_pair(
        self,
        base_eval: StrategyEvaluation,
        rs_map: Dict[str, Dict[str, float]],
        flags: FeatureFlags,
    ) -> ComparisonHarness:
        ts = base_eval.event_timestamp
        champ_script = {ts: base_eval}
        chall_script = {ts: base_eval}
        return ComparisonHarness(
            champion=ScriptedBase("champion-v0.4.0", champ_script),
            challenger=RelativeStrengthChallenger(
                base_evaluator=ScriptedBase("champion-v0.4.0", chall_script),
                rs_provider=rs_provider_from_map(rs_map),
                flags=flags,
            ),
            score_delta_threshold=0.0001,
        )

    def test_symmetric_contribution_formula(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70, "MSFT": 0.60},
                    ["AAPL", "MSFT"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 30.0, "MSFT": 90.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        result = challenger.evaluate(_event(ts))

        # weight=0.20, neutral=50, range=50
        # AAPL: 0.70 + 0.20 * (30-50)/50 = 0.70 - 0.08 = 0.62
        # MSFT: 0.60 + 0.20 * (90-50)/50 = 0.60 + 0.16 = 0.76
        assert result.scores["AAPL"] == pytest.approx(0.62)
        assert result.scores["MSFT"] == pytest.approx(0.76)

    def test_neutral_rs_produces_zero_contribution(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.55},
                    ["AAPL"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 50.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        assert challenger.evaluate(_event(ts)).scores["AAPL"] == pytest.approx(0.55)

    def test_out_of_range_rs_is_clamped(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {ts: _base_evaluation("champion-v0.4.0", ts, {"AAPL": 0.50}, ["AAPL"])},
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 500.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
            overlay_weight=0.20,
        )

        # Clamped to 100 → contribution = 0.20 * (100-50)/50 = 0.20
        assert challenger.evaluate(_event(ts)).scores["AAPL"] == pytest.approx(0.70)

    def test_custom_weight_scales_linearly(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {ts: _base_evaluation("champion-v0.4.0", ts, {"AAPL": 0.50}, ["AAPL"])},
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 75.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
            overlay_weight=0.40,
        )

        # 0.50 + 0.40 * (75-50)/50 = 0.50 + 0.20 = 0.70
        assert challenger.evaluate(_event(ts)).scores["AAPL"] == pytest.approx(0.70)

    def test_rerank_by_new_scores_descending(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70, "MSFT": 0.60},
                    ["AAPL", "MSFT"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 0.0, "MSFT": 100.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        rankings = challenger.evaluate(_event(ts)).rankings
        assert rankings == [
            {"symbol": "MSFT", "rank": 1},
            {"symbol": "AAPL", "rank": 2},
        ]

    def test_tie_breaks_by_symbol(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"ZM": 0.60, "AAPL": 0.60, "MSFT": 0.60},
                    ["ZM", "AAPL", "MSFT"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"ZM": 50.0, "AAPL": 50.0, "MSFT": 50.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        rankings = challenger.evaluate(_event(ts)).rankings
        assert [entry["symbol"] for entry in rankings] == ["AAPL", "MSFT", "ZM"]

    def test_explanations_report_contribution(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70},
                    ["AAPL"],
                    explanations={"AAPL": "6/6 gates"},
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 80.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        result = challenger.evaluate(_event(ts))
        text = result.explanations["AAPL"]
        assert "base: 6/6 gates" in text
        assert "rs: 80.00" in text
        assert "contribution=+0.1200" in text

    def test_symbols_not_in_rankings_are_rescored_but_unranked(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70, "NVDA": 0.30},
                    ["AAPL"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 20.0, "NVDA": 100.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        result = challenger.evaluate(_event(ts))
        assert "NVDA" in result.scores
        assert [entry["symbol"] for entry in result.rankings] == ["AAPL"]

    def test_harness_produces_ranking_disagreement_when_enabled(self):
        ts = "2026-07-02T14:30:00+00:00"
        base_eval = _base_evaluation(
            "champion-v0.4.0",
            ts,
            {"AAPL": 0.70, "MSFT": 0.60},
            ["AAPL", "MSFT"],
        )
        harness = self._harness_pair(
            base_eval,
            {ts: {"AAPL": 0.0, "MSFT": 100.0}},
            FeatureFlags(enable_relative_strength=True),
        )

        result = harness.run(
            DeterministicReplayClock([_event(ts)]),
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        ranking_records = result.disagreements_by_kind()[DISAGREEMENT_RANKING]
        assert sorted(r.symbol for r in ranking_records) == ["AAPL", "MSFT"]
        # ranking_only supersedes score_delta when ranks disagree
        assert result.disagreements_by_kind()[DISAGREEMENT_SCORE] == []

    def test_harness_produces_score_delta_only_when_ranks_match(self):
        ts = "2026-07-02T14:30:00+00:00"
        base_eval = _base_evaluation(
            "champion-v0.4.0",
            ts,
            {"AAPL": 0.70, "MSFT": 0.60},
            ["AAPL", "MSFT"],
        )
        # Both symbols get slight positive contribution; MSFT + 0.04, AAPL + 0.08
        # Rankings remain [AAPL, MSFT] because AAPL's advantage widens.
        harness = self._harness_pair(
            base_eval,
            {ts: {"AAPL": 70.0, "MSFT": 60.0}},
            FeatureFlags(enable_relative_strength=True),
        )

        result = harness.run(
            DeterministicReplayClock([_event(ts)]),
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        by_kind = result.disagreements_by_kind()
        assert by_kind[DISAGREEMENT_RANKING] == []
        assert sorted(r.symbol for r in by_kind[DISAGREEMENT_SCORE]) == [
            "AAPL",
            "MSFT",
        ]


# ---------------------------------------------------------------------------
# Missing / malformed RS data
# ---------------------------------------------------------------------------


class TestMissingRSData:
    def test_missing_value_preserves_base_score(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70, "MSFT": 0.60},
                    ["AAPL", "MSFT"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"MSFT": 90.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )

        result = challenger.evaluate(_event(ts))
        assert result.scores["AAPL"] == pytest.approx(0.70)
        assert result.scores["MSFT"] == pytest.approx(0.76)
        assert any(
            "rs data missing for 1 symbol(s): AAPL" in w for w in result.warnings
        )
        assert "unavailable" in result.explanations["AAPL"]

    def test_missing_timestamp_marks_all_symbols(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70, "MSFT": 0.60},
                    ["AAPL", "MSFT"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({}),  # empty snapshot
            flags=FeatureFlags(enable_relative_strength=True),
        )

        result = challenger.evaluate(_event(ts))
        assert result.scores == {"AAPL": 0.70, "MSFT": 0.60}
        assert any("AAPL, MSFT" in w for w in result.warnings)

    def test_provider_exception_captured_as_warning(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {ts: _base_evaluation("champion-v0.4.0", ts, {"AAPL": 0.70}, ["AAPL"])},
        )

        def broken(symbol, event):
            raise RuntimeError("rs source offline")

        challenger = RelativeStrengthChallenger(
            base,
            broken,
            flags=FeatureFlags(enable_relative_strength=True),
        )

        result = challenger.evaluate(_event(ts))
        assert result.scores["AAPL"] == pytest.approx(0.70)
        assert any(
            "rs_provider error for AAPL: rs source offline" in w
            for w in result.warnings
        )

    def test_missing_rs_symbol_agrees_when_rank_and_score_preserved(self):
        """A symbol with missing RS keeps its base score.  As long as the
        rank order still matches the Champion's, no disagreement is
        recorded for that symbol.  ``data_unavailable`` is reserved for
        the harness's own one-sided-symbol case, not for missing-RS.
        """
        ts = "2026-07-02T14:30:00+00:00"
        # Wide score gap so AAPL keeps rank 1 even after MSFT's overlay boost.
        base_eval = _base_evaluation(
            "champion-v0.4.0",
            ts,
            {"AAPL": 0.90, "MSFT": 0.30},
            ["AAPL", "MSFT"],
        )
        harness = ComparisonHarness(
            champion=ScriptedBase("champion-v0.4.0", {ts: base_eval}),
            challenger=RelativeStrengthChallenger(
                base_evaluator=ScriptedBase("champion-v0.4.0", {ts: base_eval}),
                rs_provider=rs_provider_from_map({ts: {"MSFT": 100.0}}),
                flags=FeatureFlags(enable_relative_strength=True),
            ),
            score_delta_threshold=0.0001,
        )

        result = harness.run(
            DeterministicReplayClock([_event(ts)]),
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        by_kind = result.disagreements_by_kind()
        # AAPL: missing RS + rank preserved + score preserved → no disagreement.
        symbols_by_kind = {
            kind: sorted(record.symbol for record in records)
            for kind, records in by_kind.items()
        }
        assert "AAPL" not in symbols_by_kind[DISAGREEMENT_SCORE]
        assert "AAPL" not in symbols_by_kind[DISAGREEMENT_RANKING]
        assert "AAPL" not in symbols_by_kind[DISAGREEMENT_DATA]
        assert symbols_by_kind[DISAGREEMENT_SCORE] == ["MSFT"]
        # data_unavailable is reserved for one-sided symbols in the harness.
        assert by_kind[DISAGREEMENT_DATA] == []


# ---------------------------------------------------------------------------
# rs_provider_from_map utility
# ---------------------------------------------------------------------------


class TestProviderFromMap:
    def test_returns_value_for_known_key(self):
        provider = rs_provider_from_map(
            {"2026-07-02T14:30:00+00:00": {"AAPL": 75.0}}
        )
        assert provider("AAPL", _event("2026-07-02T14:30:00+00:00")) == pytest.approx(75.0)

    def test_returns_none_for_unknown_timestamp(self):
        provider = rs_provider_from_map(
            {"2026-07-02T14:30:00+00:00": {"AAPL": 75.0}}
        )
        assert provider("AAPL", _event("2026-07-03T09:30:00+00:00")) is None

    def test_returns_none_for_unknown_symbol(self):
        provider = rs_provider_from_map(
            {"2026-07-02T14:30:00+00:00": {"AAPL": 75.0}}
        )
        assert provider("NVDA", _event("2026-07-02T14:30:00+00:00")) is None

    def test_input_map_is_defensively_copied(self):
        source = {"2026-07-02T14:30:00+00:00": {"AAPL": 75.0}}
        provider = rs_provider_from_map(source)
        source["2026-07-02T14:30:00+00:00"]["AAPL"] = 999.0
        assert provider("AAPL", _event("2026-07-02T14:30:00+00:00")) == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# Determinism + serialization sanity via the harness
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_repeat_runs_produce_identical_evaluations(self):
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {
                ts: _base_evaluation(
                    "champion-v0.4.0",
                    ts,
                    {"AAPL": 0.70, "MSFT": 0.60, "NVDA": 0.65},
                    ["AAPL", "NVDA", "MSFT"],
                )
            },
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map(
                {ts: {"AAPL": 20.0, "MSFT": 80.0, "NVDA": 55.0}}
            ),
            flags=FeatureFlags(enable_relative_strength=True),
        )
        first = challenger.evaluate(_event(ts))
        second = challenger.evaluate(_event(ts))
        assert first.scores == second.scores
        assert first.rankings == second.rankings
        assert first.explanations == second.explanations
        assert first.warnings == second.warnings

    def test_harness_stable_hash_independent_of_generated_at(self):
        ts = "2026-07-02T14:30:00+00:00"
        base_eval = _base_evaluation(
            "champion-v0.4.0",
            ts,
            {"AAPL": 0.70, "MSFT": 0.60},
            ["AAPL", "MSFT"],
        )
        rs_map = {ts: {"AAPL": 20.0, "MSFT": 80.0}}

        def build_harness() -> ComparisonHarness:
            return ComparisonHarness(
                champion=ScriptedBase("champion-v0.4.0", {ts: base_eval}),
                challenger=RelativeStrengthChallenger(
                    base_evaluator=ScriptedBase("champion-v0.4.0", {ts: base_eval}),
                    rs_provider=rs_provider_from_map(rs_map),
                    flags=FeatureFlags(enable_relative_strength=True),
                ),
                score_delta_threshold=0.0001,
            )

        first = build_harness().run(
            DeterministicReplayClock([_event(ts)]),
            dataset_id="unit-test",
            generated_at="2026-07-02T12:00:00+00:00",
        )
        second = build_harness().run(
            DeterministicReplayClock([_event(ts)]),
            dataset_id="unit-test",
            generated_at="2027-01-01T00:00:00+00:00",
        )
        assert first.stable_hash() == second.stable_hash()


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_source_has_no_order_path_references(self):
        import strategy.rs_challenger as module

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
                f"rs_challenger must not reference {token!r}"
            )

    def test_module_does_not_import_order_path(self):
        import sys

        for name in ["strategy.rs_challenger", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.rs_challenger  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden), (
            f"rs_challenger must not import order-path modules: {added & forbidden}"
        )

    def test_global_feature_flags_remain_disabled_after_run(self):
        flags = reset_feature_flags()
        ts = "2026-07-02T14:30:00+00:00"
        base = ScriptedBase(
            "champion-v0.4.0",
            {ts: _base_evaluation("champion-v0.4.0", ts, {"AAPL": 0.7}, ["AAPL"])},
        )
        challenger = RelativeStrengthChallenger(
            base,
            rs_provider_from_map({ts: {"AAPL": 90.0}}),
            flags=FeatureFlags(enable_relative_strength=True),
        )
        challenger.evaluate(_event(ts))

        assert flags.all_disabled is True
        assert flags.enabled_flags == []
