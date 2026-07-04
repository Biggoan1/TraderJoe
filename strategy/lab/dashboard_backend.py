"""Dashboard backend — Card 8.

Read-only JSON API over the artifact tree produced by Cards 1–7 +
9.  Endpoints are plain Python functions returning JSON-ready
dicts — a follow-up card can wire them behind an HTTP framework
(FastAPI etc.) with no changes required here.

Every endpoint:

* is a ``GET``-shaped read.  There are no mutations.
* returns a ``{"data": ..., "warnings": [...],
  "generated_at": "..."}`` envelope.
* returns ``{"error": "not_found", "message": "..."}`` when the
  requested artifact is missing.
* is deterministic given a fixed on-disk tree.

Read-only guarantees enforced by source-safety tests: no
live-runner imports, no order-path references, no credential env
reads, no ``ApprovalRecord`` construction, no ``PromotionEntry``
mutation.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


DEFAULT_LAB_ROOT = "reports/weekend_lab"
DEFAULT_EXPERIMENT_ROOT = "reports/experiments"
DEFAULT_HYPOTHESIS_ROOT = "reports/hypothesis_queue"
DEFAULT_WAREHOUSE_MANIFEST_ROOT = "market_data/manifests"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def _envelope(
    data: Any,
    warnings: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    return {
        "data": data,
        "warnings": list(warnings or []),
        "generated_at": _utc_now_iso(),
    }


def _not_found(message: str) -> Dict[str, Any]:
    return {"error": "not_found", "message": message}


def _load_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _pagination(
    items: Sequence[Any],
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Sequence[Any]:
    limit = max(0, min(MAX_LIMIT, int(limit)))
    offset = max(0, int(offset))
    return items[offset:offset + limit]


# ---------------------------------------------------------------------------
# Weekend lab manifests
# ---------------------------------------------------------------------------


def list_weekend_runs(
    lab_root: str = DEFAULT_LAB_ROOT,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Dict[str, Any]:
    """List every weekend-lab run under ``lab_root``."""
    root = Path(lab_root)
    if not root.is_dir():
        return _envelope([], warnings=[f"lab_root {lab_root!r} missing"])
    runs: List[Dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        payload = _load_json_safe(manifest_path)
        if payload is None:
            continue
        runs.append({
            "run_id": payload.get("run_id"),
            "generated_at": payload.get("generated_at"),
            "dataset_id": payload.get("dataset_id"),
            "window_start": payload.get("window_start"),
            "window_end": payload.get("window_end"),
            "matrix_count": len(payload.get("matrix_experiment_ids") or []),
            "sweep_count": len(payload.get("sweep_ids") or []),
            "leaderboard_path": payload.get("leaderboard_path"),
            "dashboard_path": payload.get("dashboard_path"),
        })
    # Newest first (by generated_at, alphabetic sort works for ISO)
    runs.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return _envelope(_pagination(runs, limit=limit, offset=offset))


def latest_weekend_run(
    lab_root: str = DEFAULT_LAB_ROOT,
) -> Dict[str, Any]:
    listing = list_weekend_runs(lab_root, limit=1)
    if not listing.get("data"):
        return _not_found(f"no weekend runs found under {lab_root!r}")
    return _envelope(listing["data"][0])


def weekend_run_detail(
    run_id: str,
    lab_root: str = DEFAULT_LAB_ROOT,
) -> Dict[str, Any]:
    manifest_path = Path(lab_root) / run_id / "manifest.json"
    payload = _load_json_safe(manifest_path)
    if payload is None:
        return _not_found(f"weekend run {run_id!r} not found")
    return _envelope(payload)


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


def leaderboard(
    run_id: str,
    lab_root: str = DEFAULT_LAB_ROOT,
) -> Dict[str, Any]:
    path = Path(lab_root) / run_id / "leaderboard" / "leaderboard.json"
    payload = _load_json_safe(path)
    if payload is None:
        return _not_found(f"leaderboard for {run_id!r} not found")
    return _envelope(payload)


def latest_leaderboard(
    lab_root: str = DEFAULT_LAB_ROOT,
) -> Dict[str, Any]:
    latest = latest_weekend_run(lab_root)
    if latest.get("error"):
        return latest
    run_id = latest["data"]["run_id"]
    return leaderboard(run_id, lab_root)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def list_experiments(
    experiment_root: str = DEFAULT_EXPERIMENT_ROOT,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Dict[str, Any]:
    """Walk ``experiment_root`` and list every experiment manifest.
    """
    root = Path(experiment_root)
    if not root.is_dir():
        return _envelope([], warnings=[f"experiment_root {experiment_root!r} missing"])
    entries: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        payload = _load_json_safe(path)
        if payload is None or "experiment_id" not in payload:
            continue
        entries.append({
            "experiment_id": payload.get("experiment_id"),
            "dataset_id": payload.get("dataset_id"),
            "strategy": (payload.get("strategy") or {}).get("name"),
            "strategy_version": (payload.get("strategy") or {}).get("version"),
            "window_start": payload.get("window_start"),
            "window_end": payload.get("window_end"),
            "generated_at": payload.get("generated_at"),
            "manifest_path": str(path),
        })
    entries.sort(key=lambda e: e.get("generated_at") or "", reverse=True)
    return _envelope(_pagination(entries, limit=limit, offset=offset))


def experiment_detail(
    experiment_id: str,
    experiment_root: str = DEFAULT_EXPERIMENT_ROOT,
) -> Dict[str, Any]:
    root = Path(experiment_root)
    if not root.is_dir():
        return _not_found(f"experiment_root {experiment_root!r} not present")
    for path in root.rglob("*.json"):
        payload = _load_json_safe(path)
        if payload and payload.get("experiment_id") == experiment_id:
            return _envelope(payload)
    return _not_found(f"experiment {experiment_id!r} not found")


# ---------------------------------------------------------------------------
# Strategy rankings — folded from latest leaderboard
# ---------------------------------------------------------------------------


def strategy_rankings(
    lab_root: str = DEFAULT_LAB_ROOT,
) -> Dict[str, Any]:
    """Aggregated ranking: for every strategy name, the best
    (lowest-ranked, highest-scored) leaderboard entry from the
    most recent weekend run.  Deterministic.
    """
    lb = latest_leaderboard(lab_root)
    if lb.get("error"):
        return lb
    payload = lb["data"]
    entries = list(payload.get("entries", []))
    best_by_name: Dict[str, Dict[str, Any]] = {}
    for i, entry in enumerate(entries):
        name = (entry.get("strategy") or {}).get("name", "")
        if not name:
            continue
        if name not in best_by_name:
            best_by_name[name] = {"rank": i + 1, "entry": entry}
    ranked = sorted(
        best_by_name.values(),
        key=lambda x: x["rank"],
    )
    return _envelope({
        "primary_metric": payload.get("primary_metric"),
        "run_id": lb["data"].get("run_id"),
        "strategies": ranked,
    })


# ---------------------------------------------------------------------------
# Top parameter sets — from a sweep manifest
# ---------------------------------------------------------------------------


def top_parameter_sets(
    run_id: str,
    strategy_name: str,
    lab_root: str = DEFAULT_LAB_ROOT,
    *,
    limit: int = 10,
) -> Dict[str, Any]:
    sweep_path = (
        Path(lab_root) / run_id / "sweeps"
        / f"sweep_{strategy_name}" / "sweep.json"
    )
    payload = _load_json_safe(sweep_path)
    if payload is None:
        return _not_found(
            f"sweep for strategy {strategy_name!r} in run {run_id!r} not found"
        )
    rows = [row for row in payload.get("rows", []) if row.get("ok")]
    # No cross-row metric yet; just return them in combo order.
    return _envelope({
        "strategy_name": strategy_name,
        "run_id": run_id,
        "grid": payload.get("grid", {}),
        "rows": _pagination(rows, limit=limit, offset=0),
        "total": len(rows),
    })


# ---------------------------------------------------------------------------
# Analyst summaries
# ---------------------------------------------------------------------------


def analyst_summaries(
    experiment_root: str = DEFAULT_EXPERIMENT_ROOT,
    *,
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    root = Path(experiment_root)
    if not root.is_dir():
        return _envelope([], warnings=[f"experiment_root {experiment_root!r} missing"])
    entries: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        payload = _load_json_safe(path)
        if payload is None:
            continue
        analyst_ids = payload.get("analyst_report_ids") or []
        if not analyst_ids:
            continue
        entries.append({
            "experiment_id": payload.get("experiment_id"),
            "strategy": (payload.get("strategy") or {}).get("name"),
            "analyst_report_ids": analyst_ids,
            "generated_at": payload.get("generated_at"),
        })
    return _envelope(_pagination(entries, limit=limit, offset=0))


# ---------------------------------------------------------------------------
# Dataset provenance
# ---------------------------------------------------------------------------


def dataset_provenance(
    experiment_id: str,
    experiment_root: str = DEFAULT_EXPERIMENT_ROOT,
) -> Dict[str, Any]:
    detail = experiment_detail(experiment_id, experiment_root)
    if detail.get("error"):
        return detail
    payload = detail["data"]
    return _envelope({
        "experiment_id": payload.get("experiment_id"),
        "dataset_id": payload.get("dataset_id"),
        "dataset_provenance": payload.get("dataset_provenance", {}),
        "live_fetch_used": payload.get("live_fetch_used", False),
    })


# ---------------------------------------------------------------------------
# Warehouse status
# ---------------------------------------------------------------------------


def warehouse_status(
    warehouse_manifest_root: str = DEFAULT_WAREHOUSE_MANIFEST_ROOT,
) -> Dict[str, Any]:
    root = Path(warehouse_manifest_root)
    if not root.is_dir():
        return _envelope(
            {"manifests": [], "total": 0},
            warnings=[f"warehouse_manifest_root {warehouse_manifest_root!r} missing"],
        )
    manifests: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        payload = _load_json_safe(path)
        if payload is None:
            continue
        manifests.append({
            "dataset_id": payload.get("dataset_id"),
            "validation_status": payload.get("validation_status"),
            "provider": payload.get("provider"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "row_count": sum(
                (f.get("row_count") or 0)
                for f in (payload.get("files") or [])
            ),
            "manifest_path": str(path),
        })
    return _envelope({"manifests": manifests, "total": len(manifests)})


# ---------------------------------------------------------------------------
# Hypothesis queue
# ---------------------------------------------------------------------------


def hypothesis_queue(
    hypothesis_root: str = DEFAULT_HYPOTHESIS_ROOT,
    *,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    root = Path(hypothesis_root)
    if not root.is_dir():
        return _envelope([], warnings=[f"hypothesis_root {hypothesis_root!r} missing"])
    entries: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if status is None or item.get("status") == status:
                entries.append(item)
    return _envelope(entries)


# ---------------------------------------------------------------------------
# CLI (dumps one endpoint's JSON to stdout)
# ---------------------------------------------------------------------------


ENDPOINTS = {
    "list_weekend_runs": list_weekend_runs,
    "latest_weekend_run": latest_weekend_run,
    "weekend_run_detail": weekend_run_detail,
    "leaderboard": leaderboard,
    "latest_leaderboard": latest_leaderboard,
    "list_experiments": list_experiments,
    "experiment_detail": experiment_detail,
    "strategy_rankings": strategy_rankings,
    "top_parameter_sets": top_parameter_sets,
    "analyst_summaries": analyst_summaries,
    "dataset_provenance": dataset_provenance,
    "warehouse_status": warehouse_status,
    "hypothesis_queue": hypothesis_queue,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lab-dashboard-backend",
        description="Read-only JSON API over the Strategy Laboratory.",
    )
    p.add_argument("endpoint", choices=sorted(ENDPOINTS))
    p.add_argument("--run-id", default="")
    p.add_argument("--experiment-id", default="")
    p.add_argument("--strategy-name", default="")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--lab-root", default=DEFAULT_LAB_ROOT)
    p.add_argument("--experiment-root", default=DEFAULT_EXPERIMENT_ROOT)
    p.add_argument("--hypothesis-root", default=DEFAULT_HYPOTHESIS_ROOT)
    p.add_argument("--warehouse-manifest-root",
                   default=DEFAULT_WAREHOUSE_MANIFEST_ROOT)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn = ENDPOINTS[args.endpoint]
    call_kwargs: Dict[str, Any] = {}
    sig = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    # Positional required args come first — dispatch table
    positional: List[Any] = []
    if args.endpoint in {"weekend_run_detail", "leaderboard"}:
        if not args.run_id:
            print("FAIL: --run-id required", file=sys.stderr)
            return 2
        positional.append(args.run_id)
    if args.endpoint == "experiment_detail":
        if not args.experiment_id:
            print("FAIL: --experiment-id required", file=sys.stderr)
            return 2
        positional.append(args.experiment_id)
    if args.endpoint == "dataset_provenance":
        if not args.experiment_id:
            print("FAIL: --experiment-id required", file=sys.stderr)
            return 2
        positional.append(args.experiment_id)
    if args.endpoint == "top_parameter_sets":
        if not args.run_id or not args.strategy_name:
            print("FAIL: --run-id and --strategy-name required", file=sys.stderr)
            return 2
        positional.extend([args.run_id, args.strategy_name])
    # Keyword-only options routed if the function accepts them
    if "lab_root" in sig:
        call_kwargs["lab_root"] = args.lab_root
    if "experiment_root" in sig:
        call_kwargs["experiment_root"] = args.experiment_root
    if "hypothesis_root" in sig:
        call_kwargs["hypothesis_root"] = args.hypothesis_root
    if "warehouse_manifest_root" in sig:
        call_kwargs["warehouse_manifest_root"] = args.warehouse_manifest_root
    if "limit" in sig:
        call_kwargs["limit"] = args.limit
    if "offset" in sig:
        call_kwargs["offset"] = args.offset
    if "status" in sig and args.status:
        call_kwargs["status"] = args.status
    result = fn(*positional, **call_kwargs)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_EXPERIMENT_ROOT",
    "DEFAULT_HYPOTHESIS_ROOT",
    "DEFAULT_LAB_ROOT",
    "DEFAULT_WAREHOUSE_MANIFEST_ROOT",
    "ENDPOINTS",
    "analyst_summaries",
    "dataset_provenance",
    "experiment_detail",
    "hypothesis_queue",
    "latest_leaderboard",
    "latest_weekend_run",
    "leaderboard",
    "list_experiments",
    "list_weekend_runs",
    "main",
    "strategy_rankings",
    "top_parameter_sets",
    "warehouse_status",
    "weekend_run_detail",
]
