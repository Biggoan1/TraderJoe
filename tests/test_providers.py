"""Tests for strategy/providers/ plugins.

Fifth Phase 5.6 implementation card ``t_phase56_provider_plugins``.

Covers:

* Registry: register / get / duplicate-rejection
* AlpacaProvider: capabilities, bar mapping via a stubbed
  ResearchAccountClient, adjustment translation, per-symbol
  status, calendar mapping, corporate-actions + metadata return
  empty batches with warnings, Protocol conformance
* CsvProvider: canonical column parsing, filtering by symbol /
  start / end, missing-file + missing-column errors, per-symbol
  status
* ParquetProvider: reads via the parquet_io layer, filters by
  interval / symbol / window, Protocol conformance
* Read-only guarantees: no live-runner imports, no order path,
  no credential env reads, feature-flag invariance
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    CalendarDay,
    CalendarSessionKind,
    MarketDataProvider,
    MarketDataRequestError,
    MarketDataValidationError,
    ProviderCapabilities,
    ProviderResponse,
)
from strategy.research_account import (
    ResearchAccountClient,
    ResearchAccountRequestError,
)


# Import the plugin modules so their register() side effects run.
import strategy.providers as providers_pkg
import strategy.providers.alpaca as alpaca_module
import strategy.providers.csv as csv_module
import strategy.providers.parquet as parquet_module


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_all_three_plugins_registered_after_import(self):
        assert set(providers_pkg.registered()) >= {
            "alpaca", "csv", "parquet"
        }

    def test_get_plugin_returns_factory(self):
        factory = providers_pkg.get_plugin("csv")
        assert callable(factory)

    def test_get_missing_plugin_raises(self):
        with pytest.raises(KeyError, match="not registered"):
            providers_pkg.get_plugin("no-such-plugin")

    def test_reregister_same_factory_is_idempotent(self):
        # Re-import must not double-register or raise
        providers_pkg.register("csv", csv_module.factory)

    def test_register_different_factory_under_same_name_raises(self):
        def other(*args, **kwargs):
            raise RuntimeError("nope")
        with pytest.raises(KeyError, match="already registered"):
            providers_pkg.register("csv", other)


# ---------------------------------------------------------------------------
# AlpacaProvider
# ---------------------------------------------------------------------------


class _StubAlpacaClient:
    """Minimal shape needed by AlpacaProvider — only fetch_bars +
    list_calendar are exercised.  Records the last call so tests
    can assert the mapping was performed correctly.
    """

    def __init__(self, bars_payload=None, calendar_payload=None):
        self._bars_payload = bars_payload or {"bars": {}}
        self._calendar_payload = calendar_payload or []
        self.fetch_bars_calls: List[Dict[str, Any]] = []
        self.calendar_calls: List[Dict[str, Any]] = []
        self.raise_on_bars = False
        self.raise_on_calendar = False

    def fetch_bars(self, **kwargs):
        self.fetch_bars_calls.append(kwargs)
        if self.raise_on_bars:
            raise ResearchAccountRequestError("simulated failure")
        return self._bars_payload

    def list_calendar(self, **kwargs):
        self.calendar_calls.append(kwargs)
        if self.raise_on_calendar:
            raise ResearchAccountRequestError("simulated failure")
        return self._calendar_payload


def _bars_payload():
    return {
        "bars": {
            "AAPL": [
                {
                    "t": "2020-05-01T14:30:00+00:00",
                    "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5,
                    "v": 1_000_000, "vw": 100.25, "n": 42,
                },
                {
                    "t": "2020-05-02T14:30:00+00:00",
                    "o": 101.0, "h": 102.0, "l": 100.0, "c": 101.5,
                    "v": 900_000,
                },
            ],
            "MSFT": [],
        }
    }


class TestAlpacaProviderBars:
    def test_capabilities(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        caps = prov.provider_capabilities()
        assert caps.provider_name == "alpaca"
        assert BarInterval.DAILY in caps.supported_intervals
        assert caps.supports_calendar is True
        assert caps.supports_corporate_actions is False

    def test_conforms_to_protocol(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        assert isinstance(prov, MarketDataProvider)

    def test_fetch_daily_bars_maps_response(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        resp = prov.fetch_daily_bars(
            ["AAPL", "MSFT"], "2020-05-01", "2020-05-31"
        )
        assert resp.provider == "alpaca"
        assert len(resp.batch) == 2
        assert resp.batch[0].symbol == "AAPL"
        assert resp.batch[0].interval is BarInterval.DAILY
        assert resp.batch[0].vwap == 100.25
        assert resp.batch[0].trade_count == 42
        # Per-symbol status: AAPL got data, MSFT was empty
        assert resp.per_symbol_status["AAPL"] == "ok"
        assert resp.per_symbol_status["MSFT"] == "empty"

    def test_translates_adjustment_mode_to_alpaca_vocab(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        prov.fetch_daily_bars(
            ["AAPL"], "2020-05-01", "2020-05-31",
            adjustment=AdjustmentMode.SPLIT_DIVIDEND,
        )
        assert client.fetch_bars_calls[0]["adjustment"] == "all"

    def test_intraday_rejects_daily_interval(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        with pytest.raises(MarketDataValidationError, match="DAILY"):
            prov.fetch_intraday_bars(
                ["AAPL"], "2020-05-01", "2020-05-31",
                interval=BarInterval.DAILY,
            )

    def test_intraday_rejects_second_interval(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        with pytest.raises(MarketDataValidationError, match="second"):
            prov.fetch_intraday_bars(
                ["AAPL"], "2020-05-01", "2020-05-31",
                interval=BarInterval.SECOND_1,
            )

    def test_empty_symbols_rejected(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        with pytest.raises(MarketDataValidationError, match="symbols"):
            prov.fetch_daily_bars([], "2020-05-01", "2020-05-31")

    def test_client_failure_becomes_request_error(self):
        client = _StubAlpacaClient(_bars_payload())
        client.raise_on_bars = True
        prov = alpaca_module.AlpacaProvider(client=client)
        with pytest.raises(MarketDataRequestError, match="simulated"):
            prov.fetch_daily_bars(["AAPL"], "2020-05-01", "2020-05-31")

    def test_bad_row_skipped_not_raised(self):
        client = _StubAlpacaClient({
            "bars": {"AAPL": [{"t": "not-a-date", "o": "bogus"}]}
        })
        prov = alpaca_module.AlpacaProvider(client=client)
        # Bad row is dropped rather than raising
        resp = prov.fetch_daily_bars(
            ["AAPL"], "2020-05-01", "2020-05-31"
        )
        assert resp.batch == []

    def test_hourly_intraday_bars(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        resp = prov.fetch_intraday_bars(
            ["AAPL"], "2020-05-01", "2020-05-31",
            interval=BarInterval.HOURLY,
        )
        assert resp.batch[0].interval is BarInterval.HOURLY

    def test_bad_response_shape_raises(self):
        client = _StubAlpacaClient({"bars": "not a dict"})
        prov = alpaca_module.AlpacaProvider(client=client)
        with pytest.raises(MarketDataRequestError, match="shape"):
            prov.fetch_daily_bars(["AAPL"], "2020-05-01", "2020-05-31")


class TestAlpacaProviderMultiSymbolResponse:
    """Regression tests for the bug where a 5-symbol daily-bar
    request against a paginated Alpaca payload returned only the
    lexicographically-smallest symbol (AAPL) with data and marked
    MSFT/NVDA/SPY/QQQ as empty.  The fix is in
    ResearchAccountClient.fetch_bars; these tests exercise the
    provider layer with an already-merged payload to prove the
    provider does the right thing when every symbol has bars.
    """

    def _five_symbol_payload(self):
        # 30 daily bars per symbol, all five symbols populated.
        return {
            "bars": {
                sym: [
                    {
                        "t": f"2020-01-{d:02d}T14:30:00+00:00",
                        "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5,
                        "v": 1_000_000,
                    }
                    for d in range(1, 31)
                ]
                for sym in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ")
            }
        }

    def test_all_symbols_marked_ok_when_payload_covers_them(self):
        client = _StubAlpacaClient(self._five_symbol_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        resp = prov.fetch_daily_bars(
            ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"],
            "2020-01-01", "2020-01-30",
        )
        for sym in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ"):
            assert resp.per_symbol_status[sym] == "ok", (
                f"{sym} marked {resp.per_symbol_status[sym]!r}; "
                "expected 'ok' after pagination fix"
            )
        # Total bars = 5 symbols × 30 days
        assert len(resp.batch) == 150
        # Bars for every symbol appear
        by_symbol: Dict[str, List] = {}
        for bar in resp.batch:
            by_symbol.setdefault(bar.symbol, []).append(bar)
        for sym in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ"):
            assert len(by_symbol[sym]) == 30


class TestAlpacaProviderCalendar:
    def test_calendar_mapping(self):
        client = _StubAlpacaClient(
            calendar_payload=[
                {"date": "2020-05-01", "open": "09:30", "close": "16:00"},
                {"date": "2020-05-25"},  # holiday
            ]
        )
        prov = alpaca_module.AlpacaProvider(client=client)
        resp = prov.fetch_calendar("2020-05-01", "2020-05-31")
        assert len(resp.batch) == 2
        assert resp.batch[0].kind is CalendarSessionKind.FULL_TRADING
        assert resp.batch[1].kind is CalendarSessionKind.HOLIDAY
        assert resp.batch[1].open_time is None

    def test_calendar_client_failure_becomes_request_error(self):
        client = _StubAlpacaClient()
        client.raise_on_calendar = True
        prov = alpaca_module.AlpacaProvider(client=client)
        with pytest.raises(MarketDataRequestError, match="simulated"):
            prov.fetch_calendar("2020-05-01", "2020-05-31")


class TestAlpacaProviderUnsupportedSurfaces:
    def test_corporate_actions_returns_empty_with_warning(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        resp = prov.fetch_corporate_actions(
            ["AAPL"], "2020-01-01", "2020-12-31"
        )
        assert resp.batch == []
        assert resp.warnings
        assert "corporate actions" in resp.warnings[0]

    def test_symbol_metadata_returns_empty_with_warning(self):
        client = _StubAlpacaClient(_bars_payload())
        prov = alpaca_module.AlpacaProvider(client=client)
        resp = prov.fetch_symbol_metadata(["AAPL"])
        assert resp.batch == []
        assert resp.warnings


# ---------------------------------------------------------------------------
# CsvProvider
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows):
    header = ("symbol,timestamp,open,high,low,close,volume,vwap,trade_count\n")
    body = "".join(
        f"{r['symbol']},{r['timestamp']},{r['open']},{r['high']},"
        f"{r['low']},{r['close']},{r['volume']},{r.get('vwap','')},"
        f"{r.get('trade_count','')}\n"
        for r in rows
    )
    path.write_text(header + body, encoding="utf-8")


class TestCsvProvider:
    def test_capabilities(self, tmp_path):
        p = tmp_path / "b.csv"
        _write_csv(p, [])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        caps = prov.provider_capabilities()
        assert caps.provider_name == "csv"
        assert caps.supported_intervals == (BarInterval.DAILY,)
        assert caps.supports_calendar is False

    def test_conforms_to_protocol(self, tmp_path):
        p = tmp_path / "b.csv"
        _write_csv(p, [])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        assert isinstance(prov, MarketDataProvider)

    def test_roundtrip_reads_canonical_columns(self, tmp_path):
        p = tmp_path / "b.csv"
        _write_csv(p, [
            {"symbol": "AAPL", "timestamp": "2020-05-01T14:30:00+00:00",
             "open": 100, "high": 101, "low": 99.5, "close": 100.5,
             "volume": 1_000_000, "vwap": 100.25, "trade_count": 42},
            {"symbol": "MSFT", "timestamp": "2020-05-02T14:30:00+00:00",
             "open": 200, "high": 201, "low": 199.5, "close": 200.5,
             "volume": 500_000},
        ])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        resp = prov.fetch_daily_bars(
            ["AAPL", "MSFT"], "2020-05-01", "2020-05-31"
        )
        assert len(resp.batch) == 2
        aapl = next(b for b in resp.batch if b.symbol == "AAPL")
        assert aapl.vwap == 100.25
        assert aapl.trade_count == 42
        msft = next(b for b in resp.batch if b.symbol == "MSFT")
        assert msft.vwap is None
        assert msft.trade_count is None

    def test_filters_by_symbol(self, tmp_path):
        p = tmp_path / "b.csv"
        _write_csv(p, [
            {"symbol": "AAPL", "timestamp": "2020-05-01T00:00:00Z",
             "open": 100, "high": 101, "low": 99.5, "close": 100.5,
             "volume": 1_000_000},
            {"symbol": "MSFT", "timestamp": "2020-05-01T00:00:00Z",
             "open": 200, "high": 201, "low": 199.5, "close": 200.5,
             "volume": 500_000},
        ])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        resp = prov.fetch_daily_bars(
            ["AAPL"], "2020-05-01", "2020-05-31"
        )
        assert {b.symbol for b in resp.batch} == {"AAPL"}

    def test_filters_by_start_and_end(self, tmp_path):
        p = tmp_path / "b.csv"
        _write_csv(p, [
            {"symbol": "AAPL", "timestamp": "2020-04-30T00:00:00Z",
             "open": 100, "high": 101, "low": 99.5, "close": 100.5,
             "volume": 1_000_000},
            {"symbol": "AAPL", "timestamp": "2020-05-15T00:00:00Z",
             "open": 100, "high": 101, "low": 99.5, "close": 100.5,
             "volume": 1_000_000},
            {"symbol": "AAPL", "timestamp": "2020-06-01T00:00:00Z",
             "open": 100, "high": 101, "low": 99.5, "close": 100.5,
             "volume": 1_000_000},
        ])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        resp = prov.fetch_daily_bars(
            ["AAPL"], "2020-05-01", "2020-05-31"
        )
        assert len(resp.batch) == 1
        assert resp.batch[0].timestamp == "2020-05-15T00:00:00Z"

    def test_missing_file_raises(self, tmp_path):
        prov = csv_module.CsvProvider(
            source=tmp_path / "nope.csv", interval=BarInterval.DAILY
        )
        with pytest.raises(MarketDataRequestError, match="not found"):
            prov.fetch_daily_bars(["AAPL"], "2020-05-01", "2020-05-31")

    def test_missing_columns_raise(self, tmp_path):
        p = tmp_path / "b.csv"
        p.write_text("symbol,timestamp,open\nAAPL,2020-05-01,100\n", encoding="utf-8")
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        with pytest.raises(MarketDataRequestError, match="missing required"):
            prov.fetch_daily_bars(["AAPL"], "2020-05-01", "2020-05-31")

    def test_intraday_wrong_interval_rejected(self, tmp_path):
        p = tmp_path / "b.csv"
        _write_csv(p, [])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.HOURLY)
        with pytest.raises(MarketDataValidationError, match="HOURLY"):
            prov.fetch_intraday_bars(
                ["AAPL"], "2020-05-01", "2020-05-31",
                interval=BarInterval.MINUTE_1,
            )


# ---------------------------------------------------------------------------
# ParquetProvider (leans on parquet_io)
# ---------------------------------------------------------------------------


class TestParquetProvider:
    def _prepared(self, tmp_path):
        from strategy.warehouse.parquet_io import write_bars
        layout = WarehouseLayout(root=tmp_path / "wh")
        layout.create()
        bars = [
            Bar(
                symbol="AAPL", timestamp="2020-05-01T14:30:00+00:00",
                open=100.0, high=101.0, low=99.5, close=100.5,
                volume=1_000_000, interval=BarInterval.DAILY,
                adjustment_mode=AdjustmentMode.RAW,
            ),
            Bar(
                symbol="MSFT", timestamp="2020-05-01T14:30:00+00:00",
                open=200.0, high=201.0, low=199.5, close=200.5,
                volume=500_000, interval=BarInterval.DAILY,
                adjustment_mode=AdjustmentMode.RAW,
            ),
        ]
        written = write_bars(
            layout, bars, AssetClass.EQUITY, dataset_id="d"
        )
        return layout, [w.relative_path for w in written]

    def test_capabilities(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        prov = parquet_module.ParquetProvider(layout=layout, relative_paths=[])
        caps = prov.provider_capabilities()
        assert caps.provider_name == "parquet"
        assert set(caps.supported_intervals) == set(BarInterval)

    def test_conforms_to_protocol(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        prov = parquet_module.ParquetProvider(layout=layout, relative_paths=[])
        assert isinstance(prov, MarketDataProvider)

    def test_fetches_bars_from_parquet(self, tmp_path):
        layout, paths = self._prepared(tmp_path)
        prov = parquet_module.ParquetProvider(
            layout=layout, relative_paths=paths
        )
        resp = prov.fetch_daily_bars(
            ["AAPL", "MSFT"], "2020-05-01", "2020-05-31"
        )
        assert {b.symbol for b in resp.batch} == {"AAPL", "MSFT"}
        assert resp.per_symbol_status["AAPL"] == "ok"

    def test_filters_by_symbol_and_window(self, tmp_path):
        layout, paths = self._prepared(tmp_path)
        prov = parquet_module.ParquetProvider(
            layout=layout, relative_paths=paths
        )
        resp = prov.fetch_daily_bars(
            ["AAPL"], "2020-05-01", "2020-05-31"
        )
        assert {b.symbol for b in resp.batch} == {"AAPL"}

    def test_intraday_rejects_daily(self, tmp_path):
        layout, paths = self._prepared(tmp_path)
        prov = parquet_module.ParquetProvider(
            layout=layout, relative_paths=paths
        )
        with pytest.raises(MarketDataValidationError, match="DAILY"):
            prov.fetch_intraday_bars(
                ["AAPL"], "2020-05-01", "2020-05-31",
                interval=BarInterval.DAILY,
            )


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source(module_name: str) -> str:
    module = sys.modules[module_name]
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    @pytest.mark.parametrize(
        "module_name",
        [
            "strategy.providers",
            "strategy.providers.alpaca",
            "strategy.providers.csv",
            "strategy.providers.parquet",
        ],
    )
    def test_no_order_path_or_live_runner_imports(self, module_name):
        source = _module_source(module_name)
        for token in (
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "TradingClient",
            "from trader import",
            "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source, (
                f"{module_name} must not reference {token!r}"
            )

    @pytest.mark.parametrize(
        "module_name",
        [
            "strategy.providers",
            "strategy.providers.alpaca",
            "strategy.providers.csv",
            "strategy.providers.parquet",
        ],
    )
    def test_no_provider_credential_env_reads(self, module_name):
        source = _module_source(module_name)
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
            r'os\.environ\[\s*[\'"]CRYPTO_ALPACA_',
        ):
            assert not re.search(pattern, source), (
                f"{module_name} must not read {pattern!r}"
            )

    def test_no_approval_record_or_promotion_construction(self):
        for mod in (
            "strategy.providers",
            "strategy.providers.alpaca",
            "strategy.providers.csv",
            "strategy.providers.parquet",
        ):
            source = _module_source(mod)
            assert "ApprovalRecord(" not in source
            assert "PromotionEntry(" not in source


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled_after_module_import(self):
        flags = reset_feature_flags()
        import strategy.providers.alpaca  # noqa: F401
        import strategy.providers.csv  # noqa: F401
        import strategy.providers.parquet  # noqa: F401
        assert flags.all_disabled is True

    def test_flags_stay_disabled_after_csv_read(self, tmp_path):
        flags = reset_feature_flags()
        p = tmp_path / "b.csv"
        _write_csv(p, [
            {"symbol": "AAPL", "timestamp": "2020-05-01",
             "open": 100, "high": 101, "low": 99.5, "close": 100.5,
             "volume": 1},
        ])
        prov = csv_module.CsvProvider(source=p, interval=BarInterval.DAILY)
        prov.fetch_daily_bars(["AAPL"], "2020-05-01", "2020-05-31")
        assert flags.all_disabled is True
