"""Walk-Forward Evaluation Pipeline.

Read-only orchestration layer that runs the Champion/Challenger
comparison harness (``strategy.comparison_harness``) over time-ordered,
non-overlapping evaluation windows.

Vocabulary: this module uses **in-sample** and **out-of-sample** for the
two windows in each split.  We do not call this "training" — Phase 3
evaluation work is validation, replay, and research (see the terminology
rule saved for this project).

Anti-leakage guarantees:

* Every :class:`WalkForwardSplit` has ``out_of_sample_start`` strictly
  after ``in_sample_end`` — the schedule generator enforces this and the
  ``WalkForwardSplit`` constructor re-checks it.
* The pipeline never passes in-sample events to the harness for a given
  split.  Events whose date falls inside the in-sample window are
  reported as ``dropped_events`` for that split but do not appear in the
  harness input for the comparison.
* Events outside all splits' out-of-sample windows are recorded as
  ``unassigned_events`` and are not evaluated.

Reproducibility:

* :meth:`WalkForwardSchedule.stable_hash` is deterministic over the
  configuration parameters and the resolved split boundaries.
* :meth:`WalkForwardReport.stable_hash` folds in the harness comparison
  hashes for every split and is independent of the report
  ``generated_at`` timestamp.

The pipeline does not place orders, mutate feature flags, import
order-path modules, or wire the historical validation paper account.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from strategy.backtest_lab import BacktestEvent, DeterministicReplayClock, stable_hash, stable_json
from strategy.comparison_harness import (
    KNOWN_DISAGREEMENT_KINDS,
    ChampionChallengerComparison,
    ComparisonEvaluator,
    ComparisonHarness,
)


DEFAULT_SCHEDULE_ID = "wf-default"
_DATE_FORMAT = "%Y-%m-%d"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: str) -> date:
    return datetime.strptime(value, _DATE_FORMAT).date()


def _format_date(value: date) -> str:
    return value.strftime(_DATE_FORMAT)


def _event_date(event: BacktestEvent) -> str:
    """Return the ISO date (YYYY-MM-DD) portion of an event's timestamp."""
    timestamp = event.timestamp
    if len(timestamp) < 10:
        raise ValueError(f"event timestamp too short to contain a date: {timestamp!r}")
    return timestamp[:10]


@dataclass(frozen=True)
class WalkForwardSplit:
    """One in-sample / out-of-sample window pair."""

    split_id: str
    index: int
    in_sample_start: str
    in_sample_end: str
    out_of_sample_start: str
    out_of_sample_end: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.split_id:
            raise ValueError("split_id is required")
        if self.index < 0:
            raise ValueError("index cannot be negative")
        if self.in_sample_start > self.in_sample_end:
            raise ValueError("in_sample_start must be <= in_sample_end")
        if self.out_of_sample_start > self.out_of_sample_end:
            raise ValueError("out_of_sample_start must be <= out_of_sample_end")
        if self.out_of_sample_start <= self.in_sample_end:
            raise ValueError(
                "out_of_sample_start must be strictly after in_sample_end "
                f"(got is_end={self.in_sample_end}, oos_start={self.out_of_sample_start})"
            )

    def contains_out_of_sample(self, event_date: str) -> bool:
        return self.out_of_sample_start <= event_date <= self.out_of_sample_end

    def contains_in_sample(self, event_date: str) -> bool:
        return self.in_sample_start <= event_date <= self.in_sample_end

    def to_dict(self) -> Dict[str, Any]:
        return {
            "split_id": self.split_id,
            "index": self.index,
            "in_sample_start": self.in_sample_start,
            "in_sample_end": self.in_sample_end,
            "out_of_sample_start": self.out_of_sample_start,
            "out_of_sample_end": self.out_of_sample_end,
        }


@dataclass(frozen=True)
class WalkForwardSchedule:
    """Deterministic, reproducible walk-forward schedule."""

    schedule_id: str
    start_date: str
    end_date: str
    in_sample_days: int
    out_of_sample_days: int
    step_days: int
    splits: Tuple[WalkForwardSplit, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "splits", tuple(self.splits))
        self.validate()

    def validate(self) -> None:
        if not self.schedule_id:
            raise ValueError("schedule_id is required")
        if self.in_sample_days <= 0:
            raise ValueError("in_sample_days must be positive")
        if self.out_of_sample_days <= 0:
            raise ValueError("out_of_sample_days must be positive")
        if self.step_days <= 0:
            raise ValueError("step_days must be positive")
        if self.start_date > self.end_date:
            raise ValueError("start_date must be <= end_date")
        for previous, current in zip(self.splits, self.splits[1:]):
            if current.out_of_sample_start <= previous.out_of_sample_start:
                raise ValueError(
                    "splits must be strictly time-ordered by out_of_sample_start"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "in_sample_days": self.in_sample_days,
            "out_of_sample_days": self.out_of_sample_days,
            "step_days": self.step_days,
            "splits": [split.to_dict() for split in self.splits],
        }

    def stable_hash(self) -> str:
        return stable_hash(self.to_dict())


def generate_walk_forward_schedule(
    start_date: str,
    end_date: str,
    in_sample_days: int,
    out_of_sample_days: int,
    step_days: int,
    schedule_id: str = DEFAULT_SCHEDULE_ID,
) -> WalkForwardSchedule:
    """Generate a walk-forward schedule between two calendar dates.

    Splits are placed at fixed calendar-day offsets — this is a
    schedule generator, not a trading-day calendar.  Callers who need
    exchange-calendar alignment can post-process the resulting
    ``WalkForwardSchedule.splits`` before running the pipeline.
    """
    if in_sample_days <= 0:
        raise ValueError("in_sample_days must be positive")
    if out_of_sample_days <= 0:
        raise ValueError("out_of_sample_days must be positive")
    if step_days <= 0:
        raise ValueError("step_days must be positive")

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        raise ValueError("start_date must be <= end_date")

    splits: List[WalkForwardSplit] = []
    current = start
    index = 0
    while True:
        is_start = current
        is_end = is_start + timedelta(days=in_sample_days - 1)
        oos_start = is_end + timedelta(days=1)
        oos_end = oos_start + timedelta(days=out_of_sample_days - 1)
        if oos_end > end:
            break
        splits.append(
            WalkForwardSplit(
                split_id=f"{schedule_id}-{index:03d}",
                index=index,
                in_sample_start=_format_date(is_start),
                in_sample_end=_format_date(is_end),
                out_of_sample_start=_format_date(oos_start),
                out_of_sample_end=_format_date(oos_end),
            )
        )
        current = current + timedelta(days=step_days)
        index += 1

    return WalkForwardSchedule(
        schedule_id=schedule_id,
        start_date=start_date,
        end_date=end_date,
        in_sample_days=in_sample_days,
        out_of_sample_days=out_of_sample_days,
        step_days=step_days,
        splits=tuple(splits),
    )


@dataclass
class WalkForwardSplitResult:
    """Metrics and comparison output for one walk-forward split."""

    split: WalkForwardSplit
    total_events: int
    in_sample_event_count: int
    out_of_sample_event_count: int
    dropped_event_count: int
    comparison: ChampionChallengerComparison
    disagreement_counts: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "split": self.split.to_dict(),
            "total_events": self.total_events,
            "in_sample_event_count": self.in_sample_event_count,
            "out_of_sample_event_count": self.out_of_sample_event_count,
            "dropped_event_count": self.dropped_event_count,
            "comparison": self.comparison.to_dict(),
            "disagreement_counts": dict(self.disagreement_counts),
        }


@dataclass
class WalkForwardReport:
    """Aggregate report across all splits in a schedule."""

    schedule: WalkForwardSchedule
    champion_id: str
    challenger_id: str
    dataset_id: str
    seed: int
    score_delta_threshold: float
    total_events: int
    total_out_of_sample_events: int
    total_unassigned_events: int
    split_results: List[WalkForwardSplitResult]
    total_disagreement_counts: Dict[str, int]
    warnings: List[str]
    generated_at: str
    report_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "schedule": self.schedule.to_dict(),
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "dataset_id": self.dataset_id,
            "seed": self.seed,
            "score_delta_threshold": self.score_delta_threshold,
            "total_events": self.total_events,
            "total_out_of_sample_events": self.total_out_of_sample_events,
            "total_unassigned_events": self.total_unassigned_events,
            "split_results": [result.to_dict() for result in self.split_results],
            "total_disagreement_counts": dict(self.total_disagreement_counts),
            "warnings": list(self.warnings),
            "generated_at": self.generated_at,
        }

    def to_json(self) -> str:
        return stable_json(self.to_dict())

    def stable_hash(self) -> str:
        data = self.to_dict()
        data.pop("generated_at", None)
        # Nested per-split comparison metadata also carries a
        # ``generated_at`` field. Strip those before hashing so the
        # report hash is time-independent for reproducibility.
        for split_payload in data.get("split_results", []):
            comparison = split_payload.get("comparison", {})
            metadata = comparison.get("metadata", {})
            metadata.pop("generated_at", None)
        return stable_hash(data)


def _empty_disagreement_counts() -> Dict[str, int]:
    return {kind: 0 for kind in KNOWN_DISAGREEMENT_KINDS}


class WalkForwardPipeline:
    """Runs the comparison harness across an ordered set of splits.

    Champion and Challenger evaluators are supplied once and reused for
    every split.  The pipeline never places orders, mutates feature
    flags, or imports order-path modules.
    """

    def __init__(
        self,
        champion: ComparisonEvaluator,
        challenger: ComparisonEvaluator,
        score_delta_threshold: float = 0.0,
    ):
        if score_delta_threshold < 0:
            raise ValueError("score_delta_threshold cannot be negative")
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
        schedule: WalkForwardSchedule,
        events: Iterable[BacktestEvent],
        dataset_id: str = "",
        seed: int = 0,
        generated_at: Optional[str] = None,
    ) -> WalkForwardReport:
        event_list = list(events)
        warnings: List[str] = []

        oos_lookup = self._build_oos_lookup(schedule)

        split_events: Dict[int, List[BacktestEvent]] = {
            split.index: [] for split in schedule.splits
        }
        in_sample_counts: Dict[int, int] = {split.index: 0 for split in schedule.splits}
        unassigned = 0

        for event in event_list:
            event_date = _event_date(event)
            oos_index = oos_lookup.get(event_date)
            if oos_index is not None:
                split_events[oos_index].append(event)
                continue
            # Not in any OOS window — may still land in an IS window.
            in_split_is = False
            for split in schedule.splits:
                if split.contains_in_sample(event_date):
                    in_sample_counts[split.index] += 1
                    in_split_is = True
            if not in_split_is:
                unassigned += 1

        if unassigned:
            warnings.append(
                f"{unassigned} event(s) fell outside every configured window"
            )

        split_results: List[WalkForwardSplitResult] = []
        totals: Dict[str, int] = _empty_disagreement_counts()
        total_oos_events = 0

        for split in schedule.splits:
            harness = ComparisonHarness(
                champion=self._champion,
                challenger=self._challenger,
                score_delta_threshold=self._threshold,
            )
            oos_events = split_events[split.index]
            comparison = harness.run(
                DeterministicReplayClock(oos_events),
                dataset_id=dataset_id,
                seed=seed,
                generated_at=generated_at or _utc_now_iso(),
            )
            disagreement_counts = _empty_disagreement_counts()
            for record in comparison.disagreements:
                disagreement_counts[record.kind] = (
                    disagreement_counts.get(record.kind, 0) + 1
                )
            for kind, count in disagreement_counts.items():
                totals[kind] = totals.get(kind, 0) + count

            in_sample_events_in_split = in_sample_counts[split.index]
            split_results.append(
                WalkForwardSplitResult(
                    split=split,
                    total_events=len(oos_events) + in_sample_events_in_split,
                    in_sample_event_count=in_sample_events_in_split,
                    out_of_sample_event_count=len(oos_events),
                    dropped_event_count=in_sample_events_in_split,
                    comparison=comparison,
                    disagreement_counts=disagreement_counts,
                )
            )
            total_oos_events += len(oos_events)

        return WalkForwardReport(
            schedule=schedule,
            champion_id=self.champion_id,
            challenger_id=self.challenger_id,
            dataset_id=dataset_id,
            seed=seed,
            score_delta_threshold=self._threshold,
            total_events=len(event_list),
            total_out_of_sample_events=total_oos_events,
            total_unassigned_events=unassigned,
            split_results=split_results,
            total_disagreement_counts=totals,
            warnings=warnings,
            generated_at=generated_at or _utc_now_iso(),
            report_id=self._build_report_id(
                schedule=schedule,
                dataset_id=dataset_id,
                seed=seed,
                event_count=len(event_list),
            ),
        )

    # -- internals ----------------------------------------------------------

    def _build_oos_lookup(
        self, schedule: WalkForwardSchedule
    ) -> Dict[str, int]:
        """Map each date in any OOS window to its split index.

        A later split's OOS window supersedes an earlier one if they
        overlap (which happens when ``step_days < out_of_sample_days``).
        """
        lookup: Dict[str, int] = {}
        for split in schedule.splits:
            start = _parse_date(split.out_of_sample_start)
            end = _parse_date(split.out_of_sample_end)
            step = start
            while step <= end:
                lookup[_format_date(step)] = split.index
                step = step + timedelta(days=1)
        return lookup

    def _build_report_id(
        self,
        schedule: WalkForwardSchedule,
        dataset_id: str,
        seed: int,
        event_count: int,
    ) -> str:
        identity = {
            "schedule_hash": schedule.stable_hash(),
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "dataset_id": dataset_id,
            "seed": seed,
            "event_count": event_count,
            "score_delta_threshold": self._threshold,
        }
        digest = hashlib.sha256(
            stable_json(identity).encode("utf-8")
        ).hexdigest()
        return f"wf_{digest[:12]}"
