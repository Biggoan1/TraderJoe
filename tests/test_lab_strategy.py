"""Tests for strategy/lab/ — Card 1 (Strategy plug-in framework)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.champion_scoring import ChampionScoringConfig
from strategy.comparison_harness import ComparisonEvaluator
from strategy.config import reset_feature_flags
from strategy.lab import Strategy, StrategyIdentity, stable_parameter_hash
from strategy.lab.champion import (
    CHAMPION_STRATEGY_NAME,
    CHAMPION_STRATEGY_VERSION,
    ChampionStrategy,
)
from strategy.lab.champion_rs import (
    CHAMPION_RS_STRATEGY_NAME,
    CHAMPION_RS_STRATEGY_VERSION,
    ChampionRelativeStrengthStrategy,
)
from strategy.lab.strategy import StrategyBase


# ---------------------------------------------------------------------------
# Bar fixtures — enough history for min_gate_count to fire.
# ---------------------------------------------------------------------------


def _daily_bars(days: int = 80, base_price: float = 100.0):
    """Deterministic increasing-price bars — every gate fires."""
    bars = []
    for i in range(days):
        price = base_price + i * 0.5
        bars.append(
            {
                "t": f"2020-01-{(i % 30) + 1:02d}T14:30:00+00:00",
                "o": price - 0.1,
                "h": price + 0.5,
                "l": price - 0.5,
                "c": price,
                "v": 1_000_000 + i * 1000,
            }
        )
    # Regenerate timestamps so they're strictly ordered
    for idx, bar in enumerate(bars):
        year = 2020 + idx // 250
        day_of_year = idx % 250 + 1
        month = (day_of_year - 1) // 25 + 1
        day = (day_of_year - 1) % 25 + 1
        bar["t"] = f"{year:04d}-{month:02d}-{day:02d}T14:30:00+00:00"
    return bars


def _event(ts: str = "2020-04-15T14:30:00+00:00"):
    return BacktestEvent(
        timestamp=ts,
        event_type="market_snapshot",
        payload={},
        sequence=1,
    )


# ---------------------------------------------------------------------------
# stable_parameter_hash
# ---------------------------------------------------------------------------


class TestStableParameterHash:
    def test_same_input_same_hash(self):
        h1 = stable_parameter_hash("s", "1.0", {"a": 1, "b": 2})
        h2 = stable_parameter_hash("s", "1.0", {"a": 1, "b": 2})
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_insertion_order_ignored(self):
        h1 = stable_parameter_hash("s", "1.0", {"a": 1, "b": 2})
        h2 = stable_parameter_hash("s", "1.0", {"b": 2, "a": 1})
        assert h1 == h2

    def test_different_parameters_different_hash(self):
        h1 = stable_parameter_hash("s", "1.0", {"a": 1})
        h2 = stable_parameter_hash("s", "1.0", {"a": 2})
        assert h1 != h2

    def test_different_version_different_hash(self):
        h1 = stable_parameter_hash("s", "1.0", {"a": 1})
        h2 = stable_parameter_hash("s", "1.1", {"a": 1})
        assert h1 != h2

    def test_different_name_different_hash(self):
        h1 = stable_parameter_hash("champion", "1.0", {"a": 1})
        h2 = stable_parameter_hash("momentum", "1.0", {"a": 1})
        assert h1 != h2

    def test_nested_and_tuple_normalisation(self):
        h1 = stable_parameter_hash(
            "s", "1.0", {"a": (1, 2, 3), "b": {"c": 4}}
        )
        h2 = stable_parameter_hash(
            "s", "1.0", {"b": {"c": 4}, "a": [1, 2, 3]}
        )
        assert h1 == h2


# ---------------------------------------------------------------------------
# StrategyIdentity
# ---------------------------------------------------------------------------


class TestStrategyIdentity:
    def test_to_dict_shape(self):
        ident = StrategyIdentity(
            name="s", version="1.0", parameters={"a": 1},
            stable_hash="deadbeef" * 8,
        )
        d = ident.to_dict()
        assert d["name"] == "s"
        assert d["version"] == "1.0"
        assert d["parameters"] == {"a": 1}
        assert d["stable_hash"] == "deadbeef" * 8


# ---------------------------------------------------------------------------
# StrategyBase — default methods
# ---------------------------------------------------------------------------


class _DummyStrategy(StrategyBase):
    name = "dummy"
    version = "0.1.0"

    def parameters(self):
        return {"k": 1}

    def required_history(self):
        return 5

    def build_evaluator(self, bars_by_symbol, symbols, **kwargs):
        raise RuntimeError("build_evaluator not needed for these tests")


class TestStrategyBaseDefaults:
    def test_stable_hash_uses_name_version_parameters(self):
        s = _DummyStrategy()
        expected = stable_parameter_hash("dummy", "0.1.0", {"k": 1})
        assert s.stable_hash() == expected

    def test_identity_snapshot(self):
        s = _DummyStrategy()
        ident = s.identity()
        assert ident.name == "dummy"
        assert ident.version == "0.1.0"
        assert ident.parameters == {"k": 1}
        assert ident.stable_hash == s.stable_hash()


# ---------------------------------------------------------------------------
# ChampionStrategy (Strategy #1)
# ---------------------------------------------------------------------------


SYMBOLS = ("AAPL", "MSFT")


def _bars_for_symbols():
    return {sym: _daily_bars() for sym in SYMBOLS}


class TestChampionStrategy:
    def test_identity_and_metadata(self):
        s = ChampionStrategy()
        assert s.name == CHAMPION_STRATEGY_NAME
        assert s.version == CHAMPION_STRATEGY_VERSION
        params = s.parameters()
        assert params["sma_short"] == 20
        assert params["sma_long"] == 50
        assert params["min_gate_count"] == 4
        assert s.required_history() > 0

    def test_stable_hash_deterministic_across_instances(self):
        a = ChampionStrategy()
        b = ChampionStrategy()
        assert a.stable_hash() == b.stable_hash()

    def test_stable_hash_changes_with_parameters(self):
        a = ChampionStrategy()
        b = ChampionStrategy(config=ChampionScoringConfig(sma_short=10))
        assert a.stable_hash() != b.stable_hash()

    def test_conforms_to_strategy_protocol(self):
        s = ChampionStrategy()
        assert isinstance(s, Strategy)

    def test_build_evaluator_conforms(self):
        s = ChampionStrategy()
        ev = s.build_evaluator(_bars_for_symbols(), SYMBOLS)
        assert isinstance(ev, ComparisonEvaluator)

    def test_score_and_explain_agree(self):
        s = ChampionStrategy()
        bars = _bars_for_symbols()
        event = _event()
        scores = s.score(event, bars, SYMBOLS)
        explanations = s.explain(event, bars, SYMBOLS)
        # Every symbol has a score; every symbol has an explanation
        assert set(scores.keys()) == set(SYMBOLS)
        assert set(explanations.keys()) == set(SYMBOLS)

    def test_score_deterministic(self):
        s = ChampionStrategy()
        bars = _bars_for_symbols()
        event = _event()
        assert s.score(event, bars, SYMBOLS) == s.score(event, bars, SYMBOLS)


# ---------------------------------------------------------------------------
# ChampionRelativeStrengthStrategy (Strategy #2)
# ---------------------------------------------------------------------------


def _rs_provider(rs_by_symbol):
    def provider(symbol, event):
        return rs_by_symbol.get(symbol)
    return provider


class TestChampionRelativeStrengthStrategy:
    def test_identity_and_metadata(self):
        s = ChampionRelativeStrengthStrategy()
        assert s.name == CHAMPION_RS_STRATEGY_NAME
        assert s.version == CHAMPION_RS_STRATEGY_VERSION
        params = s.parameters()
        assert params["overlay_weight"] == pytest.approx(0.2)
        assert params["neutral_score"] == pytest.approx(50.0)
        assert params["score_range"] == pytest.approx(50.0)
        assert s.required_history() > 0

    def test_stable_hash_changes_with_overlay_weight(self):
        a = ChampionRelativeStrengthStrategy(overlay_weight=0.2)
        b = ChampionRelativeStrengthStrategy(overlay_weight=0.1)
        assert a.stable_hash() != b.stable_hash()

    def test_stable_hash_differs_from_champion_alone(self):
        assert (
            ChampionStrategy().stable_hash()
            != ChampionRelativeStrengthStrategy().stable_hash()
        )

    def test_conforms_to_strategy_protocol(self):
        s = ChampionRelativeStrengthStrategy()
        assert isinstance(s, Strategy)

    def test_build_evaluator_requires_rs_provider(self):
        s = ChampionRelativeStrengthStrategy()
        with pytest.raises(ValueError, match="rs_provider"):
            s.build_evaluator(_bars_for_symbols(), SYMBOLS)

    def test_build_evaluator_returns_comparison_evaluator(self):
        s = ChampionRelativeStrengthStrategy()
        ev = s.build_evaluator(
            _bars_for_symbols(), SYMBOLS,
            rs_provider=_rs_provider({"AAPL": 60.0, "MSFT": 55.0}),
        )
        assert isinstance(ev, ComparisonEvaluator)

    def test_score_and_explain_return_all_symbols(self):
        reset_feature_flags()
        s = ChampionRelativeStrengthStrategy()
        bars = _bars_for_symbols()
        event = _event()
        provider = _rs_provider({"AAPL": 60.0, "MSFT": 55.0})
        scores = s.score(event, bars, SYMBOLS, rs_provider=provider)
        explanations = s.explain(event, bars, SYMBOLS, rs_provider=provider)
        assert set(scores.keys()) == set(SYMBOLS)
        assert set(explanations.keys()) == set(SYMBOLS)


# ---------------------------------------------------------------------------
# Feature flags stay disabled
# ---------------------------------------------------------------------------


class TestFeatureFlagsUntouched:
    def test_champion_leaves_flags_disabled(self):
        flags = reset_feature_flags()
        s = ChampionStrategy()
        s.score(_event(), _bars_for_symbols(), SYMBOLS)
        assert flags.all_disabled is True

    def test_champion_rs_leaves_flags_disabled(self):
        flags = reset_feature_flags()
        s = ChampionRelativeStrengthStrategy()
        s.score(
            _event(), _bars_for_symbols(), SYMBOLS,
            rs_provider=_rs_provider({"AAPL": 60.0, "MSFT": 55.0}),
        )
        assert flags.all_disabled is True


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(module_name: str) -> str:
    import importlib
    mod = importlib.import_module(module_name)
    return Path(mod.__file__).read_text(encoding="utf-8")


LAB_MODULES = (
    "strategy.lab",
    "strategy.lab.strategy",
    "strategy.lab.champion",
    "strategy.lab.champion_rs",
)


class TestSourceSafety:
    @pytest.mark.parametrize("mod", LAB_MODULES)
    def test_no_live_runner_imports(self, mod):
        source = _module_source(mod)
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import", "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source, (
                f"{mod} imports live-runner token {token!r}"
            )

    @pytest.mark.parametrize("mod", LAB_MODULES)
    def test_no_order_path_tokens(self, mod):
        source = _module_source(mod)
        for token in (
            "submit_order", "place_order", "cancel_order",
            "TradingClient",
        ):
            assert token not in source

    @pytest.mark.parametrize("mod", LAB_MODULES)
    def test_no_approval_or_promotion_construction(self, mod):
        source = _module_source(mod)
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source

    @pytest.mark.parametrize("mod", LAB_MODULES)
    def test_no_credential_env_reads(self, mod):
        source = _module_source(mod)
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, source), (
                f"{mod} reads credential env {pattern!r}"
            )
