"""Helpers for the ``traderjoe-simulate`` CLI.

Small, testable wiring: build strategies by name, load bars from
the warehouse, and compute canonical window bounds.  Kept separate
from :mod:`strategy.portfolio_simulator` so the simulator itself
stays free of CLI, filesystem, and warehouse concerns.

Read-only: no live-runner imports, no broker credentials, no
order path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from strategy.backtest_lab import BacktestEvent
from strategy.champion_scoring import ChampionScoringConfig
from strategy.config import FeatureFlags
from strategy.lab.champion import ChampionStrategy
from strategy.lab.champion_rs import ChampionRelativeStrengthStrategy
from strategy.lab.mean_reversion import MeanReversionStrategy
from strategy.lab.momentum import MomentumStrategy
from strategy.lab.trend import TrendStrategy
from strategy.portfolio_simulator import (
    PortfolioSimulator,
    PortfolioSimulatorConfig,
    PortfolioSimulationResult,
    bars_from_warehouse_hit_bars,
    events_from_bars,
)
from strategy.rs_challenger import (
    DEFAULT_RS_LOOKBACK_DAYS,
    build_rs_map_from_bars,
    rs_provider_from_map,
)


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------


CHAMPION_KEY = "champion-v0.4.0"
CHAMPION_RS_KEY = "champion-rs-v0.1.0"
RS_CHALLENGER_KEY = "rs-challenger-v0.1.0"
MOMENTUM_KEY = "momentum-v0.1.0"
TREND_KEY = "trend-v0.1.0"
MEAN_REVERSION_KEY = "mean_reversion-v0.1.0"


KNOWN_STRATEGIES: Tuple[str, ...] = (
    CHAMPION_KEY,
    CHAMPION_RS_KEY,
    RS_CHALLENGER_KEY,
    MOMENTUM_KEY,
    TREND_KEY,
    MEAN_REVERSION_KEY,
)


@dataclass
class StrategyBundle:
    """The wired strategy + metadata a simulator run needs."""

    key: str
    name: str
    version: str
    strategy_id: str
    evaluator: Any


def _normalize_key(raw: str) -> str:
    lowered = raw.strip().lower()
    aliases = {
        "champion": CHAMPION_KEY,
        "champion-v0.4.0": CHAMPION_KEY,
        "champion_rs": CHAMPION_RS_KEY,
        "champion-rs": CHAMPION_RS_KEY,
        "champion-rs-v0.1.0": CHAMPION_RS_KEY,
        "rs": RS_CHALLENGER_KEY,
        "rs-challenger": RS_CHALLENGER_KEY,
        "rs-challenger-v0.1.0": RS_CHALLENGER_KEY,
        "momentum": MOMENTUM_KEY,
        "momentum-v0.1.0": MOMENTUM_KEY,
        "trend": TREND_KEY,
        "trend-v0.1.0": TREND_KEY,
        "mean_reversion": MEAN_REVERSION_KEY,
        "mean-reversion": MEAN_REVERSION_KEY,
        "mean_reversion-v0.1.0": MEAN_REVERSION_KEY,
    }
    if lowered in aliases:
        return aliases[lowered]
    raise ValueError(
        f"unknown strategy '{raw}'. Known strategies: "
        f"{', '.join(KNOWN_STRATEGIES)}"
    )


def build_strategy_bundle(
    strategy_key: str,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    symbols: Sequence[str],
    benchmarks: Sequence[str] = ("SPY", "QQQ"),
) -> StrategyBundle:
    """Instantiate the requested strategy against the caller-supplied
    bars payload.  Champion / RS variants receive an
    :class:`rs_provider` derived from the same bars.

    Callers pass either a full strategy key
    (``champion-v0.4.0``) or one of the aliases above.
    """
    key = _normalize_key(strategy_key)
    if key == CHAMPION_KEY:
        strategy = ChampionStrategy()
        evaluator = strategy.build_evaluator(bars_by_symbol, symbols)
        return StrategyBundle(
            key=CHAMPION_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key in (CHAMPION_RS_KEY, RS_CHALLENGER_KEY):
        strategy = ChampionRelativeStrengthStrategy()
        available_bench = [b for b in benchmarks if b in bars_by_symbol]
        if not available_bench:
            available_bench = [
                s
                for s in symbols
                if s in bars_by_symbol
            ][:1]
        timestamps = sorted(
            {
                str(bar.get("t"))
                for symbol_bars in bars_by_symbol.values()
                for bar in symbol_bars
                if isinstance(bar, Mapping) and bar.get("t") is not None
            }
        )
        lookback = min(DEFAULT_RS_LOOKBACK_DAYS, max(1, len(timestamps) - 1))
        rs_map = build_rs_map_from_bars(
            bars_by_symbol=bars_by_symbol,
            symbols=list(symbols),
            benchmarks=tuple(available_bench),
            lookback_days=lookback,
        )
        rs_provider = rs_provider_from_map(rs_map)
        flags = FeatureFlags(enable_relative_strength=True)
        evaluator = strategy.build_evaluator(
            bars_by_symbol,
            symbols,
            rs_provider=rs_provider,
            flags=flags,
        )
        return StrategyBundle(
            key=key,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == MOMENTUM_KEY:
        strategy = MomentumStrategy()
        evaluator = strategy.build_evaluator(bars_by_symbol, symbols)
        return StrategyBundle(
            key=MOMENTUM_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == TREND_KEY:
        strategy = TrendStrategy()
        evaluator = strategy.build_evaluator(bars_by_symbol, symbols)
        return StrategyBundle(
            key=TREND_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == MEAN_REVERSION_KEY:
        strategy = MeanReversionStrategy()
        evaluator = strategy.build_evaluator(bars_by_symbol, symbols)
        return StrategyBundle(
            key=MEAN_REVERSION_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    raise ValueError(f"unhandled strategy key {key!r}")


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


PRESET_WINDOWS: Dict[str, int] = {
    "60d": 60,
    "90d": 90,
    "6mo": 182,
    "1y": 365,
}


def resolve_window(
    preset: Optional[str],
    explicit_start: Optional[str],
    explicit_end: Optional[str],
    today: Optional[date] = None,
) -> Tuple[str, str]:
    """Return ``(start_iso, end_iso)``.

    Precedence:
      1. Explicit ``--start`` / ``--end`` (both required together).
      2. ``--window`` preset counted back from ``today``.
    """
    if explicit_start and explicit_end:
        return explicit_start, explicit_end
    if explicit_start or explicit_end:
        raise ValueError(
            "--start and --end must both be provided (or use --window)"
        )
    if preset is None:
        raise ValueError(
            "must provide --window (60d|90d|6mo|1y) or --start/--end"
        )
    days = PRESET_WINDOWS.get(preset)
    if days is None:
        raise ValueError(
            f"unknown window preset '{preset}'; "
            f"choose one of {sorted(PRESET_WINDOWS)}"
        )
    end = today or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Warehouse loader
# ---------------------------------------------------------------------------


def load_bars_from_warehouse(
    symbols: Sequence[str],
    start: str,
    end: str,
    warehouse_root: Optional[str] = None,
    allow_provider_fallback: bool = False,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """Load daily bars for ``symbols`` between ``start`` and ``end``
    from the local warehouse.

    Returns a ``(bars_by_symbol, provenance)`` tuple.  ``provenance``
    identifies the datasets used so the simulation report can cite
    provenance.

    ``allow_provider_fallback`` is exposed for parity with the
    research CLI but defaults to ``False``: the simulator is
    warehouse-first and refuses to hit a provider unless the caller
    explicitly opts in.  In that case the caller must also register
    a provider — the loader itself never registers one on the
    caller's behalf.
    """
    from strategy.local_warehouse import WarehouseLayout
    from strategy.market_data_provider import (
        AdjustmentMode,
        AssetClass,
        BarInterval,
    )
    from strategy.warehouse.research_cache import (
        ResearchCacheError,
        WarehouseReader,
    )

    env: Dict[str, str] = dict(os.environ)
    if warehouse_root:
        env["WAREHOUSE_ROOT"] = warehouse_root
    layout = WarehouseLayout.from_env(env)

    provenance: Dict[str, Any] = {
        "source": "warehouse",
        "warehouse_root": str(layout.root),
        "datasets": [],
    }
    bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    seen: Dict[str, str] = {}
    with WarehouseReader(layout=layout) as reader:
        for symbol in symbols:
            try:
                hit = reader.fetch_bars(
                    asset_class=AssetClass.EQUITY,
                    interval=BarInterval.DAILY,
                    symbols=[symbol],
                    start=start,
                    end=end,
                    adjustment=AdjustmentMode.RAW,
                )
            except ResearchCacheError as exc:
                if allow_provider_fallback:
                    provenance.setdefault("warnings", []).append(
                        f"{symbol}: {exc}"
                    )
                    continue
                raise
            bars_by_symbol[symbol] = bars_from_warehouse_hit_bars(hit.bars)
            for ds_id, ver in zip(
                (hit.dataset_id or "").split(","),
                (hit.dataset_version or "").split(","),
            ):
                if ds_id and ds_id not in seen:
                    seen[ds_id] = ver
                    provenance["datasets"].append(
                        {"dataset_id": ds_id, "version": ver}
                    )
    return bars_by_symbol, provenance


# ---------------------------------------------------------------------------
# End-to-end helper
# ---------------------------------------------------------------------------


def run_simulation(
    strategy_key: str,
    symbols: Sequence[str],
    start: str,
    end: str,
    config: Optional[PortfolioSimulatorConfig] = None,
    warehouse_root: Optional[str] = None,
    allow_provider_fallback: bool = False,
    benchmarks: Sequence[str] = ("SPY", "QQQ"),
    dataset_provenance: Optional[Mapping[str, Any]] = None,
    bars_by_symbol: Optional[
        Mapping[str, Sequence[Mapping[str, Any]]]
    ] = None,
) -> PortfolioSimulationResult:
    """One-shot orchestrator: load warehouse bars → build strategy →
    simulate → return the result.

    Tests can bypass the warehouse loader by passing ``bars_by_symbol``
    directly.
    """
    # Fail fast on unknown strategy keys before touching the warehouse.
    _normalize_key(strategy_key)

    prov: Dict[str, Any]
    bars: Dict[str, List[Dict[str, Any]]]
    if bars_by_symbol is None:
        bars, prov = load_bars_from_warehouse(
            symbols=symbols,
            start=start,
            end=end,
            warehouse_root=warehouse_root,
            allow_provider_fallback=allow_provider_fallback,
        )
    else:
        bars = {sym: list(v) for sym, v in bars_by_symbol.items()}
        prov = dict(dataset_provenance or {"source": "in_memory"})

    bundle = build_strategy_bundle(
        strategy_key,
        bars,
        symbols,
        benchmarks=tuple(benchmarks),
    )

    events = events_from_bars(bars)
    simulator = PortfolioSimulator(
        evaluator=bundle.evaluator,
        bars_by_symbol=bars,
        symbols=list(symbols),
        events=events,
        config=config or PortfolioSimulatorConfig(),
        strategy_name=bundle.name,
        strategy_version=bundle.version,
        window_start=start,
        window_end=end,
        dataset_provenance=prov,
    )
    return simulator.run()


__all__ = [
    "CHAMPION_KEY",
    "CHAMPION_RS_KEY",
    "KNOWN_STRATEGIES",
    "MEAN_REVERSION_KEY",
    "MOMENTUM_KEY",
    "PRESET_WINDOWS",
    "RS_CHALLENGER_KEY",
    "StrategyBundle",
    "TREND_KEY",
    "build_strategy_bundle",
    "load_bars_from_warehouse",
    "resolve_window",
    "run_simulation",
]
