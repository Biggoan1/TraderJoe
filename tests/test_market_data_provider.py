"""Tests for strategy/market_data_provider.py.

Foundation card ``t_phase56_provider_interface`` — this test file
covers:

* Data-model construction, invariants, and serialization round-trips.
* Enum values and behavioral properties.
* ``ProviderResponse`` shape + credential-leak guardrail.
* Protocol conformance via a fake provider implementation.
* Read-only source-level safety (no order-path tokens, no live-runner
  imports).
* ``FeatureFlags`` global state stays disabled after every fixture
  and every test.

No plugin implementations exist yet; a ``FakeProvider`` here is the
minimal Protocol-satisfying stub used only to prove the shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    CalendarDay,
    CalendarSessionKind,
    CorporateAction,
    CorporateActionKind,
    MarketDataProvider,
    MarketDataRequestError,
    MarketDataValidationError,
    ProviderCapabilities,
    ProviderResponse,
    SymbolMetadata,
)


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


class TestBarInterval:
    def test_daily_is_not_intraday(self):
        assert BarInterval.DAILY.is_intraday is False

    def test_other_intervals_are_intraday(self):
        for interval in BarInterval:
            if interval is BarInterval.DAILY:
                continue
            assert interval.is_intraday is True, (
                f"{interval} must be intraday"
            )

    def test_values_are_alpaca_shaped(self):
        # Alpaca-style values so the plugin can round-trip verbatim.
        assert BarInterval.DAILY.value == "1Day"
        assert BarInterval.HOURLY.value == "1Hour"
        assert BarInterval.MINUTE_1.value == "1Min"
        assert BarInterval.MINUTE_15.value == "15Min"
        assert BarInterval.SECOND_1.value == "1Sec"


class TestAdjustmentMode:
    def test_all_four_modes_named(self):
        names = {mode.name for mode in AdjustmentMode}
        assert names == {"RAW", "SPLIT", "SPLIT_DIVIDEND", "TOTAL_RETURN"}


class TestCorporateActionKind:
    def test_covers_the_seven_kinds_from_the_design_doc(self):
        names = {kind.name for kind in CorporateActionKind}
        assert names == {
            "SPLIT",
            "REVERSE_SPLIT",
            "CASH_DIVIDEND",
            "SPECIAL_DIVIDEND",
            "TICKER_CHANGE",
            "DELISTING",
            "MERGER",
        }


class TestCalendarSessionKind:
    def test_three_session_kinds(self):
        assert {kind.name for kind in CalendarSessionKind} == {
            "FULL_TRADING",
            "HALF_TRADING",
            "HOLIDAY",
        }


class TestAssetClass:
    def test_covers_warehouse_partitions(self):
        # Directory partitions in the design doc: equities/, crypto/,
        # options/, futures/.  ETF is an asset-class flag on the
        # metadata row, not a separate directory.
        names = {cls.name for cls in AssetClass}
        assert {"EQUITY", "ETF", "CRYPTO", "OPTION", "FUTURE"} <= names


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


def _valid_bar(**overrides) -> Bar:
    base = dict(
        symbol="AAPL",
        timestamp="2026-05-01T14:30:00+00:00",
        open=100.0,
        high=105.0,
        low=99.5,
        close=104.0,
        volume=1_000_000,
        interval=BarInterval.DAILY,
        adjustment_mode=AdjustmentMode.SPLIT_DIVIDEND,
    )
    base.update(overrides)
    return Bar(**base)


class TestBar:
    def test_basic_construction(self):
        bar = _valid_bar()
        assert bar.symbol == "AAPL"
        assert bar.close == 104.0

    def test_to_dict_shape(self):
        bar = _valid_bar()
        d = bar.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["interval"] == "1Day"
        assert d["adjustment_mode"] == "split_dividend"
        assert d["vwap"] is None
        assert d["trade_count"] is None
        assert d["adjustment_version"] == ""

    def test_json_round_trip_is_deterministic(self):
        bar = _valid_bar()
        a = json.dumps(bar.to_dict(), sort_keys=True)
        b = json.dumps(bar.to_dict(), sort_keys=True)
        assert a == b

    def test_frozen_dataclass_rejects_mutation(self):
        bar = _valid_bar()
        with pytest.raises(Exception):
            bar.close = 999  # type: ignore[misc]

    @pytest.mark.parametrize(
        "override,match",
        [
            ({"symbol": ""}, "symbol"),
            ({"timestamp": ""}, "timestamp"),
            ({"low": 106.0}, "low"),  # low > high
            ({"open": 200.0}, "open"),  # open outside [low, high]
            ({"close": 50.0}, "close"),  # close < low
            ({"volume": -1.0}, "volume"),
        ],
    )
    def test_invalid_construction_raises(self, override, match):
        with pytest.raises(MarketDataValidationError, match=match):
            _valid_bar(**override)

    def test_negative_trade_count_rejected(self):
        with pytest.raises(MarketDataValidationError, match="trade_count"):
            _valid_bar(trade_count=-1)

    def test_optional_fields_populate(self):
        bar = _valid_bar(
            vwap=103.5, trade_count=1234, adjustment_version="alpaca:2026-07-15"
        )
        d = bar.to_dict()
        assert d["vwap"] == 103.5
        assert d["trade_count"] == 1234
        assert d["adjustment_version"] == "alpaca:2026-07-15"


# ---------------------------------------------------------------------------
# CorporateAction
# ---------------------------------------------------------------------------


class TestCorporateAction:
    def test_split_requires_ratio(self):
        ca = CorporateAction(
            symbol="AAPL",
            kind=CorporateActionKind.SPLIT,
            ex_date="2026-06-01",
            ratio=4.0,
        )
        d = ca.to_dict()
        assert d["kind"] == "split"
        assert d["ratio"] == 4.0

    def test_split_without_ratio_rejected(self):
        with pytest.raises(MarketDataValidationError, match="ratio"):
            CorporateAction(
                symbol="AAPL",
                kind=CorporateActionKind.SPLIT,
                ex_date="2026-06-01",
            )

    def test_split_zero_ratio_rejected(self):
        with pytest.raises(MarketDataValidationError, match="ratio"):
            CorporateAction(
                symbol="AAPL",
                kind=CorporateActionKind.SPLIT,
                ex_date="2026-06-01",
                ratio=0.0,
            )

    def test_cash_dividend_requires_amount_and_currency(self):
        ok = CorporateAction(
            symbol="AAPL",
            kind=CorporateActionKind.CASH_DIVIDEND,
            ex_date="2026-06-01",
            pay_date="2026-06-15",
            amount=0.24,
            currency="USD",
        )
        assert ok.to_dict()["amount"] == 0.24
        with pytest.raises(MarketDataValidationError, match="amount"):
            CorporateAction(
                symbol="AAPL",
                kind=CorporateActionKind.CASH_DIVIDEND,
                ex_date="2026-06-01",
                currency="USD",
            )
        with pytest.raises(MarketDataValidationError, match="currency"):
            CorporateAction(
                symbol="AAPL",
                kind=CorporateActionKind.CASH_DIVIDEND,
                ex_date="2026-06-01",
                amount=0.24,
            )

    def test_ticker_change_requires_new_symbol(self):
        ok = CorporateAction(
            symbol="FB",
            kind=CorporateActionKind.TICKER_CHANGE,
            ex_date="2022-06-09",
            new_symbol="META",
        )
        assert ok.to_dict()["new_symbol"] == "META"
        with pytest.raises(MarketDataValidationError, match="new_symbol"):
            CorporateAction(
                symbol="FB",
                kind=CorporateActionKind.TICKER_CHANGE,
                ex_date="2022-06-09",
            )

    def test_delisting_requires_reason(self):
        with pytest.raises(MarketDataValidationError, match="reason"):
            CorporateAction(
                symbol="XYZ",
                kind=CorporateActionKind.DELISTING,
                ex_date="2026-06-01",
            )

    def test_merger_requires_surviving_entity(self):
        with pytest.raises(MarketDataValidationError, match="surviving_entity"):
            CorporateAction(
                symbol="XYZ",
                kind=CorporateActionKind.MERGER,
                ex_date="2026-06-01",
            )

    def test_reverse_split_requires_ratio(self):
        ok = CorporateAction(
            symbol="XYZ",
            kind=CorporateActionKind.REVERSE_SPLIT,
            ex_date="2026-06-01",
            ratio=0.1,
        )
        assert ok.to_dict()["kind"] == "reverse_split"
        with pytest.raises(MarketDataValidationError, match="ratio"):
            CorporateAction(
                symbol="XYZ",
                kind=CorporateActionKind.REVERSE_SPLIT,
                ex_date="2026-06-01",
            )


# ---------------------------------------------------------------------------
# SymbolMetadata
# ---------------------------------------------------------------------------


class TestSymbolMetadata:
    def test_construction_and_to_dict(self):
        meta = SymbolMetadata(
            symbol="AAPL",
            name="Apple Inc.",
            exchange="XNAS",
            asset_class=AssetClass.EQUITY,
            listing_date="1980-12-12",
            sector="Technology",
            industry="Consumer Electronics",
            aliases=["APPLE"],
            currency="USD",
        )
        d = meta.to_dict()
        assert d["asset_class"] == "equity"
        assert d["aliases"] == ["APPLE"]
        assert d["listing_date"] == "1980-12-12"

    def test_aliases_normalized_to_tuple(self):
        meta = SymbolMetadata(
            symbol="AAPL",
            name="Apple Inc.",
            exchange="XNAS",
            asset_class=AssetClass.EQUITY,
            aliases=["A", "B"],
        )
        assert isinstance(meta.aliases, tuple)

    def test_missing_symbol_rejected(self):
        with pytest.raises(MarketDataValidationError, match="symbol"):
            SymbolMetadata(
                symbol="",
                name="x",
                exchange="XNAS",
                asset_class=AssetClass.EQUITY,
            )

    def test_missing_exchange_rejected(self):
        with pytest.raises(MarketDataValidationError, match="exchange"):
            SymbolMetadata(
                symbol="AAPL",
                name="x",
                exchange="",
                asset_class=AssetClass.EQUITY,
            )


# ---------------------------------------------------------------------------
# CalendarDay
# ---------------------------------------------------------------------------


class TestCalendarDay:
    def test_full_trading_day_requires_open_and_close(self):
        ok = CalendarDay(
            exchange="XNYS",
            date="2026-06-01",
            kind=CalendarSessionKind.FULL_TRADING,
            open_time="09:30",
            close_time="16:00",
            timezone="America/New_York",
        )
        assert ok.to_dict()["kind"] == "full_trading"
        with pytest.raises(MarketDataValidationError, match="open_time"):
            CalendarDay(
                exchange="XNYS",
                date="2026-06-01",
                kind=CalendarSessionKind.FULL_TRADING,
                close_time="16:00",
            )

    def test_holiday_forbids_open_and_close(self):
        ok = CalendarDay(
            exchange="XNYS",
            date="2026-07-04",
            kind=CalendarSessionKind.HOLIDAY,
        )
        assert ok.to_dict()["open_time"] is None
        with pytest.raises(MarketDataValidationError, match="holiday"):
            CalendarDay(
                exchange="XNYS",
                date="2026-07-04",
                kind=CalendarSessionKind.HOLIDAY,
                open_time="09:30",
            )

    def test_half_trading_session_populated(self):
        ok = CalendarDay(
            exchange="XNYS",
            date="2026-11-27",
            kind=CalendarSessionKind.HALF_TRADING,
            open_time="09:30",
            close_time="13:00",
            timezone="America/New_York",
        )
        assert ok.to_dict()["close_time"] == "13:00"


# ---------------------------------------------------------------------------
# ProviderCapabilities
# ---------------------------------------------------------------------------


def _default_caps(**overrides) -> ProviderCapabilities:
    base = dict(
        provider_name="fake",
        supported_intervals=[BarInterval.DAILY, BarInterval.HOURLY],
        supported_adjustments=[AdjustmentMode.RAW, AdjustmentMode.SPLIT],
        supported_asset_classes=[AssetClass.EQUITY, AssetClass.ETF],
        supports_corporate_actions=True,
        supports_symbol_metadata=True,
        supports_calendar=True,
    )
    base.update(overrides)
    return ProviderCapabilities(**base)


class TestProviderCapabilities:
    def test_supports_interval_query(self):
        caps = _default_caps()
        assert caps.supports_interval(BarInterval.DAILY) is True
        assert caps.supports_interval(BarInterval.MINUTE_1) is False

    def test_supports_adjustment_query(self):
        caps = _default_caps()
        assert caps.supports_adjustment(AdjustmentMode.RAW) is True
        assert caps.supports_adjustment(AdjustmentMode.TOTAL_RETURN) is False

    def test_supports_asset_class_query(self):
        caps = _default_caps()
        assert caps.supports_asset_class(AssetClass.EQUITY) is True
        assert caps.supports_asset_class(AssetClass.OPTION) is False

    def test_lists_normalized_to_tuples(self):
        caps = _default_caps()
        assert isinstance(caps.supported_intervals, tuple)
        assert isinstance(caps.supported_adjustments, tuple)
        assert isinstance(caps.supported_asset_classes, tuple)
        assert isinstance(caps.supported_exchanges, tuple)

    def test_to_dict_flattens_enums_to_strings(self):
        caps = _default_caps()
        d = caps.to_dict()
        assert d["supported_intervals"] == ["1Day", "1Hour"]
        assert d["supported_adjustments"] == ["raw", "split"]
        assert d["supported_asset_classes"] == ["equity", "etf"]

    def test_negative_lag_rejected(self):
        with pytest.raises(MarketDataValidationError, match="lag"):
            _default_caps(latest_data_lag_minutes=-1)

    def test_zero_rate_limit_rejected(self):
        with pytest.raises(MarketDataValidationError, match="rate_limit"):
            _default_caps(rate_limit_per_minute=0)

    def test_negative_cost_rejected(self):
        with pytest.raises(MarketDataValidationError, match="cost"):
            _default_caps(cost_per_request_usd=-0.001)

    def test_empty_provider_name_rejected(self):
        with pytest.raises(MarketDataValidationError, match="provider_name"):
            _default_caps(provider_name="")


# ---------------------------------------------------------------------------
# ProviderResponse
# ---------------------------------------------------------------------------


class TestProviderResponse:
    def test_bars_batch_serializes(self):
        bar = _valid_bar()
        r = ProviderResponse(provider="fake", batch=[bar])
        d = r.to_dict()
        assert d["batch"][0]["symbol"] == "AAPL"
        assert d["provider"] == "fake"

    def test_carries_per_symbol_status(self):
        r = ProviderResponse(
            provider="fake",
            batch=[],
            per_symbol_status={"AAPL": "ok", "XYZ": "empty"},
        )
        d = r.to_dict()
        assert d["per_symbol_status"]["XYZ"] == "empty"

    def test_warnings_normalized_to_tuple(self):
        r = ProviderResponse(
            provider="fake", batch=[], warnings=["one", "two"]
        )
        assert isinstance(r.warnings, tuple)
        assert r.to_dict()["warnings"] == ["one", "two"]

    def test_missing_provider_rejected(self):
        with pytest.raises(MarketDataValidationError, match="provider"):
            ProviderResponse(provider="", batch=[])

    def test_credential_leak_guardrail(self):
        with pytest.raises(MarketDataValidationError, match="credentials"):
            ProviderResponse(
                provider="fake",
                batch=[],
                request_url_redacted="https://x/api?api_key=leaked",
            )

    def test_redacted_url_with_stars_ok(self):
        r = ProviderResponse(
            provider="fake",
            batch=[],
            request_url_redacted="https://x/api?apikey***",
        )
        assert r.to_dict()["request_url_redacted"] == "https://x/api?apikey***"

    def test_batch_of_corporate_actions_serializes(self):
        ca = CorporateAction(
            symbol="AAPL",
            kind=CorporateActionKind.SPLIT,
            ex_date="2020-08-31",
            ratio=4.0,
        )
        r = ProviderResponse(provider="fake", batch=[ca])
        assert r.to_dict()["batch"][0]["ratio"] == 4.0


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class FakeProvider:
    """Protocol-satisfying stub used only for interface tests.

    Never lands under strategy/providers/ — that directory holds real
    plugins in later cards.  This lives inside the test module so
    conformance is proven without polluting the production surface.
    """

    name = "fake"

    def provider_capabilities(self) -> ProviderCapabilities:
        return _default_caps()

    def fetch_daily_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_intraday_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        interval: BarInterval,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: str, end: str
    ) -> ProviderResponse[Sequence[CorporateAction]]:
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_symbol_metadata(
        self, symbols: Sequence[str]
    ) -> ProviderResponse[Sequence[SymbolMetadata]]:
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_calendar(
        self, start: str, end: str, exchange: str = "XNYS"
    ) -> ProviderResponse[Sequence[CalendarDay]]:
        return ProviderResponse(provider=self.name, batch=[])


class TestProtocolConformance:
    def test_fake_provider_satisfies_runtime_checkable_protocol(self):
        assert isinstance(FakeProvider(), MarketDataProvider)

    def test_missing_method_fails_isinstance_check(self):
        class Broken:
            name = "broken"
            def provider_capabilities(self) -> ProviderCapabilities:
                return _default_caps()
            # deliberately missing every fetch_* method

        assert not isinstance(Broken(), MarketDataProvider)

    def test_all_five_fetch_methods_return_provider_response(self):
        p = FakeProvider()
        assert isinstance(
            p.fetch_daily_bars(["AAPL"], "2026-01-01", "2026-01-31"),
            ProviderResponse,
        )
        assert isinstance(
            p.fetch_intraday_bars(
                ["AAPL"], "2026-01-01", "2026-01-31", BarInterval.MINUTE_1
            ),
            ProviderResponse,
        )
        assert isinstance(
            p.fetch_corporate_actions(["AAPL"], "2026-01-01", "2026-01-31"),
            ProviderResponse,
        )
        assert isinstance(
            p.fetch_symbol_metadata(["AAPL"]),
            ProviderResponse,
        )
        assert isinstance(
            p.fetch_calendar("2026-01-01", "2026-01-31"),
            ProviderResponse,
        )


# ---------------------------------------------------------------------------
# Read-only source safety
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.market_data_provider as module
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_references_in_source(self):
        source = _module_source()
        for token in [
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "close_all_positions",
            "create_order",
            "replace_order",
            "TradingClient",
        ]:
            assert token not in source, (
                f"market_data_provider must not reference {token!r}"
            )

    def test_no_live_runner_imports(self):
        source = _module_source()
        for token in [
            "from trader import",
            "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "import trader_cli",
            "from telegram_approvals import",
            "import telegram_approvals",
            "from strategy.runner import",
            "import strategy.runner",
        ]:
            assert token not in source, (
                f"market_data_provider must not import {token!r}"
            )

    def test_no_alpaca_or_apca_env_reads(self):
        source = _module_source()
        # No provider-specific credential reads at this layer; those
        # belong on plugins.
        for pattern in [
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\.get\(\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.getenv\(\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
            r'os\.environ\[\s*[\'"]CRYPTO_ALPACA_',
        ]:
            assert not re.search(pattern, source), (
                f"market_data_provider must not read from provider "
                f"credential env vars: pattern {pattern!r}"
            )

    def test_no_yfinance_or_pandas_dependency(self):
        source = _module_source()
        assert "import yfinance" not in source
        assert "import pandas" not in source
        assert "yf.download" not in source

    def test_no_approval_record_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source

    def test_no_promotion_state_advance(self):
        source = _module_source()
        # We may reference PromotionEntry in docstrings; forbid
        # constructor calls that would advance state.
        assert "PromotionEntry(" not in source

    def test_no_file_writes(self):
        source = _module_source()
        # The interface layer must not write anywhere.  Plugins may
        # write to the warehouse, but their tests will cover that.
        assert "write_text" not in source
        assert "write_bytes" not in source
        assert not re.search(
            r"(?<!url)\bopen\([^)]*[\'\"][wxa][b+]?[\'\"]", source
        )
        assert "with open" not in source

    def test_terminology_avoids_training(self):
        source = _module_source()
        # The module docstring names the forbidden term once to say
        # "never use it" — strip that deliberate mention before
        # scanning, mirroring the pattern in
        # tests/test_research_account.py.
        stripped = source.replace('Never "training".', "")
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped, (
                f"market_data_provider must use 'validation / replay / "
                f"research / acquisition' rather than {token!r}"
            )

    def test_import_does_not_pull_in_live_runner(self):
        import sys
        for name in ("strategy.market_data_provider", "strategy"):
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.market_data_provider  # noqa: F401
        added = set(sys.modules) - before
        forbidden = {
            "trader",
            "trader_cli",
            "crypto_trader",
            "telegram_approvals",
        }
        assert not (added & forbidden), (
            f"forbidden imports pulled in: {added & forbidden}"
        )


# ---------------------------------------------------------------------------
# Global state invariants
# ---------------------------------------------------------------------------


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled_after_fake_provider_use(self):
        flags = reset_feature_flags()
        provider = FakeProvider()
        provider.fetch_daily_bars(["AAPL"], "2026-01-01", "2026-01-31")
        provider.fetch_calendar("2026-01-01", "2026-01-31")
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_flags_stay_disabled_after_response_construction(self):
        flags = reset_feature_flags()
        ProviderResponse(provider="fake", batch=[_valid_bar()])
        _default_caps()
        assert flags.all_disabled is True

    def test_module_import_does_not_toggle_flags(self):
        flags = reset_feature_flags()
        import strategy.market_data_provider  # noqa: F401
        assert flags.all_disabled is True
