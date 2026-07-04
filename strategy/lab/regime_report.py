"""CLI: research-regime-report.

Reads a matrix run manifest + comparison artifact from disk,
applies the default (VXX / HYG-LQD / TLT) regime pack, and prints
a per-regime breakdown of the challenger's score-delta behaviour.

Read-only.  Never modifies artifacts on disk.  Never enables
feature flags.  Never touches production credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

from strategy.lab.regime_tagger import (
    RegimeSpec,
    default_regime_specs,
    regime_conditioned_metrics,
)


DEFAULT_RUN_MANIFEST_ROOT = Path("reports/run_manifests")
DEFAULT_VALIDATION_ROOT = Path("reports/validation")
DEFAULT_WAREHOUSE_ROOT = Path("market_data")


class RegimeReportError(RuntimeError):
    """Raised when the CLI cannot proceed."""


# ---------------------------------------------------------------------------
# Bundle reconstruction from on-disk artifacts
# ---------------------------------------------------------------------------


def _load_matrix_artifacts(
    run_manifest_path: Path,
    validation_root: Path,
) -> Any:
    """Read a matrix run manifest + its comparison report + its
    disagreements list from disk and reconstruct a duck-typed
    object shaped like the ExperimentBundle attributes that
    ``regime_conditioned_metrics`` inspects.
    """
    if not run_manifest_path.is_file():
        raise RegimeReportError(
            f"run manifest {run_manifest_path} not found"
        )
    manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    cmp_ref = manifest.get("comparison_artifact") or {}
    artifact_id = cmp_ref.get("artifact_id")
    if not artifact_id:
        raise RegimeReportError(
            f"run manifest {run_manifest_path} has no comparison_artifact"
        )

    # Matrix layout: reports/validation/matrix-<label>/backtests/<rr_id>/
    dataset_id = manifest.get("dataset_id", "")
    # Prefer the exact path from the manifest if present
    artifact_path = cmp_ref.get("path", "")
    if artifact_path and Path(artifact_path).is_dir():
        art_dir = Path(artifact_path)
    else:
        # Walk validation_root looking for the artifact_id folder
        candidates = list(validation_root.rglob(artifact_id))
        art_dir = next((c for c in candidates if c.is_dir()), None)
        if art_dir is None:
            raise RegimeReportError(
                f"comparison artifact {artifact_id} not found under "
                f"{validation_root}"
            )

    report_path = art_dir / "report.json"
    disagreements_path = art_dir / "disagreements.json"
    if not report_path.is_file():
        raise RegimeReportError(
            f"comparison report.json not found under {art_dir}"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    disagreements_raw: List[Dict[str, Any]]
    if disagreements_path.is_file():
        loaded = json.loads(disagreements_path.read_text(encoding="utf-8"))
        disagreements_raw = (
            loaded if isinstance(loaded, list)
            else loaded.get("disagreements", [])
        )
    else:
        disagreements_raw = report.get("comparison", {}).get(
            "disagreements", []
        )

    # Reconstruct score_tables + disagreements as duck objects.  The
    # regime tagger only reads a small handful of attributes.
    score_tables: List[Any] = []
    daily = report.get("daily_summary", [])
    # daily_summary in the report is per-event but does not carry
    # per-row selection details.  Rebuild rows from disagreement
    # symbol lists so `total_rows_in_bucket` and
    # `selection_agreement_rate` remain meaningful.  We treat each
    # disagreement's (event, symbol) as an "observed row" with a
    # matching champion_selected == challenger_selected.  Rows outside
    # the disagreement list are inferred as `both selected the same`
    # by counting the daily_summary's event symbol_count.
    by_event: Dict[str, List[Any]] = {}
    for d in disagreements_raw:
        ts = d.get("event_timestamp")
        if not ts:
            continue
        by_event.setdefault(ts, []).append(d)

    for entry in daily:
        ts = entry.get("event_timestamp")
        if not ts:
            continue
        symbol_count = int(entry.get("symbol_count", 0))
        seen_dis = by_event.get(ts, [])
        rows: List[Any] = []
        # For each disagreement at this event, record a row where
        # champion/challenger selection may or may not match.  We
        # can't tell from the on-disk report — treat every row as
        # a plain observation for total-rows accounting; selection
        # agreement rate is separately captured by the row-level
        # champion_selected / challenger_selected on the DisagreementRecord
        # objects downstream, so we approximate here by making rows
        # that mirror the disagreement selections when available.
        for d in seen_dis:
            rows.append(SimpleNamespace(
                symbol=d.get("symbol", ""),
                champion_selected=bool(d.get("champion_selected", False)),
                challenger_selected=bool(d.get("challenger_selected", False)),
            ))
        # Pad remaining rows with symbols that did NOT disagree — the
        # regime tagger uses row_count as its bucket denominator, so
        # padding matters for correct disagreement_rate.
        missing_rows = max(0, symbol_count - len(seen_dis))
        for _ in range(missing_rows):
            rows.append(SimpleNamespace(
                symbol="", champion_selected=True, challenger_selected=True,
            ))
        score_tables.append(SimpleNamespace(
            event_timestamp=ts,
            rows=rows,
        ))

    # Disagreement duck objects — carry only what the tagger reads
    disagreement_objects: List[Any] = []
    for d in disagreements_raw:
        disagreement_objects.append(SimpleNamespace(
            event_timestamp=d.get("event_timestamp", ""),
            champion_explanation=d.get("champion_explanation", ""),
            score_delta=d.get("score_delta"),
        ))

    comparison = SimpleNamespace(
        score_tables=score_tables,
        disagreements=disagreement_objects,
    )
    validation_bundle = SimpleNamespace(comparison=comparison)
    return SimpleNamespace(
        validation_bundle=validation_bundle,
        manifest_path=str(run_manifest_path),
        dataset_id=dataset_id,
        raw_manifest=manifest,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(
    result: Dict[str, Dict[str, Any]],
    *,
    specs: Sequence[RegimeSpec],
    manifest: Dict[str, Any],
    printer=print,
) -> None:
    printer(f"=== research-regime-report ===")
    printer(f"run_id:   {manifest.get('run_id', '?')}")
    printer(f"dataset:  {manifest.get('dataset_id', '?')}")
    printer(f"window:   {manifest.get('window_start', '?')} .. "
            f"{manifest.get('window_end', '?')}")
    printer(f"regimes:  {[s.name for s in specs]}")
    for regime_name, buckets in result.items():
        printer(f"\n--- regime: {regime_name} ---")
        printer(
            f"  {'label':<15s} {'n_events':>8s} {'n_dis':>6s} "
            f"{'rate':>7s} {'mean Δ':>10s} {'+%':>5s} {'-%':>5s} "
            f"{'|Δ|max':>8s} {'sel_agree':>10s}"
        )
        for label, m in buckets.items():
            printer(
                f"  {label:<15s} {m.n_events_tagged:>8d} "
                f"{m.n_disagreements:>6d} "
                f"{m.disagreement_rate:>7.3f} "
                f"{m.mean_score_delta:>+10.4f} "
                f"{m.positive_pct * 100:>4.0f}% "
                f"{m.negative_pct * 100:>4.0f}% "
                f"{m.max_abs_score_delta:>8.4f} "
                f"{m.selection_agreement_rate:>10.4f}"
            )


def result_to_json(
    result: Dict[str, Dict[str, Any]],
    manifest: Dict[str, Any],
    specs: Sequence[RegimeSpec],
) -> Dict[str, Any]:
    return {
        "run_id": manifest.get("run_id"),
        "dataset_id": manifest.get("dataset_id"),
        "window_start": manifest.get("window_start"),
        "window_end": manifest.get("window_end"),
        "regime_specs": [s.to_dict() for s in specs],
        "regimes": {
            regime_name: {
                label: m.to_dict() for label, m in buckets.items()
            }
            for regime_name, buckets in result.items()
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="research-regime-report",
        description="Regime-conditioned breakdown of a matrix run.",
    )
    p.add_argument(
        "--run-manifest",
        required=True,
        help="path to a matrix run manifest (e.g. reports/run_manifests/matrix-1y-2026-07-04/latest.json)",
    )
    p.add_argument(
        "--warehouse-root", default=str(DEFAULT_WAREHOUSE_ROOT),
    )
    p.add_argument(
        "--validation-root", default=str(DEFAULT_VALIDATION_ROOT),
    )
    p.add_argument(
        "--regime-dataset", default="regime-pack-2016",
        help="warehouse dataset id carrying the regime indicator bars",
    )
    p.add_argument(
        "--include-champion-rejected", action="store_true",
        help="include disagreements where Champion was rejected for insufficient history",
    )
    p.add_argument(
        "--output-json", default="",
        help="if set, write the full result as JSON to this path",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = Path(args.run_manifest)
    bundle = _load_matrix_artifacts(
        manifest_path, Path(args.validation_root)
    )
    specs = default_regime_specs(regime_dataset_id=args.regime_dataset)
    result = regime_conditioned_metrics(
        bundle, specs,
        warehouse_root=Path(args.warehouse_root),
        exclude_champion_rejected=not args.include_champion_rejected,
    )
    render_report(result, specs=specs, manifest=bundle.raw_manifest)
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(
            json.dumps(
                result_to_json(result, bundle.raw_manifest, specs),
                indent=2, sort_keys=True,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON written to {args.output_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "RegimeReportError",
    "build_parser",
    "main",
    "render_report",
    "result_to_json",
]
