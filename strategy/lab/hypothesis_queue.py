"""Hypothesis queue — Card 7.

Research proposals emitted by the analyst (or a human) are
appended here as :class:`ExperimentProposal` records.  The queue
NEVER auto-runs anything.  A reviewer explicitly promotes a
proposal to ``approved`` before an operator ever considers
turning it into an experiment.

Backing store: JSONL under ``reports/hypothesis_queue/*.jsonl``,
one record per line for append safety.  A deterministic
``proposal_id`` (sha256 over slug + strategy_name + suggested
parameters) means the same idea proposed twice is stored once.

Read-only against everything else in the system: no live trading
path, no order-path references, no credential env reads.
Approving a proposal never advances a PromotionEntry — the queue
is a research inbox, not a promotion gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_QUEUED = "queued"
STATUS_ARCHIVED = "archived"

KNOWN_STATUSES = frozenset({
    STATUS_PROPOSED,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_QUEUED,
    STATUS_ARCHIVED,
})

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = _SLUG_PATTERN.sub("-", text)
    return text.strip("-") or "untitled"


def compute_proposal_id(
    title: str,
    strategy_name: str,
    suggested_parameters: Mapping[str, Any],
) -> str:
    """Deterministic proposal id — sha256 over the identity block.
    Two proposals with the same title + strategy + parameters hash
    to the same id.  Notes / confidence / motivation do NOT
    influence the id so a revised description doesn't multiply
    entries.
    """
    payload = {
        "title": title.strip().lower(),
        "strategy_name": strategy_name.strip().lower(),
        "suggested_parameters": _canonicalize(suggested_parameters),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"hyp_{digest[:16]}"


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class HypothesisQueueError(RuntimeError):
    """Base class for queue errors."""


@dataclass(frozen=True)
class ExperimentProposal:
    """One research idea awaiting review.

    * ``title`` — short human label
    * ``strategy_name`` — the registered strategy this proposal
      would sweep or reparameterise (e.g. ``"champion_rs"``)
    * ``suggested_parameters`` — parameter overrides / grid axes
      the analyst suggests exploring
    * ``motivation`` — short rationale in the analyst's own words
    * ``confidence`` — analyst-reported confidence in ``[0, 1]``
    * ``conditions`` — dict describing when the parameter change
      would apply (e.g. ``{"VIX_gt": 25}``)
    * ``source`` — where the proposal came from (analyst
      narrative id, human name, etc.)
    * ``status`` — one of ``KNOWN_STATUSES``
    * ``created_at`` / ``updated_at`` — ISO timestamps
    """

    proposal_id: str
    title: str
    strategy_name: str
    suggested_parameters: Mapping[str, Any]
    motivation: str = ""
    confidence: float = 0.5
    conditions: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    status: str = STATUS_PROPOSED
    created_at: str = ""
    updated_at: str = ""
    review_notes: str = ""

    def __post_init__(self) -> None:
        if not self.title or not self.title.strip():
            raise HypothesisQueueError("title must be non-empty")
        if not self.strategy_name or not self.strategy_name.strip():
            raise HypothesisQueueError("strategy_name must be non-empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise HypothesisQueueError(
                f"confidence must be in [0, 1]; got {self.confidence!r}"
            )
        if self.status not in KNOWN_STATUSES:
            raise HypothesisQueueError(
                f"unknown status {self.status!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "strategy_name": self.strategy_name,
            "suggested_parameters": _canonicalize(self.suggested_parameters),
            "motivation": self.motivation,
            "confidence": float(self.confidence),
            "conditions": _canonicalize(self.conditions),
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "review_notes": self.review_notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentProposal":
        return cls(
            proposal_id=str(data["proposal_id"]),
            title=str(data["title"]),
            strategy_name=str(data["strategy_name"]),
            suggested_parameters=dict(data.get("suggested_parameters", {})),
            motivation=str(data.get("motivation", "")),
            confidence=float(data.get("confidence", 0.5)),
            conditions=dict(data.get("conditions", {})),
            source=str(data.get("source", "")),
            status=str(data.get("status", STATUS_PROPOSED)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            review_notes=str(data.get("review_notes", "")),
        )


# ---------------------------------------------------------------------------
# Persistent queue
# ---------------------------------------------------------------------------


class HypothesisQueue:
    """JSONL-backed hypothesis queue.

    Records are stored one-per-line.  ``propose`` and ``update``
    rewrite the file atomically (temp file + rename) to guarantee
    the on-disk view is always a valid JSONL.

    Thread-safe within a single process; concurrent processes
    should serialise via a file lock (not implemented here — this
    class is designed for the single-writer weekend-lab path).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _read_all(self) -> List[ExperimentProposal]:
        if not self.path.is_file():
            return []
        out: List[ExperimentProposal] = []
        for i, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines()
        ):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(ExperimentProposal.from_dict(json.loads(line)))
            except Exception as exc:  # noqa: BLE001
                raise HypothesisQueueError(
                    f"line {i + 1} in {self.path} is not a valid "
                    f"ExperimentProposal: {exc}"
                ) from exc
        return out

    def _write_all(self, proposals: Sequence[ExperimentProposal]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for p in proposals:
                fh.write(p.to_json())
                fh.write("\n")
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def propose(
        self,
        *,
        title: str,
        strategy_name: str,
        suggested_parameters: Mapping[str, Any],
        motivation: str = "",
        confidence: float = 0.5,
        conditions: Optional[Mapping[str, Any]] = None,
        source: str = "",
        now_iso: Optional[str] = None,
    ) -> ExperimentProposal:
        """Append a new proposal.  Idempotent by proposal_id:
        proposing the same identity block twice returns the
        existing record without mutation.
        """
        now = now_iso or _utc_now_iso()
        pid = compute_proposal_id(title, strategy_name, suggested_parameters)
        with self._lock:
            proposals = self._read_all()
            for existing in proposals:
                if existing.proposal_id == pid:
                    return existing
            proposal = ExperimentProposal(
                proposal_id=pid,
                title=title,
                strategy_name=strategy_name,
                suggested_parameters=dict(suggested_parameters),
                motivation=motivation,
                confidence=confidence,
                conditions=dict(conditions or {}),
                source=source,
                status=STATUS_PROPOSED,
                created_at=now,
                updated_at=now,
            )
            proposals.append(proposal)
            self._write_all(proposals)
            return proposal

    def list(
        self,
        status: Optional[str] = None,
    ) -> List[ExperimentProposal]:
        with self._lock:
            proposals = self._read_all()
        if status is None:
            return proposals
        if status not in KNOWN_STATUSES:
            raise HypothesisQueueError(f"unknown status {status!r}")
        return [p for p in proposals if p.status == status]

    def get(self, proposal_id: str) -> ExperimentProposal:
        with self._lock:
            for p in self._read_all():
                if p.proposal_id == proposal_id:
                    return p
        raise HypothesisQueueError(f"proposal {proposal_id!r} not found")

    def update_status(
        self,
        proposal_id: str,
        status: str,
        *,
        review_notes: str = "",
        now_iso: Optional[str] = None,
    ) -> ExperimentProposal:
        """Set the status of an existing proposal.  The queue
        never auto-implements approved proposals — approval is a
        research signal, not a promotion event.
        """
        if status not in KNOWN_STATUSES:
            raise HypothesisQueueError(f"unknown status {status!r}")
        now = now_iso or _utc_now_iso()
        with self._lock:
            proposals = self._read_all()
            for i, p in enumerate(proposals):
                if p.proposal_id == proposal_id:
                    updated = ExperimentProposal(
                        proposal_id=p.proposal_id,
                        title=p.title,
                        strategy_name=p.strategy_name,
                        suggested_parameters=dict(p.suggested_parameters),
                        motivation=p.motivation,
                        confidence=p.confidence,
                        conditions=dict(p.conditions),
                        source=p.source,
                        status=status,
                        created_at=p.created_at,
                        updated_at=now,
                        review_notes=review_notes,
                    )
                    proposals[i] = updated
                    self._write_all(proposals)
                    return updated
        raise HypothesisQueueError(f"proposal {proposal_id!r} not found")


__all__ = [
    "ExperimentProposal",
    "HypothesisQueue",
    "HypothesisQueueError",
    "KNOWN_STATUSES",
    "STATUS_APPROVED",
    "STATUS_ARCHIVED",
    "STATUS_PROPOSED",
    "STATUS_QUEUED",
    "STATUS_REJECTED",
    "compute_proposal_id",
]
