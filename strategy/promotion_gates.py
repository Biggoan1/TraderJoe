"""Feature Promotion Gates.

Read-only encoding of the Phase 3 promotion process from the ROADMAP:

    Disabled → Backtest → Walk Forward → Paper Trading → Candidate
             → Approved → Production

This module never enables a feature flag, never places orders, never
calls broker APIs, and never touches the historical validation paper
account credentials.  It only:

* declares the ordered set of promotion states and the required
  evidence for each transition,
* models :class:`ApprovalRecord`, :class:`RollbackCriterion`, and
  :class:`RollbackAlert` as immutable data,
* evaluates a :class:`PromotionEntry` against the gate definitions and
  a caller-supplied metrics dict, producing a :class:`PromotionReport`
  with deterministic serialization.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 3 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from strategy.backtest_lab import stable_hash, stable_json


# ---------------------------------------------------------------------------
# States and gate definitions
# ---------------------------------------------------------------------------


STATE_DISABLED = "disabled"
STATE_BACKTEST = "backtest"
STATE_WALK_FORWARD = "walk_forward"
STATE_PAPER_TRADING = "paper_trading"
STATE_CANDIDATE = "candidate"
STATE_APPROVED = "approved"
STATE_PRODUCTION = "production"

PROMOTION_STATES: Tuple[str, ...] = (
    STATE_DISABLED,
    STATE_BACKTEST,
    STATE_WALK_FORWARD,
    STATE_PAPER_TRADING,
    STATE_CANDIDATE,
    STATE_APPROVED,
    STATE_PRODUCTION,
)

# Required evidence keys for entering each state, per ROADMAP "Feature
# Flag Promotion Process".  ``approval_record`` is a virtual key — it is
# satisfied by the presence of one or more :class:`ApprovalRecord`
# entries on the :class:`PromotionEntry`.
REQUIRED_EVIDENCE_PER_STATE: Dict[str, Tuple[str, ...]] = {
    STATE_DISABLED: (),
    STATE_BACKTEST: ("experiment_manifest", "dataset_id"),
    STATE_WALK_FORWARD: ("backtest_report_id",),
    STATE_PAPER_TRADING: ("walk_forward_report_id", "rollback_plan"),
    STATE_CANDIDATE: ("paper_trading_report_id", "disagreement_summary"),
    STATE_APPROVED: ("approval_record",),
    STATE_PRODUCTION: (
        "monitoring_dashboard",
        "rollback_owner",
        "approval_record",
    ),
}

APPROVAL_EVIDENCE_KEY = "approval_record"


def is_terminal_state(state: str) -> bool:
    return state == STATE_PRODUCTION


def next_state(state: str) -> Optional[str]:
    """Return the immediately-following promotion state, or ``None`` if terminal."""
    if state not in PROMOTION_STATES:
        raise ValueError(
            f"unknown promotion state: {state!r} (expected one of {PROMOTION_STATES})"
        )
    if is_terminal_state(state):
        return None
    return PROMOTION_STATES[PROMOTION_STATES.index(state) + 1]


def state_index(state: str) -> int:
    if state not in PROMOTION_STATES:
        raise ValueError(
            f"unknown promotion state: {state!r} (expected one of {PROMOTION_STATES})"
        )
    return PROMOTION_STATES.index(state)


# ---------------------------------------------------------------------------
# Approval records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRecord:
    """One recorded human approval for a promotion transition.

    Approvals are data artifacts.  This module never constructs them
    autonomously — callers build them from repository documentation or
    operations records and pass them into :class:`PromotionEntry`.
    """

    approver: str
    approved_at: str
    flag_name: str
    scope: str
    monitoring: str
    rollback_plan: str
    commit: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.approver:
            raise ValueError("ApprovalRecord.approver is required")
        if not self.approved_at:
            raise ValueError("ApprovalRecord.approved_at is required")
        if not self.flag_name:
            raise ValueError("ApprovalRecord.flag_name is required")
        if not self.scope:
            raise ValueError("ApprovalRecord.scope is required")
        if not self.monitoring:
            raise ValueError("ApprovalRecord.monitoring is required")
        if not self.rollback_plan:
            raise ValueError("ApprovalRecord.rollback_plan is required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approver": self.approver,
            "approved_at": self.approved_at,
            "flag_name": self.flag_name,
            "scope": self.scope,
            "monitoring": self.monitoring,
            "rollback_plan": self.rollback_plan,
            "commit": self.commit,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Rollback criteria
# ---------------------------------------------------------------------------


KNOWN_COMPARATORS: Tuple[str, ...] = ("lt", "le", "gt", "ge", "eq")


def _compare(actual: float, threshold: float, comparator: str) -> bool:
    if comparator == "lt":
        return actual < threshold
    if comparator == "le":
        return actual <= threshold
    if comparator == "gt":
        return actual > threshold
    if comparator == "ge":
        return actual >= threshold
    if comparator == "eq":
        return actual == threshold
    raise ValueError(
        f"unknown comparator: {comparator!r} (expected one of {KNOWN_COMPARATORS})"
    )


@dataclass(frozen=True)
class RollbackCriterion:
    """One objective rollback trigger.

    Callers supply a metrics dict at evaluation time.  Criteria whose
    ``metric_key`` is missing are reported as ``triggered=False`` with a
    warning; missing metrics are surfaced but never treated as passing.
    """

    name: str
    description: str
    metric_key: str
    threshold: float
    comparator: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("RollbackCriterion.name is required")
        if not self.metric_key:
            raise ValueError("RollbackCriterion.metric_key is required")
        if self.comparator not in KNOWN_COMPARATORS:
            raise ValueError(
                f"unknown comparator: {self.comparator!r} "
                f"(expected one of {KNOWN_COMPARATORS})"
            )

    def check(self, metrics: Dict[str, float]) -> "RollbackAlert":
        if self.metric_key not in metrics:
            return RollbackAlert(
                criterion=self.name,
                metric_key=self.metric_key,
                actual_value=None,
                threshold=self.threshold,
                comparator=self.comparator,
                triggered=False,
                data_available=False,
                detail=f"metric {self.metric_key!r} not present in metrics dict",
            )
        actual = float(metrics[self.metric_key])
        triggered = _compare(actual, self.threshold, self.comparator)
        return RollbackAlert(
            criterion=self.name,
            metric_key=self.metric_key,
            actual_value=actual,
            threshold=self.threshold,
            comparator=self.comparator,
            triggered=triggered,
            data_available=True,
            detail=(
                f"{self.metric_key}={actual} {self.comparator} {self.threshold}"
                if triggered
                else f"{self.metric_key}={actual} within tolerance "
                f"(comparator={self.comparator}, threshold={self.threshold})"
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "metric_key": self.metric_key,
            "threshold": self.threshold,
            "comparator": self.comparator,
        }


@dataclass(frozen=True)
class RollbackAlert:
    """Outcome of evaluating one :class:`RollbackCriterion`."""

    criterion: str
    metric_key: str
    actual_value: Optional[float]
    threshold: float
    comparator: str
    triggered: bool
    data_available: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion": self.criterion,
            "metric_key": self.metric_key,
            "actual_value": self.actual_value,
            "threshold": self.threshold,
            "comparator": self.comparator,
            "triggered": self.triggered,
            "data_available": self.data_available,
            "detail": self.detail,
        }


# Standard set of rollback criteria mirroring the ROADMAP "Failure /
# Rollback Criteria" section.  Callers can extend or override with
# their own thresholds via ``evaluate_promotion(rollback_criteria=...)``.
STANDARD_ROLLBACK_CRITERIA: Tuple[RollbackCriterion, ...] = (
    RollbackCriterion(
        name="expectancy_worse_than_champion",
        description="Challenger expectancy is worse than Champion for the window.",
        metric_key="challenger_expectancy_delta",
        threshold=0.0,
        comparator="lt",
    ),
    RollbackCriterion(
        name="drawdown_worse_than_limit",
        description="Challenger max drawdown exceeds the approved delta limit.",
        metric_key="challenger_max_drawdown_delta",
        threshold=0.0,
        comparator="gt",
    ),
    RollbackCriterion(
        name="profit_factor_worse_than_champion",
        description="Challenger profit factor is lower than Champion beyond tolerance.",
        metric_key="challenger_profit_factor_delta",
        threshold=0.0,
        comparator="lt",
    ),
    RollbackCriterion(
        name="trade_frequency_below_minimum",
        description="Challenger trade frequency falls below the minimum sample requirement.",
        metric_key="challenger_trade_count",
        threshold=1.0,
        comparator="lt",
    ),
    RollbackCriterion(
        name="concentration_exceeds_limit",
        description="Challenger concentration exceeds approved symbol / sector / regime limits.",
        metric_key="challenger_top_symbol_share",
        threshold=0.5,
        comparator="gt",
    ),
    RollbackCriterion(
        name="data_quality_below_tolerance",
        description="Data-quality score falls below the approved missing-data tolerance.",
        metric_key="data_quality_score",
        threshold=0.9,
        comparator="lt",
    ),
    RollbackCriterion(
        name="reproducibility_check_failed",
        description="Same run id / config / dataset failed to regenerate the same outputs.",
        metric_key="reproducibility_ok",
        threshold=1.0,
        comparator="lt",
    ),
)


# ---------------------------------------------------------------------------
# Promotion entries
# ---------------------------------------------------------------------------


@dataclass
class PromotionEntry:
    """Current promotion state and recorded evidence for one feature flag."""

    flag_name: str
    current_state: str = STATE_DISABLED
    evidence: Dict[str, str] = field(default_factory=dict)
    approvals: List[ApprovalRecord] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.flag_name:
            raise ValueError("PromotionEntry.flag_name is required")
        if self.current_state not in PROMOTION_STATES:
            raise ValueError(
                f"unknown promotion state: {self.current_state!r} "
                f"(expected one of {PROMOTION_STATES})"
            )
        for record in self.approvals:
            if record.flag_name and record.flag_name != self.flag_name:
                raise ValueError(
                    "ApprovalRecord.flag_name must match the entry flag_name "
                    f"({record.flag_name!r} vs {self.flag_name!r})"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag_name": self.flag_name,
            "current_state": self.current_state,
            "evidence": dict(self.evidence),
            "approvals": [approval.to_dict() for approval in self.approvals],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


PROMOTION_REPORT_ID_PREFIX = "pg"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _missing_evidence(
    entry: PromotionEntry, target_state: Optional[str]
) -> List[str]:
    if target_state is None:
        return []
    required = REQUIRED_EVIDENCE_PER_STATE.get(target_state, ())
    missing: List[str] = []
    for key in required:
        if key == APPROVAL_EVIDENCE_KEY:
            if not entry.approvals:
                missing.append(key)
            continue
        if not entry.evidence.get(key):
            missing.append(key)
    return missing


def _rollback_alerts(
    metrics: Dict[str, float],
    criteria: Iterable[RollbackCriterion],
) -> List[RollbackAlert]:
    return [criterion.check(metrics) for criterion in criteria]


@dataclass
class PromotionReport:
    """Promotion-state report for one feature flag."""

    report_id: str
    flag_name: str
    current_state: str
    current_state_index: int
    next_state: Optional[str]
    required_evidence: List[str]
    missing_evidence: List[str]
    approvals: List[Dict[str, Any]]
    rollback_alerts: List[Dict[str, Any]]
    warnings: List[str]
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "flag_name": self.flag_name,
            "current_state": self.current_state,
            "current_state_index": self.current_state_index,
            "next_state": self.next_state,
            "required_evidence": list(self.required_evidence),
            "missing_evidence": list(self.missing_evidence),
            "approvals": list(self.approvals),
            "rollback_alerts": list(self.rollback_alerts),
            "warnings": list(self.warnings),
            "generated_at": self.generated_at,
        }

    def to_json(self) -> str:
        return stable_json(self.to_dict())

    def to_markdown(self) -> str:
        lines: List[str] = [
            f"# Promotion Report: `{self.flag_name}`",
            "",
            f"_Report ID: `{self.report_id}`_",
            f"_Generated: {self.generated_at}_",
            "",
            "Observational research only. Does not enable feature flags or alter strategy behavior.",
            "",
            "## Current State",
            f"- Flag: `{self.flag_name}`",
            f"- State: `{self.current_state}` (index {self.current_state_index})",
        ]
        if self.next_state is None:
            lines.append("- Next state: _terminal (Production)_")
        else:
            lines.append(f"- Next state: `{self.next_state}`")

        lines.extend(["", "## Required Evidence for Next Transition"])
        if not self.required_evidence:
            lines.append("_No transition required._")
        else:
            for key in self.required_evidence:
                marker = "✓" if key not in self.missing_evidence else "✗"
                lines.append(f"- {marker} `{key}`")

        lines.extend(["", "## Approvals"])
        if not self.approvals:
            lines.append("_No approvals recorded._")
        else:
            for approval in self.approvals:
                lines.append(
                    f"- `{approval['approver']}` on "
                    f"`{approval['approved_at']}` — {approval['scope']}"
                )

        lines.extend(["", "## Rollback Checks"])
        if not self.rollback_alerts:
            lines.append("_No rollback criteria configured._")
        else:
            for alert in self.rollback_alerts:
                marker = "!" if alert["triggered"] else "·"
                data_note = (
                    "" if alert["data_available"] else " _(metric missing)_"
                )
                lines.append(
                    f"- {marker} `{alert['criterion']}`{data_note} — "
                    f"{alert['detail']}"
                )

        if self.warnings:
            lines.extend(["", "## Warnings"])
            for warning in self.warnings:
                lines.append(f"- {warning}")

        return "\n".join(lines)

    def stable_hash(self) -> str:
        data = self.to_dict()
        data.pop("generated_at", None)
        return stable_hash(data)


def _build_report_id(
    entry: PromotionEntry,
    target_state: Optional[str],
    metrics: Dict[str, float],
    criteria: Iterable[RollbackCriterion],
) -> str:
    identity = {
        "flag_name": entry.flag_name,
        "current_state": entry.current_state,
        "target_state": target_state,
        "evidence_keys": sorted(entry.evidence),
        "approval_count": len(entry.approvals),
        "metric_keys": sorted(metrics),
        "criteria": [criterion.name for criterion in criteria],
    }
    digest = hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()
    return f"{PROMOTION_REPORT_ID_PREFIX}_{digest[:12]}"


def evaluate_promotion(
    entry: PromotionEntry,
    metrics: Optional[Dict[str, float]] = None,
    rollback_criteria: Optional[Iterable[RollbackCriterion]] = None,
    generated_at: Optional[str] = None,
) -> PromotionReport:
    """Evaluate a :class:`PromotionEntry` against gate + rollback rules."""
    metrics = dict(metrics or {})
    criteria_tuple: Tuple[RollbackCriterion, ...] = tuple(
        rollback_criteria
        if rollback_criteria is not None
        else STANDARD_ROLLBACK_CRITERIA
    )
    target = next_state(entry.current_state)
    required = list(REQUIRED_EVIDENCE_PER_STATE.get(target or "", ()))
    missing = _missing_evidence(entry, target)
    alerts = _rollback_alerts(metrics, criteria_tuple)
    warnings: List[str] = []

    # Approvals required for Approved and Production states — flag it explicitly.
    if entry.current_state in {STATE_APPROVED, STATE_PRODUCTION}:
        if not entry.approvals:
            warnings.append(
                f"state {entry.current_state!r} recorded without any ApprovalRecord"
            )

    # Alerts with data_available=False should surface as warnings so
    # missing metrics do not silently look like a pass.
    for alert in alerts:
        if not alert.data_available:
            warnings.append(
                f"rollback metric {alert.metric_key!r} missing for criterion "
                f"{alert.criterion!r}"
            )

    if entry.current_state != STATE_DISABLED and missing:
        warnings.append(
            f"state {entry.current_state!r} is missing required evidence "
            f"for next transition to {target!r}: {missing}"
        )

    generated = generated_at or _utc_now_iso()
    report_id = _build_report_id(entry, target, metrics, criteria_tuple)
    return PromotionReport(
        report_id=report_id,
        flag_name=entry.flag_name,
        current_state=entry.current_state,
        current_state_index=state_index(entry.current_state),
        next_state=target,
        required_evidence=required,
        missing_evidence=missing,
        approvals=[approval.to_dict() for approval in entry.approvals],
        rollback_alerts=[alert.to_dict() for alert in alerts],
        warnings=warnings,
        generated_at=generated,
    )


def triggered_alerts(
    alerts: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Return only the alerts whose ``triggered`` flag is ``True``."""
    return [alert for alert in alerts if alert.get("triggered")]
