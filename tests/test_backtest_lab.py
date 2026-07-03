"""Tests for strategy/backtest_lab.py."""

import json

import pytest

from strategy.backtest_lab import (
    BacktestArtifactPaths,
    BacktestConfig,
    BacktestEvent,
    BacktestReport,
    BacktestRunManifest,
    BacktestRunMetadata,
    BacktestStrategyResult,
    DeterministicReplayClock,
    NoOpStrategyAdapter,
    StrategyEvaluation,
    create_backtest_report,
    stable_hash,
    stable_json,
)
from strategy.config import reset_feature_flags


def _config(**overrides):
    values = {
        "dataset_id": "paper-2026-q3",
        "strategy_id": "champion-v0.4.0",
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "symbols": ("AAPL", "MSFT"),
        "benchmarks": ("SPY", "QQQ"),
        "initial_cash": 100000.0,
        "fee_bps": 1.0,
        "slippage_bps": 2.0,
        "seed": 42,
        "metadata": {"purpose": "unit-test"},
    }
    values.update(overrides)
    return BacktestConfig(**values)


class TestStableSerialization:
    def test_stable_json_sorts_nested_keys(self):
        left = {"b": 2, "a": {"d": 4, "c": 3}}
        right = {"a": {"c": 3, "d": 4}, "b": 2}

        assert stable_json(left) == stable_json(right)

    def test_stable_hash_is_deterministic(self):
        data = {"b": [2, 1], "a": {"z": 9}}
        assert stable_hash(data) == stable_hash({"a": {"z": 9}, "b": [2, 1]})
        assert len(stable_hash(data)) == 64


class TestBacktestConfig:
    def test_to_dict(self):
        config = _config()
        d = config.to_dict()

        assert d["dataset_id"] == "paper-2026-q3"
        assert d["strategy_id"] == "champion-v0.4.0"
        assert d["symbols"] == ["AAPL", "MSFT"]
        assert d["benchmarks"] == ["SPY", "QQQ"]
        json.dumps(d)

    def test_hash_is_deterministic(self):
        assert _config().stable_hash() == _config().stable_hash()
        assert _config(seed=1).stable_hash() != _config(seed=2).stable_hash()

    def test_symbols_and_benchmarks_are_tuples(self):
        config = _config(symbols=["MSFT", "AAPL"], benchmarks=["QQQ"])
        assert config.symbols == ("MSFT", "AAPL")
        assert config.benchmarks == ("QQQ",)

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"dataset_id": ""}, "dataset_id is required"),
            ({"strategy_id": ""}, "strategy_id is required"),
            ({"start_date": ""}, "start_date and end_date are required"),
            ({"start_date": "2026-08-01", "end_date": "2026-07-01"}, "start_date"),
            ({"initial_cash": 0}, "initial_cash"),
            ({"fee_bps": -1}, "fee_bps"),
            ({"slippage_bps": -1}, "slippage_bps"),
            ({"seed": -1}, "seed"),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            _config(**overrides)


class TestRunMetadata:
    def test_create_metadata(self):
        config = _config()
        metadata = BacktestRunMetadata.create(
            config,
            git_commit="abc123",
            code_version="v0.10.0",
            created_at="2026-07-02T12:00:00+00:00",
            operator="codex",
            notes="dry run",
        )

        assert metadata.run_id == f"bt_{config.stable_hash()[:12]}"
        assert metadata.config_hash == config.stable_hash()
        assert metadata.git_commit == "abc123"
        assert metadata.code_version == "v0.10.0"
        assert metadata.operator == "codex"
        json.dumps(metadata.to_dict())

    def test_metadata_run_id_is_independent_of_timestamp(self):
        config = _config()
        first = BacktestRunMetadata.create(config, created_at="2026-07-02T12:00:00+00:00")
        second = BacktestRunMetadata.create(config, created_at="2026-07-03T12:00:00+00:00")

        assert first.run_id == second.run_id
        assert first.config_hash == second.config_hash


class TestArtifactPaths:
    def test_artifact_paths(self):
        config = _config(output_dir="reports/backtests")
        metadata = BacktestRunMetadata.create(config, created_at="2026-07-02T12:00:00+00:00")
        paths = BacktestArtifactPaths.for_run(config, metadata)
        d = paths.to_dict()

        assert d["run_dir"].endswith(metadata.run_id)
        assert d["manifest_path"].endswith(f"{metadata.run_id}/manifest.json")
        assert d["report_json_path"].endswith(f"{metadata.run_id}/report.json")
        assert d["report_markdown_path"].endswith(f"{metadata.run_id}/report.md")
        json.dumps(d)


class TestRunManifest:
    def test_create_manifest(self):
        config = _config()
        manifest = BacktestRunManifest.create(
            config,
            git_commit="abc123",
            code_version="v0.10.0",
            created_at="2026-07-02T12:00:00+00:00",
            operator="codex",
            inputs={"watchlist": "trending_watchlist.json"},
        )
        d = manifest.to_dict()

        assert d["config"]["dataset_id"] == "paper-2026-q3"
        assert d["metadata"]["git_commit"] == "abc123"
        assert d["inputs"]["watchlist"] == "trending_watchlist.json"
        assert d["status"] == "created"
        json.dumps(d)

    def test_manifest_hash_ignores_created_at(self):
        config = _config()
        first = BacktestRunManifest.create(config, created_at="2026-07-02T12:00:00+00:00")
        second = BacktestRunManifest.create(config, created_at="2026-07-03T12:00:00+00:00")

        assert first.stable_hash() == second.stable_hash()


class TestReplayFoundation:
    def test_backtest_event_to_dict(self):
        event = BacktestEvent(
            timestamp="2026-07-02T14:30:00+00:00",
            event_type="market_snapshot",
            payload={"symbol": "AAPL", "close": 200.0},
            sequence=2,
        )
        d = event.to_dict()

        assert d["timestamp"] == "2026-07-02T14:30:00+00:00"
        assert d["event_type"] == "market_snapshot"
        assert d["payload"]["symbol"] == "AAPL"
        json.dumps(d)

    def test_replay_clock_orders_events_deterministically(self):
        events = [
            BacktestEvent("2026-07-02T15:00:00+00:00", "candidate", sequence=2),
            BacktestEvent("2026-07-02T14:30:00+00:00", "market", sequence=1),
            BacktestEvent("2026-07-02T15:00:00+00:00", "market", sequence=1),
        ]
        clock = DeterministicReplayClock(events)

        ordered = list(clock)
        assert [event.event_type for event in ordered] == ["market", "market", "candidate"]
        assert clock.to_list()[0]["timestamp"] == "2026-07-02T14:30:00+00:00"

    def test_strategy_evaluation_to_dict(self):
        evaluation = StrategyEvaluation(
            strategy_id="noop",
            event_timestamp="2026-07-02T14:30:00+00:00",
            scores={"AAPL": 0.0},
            rankings=[{"symbol": "AAPL", "rank": 1}],
            explanations={"AAPL": "no-op"},
            warnings=["test"],
        )
        d = evaluation.to_dict()

        assert d["strategy_id"] == "noop"
        assert d["scores"]["AAPL"] == 0.0
        assert d["rankings"][0]["symbol"] == "AAPL"
        json.dumps(d)

    def test_noop_strategy_adapter(self):
        adapter = NoOpStrategyAdapter(strategy_id="noop-foundation")
        event = BacktestEvent("2026-07-02T14:30:00+00:00", "market_snapshot")
        evaluation = adapter.evaluate(event)

        assert evaluation.strategy_id == "noop-foundation"
        assert evaluation.event_timestamp == event.timestamp
        assert evaluation.scores == {}
        assert evaluation.rankings == []
        assert evaluation.warnings == ["no-op adapter; no strategy behavior executed"]


class TestStrategyResultAndReport:
    def test_strategy_result_to_dict(self):
        result = BacktestStrategyResult(
            strategy_id="champion-v0.4.0",
            metrics={"profit_factor": 1.25, "max_drawdown": 100.0},
            trade_count=2,
            trades=[{"symbol": "AAPL", "pl": 10.0}],
            warnings=["sample warning"],
            data_quality="complete",
        )
        d = result.to_dict()

        assert d["strategy_id"] == "champion-v0.4.0"
        assert d["metrics"]["profit_factor"] == 1.25
        assert d["trade_count"] == 2
        json.dumps(d)

    def test_report_serialization(self):
        config = _config()
        manifest = BacktestRunManifest.create(
            config,
            created_at="2026-07-02T12:00:00+00:00",
        )
        report = BacktestReport(
            manifest=manifest,
            champion_result=BacktestStrategyResult(strategy_id="champion-v0.4.0"),
            challenger_results=[
                BacktestStrategyResult(strategy_id="rs-challenger-v0.1.0")
            ],
            summary={"status": "empty foundation"},
            warnings=["no replay executed"],
            status="created",
        )
        d = report.to_dict()

        assert d["manifest"]["metadata"]["run_id"] == manifest.metadata.run_id
        assert d["champion_result"]["strategy_id"] == "champion-v0.4.0"
        assert d["challenger_results"][0]["strategy_id"] == "rs-challenger-v0.1.0"
        assert "no replay executed" in d["warnings"]
        json.loads(report.to_json())

    def test_report_markdown(self):
        report = create_backtest_report(
            _config(),
            created_at="2026-07-02T12:00:00+00:00",
        )
        text = report.to_markdown()

        assert "Backtest Report" in text
        assert "paper-2026-q3" in text
        assert "Observational research only" in text

    def test_create_backtest_report(self):
        config = _config()
        report = create_backtest_report(
            config,
            git_commit="abc123",
            code_version="v0.10.0",
            created_at="2026-07-02T12:00:00+00:00",
            operator="codex",
            inputs={"dataset_manifest": "research_data/datasets.json"},
        )

        assert isinstance(report, BacktestReport)
        assert report.manifest.config == config
        assert report.manifest.metadata.git_commit == "abc123"
        assert report.manifest.inputs["dataset_manifest"] == "research_data/datasets.json"

    def test_observational_only_contract(self):
        flags = reset_feature_flags()
        report = create_backtest_report(
            _config(),
            created_at="2026-07-02T12:00:00+00:00",
        )
        d = report.to_dict()

        assert flags.all_disabled is True
        assert flags.enabled_flags == []
        assert "place_order" not in json.dumps(d)
        assert "alpaca" not in json.dumps(d).lower()
