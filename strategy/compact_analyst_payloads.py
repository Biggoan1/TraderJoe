"""Compact Research Analyst payload builder.

Reduces prompt size and inference latency by sending compact
structured summaries to the local LLM instead of embedding full
artifact bodies.  The full artifacts remain on disk; the compact
payload cites them by id and path.

Design goals:

* Deterministic — identical inputs produce byte-identical output.
* Bounded — every compact payload respects the configured limits
  (``max_disagreements``, ``max_findings``, ``max_prompt_chars``,
  ``max_output_tokens``).
* Truncation is deterministic and marked with a warning so the
  analyst can flag its own answer.
* Never embeds a full artifact body; only summaries + artifact
  ids + paths.
* Forbidden-output detection continues to run downstream since
  the LLM's output surface is unchanged.

Read-only: no live-runner imports, no order-path tokens, no
credential env reads, no ``ApprovalRecord`` construction.  The
payload is a research artifact, not a promotion signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence


DEFAULT_MAX_DISAGREEMENTS = 20
DEFAULT_MAX_FINDINGS = 20
DEFAULT_MAX_PROMPT_CHARS = 20_000
DEFAULT_MAX_OUTPUT_TOKENS = 1_200
DEFAULT_MAX_WARNING_SAMPLE = 5


@dataclass(frozen=True)
class CompactPayloadLimits:
    """Configurable size limits for the compact payload."""

    max_disagreements: int = DEFAULT_MAX_DISAGREEMENTS
    max_findings: int = DEFAULT_MAX_FINDINGS
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_warning_sample: int = DEFAULT_MAX_WARNING_SAMPLE

    def __post_init__(self) -> None:
        for name in (
            "max_disagreements",
            "max_findings",
            "max_prompt_chars",
            "max_output_tokens",
            "max_warning_sample",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_disagreements": self.max_disagreements,
            "max_findings": self.max_findings,
            "max_prompt_chars": self.max_prompt_chars,
            "max_output_tokens": self.max_output_tokens,
            "max_warning_sample": self.max_warning_sample,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize_warnings(
    warnings: Sequence[str], limit: int
) -> Dict[str, Any]:
    if not warnings:
        return {"total": 0, "unique": 0, "sample": []}
    unique = sorted(set(warnings))
    return {
        "total": len(warnings),
        "unique": len(unique),
        "sample": unique[:limit],
    }


def _top_disagreements(
    disagreements: Sequence[Mapping[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    def _sort_key(d: Mapping[str, Any]):
        # Deterministic: highest absolute score_delta first, then
        # lexical (event_timestamp, symbol) for ties.
        score = d.get("score_delta")
        magnitude = abs(float(score)) if isinstance(score, (int, float)) else 0.0
        return (
            -magnitude,
            str(d.get("event_timestamp", "")),
            str(d.get("symbol", "")),
        )

    items = sorted(disagreements, key=_sort_key)[:limit]
    trimmed: List[Dict[str, Any]] = []
    for d in items:
        trimmed.append(
            {
                "event_timestamp": d.get("event_timestamp"),
                "symbol": d.get("symbol"),
                "kind": d.get("kind"),
                "champion_score": d.get("champion_score"),
                "challenger_score": d.get("challenger_score"),
                "score_delta": d.get("score_delta"),
                "rank_delta": d.get("rank_delta"),
                "champion_explanation": d.get("champion_explanation") or "",
                "challenger_explanation": d.get("challenger_explanation") or "",
            }
        )
    return trimmed


def _summarize_findings(
    findings: Sequence[Mapping[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    items = sorted(findings, key=lambda f: (
        -float(f.get("sample_size") or 0),
        str(f.get("metric", "")),
    ))[:limit]
    return [
        {
            "metric": f.get("metric"),
            "label": f.get("label"),
            "effect_size": f.get("effect_size"),
            "sample_size": f.get("sample_size"),
            "ci_low": f.get("ci_low"),
            "ci_high": f.get("ci_high"),
            "detail": f.get("detail", ""),
        }
        for f in items
    ]


def _walk_forward_summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
    summary = payload.get("summary") or {}
    return {
        "report_id": payload.get("report_id"),
        "split_count": summary.get("split_count"),
        "total_events": summary.get("total_events"),
        "total_out_of_sample_events": summary.get(
            "total_out_of_sample_events"
        ),
        "disagreement_counts": summary.get("disagreement_counts"),
        "total_disagreements": summary.get("total_disagreements"),
        "in_sample_days": summary.get("in_sample_days"),
        "out_of_sample_days": summary.get("out_of_sample_days"),
        "score_delta_threshold": summary.get("score_delta_threshold"),
        "warnings": summary.get("warnings", []),
    }


def _promotion_evidence_summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
    entry = payload.get("promotion_entry") or {}
    evidence = entry.get("evidence") or {}
    return {
        "flag_name": entry.get("flag_name"),
        "current_state": entry.get("current_state"),
        "approvals": len(entry.get("approvals") or []),
        "evidence_keys": sorted(evidence.keys()),
        "dataset_provenance_id": evidence.get("dataset_provenance_id"),
        "notes": entry.get("notes", ""),
    }


# ---------------------------------------------------------------------------
# Compact payloads per artifact
# ---------------------------------------------------------------------------


def compact_comparison_payload(
    full_payload: Mapping[str, Any],
    artifact_id: str,
    artifact_path: str = "",
    limits: Optional[CompactPayloadLimits] = None,
) -> Dict[str, Any]:
    """Compact representation of a ``ChampionChallengerComparison``.

    ``full_payload`` is the dict returned by
    :meth:`ChampionChallengerComparison.to_analyst_payload` (or
    ``to_dict``).  The compact payload cites the artifact by id
    instead of embedding score tables.
    """
    limits = limits or CompactPayloadLimits()
    metadata = dict(full_payload.get("metadata") or {})
    disagreements = list(full_payload.get("disagreements") or [])
    warnings = list(full_payload.get("warnings") or [])
    provenance = full_payload.get("dataset_provenance")
    compact: Dict[str, Any] = {
        "kind": "champion_challenger_comparison_compact",
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
        "metadata": metadata,
        "total_disagreements": len(disagreements),
        "top_disagreements": _top_disagreements(
            disagreements, limits.max_disagreements
        ),
        "warnings": _summarize_warnings(
            warnings, limits.max_warning_sample
        ),
    }
    if provenance:
        compact["dataset_provenance"] = dict(provenance)
    return compact


def compact_walk_forward_payload(
    full_payload: Mapping[str, Any],
    artifact_id: str,
    artifact_path: str = "",
    limits: Optional[CompactPayloadLimits] = None,
) -> Dict[str, Any]:
    limits = limits or CompactPayloadLimits()
    disagreements: List[Mapping[str, Any]] = []
    for split in full_payload.get("split_results", []) or []:
        for d in (split.get("comparison") or {}).get(
            "disagreements", []
        ) or []:
            disagreements.append(d)
    compact: Dict[str, Any] = {
        "kind": "walk_forward_report_compact",
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
        "summary": _walk_forward_summary(full_payload),
        "top_disagreements": _top_disagreements(
            disagreements, limits.max_disagreements
        ),
    }
    provenance = full_payload.get("dataset_provenance")
    if provenance:
        compact["dataset_provenance"] = dict(provenance)
    return compact


def compact_learning_payload(
    full_payload: Mapping[str, Any],
    artifact_id: str,
    artifact_path: str = "",
    limits: Optional[CompactPayloadLimits] = None,
) -> Dict[str, Any]:
    limits = limits or CompactPayloadLimits()
    findings = list(full_payload.get("statistical_findings") or [])
    summary = full_payload.get("summary") or {}
    compact: Dict[str, Any] = {
        "kind": "learning_report_compact",
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
        "title": full_payload.get("title", ""),
        "summary": {
            "finding_count": summary.get("finding_count"),
            "hypothesis_count": summary.get("hypothesis_count"),
            "importance_count": summary.get("importance_count"),
            "recommendation_count": summary.get("recommendation_count"),
            "finding_labels": summary.get("finding_labels"),
        },
        "top_findings": _summarize_findings(
            findings, limits.max_findings
        ),
    }
    if "explanation_summary" in full_payload:
        compact["explanation_summary"] = full_payload["explanation_summary"]
    return compact


def compact_bundle_payload(
    bundle_dict: Mapping[str, Any],
    limits: Optional[CompactPayloadLimits] = None,
) -> Dict[str, Any]:
    """Compact summary of an entire
    :class:`~strategy.historical_validation.HistoricalValidationBundle`
    (via its ``to_dict``).  Includes artifact ids/paths and the
    promotion evidence summary.
    """
    limits = limits or CompactPayloadLimits()
    return {
        "kind": "historical_validation_bundle_compact",
        "config_hash": bundle_dict.get("config_hash"),
        "dataset_manifest_path": bundle_dict.get("dataset_manifest_path"),
        "comparison_report_id": bundle_dict.get("comparison_report_id"),
        "walk_forward_report_id": bundle_dict.get("walk_forward_report_id"),
        "walk_forward_research_report_id": bundle_dict.get(
            "walk_forward_research_report_id"
        ),
        "learning_report_id": bundle_dict.get("learning_report_id"),
        "analyst_report_ids": list(
            bundle_dict.get("analyst_report_ids") or []
        ),
        "warnings": _summarize_warnings(
            list(bundle_dict.get("warnings") or []),
            limits.max_warning_sample,
        ),
        "live_fetch_used": bundle_dict.get("live_fetch_used"),
        "dataset_provenance": dict(bundle_dict.get("dataset_provenance") or {}),
        "promotion_evidence": _promotion_evidence_summary(bundle_dict),
        "generated_at": bundle_dict.get("generated_at"),
    }


# ---------------------------------------------------------------------------
# Truncation / size enforcement
# ---------------------------------------------------------------------------


def enforce_prompt_size(
    payload: Dict[str, Any],
    limits: CompactPayloadLimits,
) -> Dict[str, Any]:
    """Deterministically shrink ``payload`` until its JSON
    representation fits within ``limits.max_prompt_chars``.

    Truncation rule: repeatedly halve ``top_disagreements`` /
    ``top_findings`` in a fixed order until the payload fits (or
    those lists are empty).  If the payload still exceeds the
    limit, the caller sees a ``truncation`` warning describing what
    was dropped.  This never changes the artifact ids or the
    ``dataset_provenance`` — those stay authoritative.
    """
    working = json.loads(json.dumps(payload, sort_keys=True))
    truncation_notes: List[str] = []
    fields = ("top_disagreements", "top_findings")

    def _size() -> int:
        return len(json.dumps(working, sort_keys=True))

    while _size() > limits.max_prompt_chars:
        shrank = False
        for name in fields:
            items = working.get(name)
            if isinstance(items, list) and len(items) > 1:
                new_len = max(1, len(items) // 2)
                truncation_notes.append(
                    f"{name} truncated {len(items)}→{new_len}"
                )
                working[name] = items[:new_len]
                shrank = True
                break
        if not shrank:
            # Nothing left to shrink safely
            break

    if truncation_notes:
        warnings = working.setdefault("truncation_warnings", [])
        warnings.extend(truncation_notes)

    return working


# ---------------------------------------------------------------------------
# Wrapper for ResearchAnalyst
# ---------------------------------------------------------------------------


class CompactAnalystSource:
    """Wraps a comparison / walk-forward / learning artifact so
    ``ResearchAnalyst.analyze_*`` sends a compact payload instead
    of the full body.

    Delegates every attribute (``metadata``, ``stable_hash``,
    ``report_id``, etc.) to the wrapped source, but replaces
    ``to_analyst_payload`` / ``to_dict`` with the compact version.
    """

    def __init__(
        self,
        source: Any,
        kind: str,
        artifact_id: str,
        artifact_path: str = "",
        limits: Optional[CompactPayloadLimits] = None,
    ) -> None:
        self._source = source
        self._kind = kind
        self._artifact_id = artifact_id
        self._artifact_path = artifact_path
        self._limits = limits or CompactPayloadLimits()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def _base_payload(self) -> Dict[str, Any]:
        # Prefer to_analyst_payload, then to_dict, then .payload —
        # LearningReport carries its data on a bare .payload attribute
        # rather than a serialisation method.
        payload_fn = getattr(self._source, "to_analyst_payload", None)
        if callable(payload_fn):
            return payload_fn()
        payload_fn = getattr(self._source, "to_dict", None)
        if callable(payload_fn):
            return payload_fn()
        payload = getattr(self._source, "payload", None)
        if payload is not None:
            return dict(payload)
        raise AttributeError(
            f"CompactAnalystSource: wrapped source "
            f"{type(self._source).__name__} has no to_analyst_payload, "
            f"to_dict, or payload attribute"
        )

    def to_analyst_payload(self) -> Dict[str, Any]:
        full = self._base_payload()
        if self._kind == "comparison":
            compact = compact_comparison_payload(
                full, self._artifact_id, self._artifact_path,
                self._limits,
            )
        elif self._kind == "walk_forward":
            compact = compact_walk_forward_payload(
                full, self._artifact_id, self._artifact_path,
                self._limits,
            )
        elif self._kind == "learning":
            compact = compact_learning_payload(
                full, self._artifact_id, self._artifact_path,
                self._limits,
            )
        else:
            raise ValueError(f"unknown compact kind: {self._kind!r}")
        return enforce_prompt_size(compact, self._limits)

    def to_dict(self) -> Dict[str, Any]:
        return self.to_analyst_payload()


__all__ = [
    "DEFAULT_MAX_DISAGREEMENTS",
    "DEFAULT_MAX_FINDINGS",
    "DEFAULT_MAX_PROMPT_CHARS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "CompactAnalystSource",
    "CompactPayloadLimits",
    "compact_bundle_payload",
    "compact_comparison_payload",
    "compact_learning_payload",
    "compact_walk_forward_payload",
    "enforce_prompt_size",
]
