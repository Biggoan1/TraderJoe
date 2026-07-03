"""Isolated Research Alpaca paper account client.

Read-only wrapper around the dedicated Research Alpaca paper account.
Validation, replay, research only.  Never used by the live runner,
crypto trader, Telegram approval path, `trader_cli.py`, the Hermes
plugin, or any scheduler.

The client exposes only historical-data and account-metadata reads
(`fetch_bars`, `list_calendar`, `paper_account_info`).  It does not
implement any order-placement method and does not import any live
runner module.

Credentials come from the ``RESEARCH_ALPACA_*`` env-var namespace
only.  The client refuses to fall back to ``ALPACA_*`` or
``APCA_*`` — falling back would defeat the isolation guarantee that
protects the live paper-trading account.  The config object stores
env-var *names*, not values; credentials are resolved at call time
only and are never written to disk.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 5 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


# ---------------------------------------------------------------------------
# Env-var namespace
# ---------------------------------------------------------------------------


RESEARCH_ALPACA_API_KEY_ENV = "RESEARCH_ALPACA_API_KEY"
RESEARCH_ALPACA_SECRET_KEY_ENV = "RESEARCH_ALPACA_SECRET_KEY"
RESEARCH_ALPACA_ENDPOINT_ENV = "RESEARCH_ALPACA_ENDPOINT"
RESEARCH_ALPACA_DATA_ENDPOINT_ENV = "RESEARCH_ALPACA_DATA_ENDPOINT"

REQUIRED_ENV_VARS: Tuple[str, ...] = (
    RESEARCH_ALPACA_API_KEY_ENV,
    RESEARCH_ALPACA_SECRET_KEY_ENV,
    RESEARCH_ALPACA_ENDPOINT_ENV,
    RESEARCH_ALPACA_DATA_ENDPOINT_ENV,
)

ENDPOINT_KIND_TRADING = "trading"
ENDPOINT_KIND_DATA = "data"

# We explicitly refuse to read from these — they belong to the live
# paper account (or its SDK alias) and would break the isolation
# invariant if the Research client ever fell back to them.
FORBIDDEN_ENV_FALLBACKS: Tuple[str, ...] = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_ENDPOINT",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
)

# Alpaca REST authentication headers
HEADER_API_KEY = "APCA-API-KEY-ID"
HEADER_SECRET_KEY = "APCA-API-SECRET-KEY"

# Request paths (relative to the configured endpoint)
BARS_PATH = "/v2/stocks/bars"
CALENDAR_PATH = "/v2/calendar"
ACCOUNT_PATH = "/v2/account"

# Defaults
DEFAULT_TIMEFRAME = "1Day"
DEFAULT_BAR_LIMIT = 1000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_ADJUSTMENT = "raw"
DEFAULT_FEED = "iex"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ResearchAccountConfigError(ValueError):
    """Raised when the Research account config or credentials are invalid."""


class ResearchAccountRequestError(RuntimeError):
    """Raised when a Research account HTTP request cannot be completed."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchAccountConfig:
    """Env-var names for the isolated Research Alpaca account.

    The config stores variable *names* only; values are read at call
    time via :meth:`resolve_credentials` / :meth:`resolve_data_credentials`
    so no credential is retained on the config object or serialized
    to disk.

    Two endpoints are tracked separately: the *trading* endpoint
    (``paper-api.alpaca.markets``) serves calendar and account
    reads, while the *market-data* endpoint (``data.alpaca.markets``)
    serves historical bars.  Alpaca hosts these on distinct domains,
    so callers that only need one still name both — the split lives
    entirely inside the ``RESEARCH_ALPACA_*`` namespace.
    """

    api_key_env: str = RESEARCH_ALPACA_API_KEY_ENV
    secret_key_env: str = RESEARCH_ALPACA_SECRET_KEY_ENV
    endpoint_env: str = RESEARCH_ALPACA_ENDPOINT_ENV
    data_endpoint_env: str = RESEARCH_ALPACA_DATA_ENDPOINT_ENV

    def __post_init__(self) -> None:
        for name in (
            self.api_key_env,
            self.secret_key_env,
            self.endpoint_env,
            self.data_endpoint_env,
        ):
            if not name.startswith("RESEARCH_ALPACA"):
                raise ResearchAccountConfigError(
                    f"env var {name!r} does not start with 'RESEARCH_ALPACA'; "
                    "credential namespace must be isolated from ALPACA_*"
                )

    @classmethod
    def from_env(
        cls, env: Optional[Mapping[str, str]] = None
    ) -> "ResearchAccountConfig":
        """Build a config from the ``RESEARCH_ALPACA_*`` namespace and
        fail fast if any credential or endpoint is missing.

        Both the trading endpoint and the market-data endpoint are
        required — the client refuses to guess which of the two a
        caller wants.  The returned object still stores env-var
        *names* only — credential values are read at each call to
        :meth:`resolve_credentials` / :meth:`resolve_data_credentials`
        and are never retained on the instance.
        """
        config = cls()
        config.resolve_credentials(env)
        config.resolve_data_credentials(env)
        return config

    def _resolve(
        self,
        env: Optional[Mapping[str, str]],
        endpoint_env: str,
    ) -> Tuple[str, str, str]:
        source = env if env is not None else os.environ
        missing = [
            name
            for name in (self.api_key_env, self.secret_key_env, endpoint_env)
            if not source.get(name)
        ]
        if missing:
            raise ResearchAccountConfigError(
                f"missing required env vars: {missing}"
            )
        return (
            source[self.api_key_env],
            source[self.secret_key_env],
            source[endpoint_env],
        )

    def resolve_credentials(
        self, env: Optional[Mapping[str, str]] = None
    ) -> Tuple[str, str, str]:
        """Resolve the trading-endpoint credentials.

        Returns ``(api_key, secret_key, trading_endpoint)``.  The
        trading endpoint serves account and calendar reads.
        """
        return self._resolve(env, self.endpoint_env)

    def resolve_data_credentials(
        self, env: Optional[Mapping[str, str]] = None
    ) -> Tuple[str, str, str]:
        """Resolve the market-data-endpoint credentials.

        Returns ``(api_key, secret_key, data_endpoint)``.  The
        market-data endpoint serves historical bars.
        """
        return self._resolve(env, self.data_endpoint_env)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "api_key_env": self.api_key_env,
            "secret_key_env": self.secret_key_env,
            "endpoint_env": self.endpoint_env,
            "data_endpoint_env": self.data_endpoint_env,
        }


# ---------------------------------------------------------------------------
# Request record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class ResearchAccountRequest:
    """Deterministic HTTP request record for testing and inspection.

    :meth:`redacted_dict` masks credential headers so requests can be
    logged or persisted without leaking API keys.  ``__repr__`` and
    ``__str__`` route through the same redaction so accidental
    ``print(request)`` or exception traces cannot expose credentials.
    """

    method: str
    url: str
    headers: Mapping[str, str]

    def redacted_dict(self) -> Dict[str, Any]:
        redacted = {
            key: (
                "***REDACTED***"
                if key in {HEADER_API_KEY, HEADER_SECRET_KEY}
                else value
            )
            for key, value in self.headers.items()
        }
        return {
            "method": self.method,
            "url": self.url,
            "headers": redacted,
        }

    def __repr__(self) -> str:
        return (
            f"ResearchAccountRequest(method={self.method!r}, "
            f"url={self.url!r}, headers={self.redacted_dict()['headers']!r})"
        )

    __str__ = __repr__


HttpGetCallable = Callable[["ResearchAccountRequest", float], bytes]


def _urllib_http_get(
    request: ResearchAccountRequest, timeout: float
) -> bytes:
    """Default HTTP GET implementation using :mod:`urllib.request`.

    Tests inject a fake to avoid touching the network.
    """
    urlreq = urllib.request.Request(
        request.url,
        method=request.method,
        headers=dict(request.headers),
    )
    with urllib.request.urlopen(urlreq, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ResearchAccountClient:
    """Read-only client for the dedicated Research Alpaca paper account.

    Exposes only historical-data reads (:meth:`fetch_bars`,
    :meth:`list_calendar`, :meth:`paper_account_info`).  No
    order-placement method exists.  Never imports any live-runner
    module, never persists credentials, and never falls back to the
    ``ALPACA_*`` env namespace used by the normal paper account.
    """

    def __init__(
        self,
        config: Optional[ResearchAccountConfig] = None,
        env: Optional[Mapping[str, str]] = None,
        http_get: Optional[HttpGetCallable] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        if timeout <= 0:
            raise ResearchAccountConfigError("timeout must be positive")
        self._config = config if config is not None else ResearchAccountConfig()
        self._env = env
        self._http_get: HttpGetCallable = (
            http_get if http_get is not None else _urllib_http_get
        )
        self._timeout = float(timeout)

    @property
    def config(self) -> ResearchAccountConfig:
        return self._config

    @property
    def timeout(self) -> float:
        return self._timeout

    # -- request builders -------------------------------------------------

    def build_bars_request(
        self,
        symbols: Sequence[str],
        timeframe: str = DEFAULT_TIMEFRAME,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = DEFAULT_BAR_LIMIT,
        adjustment: str = DEFAULT_ADJUSTMENT,
        feed: str = DEFAULT_FEED,
    ) -> ResearchAccountRequest:
        if not symbols:
            raise ResearchAccountConfigError("symbols is required")
        if limit <= 0:
            raise ResearchAccountConfigError("limit must be positive")
        params: Dict[str, str] = {
            "symbols": ",".join(sorted(symbols)),
            "timeframe": timeframe,
            "limit": str(limit),
            "adjustment": adjustment,
            "feed": feed,
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return self._build_request(
            "GET", BARS_PATH, params, endpoint_kind=ENDPOINT_KIND_DATA
        )

    def build_calendar_request(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> ResearchAccountRequest:
        params: Dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return self._build_request("GET", CALENDAR_PATH, params)

    def build_account_request(self) -> ResearchAccountRequest:
        return self._build_request("GET", ACCOUNT_PATH, {})

    def _build_request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str],
        endpoint_kind: str = ENDPOINT_KIND_TRADING,
    ) -> ResearchAccountRequest:
        if endpoint_kind == ENDPOINT_KIND_DATA:
            api_key, secret_key, endpoint = (
                self._config.resolve_data_credentials(self._env)
            )
        elif endpoint_kind == ENDPOINT_KIND_TRADING:
            api_key, secret_key, endpoint = self._config.resolve_credentials(
                self._env
            )
        else:
            raise ResearchAccountConfigError(
                f"unknown endpoint_kind: {endpoint_kind!r}"
            )
        endpoint = endpoint.rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ResearchAccountConfigError(
                f"endpoint {endpoint!r} must start with http:// or https://"
            )
        ordered = sorted(params.items())
        query = urllib.parse.urlencode(
            ordered, quote_via=urllib.parse.quote_plus
        )
        url = f"{endpoint}{path}"
        if query:
            url = f"{url}?{query}"
        headers: Dict[str, str] = {
            HEADER_API_KEY: api_key,
            HEADER_SECRET_KEY: secret_key,
            "Accept": "application/json",
        }
        return ResearchAccountRequest(
            method=method, url=url, headers=headers
        )

    # -- reads ------------------------------------------------------------

    def fetch_bars(
        self,
        symbols: Sequence[str],
        timeframe: str = DEFAULT_TIMEFRAME,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = DEFAULT_BAR_LIMIT,
        adjustment: str = DEFAULT_ADJUSTMENT,
        feed: str = DEFAULT_FEED,
    ) -> Dict[str, Any]:
        request = self.build_bars_request(
            symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
            adjustment=adjustment,
            feed=feed,
        )
        payload = self._perform(request)
        if not isinstance(payload, dict):
            raise ResearchAccountRequestError(
                f"bars response is not an object: {type(payload).__name__}"
            )
        return payload

    def list_calendar(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        request = self.build_calendar_request(start=start, end=end)
        payload = self._perform(request)
        if not isinstance(payload, list):
            raise ResearchAccountRequestError(
                f"calendar response is not a list: {type(payload).__name__}"
            )
        return payload

    def paper_account_info(self) -> Dict[str, Any]:
        request = self.build_account_request()
        payload = self._perform(request)
        if not isinstance(payload, dict):
            raise ResearchAccountRequestError(
                f"account response is not an object: {type(payload).__name__}"
            )
        return payload

    # -- transport --------------------------------------------------------

    def _perform(self, request: ResearchAccountRequest) -> Any:
        try:
            body = self._http_get(request, self._timeout)
        except Exception as exc:  # noqa: BLE001 -- surface as request error
            raise ResearchAccountRequestError(
                f"HTTP {request.method} {request.url} failed: {exc}"
            ) from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ResearchAccountRequestError(
                f"could not decode response body as JSON: {exc}"
            ) from exc
