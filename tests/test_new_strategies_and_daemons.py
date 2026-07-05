"""Tests for the new strategy plug-ins, presets, market calendar,
equity paper daemon, and crypto 24/7 paper daemon.

Focus areas:

* Every new strategy is discovered by the registry.
* Every new strategy exposes score / explain / parameters /
  stable_hash / version / required_history / supported_intervals.
* Intraday strategies refuse to score against daily-only data
  through the ``interval`` kwarg.
* Presets round-trip and reference known strategies.
* Market-calendar helper handles holidays, weekends, session
  bounds.
* Equity daemon dry-run makes zero broker calls.
* Equity daemon refuses execution unless BOTH env flag and CLI
  flag are set.
* Equity daemon refuses to run outside regular session.
* Equity daemon enforces cooldowns, daily notional cap, daily
  trade cap, duplicate protection.
* Equity daemon writes plan + log.
* Crypto daemon refuses .env.paper and .env.production.
* Crypto daemon respects emergency stop file.
* Crypto daemon respects cooldown / cap / duplicate protection.
* No live-trading path files touched.
* No ``PAPER = False`` code introduced.
* No feature flags globally enabled by the new modules.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from strategy.backtest_lab import BacktestEvent, StrategyEvaluation
from strategy.crypto_paper_daemon import (
    AUTO_EXECUTE_ENV_VAR as CRYPTO_AUTO_EXECUTE_ENV_VAR,
    CryptoPaperDaemon,
    CryptoPaperDaemonConfig,
    CryptoPaperDaemonError,
    default_crypto_bar_loader,
    load_crypto_env,
)
from strategy.lab.interval_requirements import IntervalMismatchError
from strategy.lab.momentum_15m import Momentum15mStrategy
from strategy.lab.opening_range_breakout import OpeningRangeBreakoutStrategy
from strategy.lab.presets import (
    CRYPTO_24X7_PAPER_RESEARCH_PRESET,
    DAILY_SWING_WATCHLIST_PRESET,
    INTRADAY_15M_WATCHLIST_PRESET,
    get_preset,
    list_presets,
)
from strategy.lab.registry import (
    StrategyRegistry,
    discover_strategies,
)
from strategy.lab.rsi_mean_reversion import RSIMeanReversionStrategy
from strategy.lab.sector_rotation_daily import SectorRotationDailyStrategy
from strategy.lab.volatility_regime_filter import (
    VolatilityRegimeFilterStrategy,
)
from strategy.market_calendar import (
    is_regular_session,
    session_status,
    us_market_holidays,
)
from strategy.market_data_provider import BarInterval
from strategy.paper_bridge import PaperTradingBridge
from strategy.paper_daemon import (
    AUTO_EXECUTE_ENV_VAR,
    DaemonState,
    EquityPaperDaemon,
    EquityPaperDaemonConfig,
)


NEW_STRATEGIES = (
    Momentum15mStrategy,
    OpeningRangeBreakoutStrategy,
    RSIMeanReversionStrategy,
    SectorRotationDailyStrategy,
    VolatilityRegimeFilterStrategy,
)


# ---------------------------------------------------------------------------
# Strategy registration + interface
# ---------------------------------------------------------------------------


class TestStrategyRegistration:
    def test_all_new_strategies_are_discovered(self):
        registry = StrategyRegistry()
        registry.discover(package="strategy.lab")
        names = set(registry.names())
        for cls in NEW_STRATEGIES:
            assert cls.name in names, f"missing {cls.name} in registry"

    def test_default_registry_also_discovers(self):
        # Uses the module-level default registry (idempotent).
        discover_strategies()
        # No exception is the test.  Names must be present.
        # (Re-running discover_strategies is idempotent by design.)

    def test_strategies_have_required_metadata(self):
        for cls in NEW_STRATEGIES:
            instance = cls()
            assert instance.name
            assert instance.version
            assert instance.strategy_id
            params = instance.parameters()
            assert isinstance(params, Mapping)
            # stable_hash is deterministic across calls.
            assert instance.stable_hash() == instance.stable_hash()
            # required_history returns a positive int.
            assert instance.required_history() >= 1
            supported = cls.supported_intervals()
            assert supported, f"{cls.name} declared no supported intervals"

    def test_supported_intervals_are_bar_intervals(self):
        for cls in NEW_STRATEGIES:
            for interval in cls.supported_intervals():
                assert isinstance(interval, BarInterval)


# ---------------------------------------------------------------------------
# Intraday refusal
# ---------------------------------------------------------------------------


def _daily_bars() -> Dict[str, List[Dict[str, Any]]]:
    bars = []
    price = 100.0
    for i in range(60):
        ts = f"2026-01-{i + 1:02d}T00:00:00+00:00" if i < 31 else f"2026-02-{i - 30:02d}T00:00:00+00:00"
        price *= 1.001
        bars.append(
            {"t": ts, "o": price, "h": price, "l": price, "c": price, "v": 1000.0}
        )
    return {"AAA": bars, "BBB": [dict(b, c=b["c"] * 0.9) for b in bars]}


def _intraday_bars() -> Dict[str, List[Dict[str, Any]]]:
    bars = []
    price = 100.0
    for hour in range(9, 16):
        for minute in (0, 15, 30, 45):
            ts = f"2026-01-05T{hour:02d}:{minute:02d}:00+00:00"
            price *= 1.0005
            bars.append(
                {
                    "t": ts,
                    "o": price,
                    "h": price * 1.001,
                    "l": price * 0.999,
                    "c": price,
                    "v": 100.0,
                }
            )
    return {"AAA": bars, "BBB": bars.copy()}


class TestIntervalRefusal:
    def test_momentum_15m_refuses_daily_interval(self):
        strategy = Momentum15mStrategy()
        with pytest.raises(IntervalMismatchError):
            strategy.build_evaluator(
                _daily_bars(),
                ["AAA", "BBB"],
                interval=BarInterval.DAILY,
            )

    def test_opening_range_breakout_refuses_daily_interval(self):
        strategy = OpeningRangeBreakoutStrategy()
        with pytest.raises(IntervalMismatchError):
            strategy.build_evaluator(
                _daily_bars(),
                ["AAA", "BBB"],
                interval=BarInterval.DAILY,
            )

    def test_sector_rotation_refuses_intraday(self):
        strategy = SectorRotationDailyStrategy()
        with pytest.raises(IntervalMismatchError):
            strategy.build_evaluator(
                _intraday_bars(),
                ["AAA", "BBB"],
                interval=BarInterval.MINUTE_15,
            )

    def test_rsi_mean_reversion_accepts_daily_and_intraday(self):
        strategy = RSIMeanReversionStrategy()
        # Should not raise for either.
        strategy.build_evaluator(
            _daily_bars(), ["AAA", "BBB"], interval=BarInterval.DAILY
        )
        strategy.build_evaluator(
            _intraday_bars(),
            ["AAA", "BBB"],
            interval=BarInterval.MINUTE_15,
        )


class TestStrategyScoresProduceEvaluations:
    def test_momentum_15m_scores_on_intraday_bars(self):
        strategy = Momentum15mStrategy(lookback_bars=4)
        evaluator = strategy.build_evaluator(
            _intraday_bars(),
            ["AAA", "BBB"],
            interval=BarInterval.MINUTE_15,
        )
        event = BacktestEvent(
            timestamp="2026-01-05T15:45:00+00:00",
            event_type="market_snapshot",
        )
        result = evaluator.evaluate(event)
        assert isinstance(result, StrategyEvaluation)
        assert set(result.scores.keys()) <= {"AAA", "BBB"}
        assert result.rankings

    def test_sector_rotation_uses_sector_map(self):
        strategy = SectorRotationDailyStrategy(lookback_days=5)
        bars = _daily_bars()
        # Add a benchmark series
        bars["XLK"] = [
            dict(b, c=b["c"] * 1.02) for b in bars["AAA"]
        ]
        evaluator = strategy.build_evaluator(
            bars,
            ["AAA", "BBB"],
            interval=BarInterval.DAILY,
            sector_map={"AAA": "XLK", "BBB": "XLK"},
        )
        event = BacktestEvent(
            timestamp=bars["AAA"][-1]["t"],
            event_type="market_snapshot",
        )
        result = evaluator.evaluate(event)
        assert "AAA" in result.scores or "BBB" in result.scores


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


class TestPresets:
    def test_all_expected_presets_registered(self):
        keys = set(list_presets())
        assert DAILY_SWING_WATCHLIST_PRESET in keys
        assert INTRADAY_15M_WATCHLIST_PRESET in keys
        assert CRYPTO_24X7_PAPER_RESEARCH_PRESET in keys

    def test_intraday_preset_references_intraday_strategies(self):
        preset = get_preset(INTRADAY_15M_WATCHLIST_PRESET)
        assert preset.interval == BarInterval.MINUTE_15.value
        assert "momentum_15m_v1" in preset.strategies
        assert "opening_range_breakout_v1" in preset.strategies

    def test_daily_preset_references_daily_strategies(self):
        preset = get_preset(DAILY_SWING_WATCHLIST_PRESET)
        assert preset.interval == BarInterval.DAILY.value
        assert "champion-v0.4.0" in preset.strategies

    def test_crypto_preset_uses_crypto_symbols(self):
        preset = get_preset(CRYPTO_24X7_PAPER_RESEARCH_PRESET)
        assert preset.asset_class == "crypto"
        assert any("/" in s for s in preset.universe)


# ---------------------------------------------------------------------------
# Market calendar
# ---------------------------------------------------------------------------


class TestMarketCalendar:
    def test_regular_session_at_1030_et_weekday(self):
        # 2026-01-05 is a Monday.
        weekday_open = datetime(2026, 1, 5, 10, 30)
        status = session_status(weekday_open)
        assert status.is_regular_session
        assert status.reason == "regular_session"

    def test_weekend_is_closed(self):
        saturday = datetime(2026, 1, 3, 10, 30)
        status = session_status(saturday)
        assert not status.is_regular_session
        assert status.reason == "weekend"

    def test_pre_market_before_930(self):
        early = datetime(2026, 1, 5, 8, 0)
        status = session_status(early)
        assert not status.is_regular_session
        assert status.reason == "pre_market"

    def test_post_market_after_1600(self):
        late = datetime(2026, 1, 5, 16, 30)
        status = session_status(late)
        assert not status.is_regular_session
        assert status.reason == "post_market"

    def test_holiday_is_closed(self):
        # July 4, 2026 is a Saturday → observed on Friday July 3.
        holiday_obs = datetime(2026, 7, 3, 10, 30)
        status = session_status(holiday_obs)
        assert not status.is_regular_session
        assert status.reason == "holiday"

    def test_christmas_is_holiday(self):
        holidays = us_market_holidays(2026)
        # 2026-12-25 is a Friday, observed same day.
        assert date(2026, 12, 25) in holidays


# ---------------------------------------------------------------------------
# Equity paper daemon
# ---------------------------------------------------------------------------


def _daemon_bars() -> Dict[str, List[Dict[str, Any]]]:
    bars: Dict[str, List[Dict[str, Any]]] = {}
    for symbol, mult in (("AAA", 1.001), ("BBB", 1.0005), ("CCC", 0.999)):
        price = 100.0
        symbol_bars = []
        for hour in range(9, 16):
            for minute in (0, 15, 30, 45):
                ts = f"2026-01-05T{hour:02d}:{minute:02d}:00+00:00"
                price *= mult
                symbol_bars.append(
                    {
                        "t": ts,
                        "o": price,
                        "h": price * 1.001,
                        "l": price * 0.999,
                        "c": price,
                        "v": 1_000_000.0,
                    }
                )
        bars[symbol] = symbol_bars
    return bars


class _FakeEvaluator:
    strategy_id = "test-momentum-v1"

    def __init__(self, ranking: Sequence[str], scores: Mapping[str, float]):
        self._ranking = list(ranking)
        self._scores = dict(scores)

    def evaluate(self, event: BacktestEvent) -> StrategyEvaluation:
        rankings = [
            {"symbol": s, "rank": i + 1}
            for i, s in enumerate(self._ranking)
        ]
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            event_timestamp=event.timestamp,
            scores=dict(self._scores),
            rankings=rankings,
        )


def _fake_strategy_builder(**kwargs):
    return _FakeEvaluator(
        ranking=["AAA", "BBB", "CCC"],
        scores={"AAA": 0.9, "BBB": 0.5, "CCC": 0.1},
    )


def _fake_stop_file(tmp_path: Path) -> str:
    return str(tmp_path / "STOP_EQUITY_PAPER")


def _make_equity_daemon(
    tmp_path: Path,
    **override,
) -> EquityPaperDaemon:
    defaults = dict(
        symbol_allowlist=("AAA", "BBB", "CCC"),
        strategy_key="momentum_15m_v1",
        interval="15Min",
        execute=False,
        max_per_order_notional=2_000.0,
        max_total_daily_notional=10_000.0,
        max_trades_per_day=3,
        cooldown_minutes=60,
        top_n_per_tick=3,
        env_file=".env.paper",
        emergency_stop_file=_fake_stop_file(tmp_path),
        state_file=str(tmp_path / "state.json"),
        log_root=str(tmp_path / "logs"),
        plan_root=str(tmp_path / "plans"),
    )
    defaults.update(override)
    config = EquityPaperDaemonConfig(**defaults)
    return EquityPaperDaemon(
        config,
        bar_loader=lambda symbols, interval, start, end: _daemon_bars(),
        strategy_builder=_fake_strategy_builder,
    )


REGULAR_TICK_ET = datetime(2026, 1, 5, 10, 30)


class TestEquityDaemonDryRun:
    def test_no_broker_calls_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        called: List[str] = []

        def _guard(self, *args, **kwargs):
            called.append("_submit_orders")
            raise AssertionError("bridge must not submit in dry-run")

        monkeypatch.setattr(
            PaperTradingBridge, "_submit_orders", _guard, raising=True
        )
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
        )
        assert result.executed is False
        assert result.reason == "cli_execute_flag_not_set"
        assert not called
        assert result.log_path
        assert Path(result.log_path).exists()

    def test_produces_proposed_orders(self, tmp_path: Path):
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
        )
        assert len(result.proposed_orders) >= 1


class TestEquityDaemonSessionGate:
    @pytest.mark.parametrize(
        "when,expected_reason",
        [
            (datetime(2026, 1, 3, 10, 30), "session_not_open:weekend"),
            (datetime(2026, 1, 5, 8, 0), "session_not_open:pre_market"),
            (datetime(2026, 1, 5, 17, 0), "session_not_open:post_market"),
            (datetime(2026, 7, 3, 10, 30), "session_not_open:holiday"),
        ],
    )
    def test_refuses_outside_regular_session(
        self, tmp_path: Path, when: datetime, expected_reason: str
    ):
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(now=when, env={})
        assert result.executed is False
        assert result.reason == expected_reason
        assert not result.proposed_orders


class TestEquityDaemonAutoExecuteGate:
    def _tick(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_flag: str,
        cli_execute: bool,
    ):
        submit_calls: List[Any] = []

        def _fake_submit(self, orders, api_key, secret_key, endpoint):
            submit_calls.append(list(orders))
            return [
                {"symbol": o.symbol, "status": "submitted"}
                for o in orders
            ]

        monkeypatch.setattr(
            PaperTradingBridge,
            "_submit_orders",
            _fake_submit,
            raising=True,
        )
        daemon = _make_equity_daemon(tmp_path, execute=cli_execute)
        env = {
            "PAPER_ALPACA_API_KEY": "k",
            "PAPER_ALPACA_SECRET_KEY": "s",
            "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
        }
        if env_flag:
            env[AUTO_EXECUTE_ENV_VAR] = env_flag
        return daemon.run_tick(now=REGULAR_TICK_ET, env=env), submit_calls

    def test_env_flag_alone_does_not_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, calls = self._tick(
            tmp_path, monkeypatch, env_flag="true", cli_execute=False
        )
        assert result.executed is False
        assert not calls

    def test_cli_flag_alone_does_not_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, calls = self._tick(
            tmp_path, monkeypatch, env_flag="", cli_execute=True
        )
        assert result.executed is False
        assert (
            f"{AUTO_EXECUTE_ENV_VAR}_not_true" in result.reason
        )
        assert not calls

    def test_both_flags_together_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, calls = self._tick(
            tmp_path, monkeypatch, env_flag="true", cli_execute=True
        )
        assert result.executed
        assert calls, "bridge should have received orders"


class TestEquityDaemonProductionRefusal:
    def test_refuses_production_context(self, tmp_path: Path):
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "HERMES_CONTEXT": "production",
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
        )
        assert result.executed is False
        assert result.reason.startswith("env_refused")

    def test_refuses_paper_false(self, tmp_path: Path):
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "PAPER": "False",
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
        )
        assert result.executed is False
        assert result.reason.startswith("env_refused")


class TestEquityDaemonCapsAndCooldowns:
    def _tick_with_state(
        self, tmp_path: Path, state: DaemonState
    ):
        daemon = _make_equity_daemon(tmp_path)
        env = {
            "PAPER_ALPACA_API_KEY": "k",
            "PAPER_ALPACA_SECRET_KEY": "s",
            "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
        }
        return daemon.run_tick(
            now=REGULAR_TICK_ET, env=env, state=state
        )

    def test_duplicate_symbol_rejected(self, tmp_path: Path):
        state = DaemonState(current_day="2026-01-05")
        state.submitted_order_ids.append("AAA")
        result = self._tick_with_state(tmp_path, state)
        symbols_accepted = [o["symbol"] for o in result.proposed_orders]
        assert "AAA" not in symbols_accepted
        assert any(
            r["symbol"] == "AAA"
            and r["rejection"] == "duplicate_symbol_today"
            for r in result.rejected_orders
        )

    def test_cooldown_blocks_symbol(self, tmp_path: Path):
        state = DaemonState(current_day="2026-01-05")
        # Match the daemon's internal UTC clock: session_status()
        # converts REGULAR_TICK_ET (naive → ET) to ET-aware, and the
        # cooldown uses UTC.  Set "last order" 30s before that UTC
        # instant so the cooldown is definitely still active.
        session_now = session_status(REGULAR_TICK_ET).now_et
        recent_utc = session_now.astimezone(timezone.utc)
        state.last_order_iso_by_symbol["AAA"] = recent_utc.isoformat()
        result = self._tick_with_state(tmp_path, state)
        assert not any(
            o["symbol"] == "AAA" for o in result.proposed_orders
        )
        assert any(
            r["symbol"] == "AAA" and "cooldown" in r["rejection"]
            for r in result.rejected_orders
        )

    def test_max_trades_per_day_cap(self, tmp_path: Path):
        state = DaemonState(current_day="2026-01-05")
        state.trades_today = 3  # matches default cap
        result = self._tick_with_state(tmp_path, state)
        assert not result.proposed_orders
        assert any(
            r["rejection"] == "max_trades_per_day_reached"
            for r in result.rejected_orders
        )

    def test_daily_notional_cap(self, tmp_path: Path):
        daemon = _make_equity_daemon(
            tmp_path,
            max_total_daily_notional=2_500.0,
            max_per_order_notional=2_000.0,
        )
        state = DaemonState(current_day="2026-01-05", notional_today=1_500.0)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
            state=state,
        )
        # Only one $1000 slice fits before the aggregate cap trips.
        assert any(
            r["rejection"] == "max_total_daily_notional_reached"
            for r in result.rejected_orders
        )

    def test_state_reset_on_new_day(self, tmp_path: Path):
        # Yesterday's state should not block today's trades.
        old_state = DaemonState(
            current_day="2026-01-02",
            trades_today=99,
            notional_today=99_999.0,
            last_order_iso_by_symbol={
                "AAA": "2026-01-02T10:30:00+00:00"
            },
            submitted_order_ids=["AAA", "BBB", "CCC"],
        )
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
            state=old_state,
        )
        assert result.proposed_orders  # state was reset for new day


class TestEquityDaemonEmergencyStop:
    def test_stop_file_short_circuits(self, tmp_path: Path):
        stop_file = tmp_path / "STOP_EQUITY_PAPER"
        stop_file.write_text("stop")
        daemon = _make_equity_daemon(
            tmp_path, emergency_stop_file=str(stop_file)
        )
        result = daemon.run_tick(now=REGULAR_TICK_ET, env={})
        assert result.executed is False
        assert result.reason == "emergency_stop_file_present"


class TestEquityDaemonLogging:
    def test_log_file_written(self, tmp_path: Path):
        daemon = _make_equity_daemon(tmp_path)
        result = daemon.run_tick(
            now=REGULAR_TICK_ET,
            env={
                "PAPER_ALPACA_API_KEY": "k",
                "PAPER_ALPACA_SECRET_KEY": "s",
                "PAPER_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
            },
        )
        assert result.log_path
        with open(result.log_path, "r") as fh:
            payload = json.load(fh)
        assert payload["tick_id"] == result.tick_id
        assert "session_status" in payload


# ---------------------------------------------------------------------------
# Crypto paper daemon
# ---------------------------------------------------------------------------


def _crypto_bars() -> Dict[str, List[Dict[str, Any]]]:
    bars: Dict[str, List[Dict[str, Any]]] = {}
    for symbol, drift in (
        ("BTC/USD", 1.001),
        ("ETH/USD", 1.0008),
        ("SOL/USD", 1.0002),
    ):
        price = 50_000.0 if "BTC" in symbol else 3_000.0
        symbol_bars = []
        for i in range(30):
            price *= drift
            ts = f"2026-01-05T{i:02d}:00:00+00:00"
            symbol_bars.append(
                {
                    "t": ts,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": 100.0,
                }
            )
        bars[symbol] = symbol_bars
    return bars


CRYPTO_TICK = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _crypto_env() -> Dict[str, str]:
    return {
        "CRYPTO_ALPACA_API_KEY": "k",
        "CRYPTO_ALPACA_SECRET_KEY": "s",
        "CRYPTO_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
    }


def _make_crypto_daemon(tmp_path: Path, **override) -> CryptoPaperDaemon:
    defaults = dict(
        symbol_allowlist=("BTC/USD", "ETH/USD", "SOL/USD"),
        strategy_key="momentum-v0.1.0",
        execute=False,
        max_per_order_notional=1_000.0,
        max_total_daily_notional=2_500.0,
        max_trades_per_day=3,
        cooldown_minutes=60,
        top_n_per_tick=3,
        env_file=".env.crypto",
        emergency_stop_file=str(tmp_path / "STOP_CRYPTO_PAPER"),
        state_file=str(tmp_path / "crypto_state.json"),
        log_root=str(tmp_path / "clogs"),
        plan_root=str(tmp_path / "cplans"),
    )
    defaults.update(override)
    return CryptoPaperDaemon(CryptoPaperDaemonConfig(**defaults))


class TestCryptoDaemonSafety:
    def test_config_refuses_paper_env_file(self, tmp_path: Path):
        with pytest.raises(CryptoPaperDaemonError):
            CryptoPaperDaemonConfig(
                symbol_allowlist=("BTC/USD",),
                env_file=".env.paper",
            ).validate()

    def test_config_refuses_production_env_file(self, tmp_path: Path):
        with pytest.raises(CryptoPaperDaemonError):
            CryptoPaperDaemonConfig(
                symbol_allowlist=("BTC/USD",),
                env_file=".env.production",
            ).validate()

    def test_dry_run_makes_no_broker_calls(self, tmp_path: Path):
        called: List[str] = []

        daemon = _make_crypto_daemon(tmp_path)
        # Patch the submit method on the instance.
        daemon._submit = lambda *a, **kw: (  # type: ignore
            called.append("_submit"),
            [],
        )[1]
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),
            bars_by_symbol=_crypto_bars(),
        )
        assert result.executed is False
        assert not called

    def test_refuses_production_context(self, tmp_path: Path):
        daemon = _make_crypto_daemon(tmp_path)
        env = dict(_crypto_env())
        env["HERMES_CONTEXT"] = "production"
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=env,
            bars_by_symbol=_crypto_bars(),
        )
        assert result.reason.startswith("env_refused")

    def test_refuses_paper_false(self, tmp_path: Path):
        daemon = _make_crypto_daemon(tmp_path)
        env = dict(_crypto_env())
        env["PAPER"] = "False"
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=env,
            bars_by_symbol=_crypto_bars(),
        )
        assert result.reason.startswith("env_refused")

    def test_emergency_stop_file(self, tmp_path: Path):
        stop = tmp_path / "STOP_CRYPTO_PAPER"
        stop.write_text("stop")
        daemon = _make_crypto_daemon(
            tmp_path, emergency_stop_file=str(stop)
        )
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),
            bars_by_symbol=_crypto_bars(),
        )
        assert result.reason == "emergency_stop_file_present"


class TestCryptoDaemonCapsAndCooldowns:
    def test_produces_proposed_orders(self, tmp_path: Path):
        daemon = _make_crypto_daemon(tmp_path)
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),
            bars_by_symbol=_crypto_bars(),
        )
        assert result.proposed_orders

    def test_duplicate_symbol_rejected(self, tmp_path: Path):
        from strategy.crypto_paper_daemon import CryptoDaemonState

        state = CryptoDaemonState(current_day="2026-01-05")
        state.submitted_order_ids.append("BTC/USD")
        daemon = _make_crypto_daemon(tmp_path)
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),
            bars_by_symbol=_crypto_bars(),
            state=state,
        )
        assert not any(
            o["symbol"] == "BTC/USD" for o in result.proposed_orders
        )
        assert any(
            r["symbol"] == "BTC/USD"
            and r["rejection"] == "duplicate_symbol_today"
            for r in result.rejected_orders
        )

    def test_cooldown_active(self, tmp_path: Path):
        from strategy.crypto_paper_daemon import CryptoDaemonState

        state = CryptoDaemonState(current_day="2026-01-05")
        state.last_order_iso_by_symbol["BTC/USD"] = CRYPTO_TICK.isoformat()
        daemon = _make_crypto_daemon(tmp_path)
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),
            bars_by_symbol=_crypto_bars(),
            state=state,
        )
        assert any(
            r["symbol"] == "BTC/USD" and "cooldown" in r["rejection"]
            for r in result.rejected_orders
        )

    def test_max_trades_per_day_cap(self, tmp_path: Path):
        from strategy.crypto_paper_daemon import CryptoDaemonState

        state = CryptoDaemonState(
            current_day="2026-01-05", trades_today=3
        )
        daemon = _make_crypto_daemon(tmp_path, max_trades_per_day=3)
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),
            bars_by_symbol=_crypto_bars(),
            state=state,
        )
        assert not result.proposed_orders

    def test_auto_execute_requires_env_and_cli(self, tmp_path: Path):
        # CLI flag alone → dry-run.
        daemon = _make_crypto_daemon(tmp_path, execute=True)
        result = daemon.run_tick(
            now=CRYPTO_TICK,
            env=_crypto_env(),  # missing PAPER_AUTO_EXECUTE_CRYPTO
            bars_by_symbol=_crypto_bars(),
        )
        assert result.executed is False
        assert result.reason.startswith(CRYPTO_AUTO_EXECUTE_ENV_VAR)


class TestCryptoEnvLoader:
    def test_refuses_paper_file(self, tmp_path: Path):
        # even if the file exists, name check refuses.
        env_file = tmp_path / ".env.paper"
        env_file.write_text("CRYPTO_ALPACA_API_KEY=x\n")
        with pytest.raises(CryptoPaperDaemonError):
            load_crypto_env(str(env_file))

    def test_refuses_production_file(self):
        with pytest.raises(CryptoPaperDaemonError):
            load_crypto_env(".env.production")

    def test_refuses_missing_file(self, tmp_path: Path):
        with pytest.raises(CryptoPaperDaemonError):
            load_crypto_env(str(tmp_path / ".env.crypto"))


# ---------------------------------------------------------------------------
# Source-safety: live paths untouched, no PAPER=False, no feature flags
# ---------------------------------------------------------------------------


NEW_MODULE_PATHS = [
    "strategy/lab/interval_requirements.py",
    "strategy/lab/momentum_15m.py",
    "strategy/lab/opening_range_breakout.py",
    "strategy/lab/rsi_mean_reversion.py",
    "strategy/lab/sector_rotation_daily.py",
    "strategy/lab/volatility_regime_filter.py",
    "strategy/lab/presets.py",
    "strategy/market_calendar.py",
    "strategy/paper_daemon.py",
    "strategy/paper_daemon_main.py",
    "strategy/crypto_paper_daemon.py",
    "strategy/crypto_paper_daemon_main.py",
]


class TestSourceSafety:
    @pytest.mark.parametrize("relpath", NEW_MODULE_PATHS)
    def test_no_paper_false_or_live_runner_imports(self, relpath: str):
        source = Path(relpath).read_text()
        assert "PAPER = False" not in source
        assert "paper=False" not in source
        assert "from trader import" not in source
        assert "import trader\n" not in source
        assert "from crypto_trader import" not in source
        assert "strategy.runner" not in source

    @pytest.mark.parametrize("relpath", NEW_MODULE_PATHS)
    def test_no_feature_flag_enable_at_module_level(self, relpath: str):
        source = Path(relpath).read_text()
        # No module-level FeatureFlags(...enable_...=True) construction.
        # (Local factory usage inside build_evaluator is fine — that's
        # scoped to a single caller-supplied evaluator instance.)
        assert "get_feature_flags" not in source

    def test_live_runner_untouched(self):
        # trader.py must still have PAPER = True.
        source = Path("trader.py").read_text()
        assert "PAPER = True" in source
        source = Path("crypto_trader.py").read_text()
        assert "PAPER = True" in source


# ---------------------------------------------------------------------------
# Default crypto bar loader
# ---------------------------------------------------------------------------


class _FakeCryptoBar:
    """Minimal bar object matching the alpaca-py Bar attribute
    surface — we don't need the full SDK to test the loader shape.
    """

    def __init__(self, timestamp, open_, high, low, close, volume):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class _FakeBarSet:
    def __init__(self, data):
        self.data = data


class _FakeCryptoClient:
    def __init__(self, *, api_key, secret_key, response):
        self.api_key = api_key
        self.secret_key = secret_key
        self.response = response
        self.calls = 0

    def get_crypto_bars(self, request):
        self.calls += 1
        self.last_request = request
        return self.response


def _sample_response(symbols: Sequence[str]) -> _FakeBarSet:
    data = {}
    for symbol in symbols:
        bars = []
        price = 100.0 if "BTC" not in symbol else 50_000.0
        for i in range(30):
            ts = datetime(2026, 1, 5, i % 24, 0, tzinfo=timezone.utc)
            price *= 1.001
            bars.append(
                _FakeCryptoBar(
                    timestamp=ts,
                    open_=price,
                    high=price * 1.001,
                    low=price * 0.999,
                    close=price,
                    volume=100.0,
                )
            )
        data[symbol] = bars
    return _FakeBarSet(data=data)


def _crypto_env_valid() -> Dict[str, str]:
    return {
        "CRYPTO_ALPACA_API_KEY": "key",
        "CRYPTO_ALPACA_SECRET_KEY": "secret",
        "CRYPTO_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2",
    }


class TestDefaultCryptoBarLoader:
    def test_success_returns_shaped_bars(self):
        symbols = ["BTC/USD", "ETH/USD"]
        fake_response = _sample_response(symbols)
        client_holder: List[_FakeCryptoClient] = []

        def _make_client(*, api_key, secret_key):
            client = _FakeCryptoClient(
                api_key=api_key,
                secret_key=secret_key,
                response=fake_response,
            )
            client_holder.append(client)
            return client

        def _make_request(symbols, start, end):
            return {"symbols": list(symbols), "start": start, "end": end}

        bars = default_crypto_bar_loader(
            symbols=symbols,
            now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
            env=_crypto_env_valid(),
            client_factory=(_make_client, _make_request),
        )
        assert set(bars.keys()) == set(symbols)
        for sym in symbols:
            assert bars[sym], f"no bars returned for {sym}"
            first = bars[sym][0]
            assert set(first.keys()) == {"t", "o", "h", "l", "c", "v"}
            assert isinstance(first["c"], float)
        assert client_holder[0].calls == 1

    def test_missing_credentials_raises(self):
        with pytest.raises(CryptoPaperDaemonError):
            default_crypto_bar_loader(
                symbols=["BTC/USD"],
                now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
                env={
                    "CRYPTO_ALPACA_ENDPOINT": (
                        "https://paper-api.alpaca.markets/v2"
                    )
                },
            )

    def test_refuses_non_paper_endpoint(self):
        bad_env = {
            "CRYPTO_ALPACA_API_KEY": "k",
            "CRYPTO_ALPACA_SECRET_KEY": "s",
            "CRYPTO_ALPACA_ENDPOINT": "https://api.alpaca.markets/v2",
        }
        with pytest.raises(CryptoPaperDaemonError):
            default_crypto_bar_loader(
                symbols=["BTC/USD"],
                now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
                env=bad_env,
            )

    def test_refuses_paper_alpaca_credentials_only(self):
        """The crypto loader must NOT accept equity-paper creds as
        a fallback — it reads only the CRYPTO_ALPACA_* namespace.
        """
        env = {
            "PAPER_ALPACA_API_KEY": "should-not-be-used",
            "PAPER_ALPACA_SECRET_KEY": "should-not-be-used",
            "CRYPTO_ALPACA_ENDPOINT": (
                "https://paper-api.alpaca.markets/v2"
            ),
        }
        with pytest.raises(CryptoPaperDaemonError):
            default_crypto_bar_loader(
                symbols=["BTC/USD"],
                now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
                env=env,
            )

    def test_missing_endpoint_raises(self):
        with pytest.raises(CryptoPaperDaemonError):
            default_crypto_bar_loader(
                symbols=["BTC/USD"],
                now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
                env={
                    "CRYPTO_ALPACA_API_KEY": "k",
                    "CRYPTO_ALPACA_SECRET_KEY": "s",
                },
            )

    def test_invalid_lookback_raises(self):
        with pytest.raises(CryptoPaperDaemonError):
            default_crypto_bar_loader(
                symbols=["BTC/USD"],
                now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
                env=_crypto_env_valid(),
                lookback_bars=0,
                client_factory=(
                    lambda **kw: None,
                    lambda symbols, start, end: None,
                ),
            )

    def test_start_precedes_end(self):
        fake_response = _sample_response(["BTC/USD"])
        captured: Dict[str, Any] = {}

        def _make_client(*, api_key, secret_key):
            return _FakeCryptoClient(
                api_key=api_key,
                secret_key=secret_key,
                response=fake_response,
            )

        def _make_request(symbols, start, end):
            captured["start"] = start
            captured["end"] = end
            return {"symbols": list(symbols), "start": start, "end": end}

        default_crypto_bar_loader(
            symbols=["BTC/USD"],
            now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
            env=_crypto_env_valid(),
            lookback_bars=10,
            interval_minutes=60,
            client_factory=(_make_client, _make_request),
        )
        assert captured["start"] < captured["end"]
        assert (captured["end"] - captured["start"]).total_seconds() > 0


class TestCryptoDaemonWithDefaultLoader:
    """End-to-end: daemon uses the default loader (with a mock
    SDK client) and stays in dry-run without any submit calls.
    """

    def test_dry_run_uses_default_loader_and_makes_no_submit(
        self, tmp_path: Path
    ):
        fake_response = _sample_response(["BTC/USD", "ETH/USD"])
        client_holder: List[_FakeCryptoClient] = []

        def _make_client(*, api_key, secret_key):
            client = _FakeCryptoClient(
                api_key=api_key,
                secret_key=secret_key,
                response=fake_response,
            )
            client_holder.append(client)
            return client

        def _make_request(symbols, start, end):
            return {"symbols": list(symbols), "start": start, "end": end}

        submit_calls: List[Any] = []

        def _bound_loader(symbols, now, env):
            return default_crypto_bar_loader(
                symbols=symbols,
                now_utc=now,
                env=env,
                client_factory=(_make_client, _make_request),
            )

        daemon = CryptoPaperDaemon(
            CryptoPaperDaemonConfig(
                symbol_allowlist=("BTC/USD", "ETH/USD"),
                strategy_key="momentum-v0.1.0",
                execute=False,
                env_file=".env.crypto",
                emergency_stop_file=str(tmp_path / "STOP_CRYPTO_PAPER"),
                state_file=str(tmp_path / "state.json"),
                log_root=str(tmp_path / "logs"),
                plan_root=str(tmp_path / "plans"),
            ),
            bar_loader=_bound_loader,
        )
        daemon._submit = lambda *a, **kw: (  # type: ignore
            submit_calls.append("submit"),
            [],
        )[1]

        result = daemon.run_tick(
            now=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
            env=_crypto_env_valid(),
        )
        # Loader was called through the daemon.
        assert client_holder, "default loader should have been invoked"
        # Dry-run: no submit call.
        assert not submit_calls
        assert result.executed is False
        # We should have proposed at least one order (positive drift
        # ensures positive momentum score).
        assert result.proposed_orders

    def test_default_loader_bubbles_endpoint_refusal(self, tmp_path: Path):
        daemon = CryptoPaperDaemon(
            CryptoPaperDaemonConfig(
                symbol_allowlist=("BTC/USD",),
                strategy_key="momentum-v0.1.0",
                execute=False,
                env_file=".env.crypto",
                emergency_stop_file=str(tmp_path / "STOP_CRYPTO_PAPER"),
                state_file=str(tmp_path / "state.json"),
                log_root=str(tmp_path / "logs"),
                plan_root=str(tmp_path / "plans"),
            )
        )
        # Missing credentials → loader raises inside daemon.
        env_missing_creds = {
            "CRYPTO_ALPACA_API_KEY": "",
            "CRYPTO_ALPACA_SECRET_KEY": "",
            "CRYPTO_ALPACA_ENDPOINT": (
                "https://paper-api.alpaca.markets/v2"
            ),
        }
        result = daemon.run_tick(
            now=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
            env=env_missing_creds,
        )
        assert result.reason.startswith("bar_load_failed")
        assert not result.executed
