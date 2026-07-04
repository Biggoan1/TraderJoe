"""Two-Month Historical Validation Orchestrator.

Wires the Research Account Client, Data Catalog, Backtest Lab
foundation, Relative Strength Challenger, Walk-Forward pipeline,
Learning System, Research Analyst, and Promotion Gate evidence
pipeline into a single end-to-end orchestrator.

Defaults to OFFLINE / FIXTURE mode.  Live Alpaca fetches only
happen when :attr:`HistoricalValidationConfig.live_fetch` is
``True`` AND a ``research_client`` is supplied.  No feature flag
is ever enabled.  No :class:`~strategy.promotion_gates.ApprovalRecord`
is ever created.  The resulting :class:`PromotionEntry` stays at
``current_state = "disabled"``.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 5 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

from strategy.backtest_lab import (
    BacktestEvent,
    DeterministicReplayClock,
    StrategyEvaluation,
    stable_hash,
    stable_json,
)
from strategy.comparison_harness import (
    ChampionChallengerComparison,
    ComparisonHarness,
)
from strategy.config import FeatureFlags
from strategy.data_catalog import (
    DEFAULT_MANIFESTS_SUBDIR,
    HISTORICAL_BARS,
    build_dataset_manifest,
)
from strategy.learning_reports import (
    LearningReport,
    render_learning_report,
)
from strategy.promotion_gates import (
    PromotionEntry,
    STATE_DISABLED,
)
from strategy.research_analyst import (
    LLMNarrative,
    ResearchAnalystReport,
    ResearchAnalystReportPaths,
    render_analyst_report,
)
from strategy.research_reports import (
    ResearchReport,
    render_comparison_report,
    render_walk_forward_report,
)
from strategy.champion_scoring import (
    ChampionScorer,
    ChampionScoringConfig,
)
from strategy.rs_challenger import (
    DEFAULT_RS_LOOKBACK_DAYS,
    RS_CHALLENGER_STRATEGY_ID,
    RelativeStrengthChallenger,
    build_rs_map_from_bars,
    rs_provider_from_map,
)
from strategy.stats_engine import (
    STATS_SAMPLE_SIZE_FLOOR,
    StatisticalFinding,
    analyze_comparison,
    analyze_walk_forward,
)
from strategy.walk_forward import (
    WalkForwardPipeline,
    WalkForwardReport,
    generate_walk_forward_schedule,
)


DEFAULT_REPORT_ROOT = "reports/validation"
DEFAULT_RESEARCH_DATA_ROOT = "research_data"
DEFAULT_IN_SAMPLE_DAYS = 30
DEFAULT_OUT_OF_SAMPLE_DAYS = 15
DEFAULT_STEP_DAYS = 15
DEFAULT_SCORE_DELTA_THRESHOLD = 0.01
DEFAULT_FLAG_NAME = "enable_relative_strength"

CHAMPION_STRATEGY_ID = "champion-v0.4.0"


class HistoricalValidationError(RuntimeError):
    """Raised when the orchestrator refuses to run."""


class LiveFetchNotAvailableError(HistoricalValidationError):
    """Raised when live_fetch is requested but no research_client was supplied."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoricalValidationConfig:
    """Configuration for a single validation run."""

    dataset_id: str
    symbols: Tuple[str, ...]
    window_start: str
    window_end: str
    benchmarks: Tuple[str, ...] = ("SPY", "QQQ")
    research_data_root: str = DEFAULT_RESEARCH_DATA_ROOT
    report_root: str = DEFAULT_REPORT_ROOT
    in_sample_days: int = DEFAULT_IN_SAMPLE_DAYS
    out_of_sample_days: int = DEFAULT_OUT_OF_SAMPLE_DAYS
    step_days: int = DEFAULT_STEP_DAYS
    champion_id: str = CHAMPION_STRATEGY_ID
    challenger_id: str = RS_CHALLENGER_STRATEGY_ID
    seed: int = 0
    score_delta_threshold: float = DEFAULT_SCORE_DELTA_THRESHOLD
    live_fetch: bool = False
    flag_name: str = DEFAULT_FLAG_NAME
    # Fixture inputs used when live_fetch is False.
    fixture_events: Tuple[BacktestEvent, ...] = ()
    fixture_champion_scores: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )
    fixture_rs_map: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )
    fixture_bars: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "benchmarks", tuple(self.benchmarks))
        object.__setattr__(self, "fixture_events", tuple(self.fixture_events))
        object.__setattr__(
            self, "fixture_champion_scores", dict(self.fixture_champion_scores)
        )
        object.__setattr__(self, "fixture_rs_map", dict(self.fixture_rs_map))
        object.__setattr__(self, "fixture_bars", dict(self.fixture_bars))
        self.validate()

    def validate(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id is required")
        if not self.symbols:
            raise ValueError("symbols is required")
        if not self.window_start or not self.window_end:
            raise ValueError("window_start and window_end are required")
        if self.window_start > self.window_end:
            raise ValueError("window_start must be <= window_end")
        if self.in_sample_days <= 0:
            raise ValueError("in_sample_days must be positive")
        if self.out_of_sample_days <= 0:
            raise ValueError("out_of_sample_days must be positive")
        if self.step_days <= 0:
            raise ValueError("step_days must be positive")
        if self.champion_id == self.challenger_id:
            raise ValueError("champion_id and challenger_id must differ")
        if self.flag_name != DEFAULT_FLAG_NAME:
            # We deliberately hard-code the target flag to
            # enable_relative_strength — the two-month validation only
            # produces evidence for the Relative Strength Challenger.
            raise ValueError(
                f"flag_name must be {DEFAULT_FLAG_NAME!r}; got {self.flag_name!r}"
            )

    def stable_hash(self) -> str:
        data = {
            "dataset_id": self.dataset_id,
            "symbols": list(self.symbols),
            "benchmarks": list(self.benchmarks),
            "window_start": self.window_start,
            "window_end": self.window_end,
            "in_sample_days": self.in_sample_days,
            "out_of_sample_days": self.out_of_sample_days,
            "step_days": self.step_days,
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "seed": self.seed,
            "score_delta_threshold": self.score_delta_threshold,
            "live_fetch": self.live_fetch,
        }
        return stable_hash(data)


# ---------------------------------------------------------------------------
# Result bundle
# ---------------------------------------------------------------------------


@dataclass
class HistoricalValidationBundle:
    """Result of a single :func:`run_historical_validation` invocation.

    ``dataset_provenance`` records where the bars came from —
    ``source`` is one of ``"warehouse"``, ``"provider"``,
    ``"fixture"``, and (when applicable) the dataset ids + versions
    the warehouse read.  The block is empty when no bars were
    fetched (pure fixture path).
    """

    config: HistoricalValidationConfig
    dataset_manifest_path: str
    comparison: ChampionChallengerComparison
    comparison_report: ResearchReport
    comparison_report_paths: Any  # ResearchReportPaths
    walk_forward_report: WalkForwardReport
    walk_forward_report_paths: Any
    walk_forward_research_report: ResearchReport
    findings: List[StatisticalFinding]
    learning_report: LearningReport
    learning_report_paths: Any
    analyst_narratives: List[LLMNarrative]
    analyst_reports: List[ResearchAnalystReport]
    analyst_report_paths: List[ResearchAnalystReportPaths]
    promotion_entry: PromotionEntry
    warnings: List[str]
    live_fetch_used: bool
    generated_at: str
    dataset_provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_hash": self.config.stable_hash(),
            "dataset_manifest_path": self.dataset_manifest_path,
            "comparison_run_id": self.comparison.metadata.run_id,
            "comparison_report_id": self.comparison_report.report_id,
            "walk_forward_report_id": self.walk_forward_report.report_id,
            "walk_forward_research_report_id": self.walk_forward_research_report.report_id,
            "learning_report_id": self.learning_report.report_id,
            "analyst_report_ids": [
                report.report_id for report in self.analyst_reports
            ],
            "promotion_entry": self.promotion_entry.to_dict(),
            "warnings": list(self.warnings),
            "live_fetch_used": self.live_fetch_used,
            "generated_at": self.generated_at,
            "dataset_provenance": dict(self.dataset_provenance),
        }


# ---------------------------------------------------------------------------
# Champion adapter
# ---------------------------------------------------------------------------


@dataclass
class _FixtureChampion:
    """Deterministic Champion driven by a per-timestamp score map."""

    strategy_id: str
    scores_by_timestamp: Mapping[str, Mapping[str, float]]

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        symbol_scores = dict(self.scores_by_timestamp.get(event.timestamp, {}))
        rankings = [
            {"symbol": symbol, "rank": index + 1}
            for index, symbol in enumerate(
                sorted(
                    symbol_scores,
                    key=lambda name: (-symbol_scores[name], name),
                )
            )
        ]
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=symbol_scores,
            rankings=rankings,
        )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_dataset_manifest(
    config: HistoricalValidationConfig,
) -> str:
    """Write a lightweight DataCatalog manifest for the run.

    In fixture mode we synthesize a small JSON file describing the
    events; in live mode a follow-up card will replace this with the
    per-symbol bar CSVs fetched from the Research Alpaca account.
    """
    root = Path(config.research_data_root)
    manifests_dir = root / DEFAULT_MANIFESTS_SUBDIR
    manifests_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = root / config.dataset_id
    dataset_dir.mkdir(parents=True, exist_ok=True)

    events_path = dataset_dir / "events.json"
    events_payload = {
        "dataset_id": config.dataset_id,
        "window_start": config.window_start,
        "window_end": config.window_end,
        "symbols": list(config.symbols),
        "benchmarks": list(config.benchmarks),
        "event_count": len(config.fixture_events),
        "event_timestamps": [
            event.timestamp for event in config.fixture_events
        ],
    }
    events_path.write_text(
        json.dumps(events_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    manifest = build_dataset_manifest(
        root=str(root),
        dataset_id=config.dataset_id,
        kind=HISTORICAL_BARS,
        description=f"Two-month historical validation window "
        f"{config.window_start} to {config.window_end}",
        source="research-fixture" if not config.live_fetch else "research-alpaca",
        symbols=config.symbols,
        benchmarks=config.benchmarks,
        start_date=config.window_start,
        end_date=config.window_end,
        imported_at="",  # excluded from stable_hash anyway
        files=[
            {
                "path": f"{config.dataset_id}/events.json",
                "schema": [
                    "dataset_id",
                    "window_start",
                    "window_end",
                    "symbols",
                ],
            }
        ],
    )
    manifest_path = manifests_dir / f"{config.dataset_id}.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    return str(manifest_path)


def _run_comparison(
    config: HistoricalValidationConfig,
    champion: _FixtureChampion,
    challenger: RelativeStrengthChallenger,
    events: Sequence[BacktestEvent],
    generated_at: str,
) -> ChampionChallengerComparison:
    harness = ComparisonHarness(
        champion=champion,
        challenger=challenger,
        score_delta_threshold=config.score_delta_threshold,
    )
    return harness.run(
        DeterministicReplayClock(list(events)),
        dataset_id=config.dataset_id,
        seed=config.seed,
        generated_at=generated_at,
    )


def _run_walk_forward(
    config: HistoricalValidationConfig,
    champion: _FixtureChampion,
    challenger: RelativeStrengthChallenger,
    events: Sequence[BacktestEvent],
    generated_at: str,
) -> WalkForwardReport:
    schedule = generate_walk_forward_schedule(
        start_date=config.window_start,
        end_date=config.window_end,
        in_sample_days=config.in_sample_days,
        out_of_sample_days=config.out_of_sample_days,
        step_days=config.step_days,
        schedule_id=f"wf-{config.dataset_id}",
    )
    pipeline = WalkForwardPipeline(
        champion=champion,
        challenger=challenger,
        score_delta_threshold=config.score_delta_threshold,
    )
    return pipeline.run(
        schedule,
        events,
        dataset_id=config.dataset_id,
        seed=config.seed,
        generated_at=generated_at,
    )


class _ProvenanceAnalystShim:
    """Analyst-payload wrapper that injects a ``dataset_provenance``
    block into whatever ``to_analyst_payload`` or ``to_dict``
    returns.

    Delegates every other attribute (``metadata``, ``report_id``,
    ``stable_hash``, …) to the wrapped source, so
    :meth:`ResearchAnalyst.analyze_comparison` and
    :meth:`ResearchAnalyst.analyze_walk_forward` see the same
    ``source_id`` / ``source_hash`` they would from the unwrapped
    object.  The shim is a research-only construct — nothing else
    in the pipeline sees it.
    """

    def __init__(self, source: Any, provenance: Mapping[str, Any]) -> None:
        self._source = source
        self._provenance = dict(provenance)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def to_analyst_payload(self) -> Dict[str, Any]:
        # Prefer the source's trimmed payload; fall back to
        # to_dict for legacy comparison shapes.
        payload_fn = getattr(self._source, "to_analyst_payload", None)
        base = payload_fn() if callable(payload_fn) else self._source.to_dict()
        if self._provenance:
            base = dict(base)
            base["dataset_provenance"] = dict(self._provenance)
        return base

    def to_dict(self) -> Dict[str, Any]:
        base = self._source.to_dict()
        if self._provenance:
            base = dict(base)
            base["dataset_provenance"] = dict(self._provenance)
        return base


def _with_provenance(
    source: Any, provenance: Mapping[str, Any]
) -> Any:
    """Wrap ``source`` in a :class:`_ProvenanceAnalystShim` when
    ``provenance`` is non-empty, else return the source unchanged.
    """
    if not provenance:
        return source
    return _ProvenanceAnalystShim(source, provenance)


def _build_explanation_summary(
    comparison: ChampionChallengerComparison,
) -> Dict[str, Any]:
    """Aggregate champion + challenger structured explanations across
    every ``(event, symbol)`` row of the comparison so the learning
    report can carry gate pass rates, top ranking factors, and
    rejection rates for both sides.

    Empty dicts fall through gracefully — strategies without a
    structured explanation surface produce a summary with
    ``observation_count=0``.
    """
    from strategy.score_explanation import aggregate_explanation_dicts

    champion_dicts: List[Dict[str, Any]] = []
    challenger_dicts: List[Dict[str, Any]] = []
    for table in comparison.score_tables:
        for row in table.rows:
            champion = row.champion_structured_explanation
            if isinstance(champion, dict):
                champion_dicts.append(champion)
            challenger = row.challenger_structured_explanation
            if isinstance(challenger, dict):
                challenger_dicts.append(challenger)
    return {
        "champion_id": comparison.metadata.champion_id,
        "challenger_id": comparison.metadata.challenger_id,
        "champion": aggregate_explanation_dicts(champion_dicts),
        "challenger": aggregate_explanation_dicts(challenger_dicts),
    }


def _build_findings(
    comparison: ChampionChallengerComparison,
    walk_forward_report: WalkForwardReport,
) -> List[StatisticalFinding]:
    return list(analyze_comparison(comparison)) + list(
        analyze_walk_forward(walk_forward_report)
    )


def _build_promotion_entry(
    config: HistoricalValidationConfig,
    dataset_manifest_path: str,
    comparison: ChampionChallengerComparison,
    walk_forward_report: WalkForwardReport,
    learning_report: LearningReport,
    analyst_reports: Sequence[ResearchAnalystReport],
    dataset_provenance: Optional[Mapping[str, Any]] = None,
) -> PromotionEntry:
    """Attach evidence keys to a PromotionEntry — never advances state."""
    evidence: Dict[str, str] = {
        "dataset_id": config.dataset_id,
        "experiment_manifest": dataset_manifest_path,
        "backtest_report_id": comparison.metadata.run_id,
        "walk_forward_report_id": walk_forward_report.report_id,
        "learning_report_id": learning_report.report_id,
    }
    for index, report in enumerate(analyst_reports):
        evidence[f"analyst_report_{index}"] = report.report_id
    # Phase 5.6 warehouse wiring: record dataset provenance so a
    # future rerun can pin to the same warehouse dataset ids +
    # versions.  ``dataset_provenance_id`` is a compact
    # ``source:dataset_id@version`` string; the full block lives
    # in the bundle.
    if dataset_provenance:
        source = str(dataset_provenance.get("source") or "")
        provenance_datasets = dataset_provenance.get("datasets") or []
        if source and provenance_datasets:
            parts = []
            for entry in provenance_datasets:
                if isinstance(entry, Mapping):
                    ds = entry.get("dataset_id", "")
                    ver = entry.get("version", "")
                    if ds:
                        parts.append(f"{ds}@{ver}" if ver else ds)
            if parts:
                evidence["dataset_provenance_id"] = f"{source}:{','.join(parts)}"
        elif source:
            evidence["dataset_provenance_id"] = source
    entry = PromotionEntry(
        flag_name=config.flag_name,
        current_state=STATE_DISABLED,
        evidence=evidence,
        approvals=[],
        notes=(
            "Evidence produced by run_historical_validation; "
            "no ApprovalRecord constructed."
        ),
    )
    assert entry.current_state == STATE_DISABLED, (
        "orchestrator must never advance promotion state"
    )
    assert not entry.approvals, (
        "orchestrator must never construct ApprovalRecord entries"
    )
    return entry


def _write_analyst_reports(
    analyst_reports: Sequence[ResearchAnalystReport],
    root: str,
) -> List[ResearchAnalystReportPaths]:
    paths: List[ResearchAnalystReportPaths] = []
    for report in analyst_reports:
        paths.append(report.write(root))
    return paths


# ---------------------------------------------------------------------------
# Live fetch adapter
# ---------------------------------------------------------------------------


def _fetch_live_events_priority(
    config: HistoricalValidationConfig,
    warehouse_reader: Any,
    research_client: Any,
    warnings: List[str],
) -> Tuple[
    List[BacktestEvent],
    Dict[str, Dict[str, float]],
    Dict[str, Dict[str, float]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, Any],
]:
    """Priority-ordered live-fetch resolver.

    Consults the warehouse first (when supplied and coverage is
    complete); falls back to the provider client only when the
    warehouse can't satisfy the request.  Returns the same
    (events, champion_scores, rs_map, bars_by_symbol) tuple as
    :func:`_fetch_live_events`, plus a ``dataset_provenance`` dict
    describing which source produced the bars.

    Bars_by_symbol lookups from the warehouse are shaped to match
    the provider payload (``{"t": timestamp, "c": close, ...}``)
    so downstream :func:`_events_from_bar_dict` sees a homogeneous
    surface regardless of provenance.
    """
    if warehouse_reader is not None:
        symbols_needed = list(config.symbols) + list(config.benchmarks)
        try:
            complete = warehouse_reader.has_complete_coverage(
                asset_class=_default_asset_class(),
                interval=_default_interval(),
                symbols=symbols_needed,
                start=config.window_start,
                end=config.window_end,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"warehouse coverage check failed: {exc}")
            complete = False
        if complete:
            bars_by_symbol, prov = _bars_from_warehouse(
                config, warehouse_reader, symbols_needed
            )
            events, champion_scores, rs_map = _events_from_bar_dict(
                config, bars_by_symbol
            )
            return events, champion_scores, rs_map, bars_by_symbol, prov
        else:
            warnings.append(
                "warehouse coverage incomplete; falling back to provider"
            )

    if research_client is None:
        raise LiveFetchNotAvailableError(
            "config.live_fetch=True and warehouse coverage is incomplete, "
            "but no research_client was supplied"
        )
    events, champion_scores, rs_map, bars_by_symbol = _fetch_live_events(
        config, research_client
    )
    return (
        events, champion_scores, rs_map, bars_by_symbol,
        {"source": "provider", "provider_name": "research_client"},
    )


def _default_asset_class():
    from strategy.market_data_provider import AssetClass
    return AssetClass.EQUITY


def _default_interval():
    from strategy.market_data_provider import BarInterval
    return BarInterval.DAILY


def _bars_from_warehouse(
    config: HistoricalValidationConfig,
    warehouse_reader: Any,
    symbols_needed: Sequence[str],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """Fetch bars for every requested symbol via the warehouse
    reader and shape them into the ``{symbol: [{"t","c",...}]}``
    payload the downstream pipeline expects.

    Returns the bars dict and a ``dataset_provenance`` block with
    per-symbol dataset ids + versions the warehouse used.
    """
    bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    datasets: List[Dict[str, str]] = []
    seen_datasets: Dict[str, str] = {}
    for symbol in symbols_needed:
        hit = warehouse_reader.fetch_bars(
            asset_class=_default_asset_class(),
            interval=_default_interval(),
            symbols=[symbol],
            start=config.window_start,
            end=config.window_end,
        )
        symbol_bars: List[Dict[str, Any]] = []
        for bar in hit.bars:
            symbol_bars.append(
                {
                    "t": bar.timestamp,
                    "o": float(bar.open),
                    "h": float(bar.high),
                    "l": float(bar.low),
                    "c": float(bar.close),
                    "v": float(bar.volume),
                }
            )
        bars_by_symbol[symbol] = symbol_bars
        # Record dataset provenance per (dataset_id, version)
        for ds_id, ver in zip(
            (hit.dataset_id or "").split(","),
            (hit.dataset_version or "").split(","),
        ):
            if ds_id and ds_id not in seen_datasets:
                seen_datasets[ds_id] = ver
                datasets.append({"dataset_id": ds_id, "version": ver})
    return bars_by_symbol, {
        "source": "warehouse",
        "provider_name": "",
        "datasets": datasets,
    }


def _events_from_bar_dict(
    config: HistoricalValidationConfig,
    bars_by_symbol: Dict[str, List[Dict[str, Any]]],
) -> Tuple[
    List[BacktestEvent],
    Dict[str, Dict[str, float]],
    Dict[str, Dict[str, float]],
]:
    """Extract events + champion score map + RS map from a
    ``bars_by_symbol`` payload — the shared body between the
    warehouse and provider paths.
    """
    timestamps = sorted(
        {
            bar["t"]
            for symbol_bars in bars_by_symbol.values()
            if isinstance(symbol_bars, list)
            for bar in symbol_bars
            if isinstance(bar, dict) and "t" in bar
        }
    )
    scores_by_timestamp: Dict[str, Dict[str, float]] = {}
    for symbol in config.symbols:
        closes = {
            bar["t"]: float(bar.get("c", 0.0))
            for bar in bars_by_symbol.get(symbol, [])
            if isinstance(bar, dict) and "t" in bar
        }
        if not closes:
            continue
        values = list(closes.values())
        mean = sum(values) / len(values)
        for timestamp, close in closes.items():
            if timestamp not in scores_by_timestamp:
                scores_by_timestamp[timestamp] = {}
            scores_by_timestamp[timestamp][symbol] = (
                (close - mean) / mean if mean else 0.0
            )

    rs_lookback = min(DEFAULT_RS_LOOKBACK_DAYS, max(1, len(timestamps) - 1))
    rs_map = build_rs_map_from_bars(
        bars_by_symbol=bars_by_symbol,
        symbols=config.symbols,
        benchmarks=config.benchmarks,
        lookback_days=rs_lookback,
    )
    events = [
        BacktestEvent(
            timestamp=timestamp,
            event_type="market_snapshot",
            sequence=index + 1,
        )
        for index, timestamp in enumerate(timestamps)
    ]
    return events, scores_by_timestamp, rs_map


def _fetch_live_events(
    config: HistoricalValidationConfig,
    research_client: Any,
) -> Tuple[
    List[BacktestEvent],
    Dict[str, Dict[str, float]],
    Dict[str, Dict[str, float]],
    Dict[str, List[Dict[str, Any]]],
]:
    """Live-fetch pathway.

    Calls the Research Account Client for historical bars, converts
    them into deterministic events, a placeholder Champion score map
    (kept for downstream fixture persistence and legacy tests), an
    RS map keyed ``{timestamp: {symbol: rs_value}}`` for the RS
    Challenger, and the raw ``bars_by_symbol`` dict so
    :class:`strategy.champion_scoring.ChampionScorer` can produce
    real per-event Champion scores + structured explanations without
    hitting the network a second time.

    The RS map uses
    :func:`strategy.rs_challenger.build_rs_map_from_bars`, which
    reads the same bars deterministically without any external
    dependencies.
    """
    result = research_client.fetch_bars(
        symbols=list(config.symbols) + list(config.benchmarks),
        start=config.window_start,
        end=config.window_end,
    )
    bars_by_symbol = result.get("bars", {}) if isinstance(result, dict) else {}
    if not isinstance(bars_by_symbol, dict):
        raise HistoricalValidationError(
            "Research Alpaca /v2/stocks/bars response missing 'bars' object"
        )

    timestamps = sorted(
        {
            bar["t"]
            for symbol_bars in bars_by_symbol.values()
            if isinstance(symbol_bars, list)
            for bar in symbol_bars
            if isinstance(bar, dict) and "t" in bar
        }
    )
    scores_by_timestamp: Dict[str, Dict[str, float]] = {}
    for symbol in config.symbols:
        closes = {
            bar["t"]: float(bar.get("c", 0.0))
            for bar in bars_by_symbol.get(symbol, [])
            if isinstance(bar, dict) and "t" in bar
        }
        if not closes:
            continue
        values = list(closes.values())
        mean = sum(values) / len(values)
        for timestamp, close in closes.items():
            if timestamp not in scores_by_timestamp:
                scores_by_timestamp[timestamp] = {}
            scores_by_timestamp[timestamp][symbol] = (
                (close - mean) / mean if mean else 0.0
            )

    rs_lookback = min(DEFAULT_RS_LOOKBACK_DAYS, max(1, len(timestamps) - 1))
    rs_map = build_rs_map_from_bars(
        bars_by_symbol=bars_by_symbol,
        symbols=config.symbols,
        benchmarks=config.benchmarks,
        lookback_days=rs_lookback,
    )

    events = [
        BacktestEvent(
            timestamp=timestamp,
            event_type="market_snapshot",
            sequence=index + 1,
        )
        for index, timestamp in enumerate(timestamps)
    ]
    return events, scores_by_timestamp, rs_map, dict(bars_by_symbol)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_historical_validation(
    config: HistoricalValidationConfig,
    *,
    research_client: Any = None,
    llm_client: Any = None,
    warehouse_reader: Any = None,
    generated_at: Optional[str] = None,
    challenger_factory: Any = None,
) -> HistoricalValidationBundle:
    """Run the full two-month historical validation pipeline.

    Fixture mode (default): uses ``config.fixture_events`` +
    ``config.fixture_champion_scores`` + ``config.fixture_rs_map``.

    Live mode (opt-in): pass ``config.live_fetch=True``.  The
    orchestrator resolves bars in priority order:

    1. ``warehouse_reader`` if supplied and it reports complete
       coverage for the requested symbols + window.  This is the
       Phase 5.6 bridge — the warehouse is authoritative when
       populated.  ``research_client.fetch_bars`` is NOT called.
    2. ``research_client.fetch_bars(...)`` when the warehouse has
       incomplete (or no) coverage and a client is supplied.

    Callers passing neither ``warehouse_reader`` nor
    ``research_client`` alongside ``live_fetch=True`` still hit
    :class:`LiveFetchNotAvailableError` — the orchestrator refuses
    to silently fall through to fixture mode.

    The orchestrator never constructs an
    :class:`~strategy.promotion_gates.ApprovalRecord`.  The returned
    :class:`PromotionEntry` sits at ``current_state = "disabled"``.
    """
    if config.live_fetch and research_client is None and warehouse_reader is None:
        raise LiveFetchNotAvailableError(
            "config.live_fetch=True but neither research_client nor "
            "warehouse_reader was supplied; the orchestrator will not "
            "silently fall through to fixture mode"
        )

    generated = generated_at or _utc_now_iso()
    warnings: List[str] = []
    dataset_provenance: Dict[str, Any] = {}

    bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    if config.live_fetch:
        events, champion_scores, rs_map, bars_by_symbol, dataset_provenance = (
            _fetch_live_events_priority(
                config, warehouse_reader, research_client, warnings
            )
        )
        live_fetch_used = True
        # Persist a fixture-style events snapshot so downstream reruns
        # can reproduce the run without hitting the live API.
        fixture_config = HistoricalValidationConfig(
            dataset_id=config.dataset_id,
            symbols=config.symbols,
            window_start=config.window_start,
            window_end=config.window_end,
            benchmarks=config.benchmarks,
            research_data_root=config.research_data_root,
            report_root=config.report_root,
            in_sample_days=config.in_sample_days,
            out_of_sample_days=config.out_of_sample_days,
            step_days=config.step_days,
            champion_id=config.champion_id,
            challenger_id=config.challenger_id,
            seed=config.seed,
            score_delta_threshold=config.score_delta_threshold,
            live_fetch=False,
            fixture_events=tuple(events),
            fixture_champion_scores=champion_scores,
            fixture_rs_map=rs_map,
        )
        dataset_manifest_path = _write_dataset_manifest(fixture_config)
    else:
        events = list(config.fixture_events)
        champion_scores = dict(config.fixture_champion_scores)
        rs_map = dict(config.fixture_rs_map)
        live_fetch_used = False
        dataset_manifest_path = _write_dataset_manifest(config)

    if not events:
        warnings.append("no events supplied; downstream artifacts will be empty")

    # Champion: when live-fetch bars are available, use the research
    # ChampionScorer so evaluations carry structured explanations.
    # Otherwise (fixture path) fall back to _FixtureChampion.
    champion: Any
    if bars_by_symbol:
        champion = ChampionScorer(
            strategy_id=config.champion_id,
            bars_by_symbol=bars_by_symbol,
            symbols=config.symbols,
            config=ChampionScoringConfig(),
        )
    else:
        champion = _FixtureChampion(
            strategy_id=config.champion_id,
            scores_by_timestamp=champion_scores,
        )
    # Locally-scoped FeatureFlags for the challenger — global flags
    # remain disabled throughout.
    local_flags = FeatureFlags(enable_relative_strength=True)
    if challenger_factory is not None:
        challenger = challenger_factory(
            champion=champion,
            bars_by_symbol=bars_by_symbol,
            symbols=config.symbols,
            rs_map=rs_map,
            local_flags=local_flags,
        )
    else:
        challenger = RelativeStrengthChallenger(
            base_evaluator=champion,
            rs_provider=rs_provider_from_map(rs_map),
            flags=local_flags,
            strategy_id=config.challenger_id,
        )

    comparison = _run_comparison(
        config, champion, challenger, events, generated
    )
    walk_forward_report = _run_walk_forward(
        config, champion, challenger, events, generated
    )
    findings = _build_findings(comparison, walk_forward_report)
    explanation_summary = _build_explanation_summary(comparison)
    learning_report = render_learning_report(
        findings=findings,
        title=f"Learning Report — {config.dataset_id}",
        generated_at=generated,
        explanation_summary=explanation_summary,
    )

    reports_root = Path(config.report_root)
    reports_root.mkdir(parents=True, exist_ok=True)
    backtests_root = reports_root / "backtests"
    walk_forward_root = reports_root / "walk_forward"
    learning_root = reports_root / "learning"
    analyst_root = reports_root / "analyst"

    comparison_report = render_comparison_report(comparison, generated_at=generated)
    comparison_report_paths = comparison_report.write(str(backtests_root))

    walk_forward_research_report = render_walk_forward_report(
        walk_forward_report, generated_at=generated
    )
    walk_forward_report_paths = walk_forward_research_report.write(
        str(walk_forward_root)
    )
    learning_report_paths = learning_report.write(str(learning_root))

    analyst_narratives: List[LLMNarrative] = []
    analyst_reports: List[ResearchAnalystReport] = []
    if llm_client is not None:
        from strategy.research_analyst import ResearchAnalyst

        analyst = ResearchAnalyst(llm_client)
        # Wrap comparison + walk-forward in provenance-aware shims
        # so the analyst payload carries dataset_provenance without
        # requiring a research_analyst.py change.
        comparison_for_analyst = _with_provenance(
            comparison, dataset_provenance
        )
        walk_forward_for_analyst = _with_provenance(
            walk_forward_report, dataset_provenance
        )
        analyst_narratives = [
            analyst.analyze_comparison(
                comparison_for_analyst, generated_at=generated
            ),
            analyst.analyze_walk_forward(
                walk_forward_for_analyst, generated_at=generated
            ),
            analyst.analyze_learning_report(
                learning_report, generated_at=generated
            ),
        ]
        analyst_reports = [
            render_analyst_report(
                narrative, generated_at=generated
            )
            for narrative in analyst_narratives
        ]
        for narrative in analyst_narratives:
            warnings.extend(narrative.warnings)

    analyst_report_paths = _write_analyst_reports(
        analyst_reports, str(analyst_root)
    )

    promotion_entry = _build_promotion_entry(
        config=config,
        dataset_manifest_path=dataset_manifest_path,
        comparison=comparison,
        walk_forward_report=walk_forward_report,
        learning_report=learning_report,
        analyst_reports=analyst_reports,
        dataset_provenance=dataset_provenance,
    )

    # Belt-and-suspenders: refuse to return a bundle with a
    # non-disabled entry or any ApprovalRecord.
    if promotion_entry.current_state != STATE_DISABLED:
        raise HistoricalValidationError(
            "promotion_entry advanced past disabled — refusing to return"
        )
    if promotion_entry.approvals:
        raise HistoricalValidationError(
            "promotion_entry carries ApprovalRecord entries — refusing to return"
        )

    return HistoricalValidationBundle(
        config=config,
        dataset_manifest_path=dataset_manifest_path,
        comparison=comparison,
        comparison_report=comparison_report,
        comparison_report_paths=comparison_report_paths,
        walk_forward_report=walk_forward_report,
        walk_forward_report_paths=walk_forward_report_paths,
        walk_forward_research_report=walk_forward_research_report,
        findings=findings,
        learning_report=learning_report,
        learning_report_paths=learning_report_paths,
        analyst_narratives=analyst_narratives,
        analyst_reports=analyst_reports,
        analyst_report_paths=analyst_report_paths,
        promotion_entry=promotion_entry,
        warnings=warnings,
        live_fetch_used=live_fetch_used,
        generated_at=generated,
        dataset_provenance=dict(dataset_provenance),
    )
