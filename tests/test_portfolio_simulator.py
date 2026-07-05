"""Tests for the portfolio simulator, CLI wiring, and paper bridge.

Focus areas:

* deterministic simulation trajectory
* cash / position ledger correctness
* drawdown, win rate, hold-days accounting
* trade blotter export
* stable ``run_id`` and config hash
* paper bridge safety guards (dry-run default, env refusal, limits)
* no broker calls during simulator or dry-run
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.paper_bridge import (
    PaperBridgeConfig,
    PaperBridgeError,
    PaperTradingBridge,
    load_paper_env,
    load_plan,
)
from strategy.paper_bridge_main import main as paper_main
from strategy.portfolio_simulator import (
    PortfolioSimulationResult,
    PortfolioSimulator,
    PortfolioSimulatorConfig,
    events_from_bars,
    write_simulation_report,
)
from strategy.portfolio_simulator_cli import (
    resolve_window,
    run_simulation,
)
from strategy.portfolio_simulator_main import main as sim_main


# ---------------------------------------------------------------------------
# Fixture: deterministic scripted evaluator
# ---------------------------------------------------------------------------


class ScriptedEvaluator:
    """Evaluator that returns a canned score map per event.

    Emits ``rankings`` sorted by score descending so the simulator's
    top-N logic sees a stable order.  Any symbol missing from
    ``scores_by_timestamp`` for a given event is treated as "no
    signal" (dropped by the simulator).
    """

    strategy_id = "test-scripted-v1"

    def __init__(
        self,
        scores_by_timestamp: Mapping[str, Mapping[str, float]],
    ) -> None:
        self._map = {t: dict(v) for t, v in scores_by_timestamp.items()}

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        scores = dict(self._map.get(event.timestamp, {}))
        rankings = [
            {"symbol": sym, "rank": idx + 1}
            for idx, (sym, _) in enumerate(
                sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
            )
        ]
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=scores,
            rankings=rankings,
        )


def _bar(t: str, close: float, volume: float = 1_000_000.0) -> Dict[str, Any]:
    return {"t": t, "o": close, "h": close, "l": close, "c": close, "v": volume}


def _make_bars() -> Dict[str, List[Dict[str, Any]]]:
    days = [
        "2026-01-05T00:00:00+00:00",
        "2026-01-06T00:00:00+00:00",
        "2026-01-07T00:00:00+00:00",
        "2026-01-08T00:00:00+00:00",
        "2026-01-09T00:00:00+00:00",
    ]
    return {
        "AAA": [
            _bar(days[0], 100.0),
            _bar(days[1], 105.0),
            _bar(days[2], 110.0),
            _bar(days[3], 108.0),
            _bar(days[4], 112.0),
        ],
        "BBB": [
            _bar(days[0], 50.0),
            _bar(days[1], 49.0),
            _bar(days[2], 51.0),
            _bar(days[3], 55.0),
            _bar(days[4], 48.0),
        ],
    }


def _scripted_scores() -> Dict[str, Dict[str, float]]:
    return {
        "2026-01-05T00:00:00+00:00": {"AAA": 1.0, "BBB": 0.2},
        "2026-01-06T00:00:00+00:00": {"AAA": 1.0, "BBB": 0.2},
        "2026-01-07T00:00:00+00:00": {"AAA": 1.0, "BBB": 0.9},
        "2026-01-08T00:00:00+00:00": {"AAA": -0.5, "BBB": 0.9},
        "2026-01-09T00:00:00+00:00": {"AAA": 0.5, "BBB": -0.5},
    }


def _run_simulator(**config_overrides) -> PortfolioSimulationResult:
    bars = _make_bars()
    events = events_from_bars(bars)
    evaluator = ScriptedEvaluator(_scripted_scores())
    config_kwargs = {
        "starting_cash": 10_000.0,
        "max_open_positions": 2,
        "max_dollars_per_trade": 5_000.0,
        "position_size_pct": 0.5,
        "slippage_bps": 0.0,
        "commission_per_trade": 0.0,
        "top_n_per_event": 2,
        "score_entry_threshold": 0.0,
        "score_exit_threshold": 0.0,
    }
    config_kwargs.update(config_overrides)
    config = PortfolioSimulatorConfig(**config_kwargs)
    simulator = PortfolioSimulator(
        evaluator=evaluator,
        bars_by_symbol=bars,
        symbols=["AAA", "BBB"],
        events=events,
        config=config,
        strategy_name="scripted",
        strategy_version="0.0.1",
        window_start="2026-01-05",
        window_end="2026-01-09",
    )
    return simulator.run()


# ---------------------------------------------------------------------------
# Simulator determinism and ledger correctness
# ---------------------------------------------------------------------------


class TestSimulatorDeterminism:
    def test_two_runs_produce_identical_result(self):
        a = _run_simulator()
        b = _run_simulator()
        assert a.run_id == b.run_id
        assert a.config_hash == b.config_hash
        assert a.metrics.to_dict() == b.metrics.to_dict()
        assert [f.to_dict() for f in a.fills] == [
            f.to_dict() for f in b.fills
        ]

    def test_config_hash_is_sensitive_to_starting_cash(self):
        base = PortfolioSimulatorConfig().stable_hash()
        bumped = PortfolioSimulatorConfig(starting_cash=200_000.0).stable_hash()
        assert base != bumped


class TestLedgerCorrectness:
    def test_first_event_opens_positions_top_n(self):
        result = _run_simulator()
        # First day scores rank AAA above BBB, both above entry.
        first_fills = [f for f in result.fills if f.side == "buy"]
        assert first_fills, "expected at least one buy"
        symbols_bought = {f.symbol for f in first_fills}
        assert "AAA" in symbols_bought

    def test_cash_never_negative_by_default(self):
        result = _run_simulator()
        for point in result.equity_curve:
            assert point.cash >= -1e-6, (
                f"cash went negative at {point.timestamp}: {point.cash}"
            )

    def test_negative_cash_flag_gates_overdraft(self):
        # With $70 starting cash AAA @ $100 must not fill; BBB @ $50
        # fits one share.  Confirms allow_negative_cash=False refuses
        # the unaffordable order.
        bars = _make_bars()
        events = events_from_bars(bars)
        evaluator = ScriptedEvaluator(_scripted_scores())
        config = PortfolioSimulatorConfig(
            starting_cash=70.0,
            max_open_positions=2,
            max_dollars_per_trade=10_000.0,
            position_size_pct=1.0,
            slippage_bps=0.0,
            commission_per_trade=0.0,
            top_n_per_event=2,
        )
        sim = PortfolioSimulator(
            evaluator=evaluator,
            bars_by_symbol=bars,
            symbols=["AAA", "BBB"],
            events=events,
            config=config,
            window_start="2026-01-05",
            window_end="2026-01-09",
        )
        result = sim.run()
        buys = [f for f in result.fills if f.side == "buy"]
        assert not any(f.symbol == "AAA" for f in buys)
        # At least one BBB buy should still happen once cash allows.
        assert any(f.symbol == "BBB" for f in buys)
        # Cash never goes below zero.
        for point in result.equity_curve:
            assert point.cash >= -1e-6

    def test_exit_on_signal_loss(self):
        result = _run_simulator()
        # On 2026-01-08 AAA score falls below exit threshold → sell.
        sells = [f for f in result.fills if f.side == "sell"]
        assert sells, "expected at least one sell"
        aaa_sells = [f for f in sells if f.symbol == "AAA"]
        assert aaa_sells, "expected AAA to be sold when signal drops"

    def test_realized_pnl_matches_fills(self):
        result = _run_simulator()
        for trade in result.trades:
            expected = (
                (trade.exit_price - trade.entry_price) * trade.quantity
                - trade.commission
            )
            assert abs(trade.realized_pnl - expected) < 1e-6

    def test_equity_curve_matches_starting_cash_no_positions(self):
        # Every event has empty scores → no trades → equity == starting cash.
        bars = _make_bars()
        events = events_from_bars(bars)
        evaluator = ScriptedEvaluator({e.timestamp: {} for e in events})
        config = PortfolioSimulatorConfig(starting_cash=10_000.0)
        sim = PortfolioSimulator(
            evaluator=evaluator,
            bars_by_symbol=bars,
            symbols=["AAA", "BBB"],
            events=events,
            config=config,
        )
        result = sim.run()
        assert not result.fills
        assert not result.trades
        for point in result.equity_curve:
            assert point.equity == pytest.approx(10_000.0)
            assert point.drawdown == pytest.approx(0.0)

    def test_metrics_report_win_rate_and_hold_days(self):
        result = _run_simulator()
        assert result.metrics.trade_count == len(result.trades)
        if result.trades:
            expected_win_rate = sum(
                1 for t in result.trades if t.realized_pnl > 0
            ) / len(result.trades)
            assert result.metrics.win_rate == pytest.approx(expected_win_rate)
            expected_hold = sum(t.hold_days for t in result.trades) / len(
                result.trades
            )
            assert result.metrics.average_hold_days == pytest.approx(
                expected_hold
            )

    def test_drawdown_never_negative_and_bounded_to_one(self):
        result = _run_simulator()
        for point in result.equity_curve:
            assert 0.0 <= point.drawdown <= 1.0

    def test_slippage_reduces_realised_pnl_for_matched_round_trip(self):
        # Buy on day1, sell on day2 (same close moves), only slippage
        # should erode the P/L compared to a zero-slippage run.
        no_slip = _run_simulator(slippage_bps=0.0)
        with_slip = _run_simulator(slippage_bps=50.0)  # 0.5%
        # Aggregate realized on comparable trade counts:
        if no_slip.metrics.trade_count == with_slip.metrics.trade_count > 0:
            assert (
                with_slip.metrics.realized_pnl
                <= no_slip.metrics.realized_pnl + 1e-6
            )


class TestReportArtifacts:
    def test_write_report_creates_all_four_files(self, tmp_path: Path):
        result = _run_simulator()
        paths = write_simulation_report(result, root=str(tmp_path))
        for key in (
            "report_json",
            "report_md",
            "trades_csv",
            "equity_curve_csv",
            "orders_json",
        ):
            assert Path(paths[key]).exists(), f"missing {key}: {paths[key]}"
        payload = json.loads(Path(paths["report_json"]).read_text())
        assert payload["run_id"] == result.run_id
        assert payload["metrics"]["trade_count"] == result.metrics.trade_count

    def test_trades_csv_row_count_matches_result(self, tmp_path: Path):
        result = _run_simulator()
        paths = write_simulation_report(result, root=str(tmp_path))
        rows = Path(paths["trades_csv"]).read_text().strip().splitlines()
        # header + one row per trade
        assert len(rows) == 1 + result.metrics.trade_count


class TestNoBrokerCalls:
    """The simulator module must not import anything that could
    trigger a broker call at import time.
    """

    def test_no_alpaca_import_in_simulator(self):
        import strategy.portfolio_simulator as module

        source = Path(module.__file__).read_text()
        # Docstring may mention paper-Alpaca; assert on real imports.
        assert "import alpaca" not in source
        assert "from alpaca" not in source
        assert "TradingClient" not in source

    def test_no_trader_or_runner_import(self):
        import strategy.portfolio_simulator as module

        source = Path(module.__file__).read_text()
        assert "strategy.runner" not in source
        assert "from trader" not in source
        assert "import trader" not in source


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


class TestWindowResolution:
    def test_explicit_bounds_return_verbatim(self):
        assert resolve_window(None, "2026-01-01", "2026-07-04") == (
            "2026-01-01",
            "2026-07-04",
        )

    def test_preset_counts_back_from_today(self):
        import datetime as dt

        today = dt.date(2026, 7, 1)
        start, end = resolve_window("60d", None, None, today=today)
        assert end == "2026-07-01"
        assert start == "2026-05-02"

    def test_partial_explicit_bounds_error(self):
        with pytest.raises(ValueError):
            resolve_window(None, "2026-01-01", None)


# ---------------------------------------------------------------------------
# End-to-end via run_simulation
# ---------------------------------------------------------------------------


class TestRunSimulationInMemory:
    def test_champion_end_to_end_with_in_memory_bars(self):
        bars = _synth_bars()
        result = run_simulation(
            strategy_key="champion-v0.4.0",
            symbols=["AAA", "BBB"],
            start="2026-01-01",
            end="2026-06-30",
            config=PortfolioSimulatorConfig(
                starting_cash=100_000.0,
                slippage_bps=0.0,
                commission_per_trade=0.0,
            ),
            bars_by_symbol=bars,
            dataset_provenance={"source": "in_memory_test"},
        )
        assert result.metrics.trading_days > 0
        assert result.dataset_provenance["source"] == "in_memory_test"


def _synth_bars() -> Dict[str, List[Dict[str, Any]]]:
    """Simple upward-drift synthetic bars with enough history for
    Champion's SMA/RSI/MACD/ADX gates.
    """
    import datetime as dt

    d = dt.date(2025, 1, 1)
    out: Dict[str, List[Dict[str, Any]]] = {"AAA": [], "BBB": []}
    price_a = 100.0
    price_b = 50.0
    day = 0
    while d <= dt.date(2026, 6, 30):
        if d.weekday() < 5:
            ts = d.isoformat() + "T00:00:00+00:00"
            price_a *= 1.001 + (0.0005 if day % 3 == 0 else -0.0002)
            price_b *= 1.0008 + (0.0006 if day % 4 == 0 else -0.0003)
            out["AAA"].append(_bar(ts, round(price_a, 2)))
            out["BBB"].append(_bar(ts, round(price_b, 2)))
            day += 1
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestSimulatorCli:
    def test_cli_refuses_production_context(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.setenv("HERMES_CONTEXT", "production")
        rc = sim_main(
            [
                "--strategy",
                "champion-v0.4.0",
                "--symbols",
                "AAPL",
                "--window",
                "60d",
                "--no-write",
            ]
        )
        assert rc == 2
        captured = capsys.readouterr()
        assert "HERMES_CONTEXT=production" in captured.err

    def test_cli_refuses_paper_false(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.setenv("PAPER", "False")
        monkeypatch.delenv("HERMES_CONTEXT", raising=False)
        rc = sim_main(
            [
                "--strategy",
                "champion-v0.4.0",
                "--symbols",
                "AAPL",
                "--window",
                "60d",
                "--no-write",
            ]
        )
        assert rc == 2

    def test_cli_rejects_unknown_strategy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.delenv("HERMES_CONTEXT", raising=False)
        monkeypatch.delenv("PAPER", raising=False)
        rc = sim_main(
            [
                "--strategy",
                "nope",
                "--symbols",
                "AAPL",
                "--window",
                "60d",
                "--no-write",
            ]
        )
        assert rc == 2


# ---------------------------------------------------------------------------
# Paper bridge
# ---------------------------------------------------------------------------


def _write_plan(tmp_path: Path, orders: List[Dict[str, Any]]) -> Path:
    path = tmp_path / "orders.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "psim_test",
                "strategy": {
                    "name": "scripted",
                    "version": "0.0.1",
                    "strategy_id": "test-scripted-v1",
                },
                "window": {"start": "2026-01-01", "end": "2026-01-31"},
                "generated_at": "2026-07-01T00:00:00+00:00",
                "orders": orders,
            }
        )
    )
    return path


def _paper_env() -> Dict[str, str]:
    return {
        "PAPER_ALPACA_API_KEY": "PAPERKEY",
        "PAPER_ALPACA_SECRET_KEY": "PAPERSECRET",
        "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
    }


class TestPaperBridgeSafety:
    def test_config_refuses_production_env_basename(self):
        cfg = PaperBridgeConfig(env_file=".env.production")
        with pytest.raises(PaperBridgeError):
            cfg.validate()

    def test_config_refuses_random_env_basename(self):
        cfg = PaperBridgeConfig(env_file=".env.production.local")
        with pytest.raises(PaperBridgeError):
            cfg.validate()

    def test_bridge_dry_run_makes_no_broker_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        called: List[str] = []

        def _guard(self, *args, **kwargs):
            called.append("_submit_orders")
            raise AssertionError(
                "dry-run must not call _submit_orders"
            )

        monkeypatch.setattr(
            PaperTradingBridge, "_submit_orders", _guard, raising=True
        )
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100.0,
                    "reference_notional": 100.0,
                    "reason": "test",
                }
            ],
        )
        cfg = PaperBridgeConfig(
            execute=False,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        result = bridge.run(str(plan), env=_paper_env())
        assert result.executed is False
        assert not called

    def test_bridge_refuses_production_env_variable(self, tmp_path: Path):
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100.0,
                    "reference_notional": 100.0,
                }
            ],
        )
        cfg = PaperBridgeConfig(
            execute=False,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        with pytest.raises(PaperBridgeError):
            bridge.run(
                str(plan),
                env={"HERMES_CONTEXT": "production", **_paper_env()},
            )

    def test_bridge_refuses_paper_false(self, tmp_path: Path):
        plan = _write_plan(tmp_path, [])
        cfg = PaperBridgeConfig(
            execute=False,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        with pytest.raises(PaperBridgeError):
            bridge.run(str(plan), env={"PAPER": "False", **_paper_env()})

    def test_bridge_refuses_missing_paper_env_file(self, tmp_path: Path):
        cfg = PaperBridgeConfig(
            execute=False,
            env_file=str(tmp_path / ".env.paper.absent"),
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        plan = _write_plan(tmp_path, [])
        with pytest.raises(PaperBridgeError):
            bridge.run(str(plan))

    def test_bridge_enforces_per_order_notional(self, tmp_path: Path):
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100000.0,
                    "reference_notional": 100000.0,
                }
            ],
        )
        cfg = PaperBridgeConfig(
            execute=False,
            max_per_order_notional=500.0,
            max_total_notional=1_000.0,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        result = bridge.run(str(plan), env=_paper_env())
        assert result.orders[0].accepted is False
        assert "max_per_order_notional" in result.orders[0].rejection

    def test_bridge_enforces_total_notional(self, tmp_path: Path):
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 600.0,
                    "reference_notional": 600.0,
                },
                {
                    "symbol": "MSFT",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 600.0,
                    "reference_notional": 600.0,
                },
            ],
        )
        cfg = PaperBridgeConfig(
            execute=False,
            max_per_order_notional=700.0,
            max_total_notional=800.0,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        result = bridge.run(str(plan), env=_paper_env())
        accepted = [o for o in result.orders if o.accepted]
        # First order fits (600 < 800), second one blows the aggregate cap.
        assert len(accepted) == 1
        assert not result.orders[1].accepted

    def test_bridge_enforces_symbol_allowlist(self, tmp_path: Path):
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100.0,
                    "reference_notional": 100.0,
                },
                {
                    "symbol": "TSLA",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100.0,
                    "reference_notional": 100.0,
                },
            ],
        )
        cfg = PaperBridgeConfig(
            execute=False,
            symbol_allowlist=("AAPL",),
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        result = bridge.run(str(plan), env=_paper_env())
        assert result.orders[0].accepted
        assert not result.orders[1].accepted

    def test_bridge_enforces_max_order_quantity(self, tmp_path: Path):
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1_000_000,
                    "reference_price": 0.001,
                    "reference_notional": 1000.0,
                }
            ],
        )
        cfg = PaperBridgeConfig(
            execute=False,
            max_order_quantity=100.0,
            max_per_order_notional=10_000.0,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        result = bridge.run(str(plan), env=_paper_env())
        assert not result.orders[0].accepted
        assert "max_order_quantity" in result.orders[0].rejection

    def test_bridge_refuses_non_paper_endpoint(self, tmp_path: Path):
        plan = _write_plan(tmp_path, [])
        cfg = PaperBridgeConfig(
            execute=True,
            env_file=".env.paper",
            log_root=str(tmp_path / "logs"),
        )
        bridge = PaperTradingBridge(cfg)
        bad_env = {
            "PAPER_ALPACA_API_KEY": "k",
            "PAPER_ALPACA_SECRET_KEY": "s",
            "PAPER_ALPACA_ENDPOINT": "https://api.alpaca.markets/v2",
        }
        with pytest.raises(PaperBridgeError):
            bridge.run(str(plan), env=bad_env)


class TestPaperBridgeCli:
    def test_cli_default_is_dry_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        plan = _write_plan(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100.0,
                    "reference_notional": 100.0,
                }
            ],
        )
        env_file = tmp_path / ".env.paper"
        env_file.write_text(
            "PAPER_ALPACA_API_KEY=x\n"
            "PAPER_ALPACA_SECRET_KEY=y\n"
            "PAPER_ALPACA_ENDPOINT=https://paper-api.alpaca.markets/v2\n"
        )
        # Ensure process env is clean so file values are what the
        # bridge sees.
        monkeypatch.delenv("HERMES_CONTEXT", raising=False)
        monkeypatch.delenv("PAPER", raising=False)
        rc = paper_main(
            [
                "--from-simulation",
                str(plan),
                "--env-file",
                str(env_file),
                "--log-root",
                str(tmp_path / "logs"),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "DRY-RUN" in out
        assert "re-run with --execute-paper-orders" in out.lower()

    def test_cli_refuses_when_env_file_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        plan = _write_plan(tmp_path, [])
        rc = paper_main(
            [
                "--from-simulation",
                str(plan),
                "--env-file",
                str(tmp_path / ".env.paper.missing"),
                "--log-root",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 3
        err = capsys.readouterr().err
        assert "not found" in err

    def test_cli_refuses_production_env_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        plan = _write_plan(tmp_path, [])
        rc = paper_main(
            [
                "--from-simulation",
                str(plan),
                "--env-file",
                ".env.production",
                "--log-root",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 3


# ---------------------------------------------------------------------------
# Source-safety: live paths untouched
# ---------------------------------------------------------------------------


class TestSourceSafety:
    def test_paper_bridge_does_not_import_trader_at_module_level(self):
        import strategy.paper_bridge as module

        source = Path(module.__file__).read_text()
        assert "from trader" not in source
        assert "import trader" not in source
        assert "strategy.runner" not in source

    def test_simulator_never_references_paper_false(self):
        import strategy.portfolio_simulator as module

        source = Path(module.__file__).read_text()
        assert "PAPER = False" not in source
        assert "paper=False" not in source


# ---------------------------------------------------------------------------
# Env loader detail
# ---------------------------------------------------------------------------


class TestPaperEnvLoader:
    def test_refuses_production_file(self, tmp_path: Path):
        with pytest.raises(PaperBridgeError):
            load_paper_env(".env.production")

    def test_refuses_non_paper_basename(self, tmp_path: Path):
        path = tmp_path / ".env.other"
        path.write_text("PAPER_ALPACA_API_KEY=x\n")
        with pytest.raises(PaperBridgeError):
            load_paper_env(str(path))

    def test_loads_valid_paper_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("HERMES_CONTEXT", raising=False)
        monkeypatch.delenv("PAPER", raising=False)
        path = tmp_path / ".env.paper"
        path.write_text(
            "PAPER_ALPACA_API_KEY=A\n"
            "PAPER_ALPACA_SECRET_KEY=B\n"
            "PAPER_ALPACA_ENDPOINT=https://paper-api.alpaca.markets/v2\n"
        )
        env = load_paper_env(str(path))
        assert env["PAPER_ALPACA_API_KEY"] == "A"
