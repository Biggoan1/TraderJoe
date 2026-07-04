"""Tests for strategy/lab/experiment_runner.py — Card 2."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.champion_scoring import ChampionScoringConfig
from strategy.config import reset_feature_flags
from strategy.historical_validation import HistoricalValidationConfig
from strategy.lab import (
    ExperimentBundle,
    ExperimentManifest,
    run_strategy_experiment,
)
from strategy.lab.champion import ChampionStrategy
from strategy.lab.champion_rs import ChampionRelativeStrengthStrategy
from strategy.lab.strategy import StrategyBase
from strategy.promotion_gates import STATE_DISABLED


FIXTURE_WINDOW_START = "2026-05-01"
FIXTURE_WINDOW_END = "2026-06-30"
FIXTURE_SYMBOLS = ("AAPL", "MSFT", "NVDA")


def _iter_dates(start: str, end: str):
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while current <= stop:
        yield current
        current = current + timedelta(days=1)


def _make_events():
    events: List[BacktestEvent] = []
    for i, day in enumerate(_iter_dates(FIXTURE_WINDOW_START, FIXTURE_WINDOW_END)):
        events.append(
            BacktestEvent(
                timestamp=f"{day.isoformat()}T14:30:00+00:00",
                event_type="market_snapshot",
                sequence=i + 1,
            )
        )
    return events


def _make_scores(events):
    return {
        e.timestamp: {sym: 0.5 + 0.001 * i + 0.01 * j
                      for j, sym in enumerate(FIXTURE_SYMBOLS)}
        for i, e in enumerate(events)
    }


def _make_rs(events):
    return {
        e.timestamp: {"AAPL": 70.0 if i % 2 == 0 else 40.0,
                      "MSFT": 55.0,
                      "NVDA": 70.0 if i % 2 == 1 else 45.0}
        for i, e in enumerate(events)
    }


def _make_config(tmp_path, **overrides) -> HistoricalValidationConfig:
    events = _make_events()
    base = dict(
        dataset_id="exp-fixture",
        symbols=FIXTURE_SYMBOLS,
        window_start=FIXTURE_WINDOW_START,
        window_end=FIXTURE_WINDOW_END,
        research_data_root=str(tmp_path / "research_data"),
        report_root=str(tmp_path / "reports"),
        in_sample_days=30,
        out_of_sample_days=15,
        step_days=15,
        fixture_events=tuple(events),
        fixture_champion_scores=_make_scores(events),
        fixture_rs_map=_make_rs(events),
    )
    base.update(overrides)
    return HistoricalValidationConfig(**base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_champion_strategy_run_produces_bundle(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        strategy = ChampionStrategy()
        bundle = run_strategy_experiment(
            config, strategy,
            manifest_root=str(tmp_path / "experiments"),
        )
        assert isinstance(bundle, ExperimentBundle)
        assert isinstance(bundle.manifest, ExperimentManifest)
        # Manifest carries strategy identity
        assert bundle.manifest.strategy.name == "champion"
        assert bundle.manifest.strategy.stable_hash == strategy.stable_hash()
        # PromotionEntry stayed disabled
        assert bundle.manifest.promotion_state == STATE_DISABLED
        assert bundle.manifest.promotion_approvals == 0
        # Reports written
        assert bundle.validation_bundle.comparison_report.report_id
        assert bundle.validation_bundle.walk_forward_research_report.report_id
        assert bundle.validation_bundle.learning_report.report_id

    def test_champion_rs_strategy_run_produces_bundle(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        strategy = ChampionRelativeStrengthStrategy()
        bundle = run_strategy_experiment(
            config, strategy,
            manifest_root=str(tmp_path / "experiments"),
        )
        assert bundle.manifest.strategy.name == "champion_rs"
        assert bundle.manifest.strategy.stable_hash == strategy.stable_hash()

    def test_manifest_written_to_disk(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle = run_strategy_experiment(
            config, ChampionStrategy(),
            manifest_root=str(tmp_path / "experiments"),
        )
        assert Path(bundle.manifest_path).is_file()
        loaded = json.loads(Path(bundle.manifest_path).read_text())
        assert loaded["experiment_id"] == bundle.manifest.experiment_id
        assert loaded["strategy"]["name"] == "champion"

    def test_write_manifest_disable(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle = run_strategy_experiment(
            config, ChampionStrategy(),
            write_manifest=False,
            manifest_root=str(tmp_path / "experiments"),
        )
        assert bundle.manifest_path == ""

    def test_experiment_id_deterministic(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        a = run_strategy_experiment(config, ChampionStrategy(),
                                    manifest_root=str(tmp_path / "a"))
        b = run_strategy_experiment(config, ChampionStrategy(),
                                    manifest_root=str(tmp_path / "b"))
        assert a.manifest.experiment_id == b.manifest.experiment_id

    def test_two_strategies_different_experiment_ids(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        a = run_strategy_experiment(config, ChampionStrategy(),
                                    manifest_root=str(tmp_path / "a"))
        b = run_strategy_experiment(config, ChampionRelativeStrengthStrategy(),
                                    manifest_root=str(tmp_path / "b"))
        assert a.manifest.experiment_id != b.manifest.experiment_id


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_runs_produce_identical_comparison_hash(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        a = run_strategy_experiment(
            config, ChampionStrategy(),
            generated_at="2026-07-04T00:00:00+00:00",
            manifest_root=str(tmp_path / "a"),
        )
        b = run_strategy_experiment(
            config, ChampionStrategy(),
            generated_at="2026-07-04T00:00:00+00:00",
            manifest_root=str(tmp_path / "b"),
        )
        assert (
            a.validation_bundle.comparison.stable_hash()
            == b.validation_bundle.comparison.stable_hash()
        )


# ---------------------------------------------------------------------------
# Feature flags stay disabled
# ---------------------------------------------------------------------------


class TestFeatureFlagsUntouched:
    def test_flags_disabled_after_experiment(self, tmp_path):
        flags = reset_feature_flags()
        config = _make_config(tmp_path)
        run_strategy_experiment(
            config, ChampionStrategy(),
            manifest_root=str(tmp_path / "experiments"),
        )
        assert flags.all_disabled is True


# ---------------------------------------------------------------------------
# Existing HistoricalValidation callers unchanged
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_run_historical_validation_still_works_without_factory(
        self, tmp_path
    ):
        from strategy.historical_validation import run_historical_validation
        reset_feature_flags()
        config = _make_config(tmp_path)
        bundle = run_historical_validation(config)
        # Default RS challenger still used; comparison contains score
        # deltas from the RS overlay applied to fixture data.
        assert bundle.promotion_entry.current_state == STATE_DISABLED


# ---------------------------------------------------------------------------
# Injected strategy actually drives the comparison
# ---------------------------------------------------------------------------


class _ConstantScoreStrategy(StrategyBase):
    """Strategy that scores every symbol as 1.0 — different from
    Champion's fixture scores, so a comparison against Champion
    will produce distinctive disagreements.
    """
    name = "constant"
    version = "0.1.0"

    def __init__(self, constant: float = 1.0):
        self.constant = float(constant)

    def parameters(self):
        return {"constant": self.constant}

    def required_history(self):
        return 1

    def build_evaluator(self, bars_by_symbol, symbols, **kwargs):
        from strategy.backtest_lab import StrategyEvaluation

        class _Evaluator:
            strategy_id = "constant-strategy-v0.1.0"

            def __init__(inner, symbols_seq, k):
                inner._symbols = tuple(symbols_seq)
                inner._k = k

            def evaluate(inner, event):
                scores = {s: inner._k for s in inner._symbols}
                selected = sorted(inner._symbols)
                rankings = [
                    {"symbol": s, "rank": i + 1}
                    for i, s in enumerate(selected)
                ]
                explanations = {s: f"constant={inner._k}"
                                for s in inner._symbols}
                return StrategyEvaluation(
                    strategy_id=inner.strategy_id,
                    event_timestamp=event.timestamp,
                    scores=scores,
                    rankings=rankings,
                    explanations=explanations,
                    warnings=[],
                    structured_explanations={},
                )

        return _Evaluator(symbols, self.constant)


class TestInjection:
    def test_injected_strategy_is_the_challenger(self, tmp_path):
        reset_feature_flags()
        config = _make_config(tmp_path)
        # Champion fixture scores hover around 0.5; a strategy that
        # always returns 1.0 should produce many score_delta rows.
        bundle = run_strategy_experiment(
            config, _ConstantScoreStrategy(constant=1.0),
            manifest_root=str(tmp_path / "experiments"),
        )
        cmp = bundle.validation_bundle.comparison
        # There must be disagreements — otherwise the injection
        # didn't take effect.
        assert len(cmp.disagreements) > 0


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.experiment_runner")
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import", "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.experiment_runner")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.experiment_runner")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s

    def test_no_credential_env_reads(self):
        s = _module_source("strategy.lab.experiment_runner")
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, s)
