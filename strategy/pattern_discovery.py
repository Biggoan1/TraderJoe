"""Historical Pattern Discovery.

Read-only pattern discovery over per-trade context/outcome
observations.  Consumes bucketed market-intelligence context
(regime, sector leadership, market breadth, relative strength) and
realized trade outcomes, then emits :class:`PatternHypothesis`
records grouped by feature-key combinations.

Every discovered pattern defaults to ``LABEL_HYPOTHESIS`` — the module
never labels a pattern as ``validated`` without an explicit
out-of-sample check via
:func:`validate_patterns_out_of_sample`.  A pattern that becomes
``validated`` still does not constitute a promotion signal; it means
the same directional effect held on a disjoint observation window.

The module never enables feature flags, mutates production config,
places orders, imports order-path code, or wires the historical
validation paper account.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 4 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
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
    SUPPORTED_CONFIDENCE_LEVELS,
    cohens_d_one_sample,
    normal_mean_ci,
    wilson_proportion_ci,
)


PATTERN_MIN_SAMPLE_SIZE = 10
PATTERN_ID_PREFIX = "pat"

OUTCOME_KEY_WIN = "win"
OUTCOME_KEY_RETURN_PCT = "return_pct"


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternObservation:
    """One ``(context, outcome)`` pair for pattern discovery."""

    trade_id: str
    entry_timestamp: str
    exit_timestamp: str
    context: Mapping[str, str] = field(default_factory=dict)
    outcome: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", dict(self.context))
        object.__setattr__(self, "outcome", dict(self.outcome))
        object.__setattr__(self, "metadata", dict(self.metadata))
        self.validate()

    def validate(self) -> None:
        if not self.trade_id:
            raise ValueError("PatternObservation.trade_id is required")
        if not self.entry_timestamp:
            raise ValueError("PatternObservation.entry_timestamp is required")
        # exit_timestamp can be empty for still-open trades — outcome would
        # then be empty and the observation would fail the outcome key
        # guard downstream. We don't reject it here to let callers filter.
        if self.exit_timestamp and self.exit_timestamp < self.entry_timestamp:
            raise ValueError(
                f"exit_timestamp {self.exit_timestamp!r} precedes entry "
                f"{self.entry_timestamp!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "entry_timestamp": self.entry_timestamp,
            "exit_timestamp": self.exit_timestamp,
            "context": dict(self.context),
            "outcome": dict(self.outcome),
            "metadata": dict(self.metadata),
        }

    def has_context_keys(self, keys: Sequence[str]) -> bool:
        return all(key in self.context for key in keys)

    def has_outcomes(self) -> bool:
        return (
            OUTCOME_KEY_WIN in self.outcome
            and OUTCOME_KEY_RETURN_PCT in self.outcome
        )


# ---------------------------------------------------------------------------
# Pattern hypothesis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternHypothesis:
    """One deterministic pattern-discovery record."""

    pattern_id: str
    feature_keys: Tuple[str, ...]
    feature_values: Tuple[str, ...]
    description: str
    sample_size: int
    win_rate: float
    win_rate_ci_low: float
    win_rate_ci_high: float
    mean_return: float
    return_ci_low: float
    return_ci_high: float
    effect_size: float
    label: str
    confidence_level: float
    supporting_evidence_ids: Tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_keys", tuple(self.feature_keys))
        object.__setattr__(self, "feature_values", tuple(self.feature_values))
        object.__setattr__(
            self,
            "supporting_evidence_ids",
            tuple(self.supporting_evidence_ids),
        )
        self.validate()

    def validate(self) -> None:
        if not self.pattern_id:
            raise ValueError("PatternHypothesis.pattern_id is required")
        if len(self.feature_keys) != len(self.feature_values):
            raise ValueError(
                "feature_keys and feature_values must have the same length"
            )
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
        if self.win_rate_ci_low > self.win_rate_ci_high:
            raise ValueError("win_rate_ci_low > win_rate_ci_high")
        if self.return_ci_low > self.return_ci_high:
            raise ValueError("return_ci_low > return_ci_high")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "feature_keys": list(self.feature_keys),
            "feature_values": list(self.feature_values),
            "description": self.description,
            "sample_size": self.sample_size,
            "win_rate": self.win_rate,
            "win_rate_ci_low": self.win_rate_ci_low,
            "win_rate_ci_high": self.win_rate_ci_high,
            "mean_return": self.mean_return,
            "return_ci_low": self.return_ci_low,
            "return_ci_high": self.return_ci_high,
            "effect_size": self.effect_size,
            "label": self.label,
            "confidence_level": self.confidence_level,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "detail": self.detail,
        }

    def features(self) -> Dict[str, str]:
        return dict(zip(self.feature_keys, self.feature_values))

    def is_significant_return(self) -> bool:
        """True when the mean-return CI excludes zero."""
        return not (self.return_ci_low <= 0.0 <= self.return_ci_high)


def hypotheses_stable_hash(
    hypotheses: Iterable[PatternHypothesis],
) -> str:
    payload = [hypothesis.to_dict() for hypothesis in hypotheses]
    payload.sort(
        key=lambda entry: (
            entry["feature_keys"],
            entry["feature_values"],
            entry["pattern_id"],
        )
    )
    return stable_hash({"hypotheses": payload})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _feature_key_tuple_sorted(keys: Sequence[str]) -> Tuple[str, ...]:
    return tuple(sorted(keys))


def _group_key_for(observation: PatternObservation, keys: Sequence[str]) -> Tuple[str, ...]:
    return tuple(str(observation.context[key]) for key in keys)


def _pattern_id(feature_keys: Sequence[str], feature_values: Sequence[str]) -> str:
    payload = {"keys": list(feature_keys), "values": list(feature_values)}
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return f"{PATTERN_ID_PREFIX}_{digest[:12]}"


def _build_hypothesis(
    feature_keys: Tuple[str, ...],
    feature_values: Tuple[str, ...],
    observations: Sequence[PatternObservation],
    confidence_level: float,
    label: str,
) -> PatternHypothesis:
    wins = sum(int(o.outcome.get(OUTCOME_KEY_WIN, 0.0) > 0.0) for o in observations)
    returns = [float(o.outcome[OUTCOME_KEY_RETURN_PCT]) for o in observations]
    n = len(observations)
    win_rate, win_lo, win_hi, _ = wilson_proportion_ci(wins, n, confidence_level)
    mean_return, ret_lo, ret_hi, _ = normal_mean_ci(returns, confidence_level)
    effect = cohens_d_one_sample(returns)
    features_text = ", ".join(
        f"{key}={value}" for key, value in zip(feature_keys, feature_values)
    )
    return PatternHypothesis(
        pattern_id=_pattern_id(feature_keys, feature_values),
        feature_keys=feature_keys,
        feature_values=feature_values,
        description=(
            f"n={n} observations matching {features_text}"
            if features_text
            else f"n={n} observations (no context features)"
        ),
        sample_size=n,
        win_rate=win_rate,
        win_rate_ci_low=win_lo,
        win_rate_ci_high=win_hi,
        mean_return=mean_return,
        return_ci_low=ret_lo,
        return_ci_high=ret_hi,
        effect_size=effect,
        label=label,
        confidence_level=confidence_level,
        supporting_evidence_ids=tuple(sorted(o.trade_id for o in observations)),
        detail=(
            f"wins={wins}/{n}, mean_return={mean_return}, "
            f"effect_size={effect}"
        ),
    )


def discover_patterns(
    observations: Sequence[PatternObservation],
    feature_key_tuples: Sequence[Sequence[str]],
    min_sample_size: int = PATTERN_MIN_SAMPLE_SIZE,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> List[PatternHypothesis]:
    """Return hypothesis-labeled patterns for each feature-key combination.

    Observations without the required context keys or outcome fields
    are silently skipped — callers filter their input before passing it
    in.  All returned hypotheses carry ``LABEL_HYPOTHESIS``; use
    :func:`validate_patterns_out_of_sample` to promote them.
    """
    if min_sample_size < 1:
        raise ValueError("min_sample_size must be >= 1")
    if confidence_level not in SUPPORTED_CONFIDENCE_LEVELS:
        raise ValueError(
            f"unsupported confidence_level {confidence_level!r}; "
            f"supported: {SUPPORTED_CONFIDENCE_LEVELS}"
        )

    hypotheses: List[PatternHypothesis] = []
    for raw_keys in feature_key_tuples:
        keys = _feature_key_tuple_sorted(raw_keys)
        if not keys:
            continue
        groups: Dict[Tuple[str, ...], List[PatternObservation]] = {}
        for observation in observations:
            if not observation.has_context_keys(keys):
                continue
            if not observation.has_outcomes():
                continue
            group_key = _group_key_for(observation, keys)
            groups.setdefault(group_key, []).append(observation)
        for group_key in sorted(groups):
            group_obs = groups[group_key]
            if len(group_obs) < min_sample_size:
                continue
            hypotheses.append(
                _build_hypothesis(
                    feature_keys=keys,
                    feature_values=group_key,
                    observations=group_obs,
                    confidence_level=confidence_level,
                    label=LABEL_HYPOTHESIS,
                )
            )
    hypotheses.sort(
        key=lambda hypothesis: (
            hypothesis.feature_keys,
            hypothesis.feature_values,
            hypothesis.pattern_id,
        )
    )
    return hypotheses


# ---------------------------------------------------------------------------
# Out-of-sample validation
# ---------------------------------------------------------------------------


def _direction(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def validate_patterns_out_of_sample(
    hypotheses: Sequence[PatternHypothesis],
    out_of_sample_observations: Sequence[PatternObservation],
    min_sample_size: int = PATTERN_MIN_SAMPLE_SIZE,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> List[PatternHypothesis]:
    """Re-evaluate hypotheses on a disjoint observation set.

    A hypothesis is promoted to ``LABEL_VALIDATED`` iff:

    * the OOS group meets ``min_sample_size``, and
    * the OOS mean-return direction matches the in-sample direction, and
    * the OOS win-rate direction matches the in-sample direction
      (both above 0.5 or both at/below 0.5).

    Otherwise the hypothesis is re-emitted with ``LABEL_HYPOTHESIS`` and
    a detail note explaining why validation failed.  The pattern id is
    preserved so downstream consumers can join across in-sample and
    out-of-sample runs.
    """
    if min_sample_size < 1:
        raise ValueError("min_sample_size must be >= 1")
    if confidence_level not in SUPPORTED_CONFIDENCE_LEVELS:
        raise ValueError(
            f"unsupported confidence_level {confidence_level!r}; "
            f"supported: {SUPPORTED_CONFIDENCE_LEVELS}"
        )

    grouped: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[PatternObservation]] = {}
    for observation in out_of_sample_observations:
        if not observation.has_outcomes():
            continue
        for hypothesis in hypotheses:
            if not observation.has_context_keys(hypothesis.feature_keys):
                continue
            key = tuple(
                str(observation.context[k]) for k in hypothesis.feature_keys
            )
            if key != hypothesis.feature_values:
                continue
            grouped.setdefault(
                (hypothesis.feature_keys, hypothesis.feature_values),
                [],
            ).append(observation)

    validated: List[PatternHypothesis] = []
    for hypothesis in hypotheses:
        key = (hypothesis.feature_keys, hypothesis.feature_values)
        oos_group = grouped.get(key, [])
        if len(oos_group) < min_sample_size:
            validated.append(
                _replace_hypothesis(
                    hypothesis,
                    label=LABEL_HYPOTHESIS,
                    detail=(
                        f"{hypothesis.detail} | oos_check: "
                        f"insufficient OOS sample ({len(oos_group)}/{min_sample_size})"
                    ),
                )
            )
            continue
        oos = _build_hypothesis(
            feature_keys=hypothesis.feature_keys,
            feature_values=hypothesis.feature_values,
            observations=oos_group,
            confidence_level=confidence_level,
            label=LABEL_HYPOTHESIS,  # placeholder — we care about its stats
        )
        direction_match = _direction(oos.mean_return) == _direction(
            hypothesis.mean_return
        )
        win_direction_match = (oos.win_rate > 0.5) == (hypothesis.win_rate > 0.5)
        if direction_match and win_direction_match:
            validated.append(
                _replace_hypothesis(
                    hypothesis,
                    label=LABEL_VALIDATED,
                    detail=(
                        f"{hypothesis.detail} | oos_check: pass "
                        f"(oos_n={oos.sample_size}, "
                        f"oos_mean_return={oos.mean_return}, "
                        f"oos_win_rate={oos.win_rate})"
                    ),
                )
            )
        else:
            reasons: List[str] = []
            if not direction_match:
                reasons.append("return direction mismatch")
            if not win_direction_match:
                reasons.append("win-rate direction mismatch")
            validated.append(
                _replace_hypothesis(
                    hypothesis,
                    label=LABEL_HYPOTHESIS,
                    detail=(
                        f"{hypothesis.detail} | oos_check: fail "
                        f"({'; '.join(reasons)}; "
                        f"oos_n={oos.sample_size}, "
                        f"oos_mean_return={oos.mean_return})"
                    ),
                )
            )
    return validated


def _replace_hypothesis(
    hypothesis: PatternHypothesis,
    *,
    label: str,
    detail: str,
) -> PatternHypothesis:
    return PatternHypothesis(
        pattern_id=hypothesis.pattern_id,
        feature_keys=hypothesis.feature_keys,
        feature_values=hypothesis.feature_values,
        description=hypothesis.description,
        sample_size=hypothesis.sample_size,
        win_rate=hypothesis.win_rate,
        win_rate_ci_low=hypothesis.win_rate_ci_low,
        win_rate_ci_high=hypothesis.win_rate_ci_high,
        mean_return=hypothesis.mean_return,
        return_ci_low=hypothesis.return_ci_low,
        return_ci_high=hypothesis.return_ci_high,
        effect_size=hypothesis.effect_size,
        label=label,
        confidence_level=hypothesis.confidence_level,
        supporting_evidence_ids=hypothesis.supporting_evidence_ids,
        detail=detail,
    )
