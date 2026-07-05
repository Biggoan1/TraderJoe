"""Experiment presets for the portfolio simulator / research CLIs.

Each preset bundles a fixed universe, a set of strategies, and
optional simulator overrides.  Presets are consulted by the
Strategy Lab / research CLIs — they are pure metadata and do not
place any orders, load env files, or talk to a broker.

Read-only, research-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from strategy.market_data_provider import BarInterval


DAILY_SWING_WATCHLIST_PRESET = "daily_swing_watchlist"
INTRADAY_15M_WATCHLIST_PRESET = "intraday_15m_watchlist"
CRYPTO_24X7_PAPER_RESEARCH_PRESET = "crypto_24x7_paper_research"


@dataclass(frozen=True)
class ExperimentPreset:
    """Portable preset for a research / simulation run."""

    name: str
    description: str
    asset_class: str  # "equity" | "crypto"
    interval: str  # BarInterval.value
    universe: Tuple[str, ...]
    benchmarks: Tuple[str, ...]
    strategies: Tuple[str, ...]
    default_window_preset: str = "6mo"
    simulator_overrides: Mapping[str, Any] = field(default_factory=dict)
    sector_map: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""
    schedule: str = "manual"  # "manual" | "15m_regular_hours" | "24x7"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "asset_class": self.asset_class,
            "interval": self.interval,
            "universe": list(self.universe),
            "benchmarks": list(self.benchmarks),
            "strategies": list(self.strategies),
            "default_window_preset": self.default_window_preset,
            "simulator_overrides": dict(self.simulator_overrides),
            "sector_map": dict(self.sector_map),
            "notes": self.notes,
            "schedule": self.schedule,
        }


PRESETS: Dict[str, ExperimentPreset] = {
    DAILY_SWING_WATCHLIST_PRESET: ExperimentPreset(
        name=DAILY_SWING_WATCHLIST_PRESET,
        description=(
            "Daily swing candidates on the current equity watchlist. "
            "Uses Champion plus the daily research strategies against "
            "the 6-month warehouse."
        ),
        asset_class="equity",
        interval=BarInterval.DAILY.value,
        universe=("AAPL", "MSFT", "NVDA", "SPY", "QQQ"),
        benchmarks=("SPY", "QQQ"),
        strategies=(
            "champion-v0.4.0",
            "momentum-v0.1.0",
            "trend-v0.1.0",
            "rsi_mean_reversion_v1",
            "sector_rotation_daily_v1",
            "volatility_regime_filter_v1",
        ),
        default_window_preset="6mo",
        simulator_overrides={
            "starting_cash": 100_000.0,
            "max_open_positions": 5,
        },
        sector_map={
            "AAPL": "SPY",
            "MSFT": "SPY",
            "NVDA": "SPY",
            "QQQ": "SPY",
        },
        notes=(
            "Research-only. Runs against the local warehouse. "
            "Does NOT auto-promote any strategy to paper trading."
        ),
        schedule="manual",
    ),
    INTRADAY_15M_WATCHLIST_PRESET: ExperimentPreset(
        name=INTRADAY_15M_WATCHLIST_PRESET,
        description=(
            "Intraday 15-minute research on the equity watchlist. "
            "Requires intraday coverage in the warehouse — will "
            "refuse to run against daily-only data."
        ),
        asset_class="equity",
        interval=BarInterval.MINUTE_15.value,
        universe=("AAPL", "MSFT", "NVDA", "SPY", "QQQ"),
        benchmarks=("SPY", "QQQ"),
        strategies=(
            "momentum_15m_v1",
            "opening_range_breakout_v1",
        ),
        default_window_preset="60d",
        simulator_overrides={
            "starting_cash": 100_000.0,
            "max_open_positions": 3,
            "top_n_per_event": 3,
        },
        notes=(
            "Requires 15-minute equities warehouse data at "
            "market_data/equities/minute/<dataset>. Research-only. "
            "The paper daemon can be attached later once results "
            "are reviewed."
        ),
        schedule="15m_regular_hours",
    ),
    CRYPTO_24X7_PAPER_RESEARCH_PRESET: ExperimentPreset(
        name=CRYPTO_24X7_PAPER_RESEARCH_PRESET,
        description=(
            "Crypto 24/7 research bundle for BTC/ETH/SOL against "
            "the crypto paper account.  Simulation only — no orders "
            "are placed by this preset."
        ),
        asset_class="crypto",
        interval=BarInterval.HOURLY.value,
        universe=("BTC/USD", "ETH/USD", "SOL/USD"),
        benchmarks=("BTC/USD",),
        strategies=(
            "momentum-v0.1.0",
            "trend-v0.1.0",
            "rsi_mean_reversion_v1",
        ),
        default_window_preset="90d",
        simulator_overrides={
            "starting_cash": 25_000.0,
            "max_open_positions": 3,
            "top_n_per_event": 3,
        },
        notes=(
            "Crypto simulation runs are for research only. Live "
            "24/7 paper execution is gated behind PAPER_AUTO_EXECUTE_CRYPTO "
            "on the crypto paper daemon and never touches the "
            "equities paper account or the production env."
        ),
        schedule="24x7",
    ),
}


def get_preset(name: str) -> ExperimentPreset:
    """Return the preset matching ``name`` or raise ``KeyError``."""
    try:
        return PRESETS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown preset {name!r}. Known presets: {sorted(PRESETS)}"
        ) from exc


def list_presets() -> Tuple[str, ...]:
    return tuple(sorted(PRESETS))


__all__ = [
    "CRYPTO_24X7_PAPER_RESEARCH_PRESET",
    "DAILY_SWING_WATCHLIST_PRESET",
    "ExperimentPreset",
    "INTRADAY_15M_WATCHLIST_PRESET",
    "PRESETS",
    "get_preset",
    "list_presets",
]
