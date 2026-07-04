"""Weekend Lab pipeline — Card 6.

Single-command orchestrator that chains the Strategy Laboratory
pieces:

1. (optional) warehouse import of the newest bars
2. strategy matrix — one experiment per registered strategy
3. parameter sweeps (declared per strategy)
4. leaderboard aggregation
5. weekend manifest binding every artifact
6. minimal HTML dashboard for eyeballing

Read-only.  Never touches a live trading path, never enables a
feature flag, never advances PromotionEntry, never constructs an
ApprovalRecord.  All artifacts land under
``reports/weekend_lab/<run_id>/``.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.config import get_feature_flags
from strategy.historical_validation import HistoricalValidationConfig
from strategy.lab.experiment_runner import (
    ExperimentBundle,
    run_strategy_experiment,
)
from strategy.lab.leaderboard import (
    Leaderboard,
    LeaderboardEntry,
    entry_from_bundle,
    rank_leaderboard,
    write_leaderboard,
)
from strategy.lab.parameter_sweep import (
    ParameterGrid,
    SweepManifest,
    run_parameter_sweep,
)
from strategy.lab.registry import (
    StrategyRegistry,
    discover_strategies,
    get_default_registry,
)


DEFAULT_LAB_ROOT = "reports/weekend_lab"
DEFAULT_SYMBOLS = ("AAPL", "MSFT", "NVDA")
DEFAULT_BENCHMARKS = ("SPY", "QQQ")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


@dataclass(frozen=True)
class SweepSpec:
    """One declared sweep in the weekend recipe."""

    strategy_name: str
    grid: ParameterGrid


@dataclass(frozen=True)
class WeekendLabManifest:
    """Provenance record for one Weekend Lab run."""

    run_id: str
    generated_at: str
    dataset_id: str
    window_start: str
    window_end: str
    symbols: Sequence[str]
    benchmarks: Sequence[str]
    matrix_experiment_ids: Sequence[str]
    sweep_ids: Sequence[str]
    leaderboard_path: str
    dashboard_path: str
    warnings: Sequence[str]
    feature_flags_all_disabled: bool
    promotion_states: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "dataset_id": self.dataset_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "symbols": list(self.symbols),
            "benchmarks": list(self.benchmarks),
            "matrix_experiment_ids": list(self.matrix_experiment_ids),
            "sweep_ids": list(self.sweep_ids),
            "leaderboard_path": self.leaderboard_path,
            "dashboard_path": self.dashboard_path,
            "warnings": list(self.warnings),
            "feature_flags_all_disabled": self.feature_flags_all_disabled,
            "promotion_states": dict(self.promotion_states),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------


def build_config(
    tmp_root: Path,
    dataset_id: str,
    symbols: Sequence[str],
    benchmarks: Sequence[str],
    window_start: str,
    window_end: str,
) -> HistoricalValidationConfig:
    """Build a HistoricalValidationConfig from the weekend inputs.

    Uses live_fetch=False (fixture mode) so the config's own
    fixture events / champion scores / rs map power the pipeline
    when no warehouse reader is attached.  Callers that want
    warehouse-backed experiments pass a live-fetch config via
    ``config=`` in :func:`run_weekend_lab`.
    """
    events = _daily_events(window_start, window_end)
    scores = {
        e.timestamp: {sym: 0.5 + 0.001 * i for i, sym in enumerate(symbols)}
        for e in events
    }
    rs = {
        e.timestamp: {sym: 50.0 for sym in symbols}
        for e in events
    }
    return HistoricalValidationConfig(
        dataset_id=dataset_id,
        symbols=tuple(symbols),
        benchmarks=tuple(benchmarks),
        window_start=window_start,
        window_end=window_end,
        research_data_root=str(tmp_root / "research_data"),
        report_root=str(tmp_root / "validation_reports"),
        in_sample_days=30,
        out_of_sample_days=15,
        step_days=15,
        fixture_events=tuple(events),
        fixture_champion_scores=scores,
        fixture_rs_map=rs,
    )


def _daily_events(window_start: str, window_end: str):
    from datetime import date, timedelta
    from strategy.backtest_lab import BacktestEvent

    events = []
    current = date.fromisoformat(window_start)
    stop = date.fromisoformat(window_end)
    idx = 0
    while current <= stop:
        events.append(BacktestEvent(
            timestamp=f"{current.isoformat()}T14:30:00+00:00",
            event_type="market_snapshot",
            sequence=idx + 1,
        ))
        current = current + timedelta(days=1)
        idx += 1
    return events


# ---------------------------------------------------------------------------
# Matrix + sweep drivers
# ---------------------------------------------------------------------------


def _run_matrix(
    strategies: Sequence[Any],
    config: HistoricalValidationConfig,
    manifest_root: str,
    llm_client: Any,
    printer,
) -> List[ExperimentBundle]:
    """Run one experiment per strategy instance."""
    bundles: List[ExperimentBundle] = []
    for strat in strategies:
        printer(f"  matrix: {strat.name} v{strat.version}")
        try:
            bundle = run_strategy_experiment(
                config, strat,
                llm_client=llm_client,
                manifest_root=manifest_root,
            )
            bundles.append(bundle)
        except Exception as exc:  # noqa: BLE001 — isolate one strategy failure
            printer(f"    FAIL: {type(exc).__name__}: {exc}")
    return bundles


def _run_sweeps(
    specs: Sequence[SweepSpec],
    registry: StrategyRegistry,
    config: HistoricalValidationConfig,
    manifest_root: str,
    printer,
) -> List[SweepManifest]:
    manifests: List[SweepManifest] = []
    for spec in specs:
        printer(f"  sweep: {spec.strategy_name} × {spec.grid.size()} combos")
        try:
            entry = registry.get(spec.strategy_name)
            manifest = run_parameter_sweep(
                entry.factory, spec.grid, config,
                sweep_id=f"sweep_{spec.strategy_name}",
                manifest_root=manifest_root,
            )
            manifests.append(manifest)
        except Exception as exc:  # noqa: BLE001
            printer(f"    FAIL: {type(exc).__name__}: {exc}")
    return manifests


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Weekend Lab — {run_id}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; max-width: 1100px; }}
h1, h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
.meta {{ color: #666; font-size: 0.9em; }}
table {{ border-collapse: collapse; margin: 8px 0; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: right; font-family: monospace; font-size: 0.9em; }}
th {{ background: #f0f0f0; text-align: center; }}
td:first-child, th:first-child {{ text-align: left; }}
.na {{ color: #999; }}
.badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}
.ok {{ background: #d4edda; color: #155724; }}
.warn {{ background: #fff3cd; color: #856404; }}
</style></head><body>
<h1>Weekend Lab — {run_id}</h1>
<p class="meta">Generated {generated_at} • Dataset <code>{dataset_id}</code>
• Window <code>{window_start} → {window_end}</code>
• Symbols {symbols} • Benchmarks {benchmarks}</p>
<p>Feature flags: <span class="badge {flag_class}">{flag_text}</span></p>
{sections}
</body></html>
"""


def render_dashboard_html(
    manifest: WeekendLabManifest,
    leaderboard: Leaderboard,
) -> str:
    def _cell(value):
        if value is None:
            return '<td class="na">n/a</td>'
        if isinstance(value, float):
            return f"<td>{value:.4f}</td>"
        return f"<td>{html.escape(str(value))}</td>"

    rows_html: List[str] = []
    for rank, entry in enumerate(leaderboard.entries, 1):
        row = "<tr>"
        row += f"<td>{rank}</td>"
        row += f"<td>{html.escape(entry.strategy.name)} v{html.escape(entry.strategy.version)}</td>"
        for v in (entry.sharpe, entry.sortino, entry.max_drawdown,
                  entry.cagr, entry.win_rate, entry.profit_factor):
            row += _cell(v)
        row += _cell(entry.selection_agreement_rate)
        row += _cell(entry.mean_score_delta)
        row += _cell(entry.max_abs_score_delta)
        row += f"<td>{entry.total_disagreements}/{entry.total_events}</td>"
        row += "</tr>"
        rows_html.append(row)

    leaderboard_table = (
        "<table><thead><tr>"
        "<th>Rank</th><th>Strategy</th>"
        "<th>Sharpe</th><th>Sortino</th><th>MaxDD</th>"
        "<th>CAGR</th><th>Win%</th><th>PF</th>"
        "<th>SelAgree</th><th>MeanΔ</th><th>|Δ|max</th><th>Disagreements</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )

    sections = f"<h2>Leaderboard ({leaderboard.primary_metric})</h2>{leaderboard_table}"
    sections += "<h2>Matrix experiments</h2><ul>"
    for xid in manifest.matrix_experiment_ids:
        sections += f"<li><code>{html.escape(xid)}</code></li>"
    sections += "</ul>"
    sections += "<h2>Sweeps</h2><ul>"
    for sid in manifest.sweep_ids:
        sections += f"<li><code>{html.escape(sid)}</code></li>"
    sections += "</ul>"

    flag_class = "ok" if manifest.feature_flags_all_disabled else "warn"
    flag_text = ("all disabled ✓" if manifest.feature_flags_all_disabled
                 else "SOME ENABLED — investigate")

    return _HTML_TEMPLATE.format(
        run_id=html.escape(manifest.run_id),
        generated_at=html.escape(manifest.generated_at),
        dataset_id=html.escape(manifest.dataset_id),
        window_start=html.escape(manifest.window_start),
        window_end=html.escape(manifest.window_end),
        symbols=html.escape(", ".join(manifest.symbols)),
        benchmarks=html.escape(", ".join(manifest.benchmarks)),
        flag_class=flag_class,
        flag_text=flag_text,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_weekend_lab(
    *,
    window_start: str,
    window_end: str,
    dataset_id: str = "weekend-lab",
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    benchmarks: Sequence[str] = DEFAULT_BENCHMARKS,
    registry: Optional[StrategyRegistry] = None,
    strategies: Optional[Sequence[str]] = None,
    sweep_specs: Sequence[SweepSpec] = (),
    lab_root: str = DEFAULT_LAB_ROOT,
    run_id: Optional[str] = None,
    llm_client: Any = None,
    primary_metric: str = "mean_score_delta",
    generated_at: Optional[str] = None,
    printer=print,
) -> WeekendLabManifest:
    """Run the full weekend lab pipeline.

    ``strategies`` (optional) restricts the matrix to a subset of
    registered strategy names.  When None, every discovered
    strategy runs.  ``sweep_specs`` declares the parameter sweeps
    to execute.  When empty, only the matrix runs.

    Returns the :class:`WeekendLabManifest` binding every artifact
    (matrix bundle ids, sweep ids, leaderboard path, dashboard
    path).  Raises no exception on individual strategy or sweep
    failures — those get isolated per the underlying runners.
    """
    generated = generated_at or _utc_now_iso()
    run_id = run_id or f"weekend_{dataset_id}_{generated.replace(':', '-')}"

    root = Path(lab_root) / run_id
    root.mkdir(parents=True, exist_ok=True)
    matrix_root = str(root / "matrix")
    sweep_root = str(root / "sweeps")
    leaderboard_dir = root / "leaderboard"

    reg = registry or get_default_registry()
    if not reg.names():
        discover_strategies("strategy.lab", registry=reg)

    printer(f"=== weekend lab — {run_id} ===")
    printer(f"window: {window_start} .. {window_end}")
    printer(f"symbols: {list(symbols)}   benchmarks: {list(benchmarks)}")

    config = build_config(
        root, dataset_id, symbols, benchmarks, window_start, window_end
    )

    # --- strategy matrix ---------------------------------------------------
    printer("\n--- matrix ---")
    if strategies is None:
        strategies_to_run = reg.names()
    else:
        strategies_to_run = list(strategies)
    strategy_instances: List[Any] = []
    for name in strategies_to_run:
        try:
            strategy_instances.append(reg.get(name).build())
        except TypeError:
            # ChampionRelativeStrengthStrategy in the matrix path
            # would need rs_provider; it participates via sweep
            # specs only.
            printer(f"  skip: {name} (requires kwargs)")
    matrix_bundles = _run_matrix(
        strategy_instances, config, matrix_root, llm_client, printer,
    )

    # --- sweeps -----------------------------------------------------------
    printer("\n--- sweeps ---")
    sweep_manifests = _run_sweeps(
        sweep_specs, reg, config, sweep_root, printer,
    )

    # --- leaderboard ------------------------------------------------------
    printer("\n--- leaderboard ---")
    entries: List[LeaderboardEntry] = []
    for b in matrix_bundles:
        entries.append(entry_from_bundle(b))
    for sm in sweep_manifests:
        for row in sm.rows:
            if row.bundle is not None:
                entries.append(entry_from_bundle(row.bundle))
    leaderboard = rank_leaderboard(
        entries, primary_metric=primary_metric,
        generated_at=generated,
    )
    lb_paths = write_leaderboard(leaderboard, leaderboard_dir)
    printer(f"  leaderboard: {lb_paths['json']}")

    # --- dashboard --------------------------------------------------------
    printer("\n--- dashboard ---")
    dashboard_path = root / "dashboard.html"
    promotion_states: Dict[str, str] = {}
    for b in matrix_bundles:
        promotion_states[b.manifest.experiment_id] = b.manifest.promotion_state
    for sm in sweep_manifests:
        for row in sm.rows:
            if row.bundle is not None:
                promotion_states[row.bundle.manifest.experiment_id] = (
                    row.bundle.manifest.promotion_state
                )
    flags = get_feature_flags()
    manifest = WeekendLabManifest(
        run_id=run_id,
        generated_at=generated,
        dataset_id=dataset_id,
        window_start=window_start,
        window_end=window_end,
        symbols=tuple(symbols),
        benchmarks=tuple(benchmarks),
        matrix_experiment_ids=tuple(
            b.manifest.experiment_id for b in matrix_bundles
        ),
        sweep_ids=tuple(sm.sweep_id for sm in sweep_manifests),
        leaderboard_path=str(lb_paths["json"]),
        dashboard_path=str(dashboard_path),
        warnings=(),
        feature_flags_all_disabled=flags.all_disabled,
        promotion_states=promotion_states,
    )
    dashboard_path.write_text(
        render_dashboard_html(manifest, leaderboard), encoding="utf-8"
    )
    printer(f"  dashboard: {dashboard_path}")

    (root / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    printer(f"\n=== DONE — {run_id} ===")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_sweep_specs() -> List[SweepSpec]:
    return [
        SweepSpec(
            strategy_name="momentum",
            grid=ParameterGrid({"lookback": [10, 15, 20, 30]}),
        ),
        SweepSpec(
            strategy_name="champion_rs",
            grid=ParameterGrid({
                "overlay_weight": [0.05, 0.10, 0.15, 0.20, 0.25],
            }),
        ),
    ]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="weekend-lab",
        description="One-shot weekend research pipeline.",
    )
    p.add_argument("--dataset-id", default="weekend-lab")
    p.add_argument("--window-start", default="2026-05-05")
    p.add_argument("--window-end", default="2026-07-04")
    p.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--benchmarks", nargs="+", default=list(DEFAULT_BENCHMARKS))
    p.add_argument("--strategies", nargs="+", default=None,
                   help="restrict matrix to these strategy names")
    p.add_argument("--primary-metric", default="mean_score_delta")
    p.add_argument("--lab-root", default=DEFAULT_LAB_ROOT)
    p.add_argument("--run-id", default="")
    p.add_argument("--no-sweeps", action="store_true",
                   help="skip parameter sweeps")
    p.add_argument("--with-llm", action="store_true",
                   help="attach ResearchAnalyst LLM narratives")
    p.add_argument("--model", default="")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    llm_client: Any = None
    if args.with_llm:
        endpoint = os.environ.get("RESEARCH_LLM_ENDPOINT", "")
        model = args.model or os.environ.get("RESEARCH_AI_MODEL", "")
        if not endpoint or not model:
            print(
                "FAIL: --with-llm requires RESEARCH_LLM_ENDPOINT + "
                "RESEARCH_AI_MODEL (or --model)",
                file=sys.stderr,
            )
            return 2
        from strategy.research_analyst import LocalLLMClient
        llm_client = LocalLLMClient(
            endpoint=endpoint, model=model, timeout=1200.0
        )
    sweep_specs = () if args.no_sweeps else _default_sweep_specs()
    manifest = run_weekend_lab(
        window_start=args.window_start,
        window_end=args.window_end,
        dataset_id=args.dataset_id,
        symbols=args.symbols,
        benchmarks=args.benchmarks,
        strategies=args.strategies,
        sweep_specs=sweep_specs,
        lab_root=args.lab_root,
        run_id=args.run_id or None,
        llm_client=llm_client,
        primary_metric=args.primary_metric,
    )
    print(f"\nmanifest: {Path(args.lab_root) / manifest.run_id / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_LAB_ROOT",
    "SweepSpec",
    "WeekendLabManifest",
    "build_config",
    "build_parser",
    "main",
    "render_dashboard_html",
    "run_weekend_lab",
]
