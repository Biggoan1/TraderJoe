"""Structured score explanation surface for research/replay.

A ``ScoreExplanation`` captures WHY a strategy produced a specific
score for one symbol at one event.  It sits alongside the raw
numeric score in :class:`strategy.backtest_lab.StrategyEvaluation`
so downstream consumers — Champion vs Challenger comparison, walk-
forward reports, learning reports, and the Research Analyst — can
reason about both sides of a disagreement instead of inferring
motives from bare numbers.

The explanation is a research artifact.  It never influences live
trading, is never persisted to the paper/production credentials
namespace, and never carries or produces order-path signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.backtest_lab import stable_hash, stable_json


SCORE_EXPLANATION_KIND = "score_explanation"


@dataclass(frozen=True)
class ScoreComponent:
    """One named contribution to a symbol's final score."""

    name: str
    contribution: float
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "contribution": round(float(self.contribution), 6),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScoreExplanation:
    """Structured reasoning behind a single strategy's per-symbol score.

    Every field except ``strategy_id``, ``symbol``, ``event_timestamp``,
    and ``final_score`` is optional so producers can populate only what
    they compute.  Serialization is deterministic and consumers should
    treat missing fields as "not applicable to this strategy".
    """

    strategy_id: str
    symbol: str
    event_timestamp: str
    final_score: float
    components: Tuple[ScoreComponent, ...] = ()
    ranking_factors: Tuple[str, ...] = ()
    rs_contribution: Optional[float] = None
    breadth_contribution: Optional[float] = None
    momentum_contribution: Optional[float] = None
    penalties: Tuple[Tuple[str, float], ...] = ()
    bonuses: Tuple[Tuple[str, float], ...] = ()
    rejected: bool = False
    rejection_reasons: Tuple[str, ...] = ()
    confidence: Optional[float] = None
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": SCORE_EXPLANATION_KIND,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "event_timestamp": self.event_timestamp,
            "final_score": round(float(self.final_score), 6),
            "components": [c.to_dict() for c in self.components],
            "ranking_factors": list(self.ranking_factors),
            "rs_contribution": (
                None
                if self.rs_contribution is None
                else round(float(self.rs_contribution), 6)
            ),
            "breadth_contribution": (
                None
                if self.breadth_contribution is None
                else round(float(self.breadth_contribution), 6)
            ),
            "momentum_contribution": (
                None
                if self.momentum_contribution is None
                else round(float(self.momentum_contribution), 6)
            ),
            "penalties": [
                {"reason": r, "magnitude": round(float(m), 6)}
                for r, m in self.penalties
            ],
            "bonuses": [
                {"reason": r, "magnitude": round(float(m), 6)}
                for r, m in self.bonuses
            ],
            "rejected": bool(self.rejected),
            "rejection_reasons": list(self.rejection_reasons),
            "confidence": (
                None
                if self.confidence is None
                else round(float(self.confidence), 6)
            ),
            "notes": list(self.notes),
        }

    def to_summary_str(self) -> str:
        """Human-readable one-line summary used as the free-text
        explanation on :class:`strategy.backtest_lab.StrategyEvaluation`
        so downstream code that only reads the string surface still
        sees WHY the score landed where it did.
        """
        parts: List[str] = [f"final={self.final_score:.4f}"]
        for component in self.components:
            parts.append(
                f"{component.name}={component.contribution:+.4f}"
            )
        if self.rs_contribution is not None:
            parts.append(f"rs={self.rs_contribution:+.4f}")
        if self.momentum_contribution is not None:
            parts.append(f"momentum={self.momentum_contribution:+.4f}")
        if self.breadth_contribution is not None:
            parts.append(f"breadth={self.breadth_contribution:+.4f}")
        for reason, magnitude in self.bonuses:
            parts.append(f"bonus[{reason}]={magnitude:+.4f}")
        for reason, magnitude in self.penalties:
            parts.append(f"penalty[{reason}]={magnitude:+.4f}")
        if self.rejected:
            parts.append(
                "rejected(" + ", ".join(self.rejection_reasons) + ")"
            )
        if self.confidence is not None:
            parts.append(f"confidence={self.confidence:.2f}")
        return " | ".join(parts)

    def stable_hash(self) -> str:
        return stable_hash(self.to_dict())


def blank_explanation(
    strategy_id: str,
    symbol: str,
    event_timestamp: str,
    final_score: float,
    note: str = "",
) -> ScoreExplanation:
    """Fallback for strategies that expose no structured reasoning."""
    notes: Tuple[str, ...] = (note,) if note else ()
    return ScoreExplanation(
        strategy_id=strategy_id,
        symbol=symbol,
        event_timestamp=event_timestamp,
        final_score=float(final_score),
        notes=notes,
    )


def append_overlay_component(
    base: ScoreExplanation,
    overlay_strategy_id: str,
    component_name: str,
    contribution: float,
    detail: str = "",
    rs_value: Optional[float] = None,
    notes: Sequence[str] = (),
) -> ScoreExplanation:
    """Derive an overlay strategy's explanation from a base one.

    The overlay preserves the base's components, ranking factors,
    penalties, and bonuses (so the challenger's story explains the
    champion's baseline verbatim) and appends its own contribution.
    Used by the RS Challenger and any future challenger overlay.
    """
    new_components = tuple(base.components) + (
        ScoreComponent(
            name=component_name, contribution=contribution, detail=detail
        ),
    )
    combined_notes = tuple(base.notes) + tuple(notes)
    return ScoreExplanation(
        strategy_id=overlay_strategy_id,
        symbol=base.symbol,
        event_timestamp=base.event_timestamp,
        final_score=base.final_score + contribution,
        components=new_components,
        ranking_factors=base.ranking_factors,
        rs_contribution=(
            rs_value if rs_value is not None else base.rs_contribution
        ),
        breadth_contribution=base.breadth_contribution,
        momentum_contribution=base.momentum_contribution,
        penalties=base.penalties,
        bonuses=base.bonuses,
        rejected=base.rejected,
        rejection_reasons=base.rejection_reasons,
        confidence=base.confidence,
        notes=combined_notes,
    )


def summarize_explanations(
    explanations: Mapping[str, ScoreExplanation],
) -> Dict[str, Any]:
    """Aggregate a batch of explanations for a single event.

    Returns per-event stats consumers can roll up into learning
    reports (gate pass rates, top ranking factors, rejection rate).
    """
    if not explanations:
        return {
            "count": 0,
            "rejected_count": 0,
            "component_totals": {},
            "top_ranking_factors": [],
        }
    component_totals: Dict[str, float] = {}
    factor_counts: Dict[str, int] = {}
    rejected = 0
    for exp in explanations.values():
        if exp.rejected:
            rejected += 1
        for c in exp.components:
            component_totals[c.name] = component_totals.get(
                c.name, 0.0
            ) + float(c.contribution)
        for factor in exp.ranking_factors:
            factor_counts[factor] = factor_counts.get(factor, 0) + 1
    top_factors = sorted(
        factor_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    return {
        "count": len(explanations),
        "rejected_count": rejected,
        "component_totals": {
            name: round(total, 6) for name, total in sorted(component_totals.items())
        },
        "top_ranking_factors": [
            {"factor": name, "count": count} for name, count in top_factors
        ],
    }


def aggregate_explanation_dicts(
    explanation_dicts: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Aggregate serialized :class:`ScoreExplanation` dicts across many
    (event, symbol) pairs.

    Consumers pass in the flat list of structured-explanation dicts
    they extracted from comparison score-table rows (either
    champion's or challenger's).  Returns:

    * ``observation_count`` — number of dicts examined
    * ``rejected_rate`` — fraction that carried ``rejected=True``
    * ``component_pass_rates`` — per-component fraction of
      observations where the component fired (contribution != 0)
    * ``mean_component_contributions`` — mean contribution across
      observations that had the component
    * ``top_ranking_factors`` — descending frequency count
    * ``mean_confidence`` — mean of ``confidence`` values that were set
    """
    total = len(explanation_dicts)
    if total == 0:
        return {
            "observation_count": 0,
            "rejected_rate": 0.0,
            "component_pass_rates": {},
            "mean_component_contributions": {},
            "top_ranking_factors": [],
            "mean_confidence": None,
        }
    rejected = 0
    component_pass_counts: Dict[str, int] = {}
    component_contrib_sums: Dict[str, float] = {}
    factor_counts: Dict[str, int] = {}
    confidence_sum = 0.0
    confidence_count = 0
    for entry in explanation_dicts:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("rejected"):
            rejected += 1
        for component in entry.get("components", []) or []:
            if not isinstance(component, Mapping):
                continue
            name = component.get("name")
            if not name:
                continue
            contribution = float(component.get("contribution") or 0.0)
            if contribution != 0.0:
                component_pass_counts[name] = (
                    component_pass_counts.get(name, 0) + 1
                )
                component_contrib_sums[name] = (
                    component_contrib_sums.get(name, 0.0) + contribution
                )
        for factor in entry.get("ranking_factors", []) or []:
            factor_counts[factor] = factor_counts.get(factor, 0) + 1
        confidence = entry.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence_sum += float(confidence)
            confidence_count += 1

    component_pass_rates = {
        name: round(count / total, 4)
        for name, count in sorted(component_pass_counts.items())
    }
    mean_component_contributions = {
        name: round(
            component_contrib_sums[name] / component_pass_counts[name], 6
        )
        for name in sorted(component_pass_counts)
    }
    top_ranking_factors = [
        {"factor": name, "count": count}
        for name, count in sorted(
            factor_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]
    return {
        "observation_count": total,
        "rejected_rate": round(rejected / total, 4),
        "component_pass_rates": component_pass_rates,
        "mean_component_contributions": mean_component_contributions,
        "top_ranking_factors": top_ranking_factors,
        "mean_confidence": (
            round(confidence_sum / confidence_count, 4)
            if confidence_count
            else None
        ),
    }
