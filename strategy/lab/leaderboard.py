"""Leaderboard — Card 4.

Ranks every strategy experiment.  Score-based fields (selection
agreement, score-delta stats, disagreement rate) are computed
here from the :class:`ExperimentBundle`.  Equity-curve fields
(Sharpe, Sortino, Calmar, MaxDD, CAGR, Win%, ProfitFactor) are
:class:`Optional[float]` — Card 9 (Performance metrics) fills
them.

Deterministic: identical inputs produce identical output.
Read-only: no live trading path, no order-path references, no
credential env reads.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from strategy.lab.experiment_runner import ExperimentBundle
from strategy.lab.strategy import StrategyIdentity


DEFAULT_PRIMARY_METRIC = "sharpe"
DEFAULT_TIEBREAK_METRIC = "mean_score_delta"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


@dataclass(frozen=True)
class LeaderboardEntry:
    """One row on the leaderboard — everything a reviewer needs
    to compare two experiments at a glance.
    """

    experiment_id: str
    strategy: StrategyIdentity
    dataset_id: str
    window_start: str
    window_end: str
    # score-based (always populated from the comparison)
    total_events: int
    total_disagreements: int
    disagreement_rate: float
    mean_score_delta: float
    max_abs_score_delta: float
    selection_agreement_rate: float
    # equity-based (Card 9 backfill; None until then)
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    max_drawdown: Optional[float] = None
    cagr: Optional[float] = None
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    # provenance
    manifest_path: str = ""
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "strategy": self.strategy.to_dict(),
            "dataset_id": self.dataset_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "total_events": self.total_events,
            "total_disagreements": self.total_disagreements,
            "disagreement_rate": self.disagreement_rate,
            "mean_score_delta": self.mean_score_delta,
            "max_abs_score_delta": self.max_abs_score_delta,
            "selection_agreement_rate": self.selection_agreement_rate,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "max_drawdown": self.max_drawdown,
            "cagr": self.cagr,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "manifest_path": self.manifest_path,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Leaderboard:
    entries: Tuple[LeaderboardEntry, ...]
    primary_metric: str
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_metric": self.primary_metric,
            "generated_at": self.generated_at,
            "entry_count": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append(f"# Strategy Leaderboard — {self.primary_metric}")
        lines.append("")
        lines.append(f"_Generated: {self.generated_at}_")
        lines.append("")
        headers = [
            "Rank", "Strategy", "Dataset", "Window",
            "Sharpe", "Sortino", "MaxDD", "CAGR", "Win%", "PF",
            "SelAgree", "MeanΔ", "|Δ|max", "Disagreements",
        ]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---" for _ in headers) + "|")
        for rank, entry in enumerate(self.entries, 1):
            row = [
                str(rank),
                f"{entry.strategy.name} v{entry.strategy.version}",
                entry.dataset_id,
                f"{entry.window_start}..{entry.window_end}",
                _fmt(entry.sharpe),
                _fmt(entry.sortino),
                _fmt(entry.max_drawdown),
                _fmt(entry.cagr),
                _fmt(entry.win_rate),
                _fmt(entry.profit_factor),
                f"{entry.selection_agreement_rate:.3f}",
                f"{entry.mean_score_delta:+.4f}",
                f"{entry.max_abs_score_delta:.4f}",
                f"{entry.total_disagreements}/{entry.total_events}",
            ]
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines) + "\n"


def _fmt(value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:.4f}"


# ---------------------------------------------------------------------------
# Score-based metric computation
# ---------------------------------------------------------------------------


def compute_score_metrics(bundle: ExperimentBundle) -> Dict[str, Any]:
    """Compute the score-side leaderboard metrics from a bundle.

    Returned dict: ``total_events``, ``total_disagreements``,
    ``disagreement_rate``, ``mean_score_delta``,
    ``max_abs_score_delta``, ``selection_agreement_rate``.

    Deterministic: identical bundle -> identical result.
    """
    comparison = bundle.validation_bundle.comparison
    tables = list(comparison.score_tables)
    disagreements = list(comparison.disagreements)

    total_events = len(tables)
    total_disagreements = len(disagreements)

    # Selection agreement: fraction of (event, symbol) rows where
    # champion_selected == challenger_selected.
    agree_rows = 0
    total_rows = 0
    for table in tables:
        for row in table.rows:
            total_rows += 1
            if row.champion_selected == row.challenger_selected:
                agree_rows += 1
    selection_agreement_rate = (
        agree_rows / total_rows if total_rows else 1.0
    )

    # Score delta stats (over disagreements only — rows with
    # score_delta == None are omitted).
    deltas: List[float] = []
    for d in disagreements:
        if isinstance(d.score_delta, (int, float)):
            deltas.append(float(d.score_delta))
    mean_score_delta = sum(deltas) / len(deltas) if deltas else 0.0
    max_abs = max((abs(x) for x in deltas), default=0.0)

    # disagreement_rate is per-ROW (matches learning-report
    # convention: fraction of (event, symbol) cells with a
    # disagreement), not per-event — a single event can carry
    # multiple disagreements.
    disagreement_rate = (
        total_disagreements / total_rows if total_rows else 0.0
    )

    return {
        "total_events": total_events,
        "total_disagreements": total_disagreements,
        "disagreement_rate": disagreement_rate,
        "mean_score_delta": mean_score_delta,
        "max_abs_score_delta": max_abs,
        "selection_agreement_rate": selection_agreement_rate,
    }


def entry_from_bundle(
    bundle: ExperimentBundle,
    *,
    equity_metrics: Optional[Mapping[str, Optional[float]]] = None,
) -> LeaderboardEntry:
    """Build a leaderboard entry from an experiment bundle.

    ``equity_metrics`` optionally supplies Card 9 metric values
    (``sharpe``, ``sortino``, ``calmar``, ``max_drawdown``,
    ``cagr``, ``win_rate``, ``profit_factor``); missing keys stay
    ``None``.
    """
    score_metrics = compute_score_metrics(bundle)
    eq = dict(equity_metrics or {})
    return LeaderboardEntry(
        experiment_id=bundle.manifest.experiment_id,
        strategy=bundle.manifest.strategy,
        dataset_id=bundle.manifest.dataset_id,
        window_start=bundle.manifest.window_start,
        window_end=bundle.manifest.window_end,
        total_events=score_metrics["total_events"],
        total_disagreements=score_metrics["total_disagreements"],
        disagreement_rate=score_metrics["disagreement_rate"],
        mean_score_delta=score_metrics["mean_score_delta"],
        max_abs_score_delta=score_metrics["max_abs_score_delta"],
        selection_agreement_rate=score_metrics["selection_agreement_rate"],
        sharpe=eq.get("sharpe"),
        sortino=eq.get("sortino"),
        calmar=eq.get("calmar"),
        max_drawdown=eq.get("max_drawdown"),
        cagr=eq.get("cagr"),
        win_rate=eq.get("win_rate"),
        profit_factor=eq.get("profit_factor"),
        manifest_path=bundle.manifest_path,
        warnings=tuple(bundle.manifest.warnings),
    )


# ---------------------------------------------------------------------------
# Ranking + IO
# ---------------------------------------------------------------------------


def _sort_key_for(metric: str):
    """Return a callable that extracts the sort key for one entry.

    Higher-is-better metrics (Sharpe, Sortino, Calmar, CAGR,
    win_rate, profit_factor, selection_agreement_rate) sort
    descending.  ``mean_score_delta`` and ``max_abs_score_delta``
    sort by magnitude ascending (smaller |delta| = closer to
    champion, more decision-neutral).  ``max_drawdown`` sorts
    ascending (smaller drawdown = better).  ``disagreement_rate``
    ascending.
    """

    def _extract(entry: LeaderboardEntry) -> Tuple[float, str]:
        value = getattr(entry, metric, None)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return (float("inf"), entry.experiment_id)
        higher_is_better = metric in {
            "sharpe", "sortino", "calmar", "cagr", "win_rate",
            "profit_factor", "selection_agreement_rate",
        }
        if higher_is_better:
            return (-float(value), entry.experiment_id)
        return (float(value), entry.experiment_id)

    return _extract


def rank_leaderboard(
    entries: Sequence[LeaderboardEntry],
    *,
    primary_metric: str = DEFAULT_PRIMARY_METRIC,
    generated_at: Optional[str] = None,
) -> Leaderboard:
    if not primary_metric:
        raise ValueError("primary_metric must be non-empty")
    ordered = sorted(entries, key=_sort_key_for(primary_metric))
    return Leaderboard(
        entries=tuple(ordered),
        primary_metric=primary_metric,
        generated_at=generated_at or _utc_now_iso(),
    )


def write_leaderboard(
    leaderboard: Leaderboard,
    output_dir: Path,
    *,
    filename: str = "leaderboard",
) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{filename}.json"
    md_path = output_dir / f"{filename}.md"
    json_path.write_text(leaderboard.to_json(), encoding="utf-8")
    md_path.write_text(leaderboard.to_markdown(), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


__all__ = [
    "DEFAULT_PRIMARY_METRIC",
    "Leaderboard",
    "LeaderboardEntry",
    "compute_score_metrics",
    "entry_from_bundle",
    "rank_leaderboard",
    "write_leaderboard",
]
