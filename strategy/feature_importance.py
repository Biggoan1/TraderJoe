"""Feature Importance Analysis.

Read-only per-feature importance scoring over trade-outcome
observations.  Numeric features are correlated with a scalar outcome
(e.g., realized return) via Pearson's r; a Fisher z-transform gives a
confidence interval around each correlation.  Look-ahead observations
— features timestamped at or after their outcome — are rejected by
default.

Every score carries a ``hypothesis`` / ``validated`` label based on
sample size and a ``flagged`` boolean with reasons for downstream
review.  The module never labels a score as ``validated`` unless the
sample meets :data:`FI_SAMPLE_SIZE_FLOOR`; even ``validated`` scores
are not promotion signals — they surface into
:class:`~strategy.promotion_gates.PromotionEntry` evidence for human
review.

The module never enables feature flags, mutates production config,
places orders, imports order-path code, or wires the historical
validation paper account.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 4 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.backtest_lab import stable_hash, stable_json
from strategy.stats_engine import (
    LABEL_HYPOTHESIS,
    LABEL_VALIDATED,
    STATS_DEFAULT_CONFIDENCE_LEVEL,
    STATS_SAMPLE_SIZE_FLOOR,
    SUPPORTED_CONFIDENCE_LEVELS,
    _z_critical,  # deliberately reused
)


FI_SAMPLE_SIZE_FLOOR = STATS_SAMPLE_SIZE_FLOOR
METHOD_PEARSON = "pearson_r"

FLAG_LOW_SAMPLE = "low_sample"
FLAG_ZERO_VARIANCE = "zero_variance"
FLAG_CI_SPANS_ZERO = "ci_spans_zero"

# Correlation values are clipped just inside the open interval so
# ``atanh`` remains finite when the Fisher z-transform runs on
# degenerate perfect-correlation inputs.  1e-9 keeps the CI usable
# without inventing precision the sample cannot support.
_FISHER_R_CLIP = 1.0 - 1e-9


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureObservation:
    """One ``(features, outcome)`` observation for importance scoring.

    ``feature_timestamp`` records the moment the feature values were
    observed; ``outcome_timestamp`` records the moment the outcome was
    realized.  Correct research practice requires
    ``feature_timestamp < outcome_timestamp`` — the module refuses to
    aggregate observations that violate this by default.
    """

    trade_id: str
    feature_timestamp: str
    outcome_timestamp: str
    features: Mapping[str, float] = field(default_factory=dict)
    outcome: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", dict(self.features))
        self.validate()

    def validate(self) -> None:
        if not self.trade_id:
            raise ValueError("FeatureObservation.trade_id is required")
        if not self.feature_timestamp:
            raise ValueError("FeatureObservation.feature_timestamp is required")
        if not self.outcome_timestamp:
            raise ValueError("FeatureObservation.outcome_timestamp is required")

    def has_lookahead(self) -> bool:
        """True when ``feature_timestamp >= outcome_timestamp``.

        The strict inequality is intentional: features observed at the
        exact outcome timestamp still carry look-ahead risk because
        the outcome is realized only when the observation window
        closes.
        """
        return self.feature_timestamp >= self.outcome_timestamp

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "feature_timestamp": self.feature_timestamp,
            "outcome_timestamp": self.outcome_timestamp,
            "features": dict(self.features),
            "outcome": self.outcome,
        }


# ---------------------------------------------------------------------------
# Score record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureImportanceScore:
    """One deterministic per-feature importance record."""

    feature_name: str
    method: str
    score: float
    sample_size: int
    ci_low: float
    ci_high: float
    confidence_level: float
    label: str
    flagged: bool
    flag_reasons: Tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "flag_reasons", tuple(self.flag_reasons))
        self.validate()

    def validate(self) -> None:
        if not self.feature_name:
            raise ValueError("FeatureImportanceScore.feature_name is required")
        if not self.method:
            raise ValueError("FeatureImportanceScore.method is required")
        if self.label not in (LABEL_HYPOTHESIS, LABEL_VALIDATED):
            raise ValueError(
                f"unknown label: {self.label!r} "
                f"(expected {LABEL_HYPOTHESIS!r} or {LABEL_VALIDATED!r})"
            )
        if not (0.0 < self.confidence_level < 1.0):
            raise ValueError(
                f"confidence_level must be in (0, 1) (got {self.confidence_level!r})"
            )
        if self.sample_size < 0:
            raise ValueError("sample_size cannot be negative")
        if self.ci_low > self.ci_high:
            raise ValueError("ci_low > ci_high")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "method": self.method,
            "score": self.score,
            "sample_size": self.sample_size,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "confidence_level": self.confidence_level,
            "label": self.label,
            "flagged": self.flagged,
            "flag_reasons": list(self.flag_reasons),
            "detail": self.detail,
        }

    def is_significant(self) -> bool:
        return not (self.ci_low <= 0.0 <= self.ci_high)


def importance_stable_hash(
    scores: Iterable[FeatureImportanceScore],
) -> str:
    payload = [score.to_dict() for score in scores]
    payload.sort(key=lambda entry: (entry["feature_name"], entry["method"]))
    return stable_hash({"scores": payload})


# ---------------------------------------------------------------------------
# Look-ahead filtering
# ---------------------------------------------------------------------------


def filter_lookahead_observations(
    observations: Sequence[FeatureObservation],
) -> Tuple[List[FeatureObservation], List[FeatureObservation]]:
    """Split observations into ``(kept, dropped_due_to_lookahead)``."""
    kept: List[FeatureObservation] = []
    dropped: List[FeatureObservation] = []
    for observation in observations:
        if observation.has_lookahead():
            dropped.append(observation)
        else:
            kept.append(observation)
    return kept, dropped


# ---------------------------------------------------------------------------
# Pearson r + Fisher z helpers
# ---------------------------------------------------------------------------


def pearson_r(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y):
        raise ValueError("pearson_r requires equal-length sequences")
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    numerator = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    var_x = sum((xi - mx) ** 2 for xi in x)
    var_y = sum((yi - my) ** 2 for yi in y)
    denominator = math.sqrt(var_x * var_y)
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def fisher_z_ci(
    r: float,
    n: int,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> Tuple[float, float]:
    """Return ``(ci_low, ci_high)`` for a Pearson r via Fisher z-transform.

    Requires ``n >= 4``.  For smaller samples the interval collapses to
    ``(r, r)`` because the transform is not defined.
    """
    if n < 4:
        return r, r
    clipped = max(-_FISHER_R_CLIP, min(_FISHER_R_CLIP, r))
    z = math.atanh(clipped)
    se = 1.0 / math.sqrt(n - 3)
    z_crit = _z_critical(confidence_level)
    zl = z - z_crit * se
    zh = z + z_crit * se
    return math.tanh(zl), math.tanh(zh)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _label_for(sample_size: int, floor: int) -> str:
    return LABEL_VALIDATED if sample_size >= floor else LABEL_HYPOTHESIS


def _score_feature(
    feature_name: str,
    observations: Sequence[FeatureObservation],
    min_sample_size: int,
    confidence_level: float,
) -> FeatureImportanceScore:
    xs: List[float] = []
    ys: List[float] = []
    for observation in observations:
        if feature_name in observation.features:
            xs.append(float(observation.features[feature_name]))
            ys.append(float(observation.outcome))
    n = len(xs)
    flags: List[str] = []

    if n < min_sample_size:
        flags.append(FLAG_LOW_SAMPLE)

    r = pearson_r(xs, ys) if n >= 2 else 0.0
    ci_low, ci_high = fisher_z_ci(r, n, confidence_level)
    var_x = 0.0
    if n >= 2:
        mx = sum(xs) / n
        var_x = sum((xi - mx) ** 2 for xi in xs)
    if var_x == 0.0 and n >= 2:
        flags.append(FLAG_ZERO_VARIANCE)
    if n >= 2 and ci_low <= 0.0 <= ci_high:
        flags.append(FLAG_CI_SPANS_ZERO)

    return FeatureImportanceScore(
        feature_name=feature_name,
        method=METHOD_PEARSON,
        score=r,
        sample_size=n,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence_level=confidence_level,
        label=_label_for(n, min_sample_size),
        flagged=bool(flags),
        flag_reasons=tuple(flags),
        detail=(
            f"pearson_r={r} with n={n} observations"
            if n
            else f"no observations carrying feature {feature_name!r}"
        ),
    )


def analyze_feature_importance(
    observations: Sequence[FeatureObservation],
    feature_names: Sequence[str],
    min_sample_size: int = FI_SAMPLE_SIZE_FLOOR,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
    reject_lookahead: bool = True,
) -> List[FeatureImportanceScore]:
    """Return per-feature Pearson-r importance scores.

    Scores are sorted by ``|score|`` descending, then by
    ``feature_name`` ascending for deterministic tie-breaking.

    When ``reject_lookahead`` is ``True`` (the default), the presence
    of any observation with ``feature_timestamp >= outcome_timestamp``
    raises :class:`ValueError` — callers can pre-clean their input via
    :func:`filter_lookahead_observations` when they want to keep the
    remaining data.
    """
    if min_sample_size < 1:
        raise ValueError("min_sample_size must be >= 1")
    if confidence_level not in SUPPORTED_CONFIDENCE_LEVELS:
        raise ValueError(
            f"unsupported confidence_level {confidence_level!r}; "
            f"supported: {SUPPORTED_CONFIDENCE_LEVELS}"
        )

    if reject_lookahead:
        _, dropped = filter_lookahead_observations(observations)
        if dropped:
            trade_ids = [obs.trade_id for obs in dropped[:5]]
            raise ValueError(
                "look-ahead detected: "
                f"{len(dropped)} observation(s) have "
                f"feature_timestamp >= outcome_timestamp; "
                f"first few trade_ids: {trade_ids}"
            )

    unique_names = sorted(set(feature_names))
    scores: List[FeatureImportanceScore] = [
        _score_feature(name, observations, min_sample_size, confidence_level)
        for name in unique_names
    ]
    scores.sort(key=lambda s: (-abs(s.score), s.feature_name))
    return scores
