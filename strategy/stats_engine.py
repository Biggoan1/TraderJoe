"""Statistical Decision-Support Layer.

Read-only statistical analysis over Phase 3 comparison artifacts.
Produces :class:`StatisticalFinding` records for downstream human
review.  The Learning System never advances promotion state, enables
feature flags, places orders, or wires broker credentials.

Findings carry a ``hypothesis`` / ``validated`` label based purely on
whether the underlying sample meets a minimum-size floor
(:data:`STATS_SAMPLE_SIZE_FLOOR` by default).  A ``validated`` label
does not mean a strategy claim is strong enough for promotion — it
means the descriptive statistic has enough data behind it to be worth
reporting.  Promotion decisions still require the full Phase 3 gate
process and explicit :class:`~strategy.promotion_gates.ApprovalRecord`
entries.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 4 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from strategy.backtest_lab import stable_hash, stable_json
from strategy.comparison_harness import ChampionChallengerComparison
from strategy.walk_forward import WalkForwardReport


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


STATS_SAMPLE_SIZE_FLOOR = 30
STATS_DEFAULT_CONFIDENCE_LEVEL = 0.95

LABEL_HYPOTHESIS = "hypothesis"
LABEL_VALIDATED = "validated"
KNOWN_STATS_LABELS: Tuple[str, ...] = (LABEL_HYPOTHESIS, LABEL_VALIDATED)

# Two-sided normal-approximation z-scores for supported confidence
# levels.  Values are the standard reference values used across finance
# and epidemiology literature.
_Z_TABLE: Dict[float, float] = {
    0.90: 1.6449,
    0.95: 1.9600,
    0.99: 2.5758,
}
SUPPORTED_CONFIDENCE_LEVELS: Tuple[float, ...] = tuple(sorted(_Z_TABLE))


METRIC_SCORE_DELTA_MEAN = "score_delta_mean"
METRIC_RANK_DELTA_MEAN = "rank_delta_mean"
METRIC_DISAGREEMENT_RATE = "disagreement_rate"
METRIC_SELECTION_AGREEMENT_RATE = "selection_agreement_rate"
METRIC_WF_SCORE_DELTA_MEAN = "wf_score_delta_mean"
METRIC_WF_DISAGREEMENT_RATE = "wf_disagreement_rate"


# ---------------------------------------------------------------------------
# Numerical helpers
# ---------------------------------------------------------------------------


def _z_critical(confidence_level: float) -> float:
    if confidence_level not in _Z_TABLE:
        raise ValueError(
            f"unsupported confidence_level {confidence_level!r}; "
            f"supported values: {SUPPORTED_CONFIDENCE_LEVELS}"
        )
    return _Z_TABLE[confidence_level]


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _sample_variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def _sample_stdev(values: Sequence[float]) -> float:
    return math.sqrt(_sample_variance(values))


def normal_mean_ci(
    values: Sequence[float],
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> Tuple[float, float, float, int]:
    """Return ``(mean, ci_low, ci_high, sample_size)`` under a normal
    approximation for the confidence interval of the mean.

    Uses the standard-normal z-score; suitable for samples that meet
    :data:`STATS_SAMPLE_SIZE_FLOOR` (n >= 30).  Small samples still
    receive a CI, but the caller is expected to label the finding as a
    hypothesis.
    """
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    mean = _mean(values)
    if n < 2:
        return mean, mean, mean, n
    se = _sample_stdev(values) / math.sqrt(n)
    z = _z_critical(confidence_level)
    return mean, mean - z * se, mean + z * se, n


def wilson_proportion_ci(
    successes: int,
    trials: int,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> Tuple[float, float, float, int]:
    """Return ``(proportion, ci_low, ci_high, sample_size)`` using the
    Wilson score interval.

    Wilson intervals are well-behaved near 0 and 1 and are the
    recommended proportion CI when normal approximation would degrade.
    """
    if trials < 0:
        raise ValueError("trials cannot be negative")
    if successes < 0:
        raise ValueError("successes cannot be negative")
    if successes > trials:
        raise ValueError("successes cannot exceed trials")
    if trials == 0:
        return 0.0, 0.0, 0.0, 0
    p = successes / trials
    z = _z_critical(confidence_level)
    z2 = z * z
    denom = 1.0 + z2 / trials
    center = (p + z2 / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(
        p * (1.0 - p) / trials + z2 / (4.0 * trials * trials)
    )
    return p, max(0.0, center - margin), min(1.0, center + margin), trials


def cohens_d_one_sample(values: Sequence[float]) -> float:
    """One-sample effect size ``mean / stdev``.

    Returns 0.0 when there is insufficient data (n < 2) or when the
    sample standard deviation is zero.  Callers use this to score
    score-delta and rank-delta distributions.
    """
    if len(values) < 2:
        return 0.0
    std = _sample_stdev(values)
    if std == 0.0:
        return 0.0
    return _mean(values) / std


def cohens_d_two_sample(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sample pooled Cohen's d.  Returns 0.0 for degenerate inputs."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    var_a = _sample_variance(a)
    var_b = _sample_variance(b)
    pooled = math.sqrt(
        ((len(a) - 1) * var_a + (len(b) - 1) * var_b) / (len(a) + len(b) - 2)
    )
    if pooled == 0.0:
        return 0.0
    return (_mean(a) - _mean(b)) / pooled


def _label_for(sample_size: int, floor: int) -> str:
    return LABEL_VALIDATED if sample_size >= floor else LABEL_HYPOTHESIS


# ---------------------------------------------------------------------------
# StatisticalFinding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatisticalFinding:
    """One deterministic descriptive-statistics finding.

    A finding is a data artifact — never a promotion decision.  Fields
    are chosen so a ``LearningReport`` can render, sort, and hash
    findings without external state.
    """

    metric: str
    effect_size: float
    ci_low: float
    ci_high: float
    sample_size: int
    methodology: str
    label: str
    confidence_level: float
    evidence_ids: Tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        self.validate()

    def validate(self) -> None:
        if not self.metric:
            raise ValueError("StatisticalFinding.metric is required")
        if not self.methodology:
            raise ValueError("StatisticalFinding.methodology is required")
        if self.label not in KNOWN_STATS_LABELS:
            raise ValueError(
                f"unknown label: {self.label!r} "
                f"(expected one of {KNOWN_STATS_LABELS})"
            )
        if not (0.0 < self.confidence_level < 1.0):
            raise ValueError(
                f"confidence_level must be in (0, 1) (got {self.confidence_level!r})"
            )
        if self.sample_size < 0:
            raise ValueError("sample_size cannot be negative")
        if self.ci_low > self.ci_high:
            raise ValueError(
                f"ci_low must be <= ci_high (got {self.ci_low!r} > {self.ci_high!r})"
            )

    def is_significant(self) -> bool:
        """True when the confidence interval excludes zero.

        Callers use this to filter findings for reporting.  It is not a
        promotion signal on its own — see the promotion gate process
        in ``strategy.promotion_gates``.
        """
        return not (self.ci_low <= 0.0 <= self.ci_high)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "effect_size": self.effect_size,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "sample_size": self.sample_size,
            "methodology": self.methodology,
            "label": self.label,
            "confidence_level": self.confidence_level,
            "evidence_ids": list(self.evidence_ids),
            "detail": self.detail,
        }


def findings_stable_hash(findings: Iterable[StatisticalFinding]) -> str:
    """Deterministic hash across a sequence of findings."""
    payload = [finding.to_dict() for finding in findings]
    payload.sort(key=lambda entry: (entry["metric"], entry["evidence_ids"]))
    return stable_hash({"findings": payload})


# ---------------------------------------------------------------------------
# Comparison analysis
# ---------------------------------------------------------------------------


def _collect_score_deltas(
    comparison: ChampionChallengerComparison,
) -> List[float]:
    deltas: List[float] = []
    for table in comparison.score_tables:
        for row in table.rows:
            if row.score_delta is not None:
                deltas.append(row.score_delta)
    return deltas


def _collect_rank_deltas(
    comparison: ChampionChallengerComparison,
) -> List[int]:
    deltas: List[int] = []
    for table in comparison.score_tables:
        for row in table.rows:
            if row.rank_delta is not None:
                deltas.append(row.rank_delta)
    return deltas


def _selection_counts(
    comparison: ChampionChallengerComparison,
) -> Tuple[int, int]:
    """Return ``(rows_agree, total_rows)`` for selection agreement."""
    agree = 0
    total = 0
    for table in comparison.score_tables:
        for row in table.rows:
            total += 1
            if row.champion_selected == row.challenger_selected:
                agree += 1
    return agree, total


def _disagreement_counts(
    comparison: ChampionChallengerComparison,
) -> Tuple[int, int]:
    """Return ``(rows_with_disagreement, total_rows)``.

    A row is counted at most once even if it is flagged with multiple
    disagreement kinds (which the harness never does — kinds are
    mutually exclusive in :func:`_classify_row`).
    """
    total = 0
    for table in comparison.score_tables:
        total += len(table.rows)
    disagreeing_rows = {
        (record.event_timestamp, record.symbol)
        for record in comparison.disagreements
    }
    return len(disagreeing_rows), total


def _score_delta_finding(
    comparison: ChampionChallengerComparison,
    sample_size_floor: int,
    confidence_level: float,
) -> StatisticalFinding:
    deltas = _collect_score_deltas(comparison)
    mean, lo, hi, n = normal_mean_ci(deltas, confidence_level)
    effect = cohens_d_one_sample(deltas)
    return StatisticalFinding(
        metric=METRIC_SCORE_DELTA_MEAN,
        effect_size=effect,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        methodology="mean CI (normal approx) + Cohen's d one-sample",
        label=_label_for(n, sample_size_floor),
        confidence_level=confidence_level,
        evidence_ids=(comparison.metadata.run_id,),
        detail=f"mean score_delta={mean}",
    )


def _rank_delta_finding(
    comparison: ChampionChallengerComparison,
    sample_size_floor: int,
    confidence_level: float,
) -> StatisticalFinding:
    deltas_int = _collect_rank_deltas(comparison)
    deltas = [float(x) for x in deltas_int]
    mean, lo, hi, n = normal_mean_ci(deltas, confidence_level)
    effect = cohens_d_one_sample(deltas)
    return StatisticalFinding(
        metric=METRIC_RANK_DELTA_MEAN,
        effect_size=effect,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        methodology="mean CI (normal approx) + Cohen's d one-sample",
        label=_label_for(n, sample_size_floor),
        confidence_level=confidence_level,
        evidence_ids=(comparison.metadata.run_id,),
        detail=f"mean rank_delta={mean}",
    )


def _disagreement_rate_finding(
    comparison: ChampionChallengerComparison,
    sample_size_floor: int,
    confidence_level: float,
) -> StatisticalFinding:
    disagreeing, total = _disagreement_counts(comparison)
    p, lo, hi, n = wilson_proportion_ci(disagreeing, total, confidence_level)
    return StatisticalFinding(
        metric=METRIC_DISAGREEMENT_RATE,
        effect_size=p,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        methodology="Wilson score proportion CI",
        label=_label_for(n, sample_size_floor),
        confidence_level=confidence_level,
        evidence_ids=(comparison.metadata.run_id,),
        detail=(
            f"{disagreeing}/{total} rows had any disagreement"
            if total
            else "no rows evaluated"
        ),
    )


def _selection_agreement_finding(
    comparison: ChampionChallengerComparison,
    sample_size_floor: int,
    confidence_level: float,
) -> StatisticalFinding:
    agree, total = _selection_counts(comparison)
    p, lo, hi, n = wilson_proportion_ci(agree, total, confidence_level)
    return StatisticalFinding(
        metric=METRIC_SELECTION_AGREEMENT_RATE,
        effect_size=p,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        methodology="Wilson score proportion CI",
        label=_label_for(n, sample_size_floor),
        confidence_level=confidence_level,
        evidence_ids=(comparison.metadata.run_id,),
        detail=(
            f"{agree}/{total} rows agreed on selection"
            if total
            else "no rows evaluated"
        ),
    )


def analyze_comparison(
    comparison: ChampionChallengerComparison,
    sample_size_floor: int = STATS_SAMPLE_SIZE_FLOOR,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> List[StatisticalFinding]:
    """Return descriptive-statistics findings for one comparison.

    Findings are returned in the fixed order:

        score_delta_mean, rank_delta_mean,
        disagreement_rate, selection_agreement_rate.

    Deterministic given a fixed input comparison.
    """
    if sample_size_floor < 0:
        raise ValueError("sample_size_floor cannot be negative")
    return [
        _score_delta_finding(comparison, sample_size_floor, confidence_level),
        _rank_delta_finding(comparison, sample_size_floor, confidence_level),
        _disagreement_rate_finding(comparison, sample_size_floor, confidence_level),
        _selection_agreement_finding(comparison, sample_size_floor, confidence_level),
    ]


# ---------------------------------------------------------------------------
# Walk-forward analysis
# ---------------------------------------------------------------------------


def analyze_walk_forward(
    wf_report: WalkForwardReport,
    sample_size_floor: int = STATS_SAMPLE_SIZE_FLOOR,
    confidence_level: float = STATS_DEFAULT_CONFIDENCE_LEVEL,
) -> List[StatisticalFinding]:
    """Return aggregate findings across all splits of a walk-forward report.

    Aggregates score deltas and per-row disagreement rates across every
    split's out-of-sample comparison — never mixes in-sample data.  The
    Phase 3 :class:`WalkForwardPipeline` already ensures in-sample
    events never reach the harness, so aggregating over
    ``split.comparison`` outputs is leakage-safe.
    """
    if sample_size_floor < 0:
        raise ValueError("sample_size_floor cannot be negative")

    score_deltas: List[float] = []
    total_rows = 0
    disagreeing_rows = 0
    evidence_ids: List[str] = [wf_report.report_id]

    for split_result in wf_report.split_results:
        comparison = split_result.comparison
        score_deltas.extend(_collect_score_deltas(comparison))
        disagreeing, split_total = _disagreement_counts(comparison)
        disagreeing_rows += disagreeing
        total_rows += split_total
        evidence_ids.append(comparison.metadata.run_id)

    mean, lo, hi, n = normal_mean_ci(score_deltas, confidence_level)
    effect = cohens_d_one_sample(score_deltas)
    score_finding = StatisticalFinding(
        metric=METRIC_WF_SCORE_DELTA_MEAN,
        effect_size=effect,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        methodology="mean CI (normal approx) + Cohen's d one-sample; aggregated across OOS splits",
        label=_label_for(n, sample_size_floor),
        confidence_level=confidence_level,
        evidence_ids=tuple(evidence_ids),
        detail=(
            f"mean OOS score_delta={mean} across "
            f"{len(wf_report.split_results)} split(s)"
        ),
    )

    p, prop_lo, prop_hi, prop_n = wilson_proportion_ci(
        disagreeing_rows, total_rows, confidence_level
    )
    rate_finding = StatisticalFinding(
        metric=METRIC_WF_DISAGREEMENT_RATE,
        effect_size=p,
        ci_low=prop_lo,
        ci_high=prop_hi,
        sample_size=prop_n,
        methodology="Wilson score proportion CI; aggregated across OOS splits",
        label=_label_for(prop_n, sample_size_floor),
        confidence_level=confidence_level,
        evidence_ids=tuple(evidence_ids),
        detail=(
            f"{disagreeing_rows}/{total_rows} OOS rows had any disagreement"
            if total_rows
            else "no OOS rows evaluated"
        ),
    )

    return [score_finding, rate_finding]
