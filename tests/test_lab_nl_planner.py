"""Tests for strategy/lab/nl_planner.py — Task 3."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from strategy.lab.nl_planner import (
    KIND_REGIME_ANALYSIS,
    KIND_RS_WEIGHT_SWEEP,
    KIND_STRATEGY_COMPARISON,
    KIND_UNRECOGNISED,
    ResearchPlan,
    build_parser,
    main,
    plan_from_question,
)


# ---------------------------------------------------------------------------
# RS weight sweep
# ---------------------------------------------------------------------------


class TestRsWeightSweep:
    def test_range_syntax(self):
        plan = plan_from_question("Sweep RS weight from 0.2 to 1.0")
        assert plan.kind == KIND_RS_WEIGHT_SWEEP
        weights = plan.parameters["overlay_weights"]
        assert len(weights) == 5
        assert weights[0] == 0.2
        assert weights[-1] == 1.0

    def test_explicit_list(self):
        plan = plan_from_question(
            "RS weight sweep 0.30, 0.50, 0.75, 1.00"
        )
        assert plan.kind == KIND_RS_WEIGHT_SWEEP
        assert plan.parameters["overlay_weights"] == [0.30, 0.50, 0.75, 1.00]

    def test_default_grid_when_no_weights(self):
        plan = plan_from_question("Sweep RS weight to find best calibration")
        assert plan.kind == KIND_RS_WEIGHT_SWEEP
        assert plan.parameters["overlay_weights"] == [0.20, 0.30, 0.50, 0.75, 1.00]

    def test_relative_strength_synonym(self):
        plan = plan_from_question(
            "Test relative strength overlay weight range 0.2 to 0.5"
        )
        assert plan.kind == KIND_RS_WEIGHT_SWEEP

    def test_requires_weight_keyword(self):
        # No 'weight' → not an RS sweep even if RS is mentioned
        plan = plan_from_question("Look at RS behaviour over 6mo")
        assert plan.kind != KIND_RS_WEIGHT_SWEEP


# ---------------------------------------------------------------------------
# Regime analysis
# ---------------------------------------------------------------------------


class TestRegimeAnalysis:
    def test_high_vol_focus(self):
        plan = plan_from_question(
            "Test whether RS works better in high-vol regimes"
        )
        assert plan.kind == KIND_REGIME_ANALYSIS
        assert "vol" in plan.parameters["regime_dimensions"]
        assert plan.parameters["focus_bucket"] == "high"

    def test_low_credit_focus(self):
        plan = plan_from_question(
            "Slice RS results by credit stress"
        )
        assert plan.kind == KIND_REGIME_ANALYSIS
        assert "credit" in plan.parameters["regime_dimensions"]
        assert plan.parameters["focus_bucket"] == "high"  # 'stress' -> high credit stress bucket

    def test_bond_trend_only(self):
        plan = plan_from_question(
            "Regime-tag by TLT trend"
        )
        assert plan.kind == KIND_REGIME_ANALYSIS
        assert plan.parameters["regime_dimensions"] == ["bond_trend"]

    def test_default_regimes_when_none_named(self):
        plan = plan_from_question(
            "Regime-condition the latest matrix run"
        )
        assert plan.kind == KIND_REGIME_ANALYSIS
        assert plan.parameters["regime_dimensions"] == ["vol", "credit", "bond_trend"]


# ---------------------------------------------------------------------------
# Strategy comparison
# ---------------------------------------------------------------------------


class TestStrategyComparison:
    def test_two_strategies_over_range(self):
        plan = plan_from_question(
            "Compare Champion vs Momentum over 2020-2026"
        )
        assert plan.kind == KIND_STRATEGY_COMPARISON
        assert set(plan.parameters["strategies"]) == {"champion", "momentum"}
        # 7 yearly windows
        assert len(plan.parameters["windows"]) == 7
        assert plan.parameters["windows"][0]["label"] == "2020"
        assert plan.parameters["windows"][-1]["label"] == "2026"

    def test_all_years(self):
        plan = plan_from_question(
            "Champion vs Trend across all years"
        )
        assert plan.kind == KIND_STRATEGY_COMPARISON
        # 'all years' -> 2020..2026
        assert len(plan.parameters["windows"]) == 7

    def test_alias_normalisation(self):
        plan = plan_from_question(
            "Compare rs-challenger vs mean_reversion over 2024-2025"
        )
        assert plan.kind == KIND_STRATEGY_COMPARISON
        assert set(plan.parameters["strategies"]) == {
            "champion_rs", "mean_reversion",
        }


# ---------------------------------------------------------------------------
# Unrecognised
# ---------------------------------------------------------------------------


class TestUnrecognised:
    def test_empty_question(self):
        plan = plan_from_question("")
        assert plan.kind == KIND_UNRECOGNISED
        assert plan.warnings

    def test_whitespace_only(self):
        plan = plan_from_question("   \t   ")
        assert plan.kind == KIND_UNRECOGNISED

    def test_random_text(self):
        plan = plan_from_question("The quick brown fox jumped over the lazy dog.")
        assert plan.kind == KIND_UNRECOGNISED
        assert "supported kinds" in plan.warnings[0]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_question_same_plan_ignoring_timestamp(self):
        q = "Sweep RS weight from 0.2 to 1.0"
        a = plan_from_question(q)
        b = plan_from_question(q)
        # generated_at differs but parameters/steps/reasons identical
        for field_name in ("kind", "question", "parameters", "steps",
                           "reasons", "warnings"):
            assert getattr(a, field_name) == getattr(b, field_name)

    def test_json_deterministic_ignoring_timestamp(self):
        q = "Compare Champion vs Momentum over 2020-2022"
        a = plan_from_question(q).to_dict()
        b = plan_from_question(q).to_dict()
        a.pop("generated_at")
        b.pop("generated_at")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# ResearchPlan roundtrip
# ---------------------------------------------------------------------------


class TestResearchPlanRoundtrip:
    def test_to_dict_shape(self):
        plan = plan_from_question("Sweep RS weight from 0.2 to 1.0")
        d = plan.to_dict()
        assert set(d.keys()) == {
            "kind", "question", "parameters", "steps",
            "reasons", "warnings", "generated_at",
        }

    def test_json_parseable(self):
        plan = plan_from_question("Sweep RS weight from 0.2 to 1.0")
        blob = plan.to_json()
        loaded = json.loads(blob)
        assert loaded["kind"] == plan.kind


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_prints_json(self, capsys):
        rc = main(["Sweep RS weight from 0.2 to 1.0"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["kind"] == KIND_RS_WEIGHT_SWEEP

    def test_cli_writes_file(self, tmp_path, capsys):
        out_path = tmp_path / "plan.json"
        rc = main([
            "Sweep RS weight from 0.2 to 1.0",
            "--output", str(out_path),
        ])
        assert rc == 0
        assert out_path.is_file()
        loaded = json.loads(out_path.read_text())
        assert loaded["kind"] == KIND_RS_WEIGHT_SWEEP

    def test_cli_unrecognised_returns_3(self, capsys):
        rc = main(["nonsense query text with no match"])
        assert rc == 3


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _source() -> str:
    import strategy.lab.nl_planner as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _source()
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _source()
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion(self):
        s = _source()
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
        assert "FeatureFlags(" not in s

    def test_no_credential_env_reads(self):
        s = _source()
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, s)

    def test_no_execution_side_effects(self):
        s = _source()
        # Planner must not directly call any runner
        for token in ("run_historical_validation(", "run_strategy_experiment(",
                      "run_parameter_sweep(", "run_weekend_lab("):
            assert token not in s
