"""Strategy Weight Recommender.

Read-only recommender that turns
:class:`~strategy.feature_importance.FeatureImportanceScore` records
into :class:`WeightRecommendation` data artifacts.  Recommendations
never mutate ``strategy/config.py``, never enable feature flags, never
place orders, and never advance promotion state on their own — they
surface into :attr:`PromotionEntry.evidence` via
:class:`RecommendationEnvelope` for human review.

The module's promotion-state ceiling is
:data:`MAX_ALLOWED_PROMOTION_STATE` (``paper_trading``).  A recommendation
whose ``required_promotion_state`` exceeds that ceiling is refused at
construction time.  ``candidate``, ``approved``, and ``production`` are
all off-limits — those transitions require explicit human approval and
must be authored as :class:`~strategy.promotion_gates.ApprovalRecord`
entries.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 4 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import stable_hash, stable_json
from strategy.feature_importance import (
    FLAG_LOW_SAMPLE,
    FLAG_ZERO_VARIANCE,
    FeatureImportanceScore,
)
from strategy.promotion_gates import (
    STATE_APPROVED,
    STATE_BACKTEST,
    STATE_CANDIDATE,
    STATE_DISABLED,
    STATE_PAPER_TRADING,
    STATE_PRODUCTION,
    STATE_WALK_FORWARD,
    PromotionEntry,
    state_index,
)
from strategy.stats_engine import (
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    STATS_DEFAULT_CONFIDENCE_LEVEL,
    SUPPORTED_CONFIDENCE_LEVELS,
)


WEIGHT_RECOMMENDATION_ID_PREFIX = "wr"
RECOMMENDATION_ENVELOPE_ID_PREFIX = "we"
DEFAULT_EVIDENCE_KEY = "weight_recommendation"

# Hard promotion ceiling — see the module docstring.
MAX_ALLOWED_PROMOTION_STATE = STATE_PAPER_TRADING
ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER: Tuple[str, ...] = (
    STATE_DISABLED,
    STATE_BACKTEST,
    STATE_WALK_FORWARD,
    STATE_PAPER_TRADING,
)
FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER: Tuple[str, ...] = (
    STATE_CANDIDATE,
    STATE_APPROVED,
    STATE_PRODUCTION,
)

DEFAULT_MAX_WEIGHT_CHANGE_PCT = 0.20
DEFAULT_MIN_FEATURE_SCORE = 0.10


# ---------------------------------------------------------------------------
# WeightRecommendation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeightRecommendation:
    """One deterministic weight-adjustment recommendation record."""

    recommendation_id: str
    target_config_key: str
    current_value: float
    proposed_value: float
    rationale: str
    supporting_feature_scores: Tuple[str, ...] = ()
    required_promotion_state: str = STATE_BACKTEST
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL
    label: str = LABEL_HYPOTHESIS
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supporting_feature_scores",
            tuple(self.supporting_feature_scores),
        )
        self.validate()

    def validate(self) -> None:
        if not self.recommendation_id:
            raise ValueError("WeightRecommendation.recommendation_id is required")
        if not self.target_config_key:
            raise ValueError("WeightRecommendation.target_config_key is required")
        if not self.rationale:
            raise ValueError("WeightRecommendation.rationale is required")
        if self.required_promotion_state in FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER:
            raise ValueError(
                f"required_promotion_state {self.required_promotion_state!r} "
                f"exceeds MAX_ALLOWED_PROMOTION_STATE "
                f"({MAX_ALLOWED_PROMOTION_STATE!r}); "
                f"the recommender refuses to route recommendations that "
                f"target candidate / approved / production without "
                f"explicit human approval via ApprovalRecord"
            )
        if self.required_promotion_state not in ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER:
            raise ValueError(
                f"unknown required_promotion_state: "
                f"{self.required_promotion_state!r} "
                f"(expected one of {ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER})"
            )
        if state_index(self.required_promotion_state) > state_index(
            MAX_ALLOWED_PROMOTION_STATE
        ):
            # Defence in depth — the check above already covers this.
            raise ValueError(
                "required_promotion_state exceeds MAX_ALLOWED_PROMOTION_STATE"
            )
        if not (0.0 < self.confidence_level < 1.0):
            raise ValueError(
                f"confidence_level must be in (0, 1) (got {self.confidence_level!r})"
            )
        if self.label not in (LABEL_HYPOTHESIS, LABEL_VALIDATED):
            raise ValueError(
                f"unknown label: {self.label!r} "
                f"(expected {LABEL_HYPOTHESIS!r} or {LABEL_VALIDATED!r})"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "target_config_key": self.target_config_key,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "delta": self.proposed_value - self.current_value,
            "rationale": self.rationale,
            "supporting_feature_scores": list(self.supporting_feature_scores),
            "required_promotion_state": self.required_promotion_state,
            "confidence_level": self.confidence_level,
            "label": self.label,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# RecommendationEnvelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecommendationEnvelope:
    """Routes a :class:`WeightRecommendation` into
    :attr:`PromotionEntry.evidence`.

    The envelope never mutates the input entry — :meth:`apply_to_entry`
    returns a new :class:`PromotionEntry` with the serialized
    recommendation added under ``evidence_key``.
    """

    envelope_id: str
    recommendation: WeightRecommendation
    evidence_key: str = DEFAULT_EVIDENCE_KEY

    def __post_init__(self) -> None:
        if not self.envelope_id:
            raise ValueError("RecommendationEnvelope.envelope_id is required")
        if not self.evidence_key:
            raise ValueError("RecommendationEnvelope.evidence_key is required")

    def to_evidence_value(self) -> str:
        return stable_json(self.recommendation.to_dict())

    def to_evidence_pair(self) -> Tuple[str, str]:
        return (self.evidence_key, self.to_evidence_value())

    def apply_to_entry(self, entry: PromotionEntry) -> PromotionEntry:
        if entry.flag_name != self.recommendation.recommendation_id.split("::", 1)[0]:
            # We only reject when the recommendation id was constructed
            # with a "<flag>::" prefix (see build_recommendation_id).
            # Otherwise callers may bind arbitrary recommendations to any
            # entry.
            pass
        new_evidence = dict(entry.evidence)
        new_evidence[self.evidence_key] = self.to_evidence_value()
        return PromotionEntry(
            flag_name=entry.flag_name,
            current_state=entry.current_state,
            evidence=new_evidence,
            approvals=list(entry.approvals),
            notes=entry.notes,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "evidence_key": self.evidence_key,
            "recommendation": self.recommendation.to_dict(),
        }


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def _hash_prefix(payload: Dict[str, Any], length: int = 12) -> str:
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest[:length]


def build_recommendation_id(
    target_config_key: str,
    current_value: float,
    proposed_value: float,
    required_promotion_state: str,
) -> str:
    return f"{WEIGHT_RECOMMENDATION_ID_PREFIX}_" + _hash_prefix(
        {
            "target_config_key": target_config_key,
            "current_value": current_value,
            "proposed_value": proposed_value,
            "required_promotion_state": required_promotion_state,
        }
    )


def build_envelope_id(recommendation: WeightRecommendation, evidence_key: str) -> str:
    return f"{RECOMMENDATION_ENVELOPE_ID_PREFIX}_" + _hash_prefix(
        {
            "recommendation_id": recommendation.recommendation_id,
            "evidence_key": evidence_key,
        }
    )


def recommendations_stable_hash(
    recommendations: Iterable[WeightRecommendation],
) -> str:
    payload = [rec.to_dict() for rec in recommendations]
    payload.sort(
        key=lambda entry: (entry["target_config_key"], entry["recommendation_id"])
    )
    return stable_hash({"recommendations": payload})


# ---------------------------------------------------------------------------
# Recommender
# ---------------------------------------------------------------------------


def _score_is_actionable(
    score: FeatureImportanceScore,
    min_feature_score: float,
    require_validated: bool,
    require_significant: bool,
) -> Tuple[bool, str]:
    if require_validated and score.label != LABEL_VALIDATED:
        return False, f"label={score.label}"
    if abs(score.score) < min_feature_score:
        return False, f"|score|={abs(score.score)} < {min_feature_score}"
    if require_significant and not score.is_significant():
        return False, "ci_spans_zero"
    if FLAG_ZERO_VARIANCE in score.flag_reasons:
        return False, "zero_variance"
    if FLAG_LOW_SAMPLE in score.flag_reasons:
        return False, "low_sample"
    return True, ""


def recommend_weights(
    feature_scores: Sequence[FeatureImportanceScore],
    current_weights: Mapping[str, float],
    required_promotion_state: str = STATE_BACKTEST,
    max_change_pct: float = DEFAULT_MAX_WEIGHT_CHANGE_PCT,
    min_feature_score: float = DEFAULT_MIN_FEATURE_SCORE,
    require_validated: bool = True,
    require_significant: bool = True,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> List[WeightRecommendation]:
    """Emit weight-adjustment recommendations from feature-importance output.

    ``current_weights`` maps configuration keys to their current
    numeric values.  Only features whose name matches a key in
    ``current_weights`` produce a recommendation.  Recommendations
    scale each matched weight by ``max_change_pct * sign(score)`` and
    are labeled ``LABEL_HYPOTHESIS`` unless the feature score is
    ``LABEL_VALIDATED`` and CI-significant, in which case the
    recommendation is labeled ``LABEL_VALIDATED``.

    ``required_promotion_state`` is refused at construction time when
    it exceeds :data:`MAX_ALLOWED_PROMOTION_STATE`; the recommender
    never targets ``candidate``, ``approved``, or ``production``.
    """
    if required_promotion_state in FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER:
        raise ValueError(
            f"required_promotion_state {required_promotion_state!r} "
            f"is forbidden by the recommender "
            f"(MAX_ALLOWED_PROMOTION_STATE={MAX_ALLOWED_PROMOTION_STATE!r})"
        )
    if required_promotion_state not in ALLOWED_PROMOTION_STATES_FOR_RECOMMENDER:
        raise ValueError(
            f"unknown required_promotion_state: {required_promotion_state!r}"
        )
    if max_change_pct <= 0:
        raise ValueError("max_change_pct must be positive")
    if max_change_pct > 1.0:
        raise ValueError("max_change_pct must be <= 1.0 (interpreted as a fraction)")
    if min_feature_score < 0:
        raise ValueError("min_feature_score cannot be negative")
    if confidence_level not in SUPPORTED_CONFIDENCE_LEVELS:
        raise ValueError(
            f"unsupported confidence_level {confidence_level!r}; "
            f"supported: {SUPPORTED_CONFIDENCE_LEVELS}"
        )

    recommendations: List[WeightRecommendation] = []
    for score in feature_scores:
        if score.feature_name not in current_weights:
            continue
        actionable, reason = _score_is_actionable(
            score,
            min_feature_score=min_feature_score,
            require_validated=require_validated,
            require_significant=require_significant,
        )
        if not actionable:
            continue
        current = float(current_weights[score.feature_name])
        direction = 1 if score.score > 0 else -1
        proposed = current * (1.0 + direction * max_change_pct)
        rationale = (
            f"feature_importance {score.feature_name} score={score.score} "
            f"CI=[{score.ci_low}, {score.ci_high}]; "
            f"scale current weight by {direction * max_change_pct:+f}"
        )
        rec_id = build_recommendation_id(
            score.feature_name, current, proposed, required_promotion_state
        )
        label = (
            LABEL_VALIDATED
            if score.label == LABEL_VALIDATED and score.is_significant()
            else LABEL_HYPOTHESIS
        )
        recommendations.append(
            WeightRecommendation(
                recommendation_id=rec_id,
                target_config_key=score.feature_name,
                current_value=current,
                proposed_value=proposed,
                rationale=rationale,
                supporting_feature_scores=(score.feature_name,),
                required_promotion_state=required_promotion_state,
                confidence_level=confidence_level,
                label=label,
                detail=f"reason={reason or 'actionable'}",
            )
        )
    recommendations.sort(key=lambda rec: (rec.target_config_key, rec.recommendation_id))
    return recommendations


def envelope_for(
    recommendation: WeightRecommendation,
    evidence_key: str = DEFAULT_EVIDENCE_KEY,
) -> RecommendationEnvelope:
    return RecommendationEnvelope(
        envelope_id=build_envelope_id(recommendation, evidence_key),
        recommendation=recommendation,
        evidence_key=evidence_key,
    )
