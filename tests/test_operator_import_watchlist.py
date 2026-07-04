"""Tests for strategy/warehouse/operator/import_watchlist.py."""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.local_warehouse import (
    STATUS_VALIDATED,
    WarehouseLayout,
    read_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    ProviderCapabilities,
    ProviderResponse,
)
from strategy.warehouse.operator import import_watchlist
from strategy.warehouse.operator.import_watchlist import (
    DEFAULT_SYMBOLS,
    ImportCommandError,
    REQUIRED_ENV_VARS,
    build_parser,
    main,
    run,
)


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------


class StubProvider:
    """Mimics AlpacaProvider without any network I/O."""

    name = "stub-alpaca"

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name=self.name,
            supported_intervals=(BarInterval.DAILY,),
            supported_adjustments=(AdjustmentMode.RAW,),
            supported_asset_classes=(AssetClass.EQUITY,),
            supports_corporate_actions=False,
            supports_symbol_metadata=False,
            supports_calendar=False,
        )

    def fetch_daily_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        self.calls.append({"symbols": list(symbols), "start": start, "end": end})
        bars: List[Bar] = []
        status: Dict[str, str] = {}
        for sym in symbols:
            status[sym] = "ok"
            for day in ("05-01", "05-02", "05-03"):
                bars.append(
                    Bar(
                        symbol=sym,
                        timestamp=f"2020-{day}T14:30:00+00:00",
                        open=100.0,
                        high=101.0,
                        low=99.5,
                        close=100.5,
                        volume=1_000_000,
                        interval=BarInterval.DAILY,
                        adjustment_mode=AdjustmentMode.RAW,
                    )
                )
        return ProviderResponse(
            provider=self.name, batch=bars, per_symbol_status=status
        )

    def fetch_intraday_bars(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_corporate_actions(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_symbol_metadata(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_calendar(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])


VALID_ENV: Dict[str, str] = {
    "RESEARCH_ALPACA_API_KEY": "test-api-key-do-not-log",
    "RESEARCH_ALPACA_SECRET_KEY": "test-secret-key-do-not-log",
    "RESEARCH_ALPACA_ENDPOINT": "https://paper-api.alpaca.markets",
    "RESEARCH_ALPACA_DATA_ENDPOINT": "https://data.alpaca.markets",
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    def test_defaults_match_the_documented_universe(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.symbols == list(DEFAULT_SYMBOLS)
        assert args.start == "2020-01-01"
        assert args.end == ""  # today at run time
        assert args.skip_validate is False
        assert args.force is False

    def test_custom_symbols_and_dates(self):
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "MSFT", "--start", "2021-01-01",
             "--end", "2021-12-31", "--dataset-id", "custom-2021"]
        )
        assert args.symbols == ["AAPL", "MSFT"]
        assert args.dataset_id == "custom-2021"


# ---------------------------------------------------------------------------
# Happy path (fully mocked)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_import_populates_warehouse(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        provider = StubProvider()
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "MSFT",
             "--start", "2020-05-01", "--end", "2020-05-03"]
        )
        printed: List[str] = []
        report = run(
            args, env=dict(VALID_ENV), provider=provider,
            layout=layout, printer=printed.append,
        )
        assert report.total_bars == 6  # 2 symbols × 3 days
        assert Path(report.manifest_path).is_file()
        # Manifest promoted to validated by default
        manifest = read_manifest(layout, report.dataset_id)
        assert manifest["validation_status"] == STATUS_VALIDATED

    def test_import_provider_was_called(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        provider = StubProvider()
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "--start", "2020-05-01", "--end", "2020-05-03"]
        )
        run(args, env=dict(VALID_ENV), provider=provider,
            layout=layout, printer=lambda *a, **k: None)
        assert len(provider.calls) == 1

    def test_skip_validate_leaves_status_unvalidated(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        provider = StubProvider()
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "--start", "2020-05-01",
             "--end", "2020-05-03", "--skip-validate"]
        )
        report = run(
            args, env=dict(VALID_ENV), provider=provider,
            layout=layout, printer=lambda *a, **k: None,
        )
        manifest = read_manifest(layout, report.dataset_id)
        assert manifest["validation_status"] == "unvalidated"


# ---------------------------------------------------------------------------
# Env-var handling
# ---------------------------------------------------------------------------


class TestEnvValidation:
    @pytest.mark.parametrize("missing_var", list(REQUIRED_ENV_VARS))
    def test_missing_env_var_fails_clearly(self, tmp_path, missing_var):
        layout = WarehouseLayout(root=tmp_path / "wh")
        env = dict(VALID_ENV)
        env.pop(missing_var)
        parser = build_parser()
        args = parser.parse_args(["--symbols", "AAPL"])
        with pytest.raises(ImportCommandError, match=missing_var):
            run(args, env=env, provider=StubProvider(), layout=layout,
                printer=lambda *a, **k: None)

    def test_all_env_vars_missing_fails(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        parser = build_parser()
        args = parser.parse_args(["--symbols", "AAPL"])
        with pytest.raises(ImportCommandError, match="missing"):
            run(args, env={}, provider=StubProvider(), layout=layout,
                printer=lambda *a, **k: None)

    def test_empty_symbol_list_rejected(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        parser = build_parser()
        # argparse won't let us pass an empty list via CLI, but the
        # runtime guard still catches it if the caller mutates args
        args = parser.parse_args(["--symbols", "AAPL"])
        args.symbols = []
        with pytest.raises(ImportCommandError, match="symbols"):
            run(args, env=dict(VALID_ENV), provider=StubProvider(),
                layout=layout, printer=lambda *a, **k: None)

    def test_start_after_end_rejected(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "--start", "2020-12-31", "--end", "2020-01-01"]
        )
        with pytest.raises(ImportCommandError, match="start"):
            run(args, env=dict(VALID_ENV), provider=StubProvider(),
                layout=layout, printer=lambda *a, **k: None)


class TestForbiddenEnvFiles:
    def test_env_production_file_refused(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        env = dict(VALID_ENV)
        env["HERMES_ENV_FILE"] = "/tmp/.env.production"
        parser = build_parser()
        args = parser.parse_args(["--symbols", "AAPL"])
        with pytest.raises(ImportCommandError, match="forbidden"):
            run(args, env=env, provider=StubProvider(), layout=layout,
                printer=lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------


class TestCredentialsNeverPrinted:
    def test_stdout_never_contains_secret_values(self, tmp_path):
        layout = WarehouseLayout(root=tmp_path / "wh")
        provider = StubProvider()
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "--start", "2020-05-01", "--end", "2020-05-03"]
        )
        printed: List[str] = []
        run(args, env=dict(VALID_ENV), provider=provider,
            layout=layout, printer=printed.append)
        haystack = "\n".join(printed)
        assert VALID_ENV["RESEARCH_ALPACA_API_KEY"] not in haystack
        assert VALID_ENV["RESEARCH_ALPACA_SECRET_KEY"] not in haystack
        # The redacted form must appear
        assert "<set, len=" in haystack

    def test_stderr_never_contains_secret_values(self, tmp_path, capsys):
        # Run the process with a missing env var; the FAIL message
        # should never quote a credential.
        parser = build_parser()
        args = parser.parse_args(["--symbols", "AAPL"])
        env = dict(VALID_ENV)
        env.pop("RESEARCH_ALPACA_SECRET_KEY")
        with pytest.raises(ImportCommandError) as excinfo:
            run(args, env=env, provider=StubProvider(),
                layout=WarehouseLayout(root=tmp_path / "wh"),
                printer=lambda *a, **k: None)
        assert VALID_ENV["RESEARCH_ALPACA_API_KEY"] not in str(excinfo.value)


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------


class TestMainExitCodes:
    def test_missing_env_yields_nonzero(self, monkeypatch):
        # Clear the required env vars so main hits ImportCommandError
        for name in REQUIRED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        rc = main(["--symbols", "AAPL"])
        assert rc == 2


# ---------------------------------------------------------------------------
# Read-only + source safety
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.operator.import_watchlist as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        source = _module_source()
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import", "import crypto_trader",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_order_path_references(self):
        source = _module_source()
        for token in (
            "submit_order", "place_order", "cancel_order",
            "TradingClient",
        ):
            assert token not in source

    def test_no_env_production_source(self):
        source = _module_source()
        # `.env.production` may appear only as a string being refused.
        # There must be no ``source .env.production`` / read of that file.
        assert "source .env.production" not in source
        assert 'open(".env.production"' not in source

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled_after_run(self, tmp_path):
        flags = reset_feature_flags()
        layout = WarehouseLayout(root=tmp_path / "wh")
        parser = build_parser()
        args = parser.parse_args(
            ["--symbols", "AAPL", "--start", "2020-05-01", "--end", "2020-05-03"]
        )
        run(args, env=dict(VALID_ENV), provider=StubProvider(),
            layout=layout, printer=lambda *a, **k: None)
        assert flags.all_disabled is True
