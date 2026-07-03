"""Tests for strategy/score_explanation.py."""

from __future__ import annotations

import json

import pytest

from strategy.score_explanation import (
    SCORE_EXPLANATION_KIND,
    ScoreComponent,
    ScoreExplanation,
    aggregate_explanation_dicts,
    append_overlay_component,
    blank_explanation,
    summarize_explanations,
)


def _base_explanation() -> ScoreExplanation:
    return ScoreExplanation(
        strategy_id="champion-v0.4.0",
        symbol="AAPL",
        event_timestamp="2026-05-15T04:00:00Z",
        final_score=6.5,
        components=(
            ScoreComponent(name="above_sma20", contribution=1.5, detail="a"),
            ScoreComponent(name="macd_positive", contribution=1.5, detail="b"),
            ScoreComponent(name="adx_strength", contribution=1.0, detail="c"),
        ),
        ranking_factors=("above_sma20", "macd_positive", "adx_strength"),
        momentum_contribution=0.75,
        bonuses=(("adx_continuous", 0.75),),
        confidence=0.5,
        notes=("gate_count=3/6",),
    )


class TestScoreExplanationSerialization:
    def test_to_dict_shape(self):
        exp = _base_explanation()
        d = exp.to_dict()
        assert d["kind"] == SCORE_EXPLANATION_KIND
        assert d["strategy_id"] == "champion-v0.4.0"
        assert d["symbol"] == "AAPL"
        assert d["final_score"] == 6.5
        assert isinstance(d["components"], list)
        assert d["components"][0] == {
            "name": "above_sma20",
            "contribution": 1.5,
            "detail": "a",
        }
        assert d["ranking_factors"] == [
            "above_sma20",
            "macd_positive",
            "adx_strength",
        ]
        assert d["momentum_contribution"] == 0.75
        assert d["bonuses"] == [
            {"reason": "adx_continuous", "magnitude": 0.75}
        ]
        assert d["confidence"] == 0.5
        assert d["notes"] == ["gate_count=3/6"]

    def test_optional_fields_serialize_as_none(self):
        exp = ScoreExplanation(
            strategy_id="s",
            symbol="X",
            event_timestamp="2026-01-01",
            final_score=0.0,
        )
        d = exp.to_dict()
        assert d["rs_contribution"] is None
        assert d["breadth_contribution"] is None
        assert d["momentum_contribution"] is None
        assert d["confidence"] is None
        assert d["components"] == []
        assert d["bonuses"] == []
        assert d["penalties"] == []

    def test_summary_string_is_human_readable(self):
        exp = _base_explanation()
        summary = exp.to_summary_str()
        assert "final=6.5000" in summary
        assert "above_sma20=+1.5000" in summary
        assert "momentum=+0.7500" in summary
        assert "bonus[adx_continuous]=+0.7500" in summary
        assert "confidence=0.50" in summary

    def test_json_round_trip(self):
        exp = _base_explanation()
        blob = json.dumps(exp.to_dict(), sort_keys=True)
        parsed = json.loads(blob)
        assert parsed["final_score"] == 6.5


class TestScoreExplanationDeterminism:
    def test_same_inputs_produce_identical_hash(self):
        a = _base_explanation()
        b = _base_explanation()
        assert a.stable_hash() == b.stable_hash()

    def test_hash_changes_when_component_changes(self):
        a = _base_explanation()
        b = ScoreExplanation(
            strategy_id=a.strategy_id,
            symbol=a.symbol,
            event_timestamp=a.event_timestamp,
            final_score=a.final_score,
            components=a.components + (
                ScoreComponent(name="extra", contribution=0.1),
            ),
            ranking_factors=a.ranking_factors,
            momentum_contribution=a.momentum_contribution,
            bonuses=a.bonuses,
            confidence=a.confidence,
            notes=a.notes,
        )
        assert a.stable_hash() != b.stable_hash()

    def test_frozen_dataclass_rejects_mutation(self):
        exp = _base_explanation()
        with pytest.raises(Exception):
            exp.final_score = 999  # type: ignore[misc]


class TestBlankExplanation:
    def test_blank_carries_zero_score_and_note(self):
        exp = blank_explanation(
            "s", "X", "2026-01-01", 0.0, note="no data"
        )
        assert exp.final_score == 0.0
        assert exp.strategy_id == "s"
        assert exp.notes == ("no data",)
        assert exp.rejected is False


class TestAppendOverlayComponent:
    def test_overlay_preserves_base_and_appends(self):
        base = _base_explanation()
        overlay = append_overlay_component(
            base,
            overlay_strategy_id="rs-challenger-v0.1.0",
            component_name="rs_overlay",
            contribution=0.20,
            detail="rs=90 weight=0.2",
            rs_value=90.0,
        )
        # Base's components + notes preserved
        assert overlay.components[:-1] == base.components
        assert overlay.notes == base.notes
        # Overlay component appended
        assert overlay.components[-1].name == "rs_overlay"
        assert overlay.components[-1].contribution == 0.20
        # rs_value promoted onto the overlay
        assert overlay.rs_contribution == 90.0
        # Strategy identity flips to the overlay
        assert overlay.strategy_id == "rs-challenger-v0.1.0"
        # Final score is base + overlay contribution
        assert overlay.final_score == pytest.approx(base.final_score + 0.20)

    def test_overlay_preserves_confidence_and_bonuses(self):
        base = _base_explanation()
        overlay = append_overlay_component(
            base, "chall", "c", 0.0
        )
        assert overlay.confidence == base.confidence
        assert overlay.bonuses == base.bonuses

    def test_overlay_appends_notes_when_supplied(self):
        base = _base_explanation()
        overlay = append_overlay_component(
            base, "chall", "c", 0.0, notes=("rs_data_missing",)
        )
        assert overlay.notes[-1] == "rs_data_missing"
        assert overlay.notes[:-1] == base.notes


class TestSummarizeExplanations:
    def test_empty_returns_empty_stats(self):
        result = summarize_explanations({})
        assert result["count"] == 0
        assert result["rejected_count"] == 0
        assert result["component_totals"] == {}

    def test_aggregates_components_and_factors(self):
        a = _base_explanation()
        b = ScoreExplanation(
            strategy_id="s",
            symbol="MSFT",
            event_timestamp="t",
            final_score=3.0,
            components=(
                ScoreComponent(name="above_sma20", contribution=1.5),
            ),
            ranking_factors=("above_sma20",),
            rejected=False,
        )
        result = summarize_explanations({"AAPL": a, "MSFT": b})
        assert result["count"] == 2
        assert result["component_totals"]["above_sma20"] == 3.0
        # AAPL contributed macd_positive/adx_strength, MSFT did not
        assert result["component_totals"]["macd_positive"] == 1.5


class TestAggregateExplanationDicts:
    def test_empty_input_returns_zeroes(self):
        result = aggregate_explanation_dicts([])
        assert result["observation_count"] == 0
        assert result["component_pass_rates"] == {}
        assert result["mean_confidence"] is None

    def test_aggregates_across_many_observations(self):
        # 4 observations, above_sma20 fires on 3
        dicts = [
            {
                "components": [
                    {"name": "above_sma20", "contribution": 1.5},
                    {"name": "adx_strength", "contribution": 1.0},
                ],
                "ranking_factors": ["above_sma20", "adx_strength"],
                "rejected": False,
                "confidence": 0.6,
            },
            {
                "components": [
                    {"name": "above_sma20", "contribution": 1.5},
                ],
                "ranking_factors": ["above_sma20"],
                "rejected": False,
                "confidence": 0.4,
            },
            {
                "components": [
                    {"name": "above_sma20", "contribution": 1.5},
                    {"name": "macd_positive", "contribution": 1.5},
                ],
                "ranking_factors": ["above_sma20", "macd_positive"],
                "rejected": False,
                "confidence": 0.8,
            },
            {
                "components": [],
                "ranking_factors": [],
                "rejected": True,
                "confidence": None,
            },
        ]
        result = aggregate_explanation_dicts(dicts)
        assert result["observation_count"] == 4
        assert result["rejected_rate"] == 0.25
        assert result["component_pass_rates"]["above_sma20"] == 0.75
        assert result["component_pass_rates"]["adx_strength"] == 0.25
        assert result["mean_component_contributions"]["above_sma20"] == 1.5
        # Confidence averaged over the 3 present values
        assert result["mean_confidence"] == pytest.approx((0.6 + 0.4 + 0.8) / 3, abs=1e-4)
        # Top ranking factor by count
        assert result["top_ranking_factors"][0]["factor"] == "above_sma20"
        assert result["top_ranking_factors"][0]["count"] == 3

    def test_ignores_non_mapping_entries(self):
        dicts = [{"components": [{"name": "x", "contribution": 1.0}]}, None]
        result = aggregate_explanation_dicts(dicts)
        assert result["observation_count"] == 2  # count both, but skip non-mapping
        assert result["component_pass_rates"]["x"] == 0.5
