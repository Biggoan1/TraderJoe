"""Tests for strategy/lab/registry.py — Card 3."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.config import reset_feature_flags
from strategy.lab import (
    RegistryConflictError,
    Strategy,
    StrategyNotFoundError,
    StrategyRegistry,
    discover_strategies,
    get_default_registry,
)
from strategy.lab.champion import ChampionStrategy
from strategy.lab.champion_rs import ChampionRelativeStrengthStrategy
from strategy.lab.mean_reversion import MeanReversionStrategy
from strategy.lab.momentum import MomentumStrategy
from strategy.lab.strategy import StrategyBase
from strategy.lab.trend import TrendStrategy


# ---------------------------------------------------------------------------
# Fresh isolated registry per test
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return StrategyRegistry()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_one_strategy(self, registry):
        entry = registry.register(ChampionStrategy)
        assert entry.name == "champion"
        assert entry.factory is ChampionStrategy
        assert registry.is_registered("champion")

    def test_register_idempotent_for_same_class(self, registry):
        registry.register(ChampionStrategy)
        registry.register(ChampionStrategy)  # no-op
        assert registry.names() == ["champion"]

    def test_register_conflict_raises(self, registry):
        class Fake(StrategyBase):
            name = "champion"
            version = "9.9.9"

            def parameters(self):
                return {}

            def required_history(self):
                return 1

            def build_evaluator(self, bars_by_symbol, symbols, **kwargs):
                raise NotImplementedError

        registry.register(ChampionStrategy)
        with pytest.raises(RegistryConflictError, match="already registered"):
            registry.register(Fake)

    def test_register_without_name_raises(self, registry):
        class NoName(StrategyBase):
            name = ""
            version = "0.1.0"

            def parameters(self):
                return {}

            def required_history(self):
                return 1

            def build_evaluator(self, bars_by_symbol, symbols, **kwargs):
                raise NotImplementedError

        with pytest.raises(Exception, match="name"):
            registry.register(NoName)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestLookup:
    def test_get_returns_entry(self, registry):
        registry.register(ChampionStrategy)
        entry = registry.get("champion")
        assert entry.name == "champion"

    def test_get_missing_raises(self, registry):
        with pytest.raises(StrategyNotFoundError):
            registry.get("nonexistent")

    def test_build_returns_strategy_instance(self, registry):
        registry.register(ChampionStrategy)
        instance = registry.build("champion")
        assert isinstance(instance, ChampionStrategy)
        assert isinstance(instance, Strategy)

    def test_build_forwards_kwargs(self, registry):
        registry.register(MomentumStrategy)
        instance = registry.build("momentum", lookback=5)
        assert instance.lookback == 5

    def test_names_sorted(self, registry):
        registry.register(TrendStrategy)
        registry.register(ChampionStrategy)
        registry.register(MomentumStrategy)
        assert registry.names() == sorted(["trend", "champion", "momentum"])

    def test_unregister(self, registry):
        registry.register(ChampionStrategy)
        assert registry.is_registered("champion")
        registry.unregister("champion")
        assert not registry.is_registered("champion")


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_registers_expected_strategies(self, registry):
        registry.discover("strategy.lab")
        registered = set(registry.names())
        # All five spec-called-out strategies must be discovered
        expected = {
            "champion", "champion_rs", "momentum", "trend", "mean_reversion",
        }
        assert expected.issubset(registered), (
            f"missing: {expected - registered}"
        )

    def test_discover_idempotent(self, registry):
        first = registry.discover("strategy.lab")
        second = registry.discover("strategy.lab")
        # Same entries returned both times
        assert {e.name for e in first} == {e.name for e in second}
        # No conflict raised

    def test_discover_skips_framework_modules(self, registry):
        registry.discover("strategy.lab")
        # StrategyBase itself never gets registered
        for entry in registry.entries():
            assert entry.factory is not StrategyBase

    def test_discover_records_source_module(self, registry):
        registry.discover("strategy.lab")
        champion_entry = registry.get("champion")
        assert champion_entry.source_module == "strategy.lab.champion"
        momentum_entry = registry.get("momentum")
        assert momentum_entry.source_module == "strategy.lab.momentum"


# ---------------------------------------------------------------------------
# Instance-based verification: each discovered strategy actually scores
# ---------------------------------------------------------------------------


def _daily_bars(days=60):
    return [
        {
            "t": f"2020-{((i//25)%12)+1:02d}-{(i%25)+1:02d}T14:30:00+00:00",
            "o": 100.0 + i * 0.5 - 0.1,
            "h": 100.0 + i * 0.5 + 0.5,
            "l": 100.0 + i * 0.5 - 0.5,
            "c": 100.0 + i * 0.5,
            "v": 1_000_000,
        }
        for i in range(days)
    ]


def _event():
    return BacktestEvent(
        timestamp="2020-04-15T14:30:00+00:00",
        event_type="market_snapshot",
        sequence=1,
    )


SYMBOLS = ("AAPL", "MSFT")


class TestDiscoveredStrategiesScore:
    @pytest.mark.parametrize("cls", [
        ChampionStrategy, MomentumStrategy, TrendStrategy,
        MeanReversionStrategy,
    ])
    def test_strategy_can_score(self, cls):
        reset_feature_flags()
        strategy = cls()
        bars = {sym: _daily_bars() for sym in SYMBOLS}
        scores = strategy.score(_event(), bars, SYMBOLS)
        assert set(scores.keys()) == set(SYMBOLS)

    def test_champion_rs_scores_with_provider(self):
        reset_feature_flags()
        strategy = ChampionRelativeStrengthStrategy()
        bars = {sym: _daily_bars() for sym in SYMBOLS}
        def provider(symbol, event):
            return 60.0
        scores = strategy.score(
            _event(), bars, SYMBOLS, rs_provider=provider,
        )
        assert set(scores.keys()) == set(SYMBOLS)


class TestSingleFactorStrategyMath:
    def test_momentum_ranks_higher_bars_higher(self):
        strategy = MomentumStrategy(lookback=10)
        # AAPL trending up strongly; MSFT trending flat.
        aapl = [{"t": f"2020-01-{i+1:02d}T14:30:00Z", "c": 100.0 + i, "o": 100.0, "h": 100.0, "l": 100.0, "v": 1e6} for i in range(30)]
        msft = [{"t": f"2020-01-{i+1:02d}T14:30:00Z", "c": 100.0, "o": 100.0, "h": 100.0, "l": 100.0, "v": 1e6} for i in range(30)]
        bars = {"AAPL": aapl, "MSFT": msft}
        scores = strategy.score(
            BacktestEvent(timestamp="2020-01-31T14:30:00Z",
                          event_type="market_snapshot", sequence=1),
            bars, ("AAPL", "MSFT"),
        )
        assert scores["AAPL"] > scores["MSFT"]

    def test_mean_reversion_scores_dip_higher(self):
        strategy = MeanReversionStrategy(lookback=10)
        # AAPL just dropped; MSFT above its mean.
        aapl = [{"t": f"2020-01-{i+1:02d}T14:30:00Z", "c": 100.0, "o": 100.0, "h": 100.0, "l": 100.0, "v": 1e6} for i in range(15)]
        aapl[-1] = dict(aapl[-1], c=80.0)
        msft = [{"t": f"2020-01-{i+1:02d}T14:30:00Z", "c": 100.0, "o": 100.0, "h": 100.0, "l": 100.0, "v": 1e6} for i in range(14)]
        msft.append(dict(msft[-1], c=120.0))
        bars = {"AAPL": aapl, "MSFT": msft}
        scores = strategy.score(
            BacktestEvent(timestamp="2020-01-16T14:30:00Z",
                          event_type="market_snapshot", sequence=1),
            bars, ("AAPL", "MSFT"),
        )
        assert scores["AAPL"] > scores["MSFT"]


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_get_default_registry_singleton(self):
        assert get_default_registry() is get_default_registry()

    def test_discover_strategies_populates_default(self):
        # Snapshot then repopulate — should not error and should
        # contain all five strategies.
        discover_strategies("strategy.lab")
        default = get_default_registry()
        expected = {"champion", "champion_rs", "momentum", "trend",
                    "mean_reversion"}
        assert expected.issubset(set(default.names()))


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


LAB_STRATEGY_MODULES = (
    "strategy.lab.registry",
    "strategy.lab.momentum",
    "strategy.lab.trend",
    "strategy.lab.mean_reversion",
    "strategy.lab.single_factor",
)


class TestSourceSafety:
    @pytest.mark.parametrize("mod", LAB_STRATEGY_MODULES)
    def test_no_live_runner_imports(self, mod):
        s = _module_source(mod)
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import", "from trader_cli import",
                      "from telegram_approvals import",
                      "from strategy.runner import"):
            assert token not in s

    @pytest.mark.parametrize("mod", LAB_STRATEGY_MODULES)
    def test_no_order_path_tokens(self, mod):
        s = _module_source(mod)
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    @pytest.mark.parametrize("mod", LAB_STRATEGY_MODULES)
    def test_no_approval_or_promotion_construction(self, mod):
        s = _module_source(mod)
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s

    @pytest.mark.parametrize("mod", LAB_STRATEGY_MODULES)
    def test_no_credential_env_reads(self, mod):
        s = _module_source(mod)
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, s)
