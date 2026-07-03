"""Tests for strategy/promotion_gates.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy.config import reset_feature_flags
from strategy.promotion_gates import (
    APPROVAL_EVIDENCE_KEY,
    KNOWN_COMPARATORS,
    PROMOTION_STATES,
    REQUIRED_EVIDENCE_PER_STATE,
    STANDARD_ROLLBACK_CRITERIA,
    STATE_APPROVED,
    STATE_BACKTEST,
    STATE_CANDIDATE,
    STATE_DISABLED,
    STATE_PAPER_TRADING,
    STATE_PRODUCTION,
    STATE_WALK_FORWARD,
    ApprovalRecord,
    PromotionEntry,
    PromotionReport,
    RollbackAlert,
    RollbackCriterion,
    evaluate_promotion,
    is_terminal_state,
    next_state,
    state_index,
    triggered_alerts,
)


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


class TestPromotionStates:
    def test_expected_ordering(self):
        assert PROMOTION_STATES == (
            STATE_DISABLED,
            STATE_BACKTEST,
            STATE_WALK_FORWARD,
            STATE_PAPER_TRADING,
            STATE_CANDIDATE,
            STATE_APPROVED,
            STATE_PRODUCTION,
        )

    def test_state_index_returns_zero_for_disabled(self):
        assert state_index(STATE_DISABLED) == 0
        assert state_index(STATE_PRODUCTION) == len(PROMOTION_STATES) - 1

    def test_state_index_rejects_unknown(self):
        with pytest.raises(ValueError, match="unknown promotion state"):
            state_index("not-a-state")

    def test_next_state_progression(self):
        assert next_state(STATE_DISABLED) == STATE_BACKTEST
        assert next_state(STATE_BACKTEST) == STATE_WALK_FORWARD
        assert next_state(STATE_WALK_FORWARD) == STATE_PAPER_TRADING
        assert next_state(STATE_PAPER_TRADING) == STATE_CANDIDATE
        assert next_state(STATE_CANDIDATE) == STATE_APPROVED
        assert next_state(STATE_APPROVED) == STATE_PRODUCTION
        assert next_state(STATE_PRODUCTION) is None

    def test_is_terminal_state(self):
        assert is_terminal_state(STATE_PRODUCTION) is True
        assert is_terminal_state(STATE_DISABLED) is False

    def test_next_state_rejects_unknown(self):
        with pytest.raises(ValueError, match="unknown promotion state"):
            next_state("mystery")

    def test_required_evidence_covers_every_state(self):
        assert set(REQUIRED_EVIDENCE_PER_STATE) == set(PROMOTION_STATES)

    def test_disabled_requires_no_evidence(self):
        assert REQUIRED_EVIDENCE_PER_STATE[STATE_DISABLED] == ()

    def test_approved_requires_approval_record(self):
        assert APPROVAL_EVIDENCE_KEY in REQUIRED_EVIDENCE_PER_STATE[STATE_APPROVED]

    def test_production_requires_approval_and_monitoring(self):
        keys = REQUIRED_EVIDENCE_PER_STATE[STATE_PRODUCTION]
        assert APPROVAL_EVIDENCE_KEY in keys
        assert "monitoring_dashboard" in keys
        assert "rollback_owner" in keys


# ---------------------------------------------------------------------------
# ApprovalRecord
# ---------------------------------------------------------------------------


class TestApprovalRecord:
    def _record(self, **overrides) -> ApprovalRecord:
        base = dict(
            approver="operator",
            approved_at="2026-07-02",
            flag_name="enable_relative_strength",
            scope="Bounded rollout in shadow mode",
            monitoring="Grafana dashboard XYZ",
            rollback_plan="Disable flag if drawdown_delta > 0",
        )
        base.update(overrides)
        return ApprovalRecord(**base)

    def test_to_dict_roundtrip(self):
        record = self._record(commit="abc123", notes="pilot")
        d = record.to_dict()
        assert d["approver"] == "operator"
        assert d["commit"] == "abc123"
        json.dumps(d)

    @pytest.mark.parametrize(
        "field,value,error",
        [
            ("approver", "", "approver"),
            ("approved_at", "", "approved_at"),
            ("flag_name", "", "flag_name"),
            ("scope", "", "scope"),
            ("monitoring", "", "monitoring"),
            ("rollback_plan", "", "rollback_plan"),
        ],
    )
    def test_missing_required_fields(self, field, value, error):
        with pytest.raises(ValueError, match=error):
            self._record(**{field: value})


# ---------------------------------------------------------------------------
# RollbackCriterion + Alert
# ---------------------------------------------------------------------------


class TestRollbackCriterion:
    @pytest.mark.parametrize(
        "actual,comparator,threshold,triggered",
        [
            (0.5, "lt", 1.0, True),
            (0.5, "le", 0.5, True),
            (2.0, "gt", 1.0, True),
            (2.0, "ge", 2.0, True),
            (1.0, "eq", 1.0, True),
            (1.0, "lt", 1.0, False),
            (1.0, "gt", 1.0, False),
        ],
    )
    def test_check_comparators(self, actual, comparator, threshold, triggered):
        criterion = RollbackCriterion(
            name="x",
            description="",
            metric_key="m",
            threshold=threshold,
            comparator=comparator,
        )
        alert = criterion.check({"m": actual})
        assert alert.triggered is triggered
        assert alert.data_available is True

    def test_missing_metric_yields_non_triggered_alert_with_flag(self):
        criterion = RollbackCriterion(
            name="x",
            description="",
            metric_key="missing",
            threshold=0.0,
            comparator="lt",
        )
        alert = criterion.check({})
        assert alert.triggered is False
        assert alert.data_available is False
        assert alert.actual_value is None

    def test_invalid_comparator_rejected_at_construction(self):
        with pytest.raises(ValueError, match="unknown comparator"):
            RollbackCriterion(
                name="x",
                description="",
                metric_key="m",
                threshold=0.0,
                comparator="not-a-comparator",
            )

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="name is required"):
            RollbackCriterion(
                name="",
                description="",
                metric_key="m",
                threshold=0.0,
                comparator="lt",
            )

    def test_alert_to_dict_json_serializable(self):
        criterion = RollbackCriterion(
            name="x",
            description="",
            metric_key="m",
            threshold=0.0,
            comparator="lt",
        )
        alert = criterion.check({"m": -1.0})
        d = alert.to_dict()
        assert d["triggered"] is True
        json.dumps(d)

    def test_known_comparators(self):
        for comparator in KNOWN_COMPARATORS:
            RollbackCriterion(
                name="x",
                description="",
                metric_key="m",
                threshold=0.0,
                comparator=comparator,
            )

    def test_standard_rollback_criteria_present(self):
        names = {criterion.name for criterion in STANDARD_ROLLBACK_CRITERIA}
        assert "expectancy_worse_than_champion" in names
        assert "drawdown_worse_than_limit" in names
        assert "profit_factor_worse_than_champion" in names
        assert "trade_frequency_below_minimum" in names
        assert "concentration_exceeds_limit" in names
        assert "data_quality_below_tolerance" in names
        assert "reproducibility_check_failed" in names


# ---------------------------------------------------------------------------
# PromotionEntry
# ---------------------------------------------------------------------------


class TestPromotionEntry:
    def test_defaults_to_disabled(self):
        entry = PromotionEntry(flag_name="enable_relative_strength")
        assert entry.current_state == STATE_DISABLED
        assert entry.approvals == []
        assert entry.evidence == {}

    def test_unknown_state_rejected(self):
        with pytest.raises(ValueError, match="unknown promotion state"):
            PromotionEntry(flag_name="x", current_state="mystery")

    def test_empty_flag_name_rejected(self):
        with pytest.raises(ValueError, match="flag_name"):
            PromotionEntry(flag_name="")

    def test_approval_flag_mismatch_rejected(self):
        approval = ApprovalRecord(
            approver="operator",
            approved_at="2026-07-02",
            flag_name="enable_market_regime",
            scope="scope",
            monitoring="monitoring",
            rollback_plan="rollback",
        )
        with pytest.raises(ValueError, match="must match"):
            PromotionEntry(
                flag_name="enable_relative_strength",
                approvals=[approval],
            )

    def test_to_dict_roundtrip(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
            evidence={"experiment_manifest": "abc", "dataset_id": "paper-2026-q3"},
        )
        d = entry.to_dict()
        assert d["current_state"] == STATE_BACKTEST
        json.dumps(d)


# ---------------------------------------------------------------------------
# evaluate_promotion
# ---------------------------------------------------------------------------


def _sample_approval(flag_name: str = "enable_relative_strength") -> ApprovalRecord:
    return ApprovalRecord(
        approver="operator",
        approved_at="2026-07-02",
        flag_name=flag_name,
        scope="Bounded shadow rollout",
        monitoring="Grafana dashboard XYZ",
        rollback_plan="Disable flag if drawdown_delta > 0",
    )


class TestEvaluatePromotion:
    def test_disabled_entry_lists_backtest_requirements(self):
        entry = PromotionEntry(flag_name="enable_relative_strength")
        report = evaluate_promotion(entry)
        assert report.current_state == STATE_DISABLED
        assert report.current_state_index == 0
        assert report.next_state == STATE_BACKTEST
        assert set(report.required_evidence) == {
            "experiment_manifest",
            "dataset_id",
        }
        assert set(report.missing_evidence) == set(report.required_evidence)

    def test_disabled_entry_does_not_emit_missing_evidence_warning(self):
        """Disabled is the default; missing gate evidence should not warn."""
        entry = PromotionEntry(flag_name="enable_relative_strength")
        report = evaluate_promotion(entry)
        assert not any(
            "missing required evidence" in w for w in report.warnings
        )

    def test_entry_at_backtest_with_all_evidence_flags_none_missing(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
            evidence={
                "experiment_manifest": "abc",
                "dataset_id": "paper-2026-q3",
                "backtest_report_id": "rr_abcd",
            },
        )
        report = evaluate_promotion(entry)
        assert report.next_state == STATE_WALK_FORWARD
        assert set(report.required_evidence) == {"backtest_report_id"}
        assert report.missing_evidence == []

    def test_approved_state_without_approval_record_missing(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_CANDIDATE,
            evidence={
                "paper_trading_report_id": "rr_ppp",
                "disagreement_summary": "summary",
            },
        )
        report = evaluate_promotion(entry)
        assert report.next_state == STATE_APPROVED
        assert APPROVAL_EVIDENCE_KEY in report.required_evidence
        assert APPROVAL_EVIDENCE_KEY in report.missing_evidence

    def test_approved_state_without_approval_emits_warning(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_APPROVED,
        )
        report = evaluate_promotion(entry)
        assert any(
            "recorded without any ApprovalRecord" in w for w in report.warnings
        )

    def test_terminal_state_has_no_next(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_PRODUCTION,
            evidence={
                "monitoring_dashboard": "grafana",
                "rollback_owner": "owner",
            },
            approvals=[_sample_approval()],
        )
        report = evaluate_promotion(entry)
        assert report.next_state is None
        assert report.required_evidence == []
        assert report.missing_evidence == []

    def test_rollback_criteria_triggered_when_metrics_worse(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_PAPER_TRADING,
            evidence={
                "walk_forward_report_id": "rr_wf",
                "rollback_plan": "disable flag",
            },
        )
        metrics = {
            "challenger_expectancy_delta": -0.05,
            "challenger_max_drawdown_delta": 0.02,
            "challenger_profit_factor_delta": -0.10,
            "challenger_trade_count": 0.0,
            "challenger_top_symbol_share": 0.75,
            "data_quality_score": 0.5,
            "reproducibility_ok": 0.0,
        }
        report = evaluate_promotion(entry, metrics=metrics)
        triggered = triggered_alerts(report.rollback_alerts)
        triggered_names = {alert["criterion"] for alert in triggered}
        assert "expectancy_worse_than_champion" in triggered_names
        assert "drawdown_worse_than_limit" in triggered_names
        assert "profit_factor_worse_than_champion" in triggered_names
        assert "trade_frequency_below_minimum" in triggered_names
        assert "concentration_exceeds_limit" in triggered_names
        assert "data_quality_below_tolerance" in triggered_names
        assert "reproducibility_check_failed" in triggered_names

    def test_rollback_criteria_pass_when_metrics_safe(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_PAPER_TRADING,
        )
        metrics = {
            "challenger_expectancy_delta": 0.10,
            "challenger_max_drawdown_delta": -0.02,
            "challenger_profit_factor_delta": 0.15,
            "challenger_trade_count": 50.0,
            "challenger_top_symbol_share": 0.25,
            "data_quality_score": 0.99,
            "reproducibility_ok": 1.0,
        }
        report = evaluate_promotion(entry, metrics=metrics)
        assert triggered_alerts(report.rollback_alerts) == []

    def test_missing_metrics_produce_warnings(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_PAPER_TRADING,
        )
        report = evaluate_promotion(entry, metrics={})
        assert any(
            "rollback metric" in w and "missing" in w for w in report.warnings
        )
        assert triggered_alerts(report.rollback_alerts) == []

    def test_custom_rollback_criteria_override_defaults(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_PAPER_TRADING,
        )
        custom = [
            RollbackCriterion(
                name="drawdown_only",
                description="",
                metric_key="challenger_max_drawdown_delta",
                threshold=0.01,
                comparator="gt",
            )
        ]
        report = evaluate_promotion(
            entry,
            metrics={"challenger_max_drawdown_delta": 0.05},
            rollback_criteria=custom,
        )
        assert len(report.rollback_alerts) == 1
        assert report.rollback_alerts[0]["criterion"] == "drawdown_only"

    def test_report_id_is_deterministic(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
            evidence={"experiment_manifest": "abc", "dataset_id": "paper"},
        )
        first = evaluate_promotion(
            entry,
            metrics={"challenger_expectancy_delta": 0.0},
            generated_at="2026-07-02T12:00:00+00:00",
        )
        second = evaluate_promotion(
            entry,
            metrics={"challenger_expectancy_delta": 0.0},
            generated_at="2027-01-01T00:00:00+00:00",
        )
        assert first.report_id == second.report_id
        assert first.stable_hash() == second.stable_hash()

    def test_report_id_changes_with_evidence_set(self):
        base = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
        )
        with_evidence = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
            evidence={"experiment_manifest": "abc", "dataset_id": "paper"},
        )
        assert evaluate_promotion(base).report_id != evaluate_promotion(
            with_evidence
        ).report_id


# ---------------------------------------------------------------------------
# PromotionReport
# ---------------------------------------------------------------------------


class TestPromotionReport:
    def test_stable_hash_excludes_generated_at(self):
        entry = PromotionEntry(flag_name="enable_relative_strength")
        first = evaluate_promotion(
            entry, generated_at="2026-07-02T12:00:00+00:00"
        )
        second = evaluate_promotion(
            entry, generated_at="2027-01-01T00:00:00+00:00"
        )
        assert first.stable_hash() == second.stable_hash()

    def test_to_json_schema(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
        )
        report = evaluate_promotion(entry)
        payload = json.loads(report.to_json())
        for key in (
            "report_id",
            "flag_name",
            "current_state",
            "current_state_index",
            "next_state",
            "required_evidence",
            "missing_evidence",
            "approvals",
            "rollback_alerts",
            "warnings",
            "generated_at",
        ):
            assert key in payload

    def test_markdown_contains_key_sections(self):
        entry = PromotionEntry(flag_name="enable_relative_strength")
        report = evaluate_promotion(entry)
        md = report.to_markdown()
        assert "# Promotion Report:" in md
        assert "## Current State" in md
        assert "## Required Evidence for Next Transition" in md
        assert "## Approvals" in md
        assert "## Rollback Checks" in md
        assert "Observational research only" in md
        assert "enable_relative_strength" in md

    def test_markdown_marks_missing_evidence(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
        )
        report = evaluate_promotion(entry)
        md = report.to_markdown()
        assert "✗ `backtest_report_id`" in md

    def test_markdown_marks_satisfied_evidence(self):
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_BACKTEST,
            evidence={"backtest_report_id": "rr_abcd"},
        )
        report = evaluate_promotion(entry)
        md = report.to_markdown()
        assert "✓ `backtest_report_id`" in md


# ---------------------------------------------------------------------------
# Observational-only guarantees
# ---------------------------------------------------------------------------


class TestObservationalOnly:
    def test_module_has_no_order_path_references(self):
        import strategy.promotion_gates as module

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
                f"promotion_gates must not reference {token!r}"
            )

    def test_terminology_avoids_training(self):
        import strategy.promotion_gates as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 3 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped

    def test_module_never_touches_global_feature_flags(self):
        flags = reset_feature_flags()
        entry = PromotionEntry(
            flag_name="enable_relative_strength",
            current_state=STATE_APPROVED,
            approvals=[_sample_approval()],
        )
        evaluate_promotion(entry, metrics={"data_quality_score": 0.5})
        # No state mutated; flag still disabled.
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.promotion_gates", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.promotion_gates  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)

    def test_default_entry_reflects_disabled_flag(self):
        """The RS Challenger flag default is Disabled, and PromotionEntry
        must mirror that as its default current_state so the promotion
        machinery cannot silently claim a feature is further along than
        the code state.
        """
        entry = PromotionEntry(flag_name="enable_relative_strength")
        report = evaluate_promotion(entry)
        assert report.current_state == STATE_DISABLED
        assert report.current_state_index == 0
