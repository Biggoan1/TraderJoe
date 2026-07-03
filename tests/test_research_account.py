"""Tests for strategy/research_account.py."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from strategy.config import reset_feature_flags
from strategy.research_account import (
    ACCOUNT_PATH,
    BARS_PATH,
    CALENDAR_PATH,
    DEFAULT_BAR_LIMIT,
    DEFAULT_TIMEFRAME,
    ENDPOINT_KIND_DATA,
    ENDPOINT_KIND_TRADING,
    FORBIDDEN_ENV_FALLBACKS,
    HEADER_API_KEY,
    HEADER_SECRET_KEY,
    REQUIRED_ENV_VARS,
    RESEARCH_ALPACA_API_KEY_ENV,
    RESEARCH_ALPACA_DATA_ENDPOINT_ENV,
    RESEARCH_ALPACA_ENDPOINT_ENV,
    RESEARCH_ALPACA_SECRET_KEY_ENV,
    ResearchAccountClient,
    ResearchAccountConfig,
    ResearchAccountConfigError,
    ResearchAccountRequest,
    ResearchAccountRequestError,
)


TEST_ENV: Mapping[str, str] = {
    RESEARCH_ALPACA_API_KEY_ENV: "test-api-key",
    RESEARCH_ALPACA_SECRET_KEY_ENV: "test-secret-key",
    RESEARCH_ALPACA_ENDPOINT_ENV: "https://research-paper.alpaca.example",
    RESEARCH_ALPACA_DATA_ENDPOINT_ENV: "https://research-data.alpaca.example",
}


def _http_get_returning(response: bytes):
    calls: List[Tuple[ResearchAccountRequest, float]] = []

    def _http_get(request: ResearchAccountRequest, timeout: float) -> bytes:
        calls.append((request, timeout))
        return response

    return _http_get, calls


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_required_env_namespace_is_research_alpaca(self):
        for name in REQUIRED_ENV_VARS:
            assert name.startswith("RESEARCH_ALPACA_")

    def test_forbidden_env_covers_alpaca_and_apca_namespaces(self):
        assert "ALPACA_API_KEY" in FORBIDDEN_ENV_FALLBACKS
        assert "ALPACA_SECRET_KEY" in FORBIDDEN_ENV_FALLBACKS
        assert "ALPACA_ENDPOINT" in FORBIDDEN_ENV_FALLBACKS
        assert "APCA_API_KEY_ID" in FORBIDDEN_ENV_FALLBACKS
        assert "APCA_API_SECRET_KEY" in FORBIDDEN_ENV_FALLBACKS

    def test_env_namespace_disjoint_from_alpaca(self):
        assert not (set(REQUIRED_ENV_VARS) & set(FORBIDDEN_ENV_FALLBACKS))

    def test_api_paths_are_read_endpoints(self):
        assert BARS_PATH == "/v2/stocks/bars"
        assert CALENDAR_PATH == "/v2/calendar"
        assert ACCOUNT_PATH == "/v2/account"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestResearchAccountConfig:
    def test_defaults_use_research_alpaca_namespace(self):
        c = ResearchAccountConfig()
        assert c.api_key_env == RESEARCH_ALPACA_API_KEY_ENV
        assert c.secret_key_env == RESEARCH_ALPACA_SECRET_KEY_ENV
        assert c.endpoint_env == RESEARCH_ALPACA_ENDPOINT_ENV
        assert c.data_endpoint_env == RESEARCH_ALPACA_DATA_ENDPOINT_ENV

    @pytest.mark.parametrize(
        "env_var",
        ["ALPACA_API_KEY", "APCA_API_KEY_ID", "MYSTERY_KEY", "ALPACA_ENDPOINT"],
    )
    def test_rejects_non_research_namespace(self, env_var):
        with pytest.raises(ResearchAccountConfigError, match="RESEARCH_ALPACA"):
            ResearchAccountConfig(api_key_env=env_var)

    def test_all_four_names_validated(self):
        with pytest.raises(ResearchAccountConfigError, match="RESEARCH_ALPACA"):
            ResearchAccountConfig(secret_key_env="ALPACA_SECRET_KEY")
        with pytest.raises(ResearchAccountConfigError, match="RESEARCH_ALPACA"):
            ResearchAccountConfig(endpoint_env="ALPACA_ENDPOINT")
        with pytest.raises(ResearchAccountConfigError, match="RESEARCH_ALPACA"):
            ResearchAccountConfig(data_endpoint_env="ALPACA_DATA_ENDPOINT")

    def test_to_dict_returns_only_names_not_values(self):
        c = ResearchAccountConfig()
        d = c.to_dict()
        assert d == {
            "api_key_env": RESEARCH_ALPACA_API_KEY_ENV,
            "secret_key_env": RESEARCH_ALPACA_SECRET_KEY_ENV,
            "endpoint_env": RESEARCH_ALPACA_ENDPOINT_ENV,
            "data_endpoint_env": RESEARCH_ALPACA_DATA_ENDPOINT_ENV,
        }
        # No credential values leaked
        assert "test-api-key" not in json.dumps(d)

    def test_resolve_reads_trading_endpoint(self):
        c = ResearchAccountConfig()
        assert c.resolve_credentials(TEST_ENV) == (
            "test-api-key",
            "test-secret-key",
            "https://research-paper.alpaca.example",
        )

    def test_resolve_data_reads_data_endpoint(self):
        c = ResearchAccountConfig()
        assert c.resolve_data_credentials(TEST_ENV) == (
            "test-api-key",
            "test-secret-key",
            "https://research-data.alpaca.example",
        )

    def test_missing_env_raises_clear_error(self):
        c = ResearchAccountConfig()
        env = {RESEARCH_ALPACA_API_KEY_ENV: "k"}
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_credentials(env)

    def test_missing_data_endpoint_raises_clear_error(self):
        c = ResearchAccountConfig()
        # Trading endpoint present, data endpoint missing
        env = {
            RESEARCH_ALPACA_API_KEY_ENV: "k",
            RESEARCH_ALPACA_SECRET_KEY_ENV: "s",
            RESEARCH_ALPACA_ENDPOINT_ENV: "https://trading.example",
        }
        # Trading resolver succeeds
        c.resolve_credentials(env)
        # Data resolver fails on the missing data endpoint
        with pytest.raises(
            ResearchAccountConfigError,
            match=re.escape(RESEARCH_ALPACA_DATA_ENDPOINT_ENV),
        ):
            c.resolve_data_credentials(env)

    def test_empty_string_env_treated_as_missing(self):
        c = ResearchAccountConfig()
        env = {
            RESEARCH_ALPACA_API_KEY_ENV: "",
            RESEARCH_ALPACA_SECRET_KEY_ENV: "s",
            RESEARCH_ALPACA_ENDPOINT_ENV: "https://x",
            RESEARCH_ALPACA_DATA_ENDPOINT_ENV: "https://y",
        }
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_credentials(env)

    def test_never_falls_back_to_alpaca_namespace(self):
        c = ResearchAccountConfig()
        env = {
            "ALPACA_API_KEY": "leak",
            "ALPACA_SECRET_KEY": "leak",
            "ALPACA_ENDPOINT": "leak",
            "APCA_API_KEY_ID": "leak",
            "APCA_API_SECRET_KEY": "leak",
        }
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_credentials(env)
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_data_credentials(env)

    def test_resolve_defaults_to_os_environ_when_not_supplied(self, monkeypatch):
        # Clean state: remove RESEARCH_ALPACA_* to make resolve fail
        for name in REQUIRED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        c = ResearchAccountConfig()
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_credentials()
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_data_credentials()


class TestFromEnv:
    def test_from_env_returns_configured_instance(self):
        c = ResearchAccountConfig.from_env(TEST_ENV)
        assert isinstance(c, ResearchAccountConfig)
        assert c.api_key_env == RESEARCH_ALPACA_API_KEY_ENV
        assert c.secret_key_env == RESEARCH_ALPACA_SECRET_KEY_ENV
        assert c.endpoint_env == RESEARCH_ALPACA_ENDPOINT_ENV
        assert c.data_endpoint_env == RESEARCH_ALPACA_DATA_ENDPOINT_ENV

    def test_from_env_fails_fast_when_credentials_missing(self):
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            ResearchAccountConfig.from_env({})

    def test_from_env_fails_fast_when_data_endpoint_missing(self):
        env = {
            RESEARCH_ALPACA_API_KEY_ENV: "k",
            RESEARCH_ALPACA_SECRET_KEY_ENV: "s",
            RESEARCH_ALPACA_ENDPOINT_ENV: "https://trading.example",
        }
        with pytest.raises(
            ResearchAccountConfigError,
            match=re.escape(RESEARCH_ALPACA_DATA_ENDPOINT_ENV),
        ):
            ResearchAccountConfig.from_env(env)

    def test_from_env_refuses_alpaca_fallback(self):
        env = {
            "ALPACA_API_KEY": "leak",
            "ALPACA_SECRET_KEY": "leak",
            "ALPACA_ENDPOINT": "leak",
        }
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            ResearchAccountConfig.from_env(env)

    def test_from_env_reads_os_environ_when_not_supplied(self, monkeypatch):
        for name in REQUIRED_ENV_VARS:
            monkeypatch.setenv(name, "present")
        c = ResearchAccountConfig.from_env()
        assert c.api_key_env == RESEARCH_ALPACA_API_KEY_ENV

    def test_from_env_does_not_retain_credential_values(self):
        c = ResearchAccountConfig.from_env(TEST_ENV)
        # to_dict must surface only env-var *names*, never values
        assert c.to_dict() == {
            "api_key_env": RESEARCH_ALPACA_API_KEY_ENV,
            "secret_key_env": RESEARCH_ALPACA_SECRET_KEY_ENV,
            "endpoint_env": RESEARCH_ALPACA_ENDPOINT_ENV,
            "data_endpoint_env": RESEARCH_ALPACA_DATA_ENDPOINT_ENV,
        }
        payload = json.dumps(c.to_dict())
        assert "test-api-key" not in payload
        assert "test-secret-key" not in payload


# ---------------------------------------------------------------------------
# Request builder — bars
# ---------------------------------------------------------------------------


class TestBarsRequest:
    def test_url_composition(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(
            ["AAPL", "MSFT"],
            timeframe="1Day",
            start="2026-05-01",
            end="2026-07-01",
            limit=500,
        )
        assert request.method == "GET"
        assert request.url.startswith(
            "https://research-data.alpaca.example/v2/stocks/bars?"
        )
        assert "symbols=AAPL%2CMSFT" in request.url
        assert "timeframe=1Day" in request.url
        assert "start=2026-05-01" in request.url
        assert "end=2026-07-01" in request.url
        assert "limit=500" in request.url

    def test_query_params_deterministically_ordered(self):
        client = ResearchAccountClient(env=TEST_ENV)
        a = client.build_bars_request(["MSFT", "AAPL"], limit=100)
        b = client.build_bars_request(["AAPL", "MSFT"], limit=100)
        assert a.url == b.url
        # Query string is sorted alphabetically for stability
        assert "adjustment=raw" in a.url
        assert a.url.index("adjustment") < a.url.index("feed")

    def test_headers_carry_credentials(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(["AAPL"])
        assert request.headers[HEADER_API_KEY] == "test-api-key"
        assert request.headers[HEADER_SECRET_KEY] == "test-secret-key"
        assert request.headers["Accept"] == "application/json"

    def test_redacted_dict_masks_credentials(self):
        client = ResearchAccountClient(env=TEST_ENV)
        d = client.build_bars_request(["AAPL"]).redacted_dict()
        assert d["headers"][HEADER_API_KEY] == "***REDACTED***"
        assert d["headers"][HEADER_SECRET_KEY] == "***REDACTED***"
        # Non-credential headers unchanged
        assert d["headers"]["Accept"] == "application/json"

    def test_repr_does_not_leak_credentials(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(["AAPL"])
        text = repr(request)
        assert "test-api-key" not in text
        assert "test-secret-key" not in text
        assert "***REDACTED***" in text

    def test_str_does_not_leak_credentials(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(["AAPL"])
        text = str(request)
        assert "test-api-key" not in text
        assert "test-secret-key" not in text
        assert "***REDACTED***" in text

    def test_exception_traceback_does_not_leak_credentials(self):
        """Exception messages must never format the raw request so that
        credentials would surface in a traceback.  We exercise this by
        forcing the transport to raise and confirming the wrapped
        error only carries the redacted URL/method.
        """
        def http_get(request, timeout):
            raise RuntimeError(f"transport error: {request!r}")

        client = ResearchAccountClient(env=TEST_ENV, http_get=http_get)
        try:
            client.fetch_bars(["AAPL"])
        except ResearchAccountRequestError as exc:
            message = str(exc)
            assert "test-api-key" not in message
            assert "test-secret-key" not in message
        else:
            pytest.fail("expected ResearchAccountRequestError")

    def test_empty_symbols_rejected(self):
        client = ResearchAccountClient(env=TEST_ENV)
        with pytest.raises(ResearchAccountConfigError, match="symbols"):
            client.build_bars_request([])

    def test_non_positive_limit_rejected(self):
        client = ResearchAccountClient(env=TEST_ENV)
        with pytest.raises(ResearchAccountConfigError, match="limit"):
            client.build_bars_request(["AAPL"], limit=0)
        with pytest.raises(ResearchAccountConfigError, match="limit"):
            client.build_bars_request(["AAPL"], limit=-1)

    def test_optional_start_and_end_omitted_when_none(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(["AAPL"])
        assert "start=" not in request.url
        assert "end=" not in request.url


# ---------------------------------------------------------------------------
# Request builder — calendar and account
# ---------------------------------------------------------------------------


class TestCalendarRequest:
    def test_url_with_dates(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_calendar_request(
            start="2026-05-01", end="2026-07-01"
        )
        assert request.url.endswith(
            "/v2/calendar?end=2026-07-01&start=2026-05-01"
        )

    def test_url_without_params(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_calendar_request()
        assert request.url.endswith("/v2/calendar")

    def test_headers_carry_credentials(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_calendar_request()
        assert request.headers[HEADER_API_KEY] == "test-api-key"


class TestAccountRequest:
    def test_url_composition(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_account_request()
        assert request.method == "GET"
        assert request.url == (
            "https://research-paper.alpaca.example/v2/account"
        )

    def test_headers_carry_credentials(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_account_request()
        assert request.headers[HEADER_API_KEY] == "test-api-key"


# ---------------------------------------------------------------------------
# Endpoint + timeout validation
# ---------------------------------------------------------------------------


class TestEndpointValidation:
    @pytest.mark.parametrize(
        "endpoint",
        ["ftp://alpaca", "example.com", "://bad", ""],
    )
    def test_trading_endpoint_must_be_http_or_https(self, endpoint):
        env = dict(TEST_ENV) | {RESEARCH_ALPACA_ENDPOINT_ENV: endpoint}
        client = ResearchAccountClient(env=env)
        with pytest.raises(ResearchAccountConfigError):
            client.build_calendar_request()

    @pytest.mark.parametrize(
        "endpoint",
        ["ftp://alpaca", "example.com", "://bad", ""],
    )
    def test_data_endpoint_must_be_http_or_https(self, endpoint):
        env = dict(TEST_ENV) | {RESEARCH_ALPACA_DATA_ENDPOINT_ENV: endpoint}
        client = ResearchAccountClient(env=env)
        with pytest.raises(ResearchAccountConfigError):
            client.build_bars_request(["AAPL"])

    def test_data_endpoint_trailing_slash_stripped(self):
        env = dict(TEST_ENV) | {
            RESEARCH_ALPACA_DATA_ENDPOINT_ENV: "https://x.example/"
        }
        client = ResearchAccountClient(env=env)
        request = client.build_bars_request(["AAPL"])
        assert "//v2/stocks/bars" not in request.url

    def test_trading_endpoint_trailing_slash_stripped(self):
        env = dict(TEST_ENV) | {
            RESEARCH_ALPACA_ENDPOINT_ENV: "https://x.example/"
        }
        client = ResearchAccountClient(env=env)
        request = client.build_calendar_request()
        assert "//v2/calendar" not in request.url

    @pytest.mark.parametrize("timeout", [0, -1, -0.001])
    def test_timeout_must_be_positive(self, timeout):
        with pytest.raises(ResearchAccountConfigError, match="timeout"):
            ResearchAccountClient(env=TEST_ENV, timeout=timeout)


class TestEndpointRouting:
    def test_bars_use_data_endpoint(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(["AAPL"])
        assert request.url.startswith(
            "https://research-data.alpaca.example/v2/stocks/bars"
        )
        assert "research-paper.alpaca.example" not in request.url

    def test_calendar_uses_trading_endpoint(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_calendar_request()
        assert request.url.startswith(
            "https://research-paper.alpaca.example/v2/calendar"
        )
        assert "research-data.alpaca.example" not in request.url

    def test_account_uses_trading_endpoint(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_account_request()
        assert request.url == (
            "https://research-paper.alpaca.example/v2/account"
        )
        assert "research-data.alpaca.example" not in request.url

    def test_trading_and_data_endpoints_are_independent(self):
        # Break the trading endpoint but leave the data endpoint valid:
        # bars must still resolve, calendar/account must fail.
        env = dict(TEST_ENV) | {RESEARCH_ALPACA_ENDPOINT_ENV: ""}
        client = ResearchAccountClient(env=env)
        # Bars still succeed because they only need the data endpoint.
        request = client.build_bars_request(["AAPL"])
        assert request.url.startswith(
            "https://research-data.alpaca.example/v2/stocks/bars"
        )
        # Calendar fails because the trading endpoint is missing.
        with pytest.raises(ResearchAccountConfigError):
            client.build_calendar_request()

    def test_data_endpoint_missing_still_permits_trading_reads(self):
        # Mirror: break the data endpoint, calendar/account still work.
        env = dict(TEST_ENV) | {RESEARCH_ALPACA_DATA_ENDPOINT_ENV: ""}
        client = ResearchAccountClient(env=env)
        request = client.build_calendar_request()
        assert request.url.startswith(
            "https://research-paper.alpaca.example/v2/calendar"
        )
        with pytest.raises(ResearchAccountConfigError):
            client.build_bars_request(["AAPL"])

    def test_unknown_endpoint_kind_rejected(self):
        client = ResearchAccountClient(env=TEST_ENV)
        with pytest.raises(
            ResearchAccountConfigError, match="endpoint_kind"
        ):
            client._build_request(
                "GET", "/v2/whatever", {}, endpoint_kind="something-else"
            )

    def test_endpoint_kind_constants_are_stable(self):
        assert ENDPOINT_KIND_TRADING == "trading"
        assert ENDPOINT_KIND_DATA == "data"


# ---------------------------------------------------------------------------
# Fetch behavior (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchBars:
    def test_returns_decoded_json_object(self):
        http_get, calls = _http_get_returning(
            json.dumps({"bars": {"AAPL": []}}).encode("utf-8")
        )
        client = ResearchAccountClient(env=TEST_ENV, http_get=http_get)
        result = client.fetch_bars(["AAPL"])
        assert result == {"bars": {"AAPL": []}}
        assert len(calls) == 1
        # Sent URL matches the deterministic request builder
        assert calls[0][0].url == client.build_bars_request(["AAPL"]).url

    def test_http_exception_wrapped(self):
        def http_get(request, timeout):
            raise RuntimeError("boom")

        client = ResearchAccountClient(env=TEST_ENV, http_get=http_get)
        with pytest.raises(ResearchAccountRequestError, match="failed"):
            client.fetch_bars(["AAPL"])

    def test_non_json_body_raises(self):
        client = ResearchAccountClient(
            env=TEST_ENV, http_get=lambda r, t: b"not json"
        )
        with pytest.raises(ResearchAccountRequestError, match="JSON"):
            client.fetch_bars(["AAPL"])

    def test_non_dict_bars_body_raises(self):
        client = ResearchAccountClient(
            env=TEST_ENV, http_get=lambda r, t: b"[1, 2, 3]"
        )
        with pytest.raises(ResearchAccountRequestError, match="object"):
            client.fetch_bars(["AAPL"])

    def test_timeout_passed_to_http_get(self):
        http_get, calls = _http_get_returning(
            json.dumps({"bars": {}}).encode("utf-8")
        )
        client = ResearchAccountClient(
            env=TEST_ENV, http_get=http_get, timeout=5.0
        )
        client.fetch_bars(["AAPL"])
        assert calls[0][1] == 5.0


class TestListCalendar:
    def test_returns_list(self):
        client = ResearchAccountClient(
            env=TEST_ENV,
            http_get=lambda r, t: json.dumps(
                [{"date": "2026-05-01"}]
            ).encode("utf-8"),
        )
        assert client.list_calendar() == [{"date": "2026-05-01"}]

    def test_non_list_rejected(self):
        client = ResearchAccountClient(
            env=TEST_ENV,
            http_get=lambda r, t: json.dumps({"error": "oops"}).encode("utf-8"),
        )
        with pytest.raises(ResearchAccountRequestError, match="list"):
            client.list_calendar()


class TestAccountInfo:
    def test_returns_dict(self):
        client = ResearchAccountClient(
            env=TEST_ENV,
            http_get=lambda r, t: json.dumps(
                {"status": "ACTIVE", "cash": "100000"}
            ).encode("utf-8"),
        )
        info = client.paper_account_info()
        assert info["status"] == "ACTIVE"

    def test_non_dict_rejected(self):
        client = ResearchAccountClient(
            env=TEST_ENV,
            http_get=lambda r, t: json.dumps([1, 2, 3]).encode("utf-8"),
        )
        with pytest.raises(ResearchAccountRequestError, match="object"):
            client.paper_account_info()


# ---------------------------------------------------------------------------
# Read-only API surface
# ---------------------------------------------------------------------------


class TestReadOnlyAPISurface:
    def test_no_order_placement_attrs_present(self):
        client = ResearchAccountClient(env=TEST_ENV)
        public_names = [n for n in dir(client) if not n.startswith("_")]
        forbidden = [
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "close_all_positions",
            "create_order",
            "buy",
            "sell",
            "replace_order",
        ]
        for token in forbidden:
            assert token not in public_names, (
                f"forbidden attribute exposed on client: {token}"
            )

    def test_expected_read_methods_exposed(self):
        client = ResearchAccountClient(env=TEST_ENV)
        for name in ("fetch_bars", "list_calendar", "paper_account_info"):
            assert callable(getattr(client, name))

    def test_no_mutation_prefixes_on_public_methods(self):
        client = ResearchAccountClient(env=TEST_ENV)
        for name in dir(client):
            if name.startswith("_"):
                continue
            assert not name.startswith("submit_")
            assert not name.startswith("place_")
            assert not name.startswith("create_")
            assert not name.startswith("cancel_")
            assert not name.startswith("delete_")


# ---------------------------------------------------------------------------
# Source-level safety
# ---------------------------------------------------------------------------


def _source() -> str:
    import strategy.research_account as module

    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_references(self):
        source = _source()
        for token in [
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "create_order",
            "TradingClient",
            "yfinance",
        ]:
            assert token not in source, (
                f"research_account must not reference {token!r}"
            )

    def test_no_live_runner_imports(self):
        source = _source()
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
                f"research_account must not import {token!r}"
            )

    def test_never_reads_alpaca_env_vars_directly(self):
        source = _source()
        # No os.environ / getenv references to ALPACA_ names outside the
        # FORBIDDEN_ENV_FALLBACKS declaration.
        forbidden_reads = [
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\.get\(\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\.get\(\s*[\'"]APCA_',
            r'os\.getenv\(\s*[\'"]APCA_',
        ]
        for pattern in forbidden_reads:
            assert not re.search(pattern, source), (
                f"research_account must not read from ALPACA_/APCA_ env: "
                f"pattern {pattern!r}"
            )

    def test_never_opens_files_for_writing(self):
        source = _source()
        # File-writing patterns forbidden — credentials must never
        # touch disk from this module.
        assert "write_text" not in source
        assert "write_bytes" not in source
        # `open(` with any write mode
        assert not re.search(
            r"(?<!url)\bopen\([^)]*[\'\"][wxa][b+]?[\'\"]", source
        )
        # `with open(` (creates a filesystem handle)
        assert "with open" not in source

    def test_module_never_imports_strategy_config(self):
        source = _source()
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source

    def test_terminology_avoids_training(self):
        source = _source()
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 5 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.research_account", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.research_account  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {
            "trader_cli",
            "trader",
            "crypto_trader",
            "telegram_approvals",
        }
        assert not (added & forbidden)

    def test_module_never_mutates_global_feature_flags(self, monkeypatch):
        flags = reset_feature_flags()
        monkeypatch.setenv(RESEARCH_ALPACA_API_KEY_ENV, "k")
        monkeypatch.setenv(RESEARCH_ALPACA_SECRET_KEY_ENV, "s")
        monkeypatch.setenv(RESEARCH_ALPACA_ENDPOINT_ENV, "https://trading.x")
        monkeypatch.setenv(
            RESEARCH_ALPACA_DATA_ENDPOINT_ENV, "https://data.x"
        )

        def http_get(request, timeout):
            if request.url.endswith("/v2/calendar"):
                return json.dumps([]).encode("utf-8")
            return json.dumps({"bars": {}}).encode("utf-8")

        client = ResearchAccountClient(http_get=http_get)
        client.fetch_bars(["AAPL"])
        client.list_calendar()
        client.paper_account_info()
        assert flags.all_disabled is True
        assert flags.enabled_flags == []


# ---------------------------------------------------------------------------
# Env-namespace isolation from ALPACA_*
# ---------------------------------------------------------------------------


class TestEnvNamespaceIsolation:
    def test_only_forbidden_vars_set_client_refuses(self, monkeypatch):
        # Simulate a shell where only the live ALPACA_ vars are present.
        for name in FORBIDDEN_ENV_FALLBACKS:
            monkeypatch.setenv(name, "leak-should-not-be-read")
        for name in REQUIRED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        c = ResearchAccountConfig()
        with pytest.raises(ResearchAccountConfigError, match="missing"):
            c.resolve_credentials()

    def test_credentials_not_serialized_by_to_dict(self):
        c = ResearchAccountConfig()
        payload = json.dumps(c.to_dict())
        for secret in ("test-api-key", "test-secret-key"):
            assert secret not in payload

    def test_credentials_not_returned_by_client_properties(self):
        client = ResearchAccountClient(env=TEST_ENV)
        # The client exposes config (env-var names) and timeout only;
        # no property returns the credential values.
        assert client.config.to_dict() == {
            "api_key_env": RESEARCH_ALPACA_API_KEY_ENV,
            "secret_key_env": RESEARCH_ALPACA_SECRET_KEY_ENV,
            "endpoint_env": RESEARCH_ALPACA_ENDPOINT_ENV,
            "data_endpoint_env": RESEARCH_ALPACA_DATA_ENDPOINT_ENV,
        }
        assert client.timeout > 0
        # No property named api_key, secret_key, etc.
        for forbidden in ("api_key", "secret_key", "credentials"):
            assert not hasattr(client, forbidden)


# ---------------------------------------------------------------------------
# Deterministic request record
# ---------------------------------------------------------------------------


class TestDeterministicRequestConstruction:
    def test_two_clients_same_env_produce_same_url(self):
        a = ResearchAccountClient(env=TEST_ENV)
        b = ResearchAccountClient(env=TEST_ENV)
        assert a.build_bars_request(["AAPL"]).url == b.build_bars_request(
            ["AAPL"]
        ).url

    def test_request_frozen(self):
        client = ResearchAccountClient(env=TEST_ENV)
        request = client.build_bars_request(["AAPL"])
        with pytest.raises((AttributeError, Exception)):
            # Frozen dataclass — writes fail
            request.url = "https://other"  # type: ignore[misc]

    def test_no_side_effect_on_env(self, monkeypatch):
        # Ensure env dict passed in isn't mutated
        env = dict(TEST_ENV)
        snapshot = dict(env)
        client = ResearchAccountClient(env=env)
        client.build_bars_request(["AAPL"])
        client.build_calendar_request()
        client.build_account_request()
        assert env == snapshot
