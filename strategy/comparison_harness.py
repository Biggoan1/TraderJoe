"""Champion / Challenger Comparison Harness.

Read-only harness that runs a Champion and a Challenger evaluator over the
same deterministic replay stream and produces:

    * a per-event ``ScoreTable`` aligning Champion and Challenger scores,
      ranks, and selection status,
    * ``DisagreementRecord`` entries classifying each divergence
      (``ranking_only``, ``entry_selection``, ``score_delta``, or
      ``data_unavailable``), and
    * a ``ChampionChallengerComparison`` container with deterministic
      serialization for reports and artifact storage.

The harness never places orders, never modifies feature flags, never
touches the runner or the order path, and does not require live data.
Evaluators are supplied by the caller and are expected to be pure
functions of the replay event.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from strategy.backtest_lab import (
    BacktestEvent,
    StrategyEvaluation,
    stable_hash,
    stable_json,
)
from strategy.config import CHALLENGER_NAME, CHAMPION_NAME


DISAGREEMENT_RANKING = "ranking_only"
DISAGREEMENT_SELECTION = "entry_selection"
DISAGREEMENT_SCORE = "score_delta"
DISAGREEMENT_DATA = "data_unavailable"

KNOWN_DISAGREEMENT_KINDS: Tuple[str, ...] = (
    DISAGREEMENT_RANKING,
    DISAGREEMENT_SELECTION,
    DISAGREEMENT_SCORE,
    DISAGREEMENT_DATA,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class ComparisonEvaluator(Protocol):
    """Structural protocol for Champion and Challenger evaluators.

    Any object exposing ``strategy_id`` and an ``evaluate`` method that
    turns a :class:`BacktestEvent` into a :class:`StrategyEvaluation`
    satisfies this protocol. The existing ``NoOpStrategyAdapter`` from
    ``strategy.backtest_lab`` already conforms.
    """

    strategy_id: str

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation: ...


@dataclass(frozen=True)
class ScoreRow:
    """Aligned Champion and Challenger view of one symbol at one event.

    ``champion_structured_explanation`` / ``challenger_structured_explanation``
    are optional dicts (serialized :class:`~strategy.score_explanation.ScoreExplanation`
    objects) that describe why each side landed on its score.  When a
    side lacks a structured explanation, the field is omitted from
    ``to_dict``.
    """

    symbol: str
    champion_score: Optional[float] = None
    challenger_score: Optional[float] = None
    champion_rank: Optional[int] = None
    challenger_rank: Optional[int] = None
    champion_selected: bool = False
    challenger_selected: bool = False
    score_delta: Optional[float] = None
    rank_delta: Optional[int] = None
    champion_explanation: str = ""
    challenger_explanation: str = ""
    champion_structured_explanation: Optional[Dict[str, Any]] = None
    challenger_structured_explanation: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "symbol": self.symbol,
            "champion_score": self.champion_score,
            "challenger_score": self.challenger_score,
            "champion_rank": self.champion_rank,
            "challenger_rank": self.challenger_rank,
            "champion_selected": self.champion_selected,
            "challenger_selected": self.challenger_selected,
            "score_delta": self.score_delta,
            "rank_delta": self.rank_delta,
            "champion_explanation": self.champion_explanation,
            "challenger_explanation": self.challenger_explanation,
        }
        if self.champion_structured_explanation is not None:
            payload["champion_structured_explanation"] = (
                self.champion_structured_explanation
            )
        if self.challenger_structured_explanation is not None:
            payload["challenger_structured_explanation"] = (
                self.challenger_structured_explanation
            )
        return payload


@dataclass
class ScoreTable:
    """Score alignment for a single replay event."""

    event_timestamp: str
    event_type: str
    event_sequence: int
    champion_id: str
    challenger_id: str
    rows: List[ScoreRow] = field(default_factory=list)
    champion_warnings: List[str] = field(default_factory=list)
    challenger_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_timestamp": self.event_timestamp,
            "event_type": self.event_type,
            "event_sequence": self.event_sequence,
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "rows": [row.to_dict() for row in self.rows],
            "champion_warnings": list(self.champion_warnings),
            "challenger_warnings": list(self.challenger_warnings),
        }


@dataclass(frozen=True)
class DisagreementRecord:
    """One classified difference between Champion and Challenger.

    Carries both sides' structured explanations when available so
    reviewers can trace why each strategy arrived at its score.
    """

    event_timestamp: str
    event_type: str
    symbol: str
    kind: str
    champion_id: str
    challenger_id: str
    champion_score: Optional[float] = None
    challenger_score: Optional[float] = None
    champion_rank: Optional[int] = None
    challenger_rank: Optional[int] = None
    score_delta: Optional[float] = None
    rank_delta: Optional[int] = None
    detail: str = ""
    champion_explanation: str = ""
    challenger_explanation: str = ""
    champion_structured_explanation: Optional[Dict[str, Any]] = None
    challenger_structured_explanation: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_DISAGREEMENT_KINDS:
            raise ValueError(
                f"unknown disagreement kind: {self.kind!r} "
                f"(expected one of {KNOWN_DISAGREEMENT_KINDS})"
            )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "event_timestamp": self.event_timestamp,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "kind": self.kind,
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "champion_score": self.champion_score,
            "challenger_score": self.challenger_score,
            "champion_rank": self.champion_rank,
            "challenger_rank": self.challenger_rank,
            "score_delta": self.score_delta,
            "rank_delta": self.rank_delta,
            "detail": self.detail,
            "champion_explanation": self.champion_explanation,
            "challenger_explanation": self.challenger_explanation,
        }
        if self.champion_structured_explanation is not None:
            payload["champion_structured_explanation"] = (
                self.champion_structured_explanation
            )
        if self.challenger_structured_explanation is not None:
            payload["challenger_structured_explanation"] = (
                self.challenger_structured_explanation
            )
        return payload


@dataclass(frozen=True)
class ChampionChallengerRunMetadata:
    """Reproducible identity metadata for a comparison run."""

    run_id: str
    champion_id: str
    challenger_id: str
    dataset_id: str
    event_count: int
    score_delta_threshold: float
    seed: int
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "dataset_id": self.dataset_id,
            "event_count": self.event_count,
            "score_delta_threshold": self.score_delta_threshold,
            "seed": self.seed,
            "generated_at": self.generated_at,
        }


@dataclass
class ChampionChallengerComparison:
    """Container of per-event score tables and classified disagreements."""

    metadata: ChampionChallengerRunMetadata
    score_tables: List[ScoreTable] = field(default_factory=list)
    disagreements: List[DisagreementRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "score_tables": [table.to_dict() for table in self.score_tables],
            "disagreements": [
                record.to_dict() for record in self.disagreements
            ],
            "warnings": list(self.warnings),
        }

    def to_analyst_payload(self) -> Dict[str, Any]:
        """Trimmed payload for the Research Analyst LLM prompt.

        The full :meth:`to_dict` output attaches structured explanations
        to every score-table row (an ``n_events × n_symbols`` fan-out
        that inflates the payload sharply once explanations are wired).
        The analyst reasons primarily about the classified
        ``DisagreementRecord`` list, so this variant strips structured
        explanations from ``score_tables`` while keeping them on
        ``disagreements`` — the fields the analyst actually cites.
        Free-text explanations are preserved on both surfaces.
        """
        trimmed_tables: List[Dict[str, Any]] = []
        for table in self.score_tables:
            rows: List[Dict[str, Any]] = []
            for row in table.rows:
                row_dict = row.to_dict()
                row_dict.pop("champion_structured_explanation", None)
                row_dict.pop("challenger_structured_explanation", None)
                rows.append(row_dict)
            trimmed_tables.append(
                {
                    "event_timestamp": table.event_timestamp,
                    "event_type": table.event_type,
                    "event_sequence": table.event_sequence,
                    "champion_id": table.champion_id,
                    "challenger_id": table.challenger_id,
                    "rows": rows,
                    "champion_warnings": list(table.champion_warnings),
                    "challenger_warnings": list(table.challenger_warnings),
                }
            )
        return {
            "metadata": self.metadata.to_dict(),
            "score_tables": trimmed_tables,
            "disagreements": [
                record.to_dict() for record in self.disagreements
            ],
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return stable_json(self.to_dict())

    def stable_hash(self) -> str:
        data = self.to_dict()
        data["metadata"] = {
            key: value
            for key, value in data["metadata"].items()
            if key != "generated_at"
        }
        return stable_hash(data)

    def disagreements_by_kind(self) -> Dict[str, List[DisagreementRecord]]:
        buckets: Dict[str, List[DisagreementRecord]] = {
            kind: [] for kind in KNOWN_DISAGREEMENT_KINDS
        }
        for record in self.disagreements:
            buckets[record.kind].append(record)
        return buckets


def _rank_lookup(evaluation: StrategyEvaluation) -> Dict[str, int]:
    ranks: Dict[str, int] = {}
    for entry in evaluation.rankings:
        symbol = entry.get("symbol")
        rank = entry.get("rank")
        if symbol is None or rank is None:
            continue
        ranks[str(symbol)] = int(rank)
    return ranks


def _all_symbols(
    champion: StrategyEvaluation, challenger: StrategyEvaluation
) -> List[str]:
    symbols = set(champion.scores) | set(challenger.scores)
    symbols |= {
        str(entry.get("symbol"))
        for entry in champion.rankings
        if entry.get("symbol") is not None
    }
    symbols |= {
        str(entry.get("symbol"))
        for entry in challenger.rankings
        if entry.get("symbol") is not None
    }
    return sorted(symbols)


def _score_delta(
    champion_score: Optional[float], challenger_score: Optional[float]
) -> Optional[float]:
    if champion_score is None or challenger_score is None:
        return None
    return challenger_score - champion_score


def _rank_delta(
    champion_rank: Optional[int], challenger_rank: Optional[int]
) -> Optional[int]:
    """Positive rank_delta means Challenger ranked the symbol better."""
    if champion_rank is None or challenger_rank is None:
        return None
    return champion_rank - challenger_rank


def _summary_from_structured_dict(payload: Mapping[str, Any]) -> str:
    """Compact one-line summary derived from a serialized
    :class:`~strategy.score_explanation.ScoreExplanation` dict.

    Mirrors ``ScoreExplanation.to_summary_str`` without needing the
    class in scope, so ``ComparisonHarness`` stays free of circular
    imports.
    """
    parts: List[str] = []
    final_score = payload.get("final_score")
    if isinstance(final_score, (int, float)):
        parts.append(f"final={float(final_score):.4f}")
    for component in payload.get("components", []) or []:
        if not isinstance(component, Mapping):
            continue
        name = component.get("name", "?")
        contribution = component.get("contribution", 0.0)
        parts.append(f"{name}={float(contribution):+.4f}")
    for key, label in (
        ("rs_contribution", "rs"),
        ("momentum_contribution", "momentum"),
        ("breadth_contribution", "breadth"),
    ):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{label}={float(value):+.4f}")
    for reason_key, tag in (("bonuses", "bonus"), ("penalties", "penalty")):
        for entry in payload.get(reason_key, []) or []:
            if not isinstance(entry, Mapping):
                continue
            reason = entry.get("reason", "?")
            magnitude = entry.get("magnitude", 0.0)
            parts.append(f"{tag}[{reason}]={float(magnitude):+.4f}")
    if payload.get("rejected"):
        reasons = payload.get("rejection_reasons") or []
        parts.append("rejected(" + ", ".join(str(r) for r in reasons) + ")")
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)):
        parts.append(f"confidence={float(confidence):.2f}")
    return " | ".join(parts)


def _classify_row(
    row: ScoreRow, score_delta_threshold: float
) -> Optional[Tuple[str, str]]:
    """Return (kind, detail) if the row is a disagreement, else None."""
    champ_has_data = row.champion_score is not None or row.champion_selected
    chall_has_data = row.challenger_score is not None or row.challenger_selected
    if not champ_has_data or not chall_has_data:
        return DISAGREEMENT_DATA, "symbol missing from one side"
    if row.champion_selected != row.challenger_selected:
        detail = (
            f"champion_selected={row.champion_selected}, "
            f"challenger_selected={row.challenger_selected}"
        )
        return DISAGREEMENT_SELECTION, detail
    if row.champion_selected and row.challenger_selected:
        if row.champion_rank != row.challenger_rank:
            detail = (
                f"champion_rank={row.champion_rank}, "
                f"challenger_rank={row.challenger_rank}"
            )
            return DISAGREEMENT_RANKING, detail
    if row.score_delta is not None and abs(row.score_delta) > score_delta_threshold:
        detail = f"score_delta={row.score_delta}"
        return DISAGREEMENT_SCORE, detail
    return None


class ComparisonHarness:
    """Runs Champion and Challenger evaluators over the same replay stream.

    The harness is stateless between calls to :meth:`run` and does not
    place orders, mutate feature flags, or import order-path code.
    """

    def __init__(
        self,
        champion: ComparisonEvaluator,
        challenger: ComparisonEvaluator,
        score_delta_threshold: float = 0.0,
    ):
        if score_delta_threshold < 0:
            raise ValueError("score_delta_threshold cannot be negative")
        if champion.strategy_id == challenger.strategy_id:
            raise ValueError(
                "champion and challenger must have distinct strategy_id values"
            )
        self._champion = champion
        self._challenger = challenger
        self._threshold = float(score_delta_threshold)

    @property
    def champion_id(self) -> str:
        return self._champion.strategy_id

    @property
    def challenger_id(self) -> str:
        return self._challenger.strategy_id

    @property
    def score_delta_threshold(self) -> float:
        return self._threshold

    def run(
        self,
        clock: Iterable[BacktestEvent],
        dataset_id: str = "",
        seed: int = 0,
        generated_at: Optional[str] = None,
    ) -> ChampionChallengerComparison:
        events = list(clock)
        score_tables: List[ScoreTable] = []
        disagreements: List[DisagreementRecord] = []
        warnings: List[str] = []

        for event in events:
            table, event_disagreements, event_warnings = self._evaluate_event(event)
            score_tables.append(table)
            disagreements.extend(event_disagreements)
            warnings.extend(event_warnings)

        disagreements.sort(
            key=lambda record: (
                record.event_timestamp,
                record.event_type,
                record.symbol,
                record.kind,
            )
        )

        metadata = self._build_metadata(
            events=events,
            dataset_id=dataset_id,
            seed=seed,
            generated_at=generated_at,
        )
        return ChampionChallengerComparison(
            metadata=metadata,
            score_tables=score_tables,
            disagreements=disagreements,
            warnings=warnings,
        )

    def _evaluate_event(
        self, event: BacktestEvent
    ) -> Tuple[ScoreTable, List[DisagreementRecord], List[str]]:
        champion_eval = self._champion.evaluate(event)
        challenger_eval = self._challenger.evaluate(event)

        champion_ranks = _rank_lookup(champion_eval)
        challenger_ranks = _rank_lookup(challenger_eval)
        symbols = _all_symbols(champion_eval, challenger_eval)

        champion_structured = getattr(
            champion_eval, "structured_explanations", {}
        ) or {}
        challenger_structured = getattr(
            challenger_eval, "structured_explanations", {}
        ) or {}

        def _structured_dict(source: Any, sym: str) -> Optional[Dict[str, Any]]:
            entry = source.get(sym) if hasattr(source, "get") else None
            if entry is None:
                return None
            if hasattr(entry, "to_dict"):
                return entry.to_dict()
            if isinstance(entry, dict):
                return entry
            return None

        rows: List[ScoreRow] = []
        disagreements: List[DisagreementRecord] = []
        for symbol in symbols:
            champion_score = champion_eval.scores.get(symbol)
            challenger_score = challenger_eval.scores.get(symbol)
            champion_rank = champion_ranks.get(symbol)
            challenger_rank = challenger_ranks.get(symbol)
            champion_explanation = champion_eval.explanations.get(symbol, "")
            challenger_explanation = challenger_eval.explanations.get(symbol, "")
            champion_struct = _structured_dict(champion_structured, symbol)
            challenger_struct = _structured_dict(challenger_structured, symbol)
            # If the free-text is empty but the structured form is populated,
            # surface the structured summary through the string surface so
            # downstream reports and analyst prompts never see an empty
            # explanation while the structured evidence is right there.
            if not champion_explanation and champion_struct is not None:
                champion_explanation = _summary_from_structured_dict(champion_struct)
            if not challenger_explanation and challenger_struct is not None:
                challenger_explanation = _summary_from_structured_dict(
                    challenger_struct
                )
            row = ScoreRow(
                symbol=symbol,
                champion_score=champion_score,
                challenger_score=challenger_score,
                champion_rank=champion_rank,
                challenger_rank=challenger_rank,
                champion_selected=symbol in champion_ranks,
                challenger_selected=symbol in challenger_ranks,
                score_delta=_score_delta(champion_score, challenger_score),
                rank_delta=_rank_delta(champion_rank, challenger_rank),
                champion_explanation=champion_explanation,
                challenger_explanation=challenger_explanation,
                champion_structured_explanation=champion_struct,
                challenger_structured_explanation=challenger_struct,
            )
            rows.append(row)
            classification = _classify_row(row, self._threshold)
            if classification is not None:
                kind, detail = classification
                disagreements.append(
                    DisagreementRecord(
                        event_timestamp=event.timestamp,
                        event_type=event.event_type,
                        symbol=symbol,
                        kind=kind,
                        champion_id=self.champion_id,
                        challenger_id=self.challenger_id,
                        champion_score=row.champion_score,
                        challenger_score=row.challenger_score,
                        champion_rank=row.champion_rank,
                        challenger_rank=row.challenger_rank,
                        score_delta=row.score_delta,
                        rank_delta=row.rank_delta,
                        detail=detail,
                        champion_explanation=row.champion_explanation,
                        challenger_explanation=row.challenger_explanation,
                        champion_structured_explanation=champion_struct,
                        challenger_structured_explanation=challenger_struct,
                    )
                )

        table = ScoreTable(
            event_timestamp=event.timestamp,
            event_type=event.event_type,
            event_sequence=event.sequence,
            champion_id=self.champion_id,
            challenger_id=self.challenger_id,
            rows=rows,
            champion_warnings=list(champion_eval.warnings),
            challenger_warnings=list(challenger_eval.warnings),
        )
        return table, disagreements, list(champion_eval.warnings) + list(
            challenger_eval.warnings
        )

    def _build_metadata(
        self,
        events: List[BacktestEvent],
        dataset_id: str,
        seed: int,
        generated_at: Optional[str],
    ) -> ChampionChallengerRunMetadata:
        identity = {
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "dataset_id": dataset_id,
            "event_count": len(events),
            "score_delta_threshold": self._threshold,
            "seed": seed,
            "event_signature": [
                {
                    "timestamp": event.timestamp,
                    "event_type": event.event_type,
                    "sequence": event.sequence,
                }
                for event in events
            ],
        }
        digest = hashlib.sha256(
            stable_json(identity).encode("utf-8")
        ).hexdigest()
        return ChampionChallengerRunMetadata(
            run_id=f"cc_{digest[:12]}",
            champion_id=self.champion_id,
            challenger_id=self.challenger_id,
            dataset_id=dataset_id,
            event_count=len(events),
            score_delta_threshold=self._threshold,
            seed=seed,
            generated_at=generated_at or _utc_now_iso(),
        )


# Convenience aliases for callers that want to name evaluators explicitly.
CHAMPION_ROLE = CHAMPION_NAME
CHALLENGER_ROLE = CHALLENGER_NAME
