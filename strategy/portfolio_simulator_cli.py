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
from strategy.lab.momentum_15m import Momentum15mStrategy
from strategy.lab.opening_range_breakout import OpeningRangeBreakoutStrategy
from strategy.lab.rsi_mean_reversion import RSIMeanReversionStrategy
from strategy.lab.sector_rotation_daily import SectorRotationDailyStrategy
from strategy.lab.trend import TrendStrategy
from strategy.lab.volatility_regime_filter import VolatilityRegimeFilterStrategy
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
MOMENTUM_15M_KEY = "momentum_15m_v1"
OPENING_RANGE_BREAKOUT_KEY = "opening_range_breakout_v1"
RSI_MEAN_REVERSION_KEY = "rsi_mean_reversion_v1"
SECTOR_ROTATION_DAILY_KEY = "sector_rotation_daily_v1"
VOLATILITY_REGIME_FILTER_KEY = "volatility_regime_filter_v1"


KNOWN_STRATEGIES: Tuple[str, ...] = (
    CHAMPION_KEY,
    CHAMPION_RS_KEY,
    RS_CHALLENGER_KEY,
    MOMENTUM_KEY,
    TREND_KEY,
    MEAN_REVERSION_KEY,
    MOMENTUM_15M_KEY,
    OPENING_RANGE_BREAKOUT_KEY,
    RSI_MEAN_REVERSION_KEY,
    SECTOR_ROTATION_DAILY_KEY,
    VOLATILITY_REGIME_FILTER_KEY,
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
        "momentum_15m": MOMENTUM_15M_KEY,
        "momentum-15m": MOMENTUM_15M_KEY,
        "momentum_15m_v1": MOMENTUM_15M_KEY,
        "opening_range_breakout": OPENING_RANGE_BREAKOUT_KEY,
        "opening-range-breakout": OPENING_RANGE_BREAKOUT_KEY,
        "opening_range_breakout_v1": OPENING_RANGE_BREAKOUT_KEY,
        "rsi_mean_reversion": RSI_MEAN_REVERSION_KEY,
        "rsi-mean-reversion": RSI_MEAN_REVERSION_KEY,
        "rsi_mean_reversion_v1": RSI_MEAN_REVERSION_KEY,
        "sector_rotation_daily": SECTOR_ROTATION_DAILY_KEY,
        "sector-rotation-daily": SECTOR_ROTATION_DAILY_KEY,
        "sector_rotation_daily_v1": SECTOR_ROTATION_DAILY_KEY,
        "volatility_regime_filter": VOLATILITY_REGIME_FILTER_KEY,
        "volatility-regime-filter": VOLATILITY_REGIME_FILTER_KEY,
        "volatility_regime_filter_v1": VOLATILITY_REGIME_FILTER_KEY,
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
    interval: Optional[Any] = None,
    sector_map: Optional[Mapping[str, str]] = None,
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
    if key == MOMENTUM_15M_KEY:
        strategy = Momentum15mStrategy()
        evaluator = strategy.build_evaluator(
            bars_by_symbol, symbols, interval=interval
        )
        return StrategyBundle(
            key=MOMENTUM_15M_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == OPENING_RANGE_BREAKOUT_KEY:
        strategy = OpeningRangeBreakoutStrategy()
        evaluator = strategy.build_evaluator(
            bars_by_symbol, symbols, interval=interval
        )
        return StrategyBundle(
            key=OPENING_RANGE_BREAKOUT_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == RSI_MEAN_REVERSION_KEY:
        strategy = RSIMeanReversionStrategy()
        evaluator = strategy.build_evaluator(
            bars_by_symbol, symbols, interval=interval
        )
        return StrategyBundle(
            key=RSI_MEAN_REVERSION_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == SECTOR_ROTATION_DAILY_KEY:
        strategy = SectorRotationDailyStrategy()
        evaluator = strategy.build_evaluator(
            bars_by_symbol,
            symbols,
            interval=interval,
            sector_map=sector_map,
        )
        return StrategyBundle(
            key=SECTOR_ROTATION_DAILY_KEY,
            name=strategy.name,
            version=strategy.version,
            strategy_id=strategy.strategy_id,
            evaluator=evaluator,
        )
    if key == VOLATILITY_REGIME_FILTER_KEY:
        strategy = VolatilityRegimeFilterStrategy()
        evaluator = strategy.build_evaluator(
            bars_by_symbol, symbols, interval=interval
        )
        return StrategyBundle(
            key=VOLATILITY_REGIME_FILTER_KEY,
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
    asset_class: Optional[Any] = None,
    interval: Optional[Any] = None,
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

    _ac = AssetClass(asset_class) if isinstance(asset_class, str) else (asset_class or AssetClass.EQUITY)
    _iv = BarInterval(interval) if isinstance(interval, str) else (interval or BarInterval.DAILY)

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
                    asset_class=_ac,
                    interval=_iv,
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
    asset_class: Optional[Any] = None,
    interval: Optional[Any] = None,
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
            asset_class=asset_class,
            interval=interval,
        )
    else:
        bars = {sym: list(v) for sym, v in bars_by_symbol.items()}
        prov = dict(dataset_provenance or {"source": "in_memory"})

    from strategy.market_data_provider import BarInterval as _BI
    _iv = interval if not isinstance(interval, str) else _BI(interval)
    bundle = build_strategy_bundle(
        strategy_key,
        bars,
        symbols,
        benchmarks=tuple(benchmarks),
        interval=_iv,
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
    "MOMENTUM_15M_KEY",
    "MOMENTUM_KEY",
    "OPENING_RANGE_BREAKOUT_KEY",
    "PRESET_WINDOWS",
    "RSI_MEAN_REVERSION_KEY",
    "RS_CHALLENGER_KEY",
    "SECTOR_ROTATION_DAILY_KEY",
    "StrategyBundle",
    "TREND_KEY",
    "VOLATILITY_REGIME_FILTER_KEY",
    "build_strategy_bundle",
    "load_bars_from_warehouse",
    "resolve_window",
    "run_simulation",
]
