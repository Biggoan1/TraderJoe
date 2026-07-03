"""Local LLM Research Analyst.

Read-only analytical component that consumes Phase 3 and Phase 4
research artifacts and produces a comprehensive research narrative.
Runs against a local LLM endpoint (Ollama / LM Studio) only; refuses
non-loopback hosts; enforces ``temperature=0.0``; refuses model names
that look like cloud providers.

The analyst NEVER:

- recommends a trade, entry, exit, position size, buy, sell, or hold;
- states or implies that any feature flag should be enabled or disabled;
- approves, endorses, or recommends a promotion to any state;
- modifies strategy behavior;
- speculates about future price movements or claims patterns will repeat.

The analyst DOES:

- explain observed statistics and hypotheses in plain language;
- identify strengths in the evidence (sample size, effect size, out-of-sample
  confirmation, tight confidence intervals);
- identify weaknesses (low sample count, wide CIs, look-ahead risk,
  concentration, hypothesis-labeled findings only);
- highlight statistical caveats (multiple-testing risk, non-stationarity,
  base-rate limitations, distribution assumptions);
- point out where the evidence is inconclusive;
- cite specific report ids, metric names, and sample sizes.

Reproducibility: ``LLMNarrative.narrative_id``, ``prompt_hash``, and
``ResearchAnalystReport.report_id`` are deterministic functions of the
source artifact and the composed prompt.  Given a deterministic LLM
response, the entire report bundle is byte-identical across reruns.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 5 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
from urllib.parse import urlparse

from strategy.backtest_lab import stable_hash, stable_json
from strategy.model_config import CONTEXT_RESEARCH, resolve_model


# ---------------------------------------------------------------------------
# Env / endpoint constants
# ---------------------------------------------------------------------------


RESEARCH_LLM_ENDPOINT_ENV = "RESEARCH_LLM_ENDPOINT"
RESEARCH_LLM_ALLOW_REMOTE_ENV = "RESEARCH_LLM_ALLOW_REMOTE"
RESEARCH_LLM_ALLOWED_HOSTS_ENV = "RESEARCH_LLM_ALLOWED_HOSTS"

DEFAULT_LOCAL_ENDPOINT = "http://127.0.0.1:8080"
LOOPBACK_HOSTS: Tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

# Truthy values accepted for RESEARCH_LLM_ALLOW_REMOTE.
_ALLOW_REMOTE_TRUTHY: Tuple[str, ...] = ("true", "1", "yes", "on")

DEFAULT_TIMEOUT_SECONDS = 60.0
REQUIRED_TEMPERATURE = 0.0

# Cloud provider tokens we refuse in a model name — defense-in-depth
# alongside the endpoint host check.
CLOUD_MODEL_TOKENS: Tuple[str, ...] = (
    "openai",
    "anthropic",
    "google",
    "azure",
    "aws",
    "gemini",
    "claude",
    "chatgpt",
)

# Cloud provider host substrings we refuse in an endpoint URL —
# defense-in-depth against a compromised allowlist.
CLOUD_HOST_TOKENS: Tuple[str, ...] = (
    "openai.com",
    "openai.azure.com",
    "anthropic.com",
    "claude.ai",
    "chatgpt.com",
    "chat.openai.com",
    "googleapis.com",
    "generativelanguage.googleapis.com",
    "gemini.google.com",
    "google.com",
    "azurewebsites.net",
    "amazonaws.com",
    "cohere.ai",
    "huggingface.co",
    "replicate.com",
)


# ---------------------------------------------------------------------------
# Source kinds
# ---------------------------------------------------------------------------


KIND_COMPARISON = "champion_challenger_comparison"
KIND_WALK_FORWARD = "walk_forward_report"
KIND_LEARNING = "learning_report"
KIND_RESEARCH_REPORT = "research_report"
KIND_PROMOTION = "promotion_report"

KNOWN_KINDS: Tuple[str, ...] = (
    KIND_COMPARISON,
    KIND_WALK_FORWARD,
    KIND_LEARNING,
    KIND_RESEARCH_REPORT,
    KIND_PROMOTION,
)


# ---------------------------------------------------------------------------
# Report path constants
# ---------------------------------------------------------------------------


ANALYST_REPORT_ID_PREFIX = "ra"
NARRATIVE_ID_PREFIX = "nar"

ANALYST_REPORT_FILENAME_MARKDOWN = "report.md"
ANALYST_REPORT_FILENAME_JSON = "report.json"
ANALYST_REPORT_FILENAME_NARRATIVE = "narrative.json"
ANALYST_REPORT_FILENAME_MANIFEST = "manifest.json"

DEFAULT_ANALYST_OUTPUT_DIR = "reports/analyst"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LocalLLMEndpointError(ValueError):
    """Raised when an LLM endpoint is not loopback."""


class LocalLLMConfigError(ValueError):
    """Raised when local LLM config (model, temperature, prompt) is invalid."""


class LocalLLMRequestError(RuntimeError):
    """Raised when an HTTP call to the local LLM fails."""


class ModelDiscoveryError(LocalLLMConfigError):
    """Raised when model discovery cannot complete (transport, decoding).

    Distinct from :class:`ModelNotAvailableError`: this covers
    endpoint failures, not "endpoint responded but model is absent".
    """


class ModelNotAvailableError(LocalLLMConfigError):
    """Raised when the configured model is not present in the
    endpoint's enumerated model list.

    Never silently substituted — the caller must inspect
    ``available`` and pick a replacement explicitly.
    """

    def __init__(
        self,
        configured: str,
        endpoint: str,
        available: Sequence[str],
        closest: Optional[str] = None,
    ) -> None:
        self.configured = configured
        self.endpoint = endpoint
        self.available = tuple(available)
        self.closest = closest
        parts = [
            f"configured model {configured!r} is not available "
            f"on {endpoint!r}",
            f"available: {list(available)}",
        ]
        if closest:
            parts.append(f"closest match: {closest!r}")
        parts.append(
            "The Research Analyst will NOT silently substitute another "
            "model; edit RESEARCH_AI_MODEL to one of the listed values."
        )
        super().__init__("; ".join(parts))


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """You are a Research Analyst working on the Trader Joe research platform.

Your role is READ-ONLY and ANALYTICAL. You produce a research narrative that explains, contextualizes, and critiques the evidence you are given.

Hard constraints — you MUST obey every one:

1. Never recommend a trade, entry, exit, position size, hold, sell, or buy. Do not tell anyone what to do with the market.
2. Never state or imply that any feature flag should be enabled or disabled.
3. Never approve, endorse, or recommend a promotion to any state (backtest, walk_forward, paper_trading, candidate, approved, or production).
4. Never modify strategy behavior. You are not authorized to change any parameter, weight, or configuration.
5. Never speculate about future price movements or make market forecasts.
6. Never state or imply that observed historical patterns will repeat.

Your job is to:

- Explain the observed statistics and hypotheses in plain language.
- Identify strengths in the evidence: sample size, effect size, out-of-sample confirmation, low disagreement variance, tight confidence intervals.
- Identify weaknesses: low sample count, wide confidence intervals, look-ahead risk, single-symbol or single-regime concentration, hypothesis-labeled findings only.
- Highlight statistical caveats: multiple-testing risk, non-stationarity of markets, sample-size floor gates, distribution assumptions, base-rate limitations.
- Point out where the evidence is inconclusive.
- Cite specific report ids, metric names, and sample sizes from the source you were given.

Output format: research narrative in Markdown. Use these headings and NO OTHERS:

## Executive Summary
## What the Evidence Says
## Strengths
## Weaknesses
## Statistical Caveats
## What This Evidence Cannot Answer

Do not add a "Recommendation" section. Do not add "Next Steps". Do not add "Action Items". If you find yourself wanting to suggest an action, stop and instead describe the gap in the evidence that would need to close before any action could be considered by a human reviewer.
"""


# Forbidden output patterns — heuristics that flag any LLM response
# that appears to contain trade recommendations, flag advocacy, or
# promotion advocacy.  Flagged narratives still return, but with a
# ``warnings`` list that a human reviewer must clear before use.
FORBIDDEN_OUTPUT_PATTERNS: Tuple[str, ...] = (
    "you should buy",
    "you should sell",
    "buy this",
    "sell this",
    "recommend buying",
    "recommend selling",
    "recommend to buy",
    "recommend to sell",
    "enable this flag",
    "enable the flag",
    "disable this flag",
    "approve this promotion",
    "promote to production",
    "should be promoted",
    "should be approved",
)

FORBIDDEN_OUTPUT_HEADINGS: Tuple[str, ...] = (
    "## recommendation",
    "## recommendations",
    "## next steps",
    "## next step",
    "## action items",
    "## action item",
    "## call to action",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


HOST_CLASS_LOOPBACK = "loopback"
HOST_CLASS_PRIVATE_IP = "private_ip"
HOST_CLASS_PUBLIC_IP = "public_ip"
HOST_CLASS_CLOUD_DOMAIN = "cloud_domain"
HOST_CLASS_HOSTNAME = "hostname"


def _host_contains_cloud_token(host: str) -> bool:
    lower = host.lower()
    for token in CLOUD_HOST_TOKENS:
        if token in lower:
            return True
    return False


def _classify_host(host: str) -> str:
    """Classify an endpoint host into one of five categories.

    The classification informs the endpoint policy check.  Cloud host
    tokens are always rejected regardless of allowlist state.  Public
    IPs are always rejected.  Loopback is always allowed.  Private IPs
    and hostnames require explicit opt-in AND allowlist membership.
    """
    host = host.lower()
    if _host_contains_cloud_token(host):
        return HOST_CLASS_CLOUD_DOMAIN
    if host in LOOPBACK_HOSTS:
        return HOST_CLASS_LOOPBACK
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal — treat as a hostname
        return HOST_CLASS_HOSTNAME
    # Order matters: unspecified addresses (0.0.0.0, ::) are also
    # classified as "private" by ipaddress — we refuse them
    # explicitly because they identify an interface bind, not a
    # trustworthy peer.
    if addr.is_unspecified:
        return HOST_CLASS_PUBLIC_IP
    if addr.is_loopback:
        return HOST_CLASS_LOOPBACK
    if addr.is_private:
        return HOST_CLASS_PRIVATE_IP
    return HOST_CLASS_PUBLIC_IP


def _is_loopback_endpoint(endpoint: str) -> bool:
    """Kept for backward compat; returns True only for loopback."""
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return _classify_host(host) == HOST_CLASS_LOOPBACK


def _assert_endpoint_allowed(
    endpoint: str,
    allow_remote: bool = False,
    allowed_hosts: Sequence[str] = (),
) -> None:
    """Validate that ``endpoint`` is safe to reach.

    Policy:

    * Loopback endpoints (``127.0.0.1`` / ``localhost`` / ``::1``) are
      always allowed.
    * Cloud provider host substrings are always refused.
    * Public IPv4 / IPv6 addresses are always refused, even with
      ``allow_remote=True`` and an allowlist match.
    * Private IPs (RFC1918 / CG-NAT / ULA / link-local) and
      hostnames are allowed ONLY when ``allow_remote`` is ``True`` AND
      the host is in ``allowed_hosts``.
    """
    if not endpoint:
        raise LocalLLMEndpointError("endpoint is required")
    if not endpoint.startswith(("http://", "https://")):
        raise LocalLLMEndpointError(
            f"endpoint {endpoint!r} must start with http:// or https://"
        )
    try:
        parsed = urlparse(endpoint)
    except ValueError as exc:
        raise LocalLLMEndpointError(
            f"endpoint {endpoint!r} could not be parsed: {exc}"
        ) from exc
    host = (parsed.hostname or "").lower()
    if not host:
        raise LocalLLMEndpointError(
            f"endpoint {endpoint!r} has no host component"
        )
    kind = _classify_host(host)

    if kind == HOST_CLASS_CLOUD_DOMAIN:
        raise LocalLLMEndpointError(
            f"endpoint host {host!r} matches a cloud provider domain "
            f"({CLOUD_HOST_TOKENS}); the Research Analyst never queries "
            f"a cloud provider"
        )

    if kind == HOST_CLASS_PUBLIC_IP:
        raise LocalLLMEndpointError(
            f"endpoint host {host!r} is a public IP address; the "
            f"Research Analyst refuses public endpoints even when "
            f"RESEARCH_LLM_ALLOW_REMOTE is set"
        )

    if kind == HOST_CLASS_LOOPBACK:
        return

    # Non-loopback (private_ip or hostname) — require opt-in + allowlist.
    if not allow_remote:
        raise LocalLLMEndpointError(
            f"endpoint host {host!r} is not loopback and "
            f"RESEARCH_LLM_ALLOW_REMOTE is not enabled; loopback hosts "
            f"({LOOPBACK_HOSTS}) work by default, or set "
            f"RESEARCH_LLM_ALLOW_REMOTE=true and add {host!r} to "
            f"RESEARCH_LLM_ALLOWED_HOSTS to explicitly trust a local "
            f"LAN endpoint"
        )

    allowlist = tuple(h.strip().lower() for h in allowed_hosts if h.strip())
    if host not in allowlist:
        raise LocalLLMEndpointError(
            f"endpoint host {host!r} is not in RESEARCH_LLM_ALLOWED_HOSTS "
            f"(configured: {list(allowlist) or 'empty'}); add {host!r} to "
            f"the comma-separated list to explicitly trust it"
        )


# Backward-compat alias for tests / callers written against the older
# loopback-only guard.
def _assert_loopback_endpoint(endpoint: str) -> None:
    _assert_endpoint_allowed(endpoint, allow_remote=False, allowed_hosts=())


def _parse_allow_remote(env: Mapping[str, str]) -> bool:
    raw = env.get(RESEARCH_LLM_ALLOW_REMOTE_ENV, "").strip().lower()
    return raw in _ALLOW_REMOTE_TRUTHY


def _parse_allowed_hosts(env: Mapping[str, str]) -> Tuple[str, ...]:
    raw = env.get(RESEARCH_LLM_ALLOWED_HOSTS_ENV, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _assert_local_model(model: str) -> None:
    if not model:
        raise LocalLLMConfigError("model is required")
    lower = model.lower()
    for token in CLOUD_MODEL_TOKENS:
        if token in lower:
            raise LocalLLMConfigError(
                f"model {model!r} contains cloud token {token!r}; "
                "the Research Analyst only runs against local models"
            )


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _narrative_id(prompt_hash_hex: str, source_hash: str) -> str:
    seed = f"{prompt_hash_hex}:{source_hash}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{NARRATIVE_ID_PREFIX}_{digest[:12]}"


def _analyst_report_id(narrative_id: str, source_hash: str) -> str:
    seed = f"{narrative_id}:{source_hash}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{ANALYST_REPORT_ID_PREFIX}_{digest[:12]}"


def _detect_forbidden_output(text: str) -> List[str]:
    warnings: List[str] = []
    lower = text.lower()
    for pattern in FORBIDDEN_OUTPUT_PATTERNS:
        if pattern in lower:
            warnings.append(
                f"LLM output contains forbidden phrase {pattern!r}; "
                "human reviewer must scrub before use"
            )
    for forbidden_heading in FORBIDDEN_OUTPUT_HEADINGS:
        if forbidden_heading in lower:
            warnings.append(
                f"LLM output contains forbidden section heading "
                f"{forbidden_heading!r}; human reviewer must scrub before use"
            )
    return warnings


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


HttpPostCallable = Callable[[str, Dict[str, Any], float], bytes]
HttpGetCallable = Callable[[str, float], bytes]

# Endpoint paths tried in order for model enumeration.  The client
# tries them until one succeeds; a 404 / connection error triggers
# the next candidate.
MODEL_LISTING_PATHS: Tuple[str, ...] = (
    "/v1/models",   # OpenAI-compat (llama.cpp, llama-swap, LM Studio,
                    # Hermes Gateway)
    "/api/tags",    # Ollama
)


def _urllib_http_post(
    url: str, body: Dict[str, Any], timeout: float
) -> bytes:
    """Default HTTP POST via :mod:`urllib.request` — tests inject a fake."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _urllib_http_get(url: str, timeout: float) -> bytes:
    """Default HTTP GET via :mod:`urllib.request` — tests inject a fake."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# ---------------------------------------------------------------------------
# LocalLLMClient
# ---------------------------------------------------------------------------


class LocalLLMClient:
    """Localhost-only chat client with temperature=0.0 enforcement.

    Speaks Ollama's ``POST /api/chat`` shape by default and also
    accepts OpenAI-compat response shapes for LM Studio / llama.cpp.
    Tests inject a fake ``http_post`` to stay offline.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        temperature: float = REQUIRED_TEMPERATURE,
        http_post: Optional[HttpPostCallable] = None,
        http_get: Optional[HttpGetCallable] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        allow_remote: Optional[bool] = None,
        allowed_hosts: Optional[Sequence[str]] = None,
    ):
        source = env if env is not None else os.environ
        resolved_endpoint = endpoint or source.get(
            RESEARCH_LLM_ENDPOINT_ENV, DEFAULT_LOCAL_ENDPOINT
        )
        resolved_allow_remote = (
            allow_remote
            if allow_remote is not None
            else _parse_allow_remote(source)
        )
        resolved_allowed_hosts: Tuple[str, ...] = (
            tuple(allowed_hosts)
            if allowed_hosts is not None
            else _parse_allowed_hosts(source)
        )
        _assert_endpoint_allowed(
            resolved_endpoint,
            allow_remote=resolved_allow_remote,
            allowed_hosts=resolved_allowed_hosts,
        )
        if temperature != REQUIRED_TEMPERATURE:
            raise LocalLLMConfigError(
                f"temperature must be {REQUIRED_TEMPERATURE} "
                f"for reproducibility (got {temperature!r})"
            )
        if timeout <= 0:
            raise LocalLLMConfigError("timeout must be positive")
        if model:
            _assert_local_model(model)
        self._endpoint = resolved_endpoint.rstrip("/")
        self._allow_remote = bool(resolved_allow_remote)
        self._allowed_hosts = resolved_allowed_hosts
        self._default_model = model
        self._temperature = float(temperature)
        self._http_post: HttpPostCallable = (
            http_post if http_post is not None else _urllib_http_post
        )
        self._http_get: HttpGetCallable = (
            http_get if http_get is not None else _urllib_http_get
        )
        self._timeout = float(timeout)
        self._env = env

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def allow_remote(self) -> bool:
        return self._allow_remote

    @property
    def allowed_hosts(self) -> Tuple[str, ...]:
        return self._allowed_hosts

    @property
    def default_model(self) -> Optional[str]:
        return self._default_model

    @property
    def temperature(self) -> float:
        return self._temperature

    def effective_model(self, explicit: Optional[str] = None) -> str:
        """Resolve the model via :func:`strategy.model_config.resolve_model`.

        Precedence: explicit arg > ``RESEARCH_AI_MODEL`` env >
        client default.  Refuses any model whose name contains a
        cloud-provider token.
        """
        chosen = resolve_model(
            CONTEXT_RESEARCH,
            explicit=explicit,
            default=self._default_model,
            env=self._env,
        )
        if not chosen:
            raise LocalLLMConfigError(
                "no model resolved; supply an explicit model, "
                "set RESEARCH_AI_MODEL, or pass model= to LocalLLMClient"
            )
        _assert_local_model(chosen)
        return chosen

    def chat(
        self,
        prompt: str,
        system_prompt: str,
        model: Optional[str] = None,
    ) -> str:
        chosen_model = self.effective_model(explicit=model)
        body = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": self._temperature},
            "stream": False,
        }
        url = f"{self._endpoint}/api/chat"
        try:
            payload_bytes = self._http_post(url, body, self._timeout)
        except Exception as exc:  # noqa: BLE001
            raise LocalLLMRequestError(
                f"local LLM request failed: {exc}"
            ) from exc
        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError as exc:
            raise LocalLLMRequestError(
                f"local LLM returned non-JSON body: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise LocalLLMRequestError(
                f"local LLM response is not an object: "
                f"{type(payload).__name__}"
            )
        return self._extract_content(payload)

    def list_models(self) -> List[str]:
        """Enumerate models available on the configured local endpoint.

        Tries the OpenAI-compatible ``/v1/models`` endpoint first,
        then Ollama's ``/api/tags`` as a fallback.  Returns a
        deterministic sorted list.  Returns an empty list if the
        endpoint responds but exposes no ``data`` / ``models`` array
        (treated as "capability unavailable" by callers).

        Raises :class:`ModelDiscoveryError` only when every candidate
        path fails at the transport or decoding layer.  The endpoint
        guard already ensured we are calling a loopback host — this
        method never queries a cloud provider.
        """
        errors: List[str] = []
        for path in MODEL_LISTING_PATHS:
            url = f"{self._endpoint}{path}"
            try:
                body = self._http_get(url, self._timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}: non-JSON response: {exc}")
                continue
            names = self._extract_model_names(payload)
            return sorted(set(names))
        raise ModelDiscoveryError(
            f"could not enumerate models on {self._endpoint}; "
            f"tried {list(MODEL_LISTING_PATHS)}; errors: {errors}"
        )

    @staticmethod
    def _extract_model_names(payload: Any) -> List[str]:
        names: List[str] = []
        if not isinstance(payload, dict):
            return names
        # OpenAI-compat: {"data": [{"id": "..."}, ...]}
        # Ollama:       {"models": [{"name": "..."}, ...]}
        for key in ("data", "models"):
            entries = payload.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        name = entry.get("id") or entry.get("name")
                        if isinstance(name, str) and name:
                            names.append(name)
                if names:
                    return names
        return names

    @staticmethod
    def _extract_content(payload: Dict[str, Any]) -> str:
        # Ollama: {"message": {"content": "..."}}
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
        # OpenAI-compat: {"choices": [{"message": {"content": "..."}}]}
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                inner = first.get("message")
                if isinstance(inner, dict):
                    content = inner.get("content")
                    if isinstance(content, str):
                        return content
        raise LocalLLMRequestError(
            "local LLM response did not contain a message content string"
        )


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def _closest_model(
    configured: str, available: Sequence[str]
) -> Optional[str]:
    """Return the closest name from ``available`` using a deterministic
    Levenshtein-style ratio via :mod:`difflib`."""
    import difflib

    if not available:
        return None
    matches = difflib.get_close_matches(
        configured, list(available), n=1, cutoff=0.4
    )
    return matches[0] if matches else None


def verify_model_available(
    client: "LocalLLMClient",
    model: str,
) -> Tuple[List[str], Optional[str]]:
    """Verify that ``model`` is present in the endpoint's model list.

    Returns ``(available, closest_or_none)`` when the model IS
    available.  Raises :class:`ModelNotAvailableError` when the
    endpoint enumerates models but the configured model is absent.

    If the endpoint does not support enumeration (all listing paths
    fail), returns ``([], None)`` and does NOT raise — callers fall
    back to the configured model without behavior change.  The
    endpoint's loopback guarantee still holds because
    :class:`LocalLLMClient` enforced it at construction time; this
    helper never touches a cloud provider.
    """
    _assert_local_model(model)
    try:
        available = client.list_models()
    except ModelDiscoveryError:
        return [], None
    if not available:
        # Endpoint responded but exposes no listing — treated as
        # capability unavailable, per the task spec.
        return [], None
    if model in available:
        return available, None
    closest = _closest_model(model, available)
    raise ModelNotAvailableError(
        configured=model,
        endpoint=client.endpoint,
        available=available,
        closest=closest,
    )


# ---------------------------------------------------------------------------
# Prompt composer
# ---------------------------------------------------------------------------


def compose_analyst_prompt(
    source_kind: str,
    source_id: str,
    source_hash: str,
    source_payload: Mapping[str, Any],
) -> str:
    """Deterministic prompt combining source metadata + stable JSON."""
    if source_kind not in KNOWN_KINDS:
        raise ValueError(
            f"unknown source_kind: {source_kind!r} "
            f"(expected one of {KNOWN_KINDS})"
        )
    if not source_id:
        raise ValueError("source_id is required")
    if not source_hash:
        raise ValueError("source_hash is required")
    return (
        f"Please analyse the following {source_kind} evidence.\n"
        f"Source id: {source_id}\n"
        f"Source hash: {source_hash}\n\n"
        "Serialized source (deterministic JSON):\n"
        f"{stable_json(dict(source_payload))}\n\n"
        "Follow the system instructions. Produce only the six "
        "sections listed there."
    )


# ---------------------------------------------------------------------------
# LLMNarrative
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMNarrative:
    """One narrative record from the Research Analyst."""

    narrative_id: str
    source_kind: str
    source_id: str
    source_hash: str
    model: str
    prompt_hash: str
    narrative: str
    generated_at: str
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))
        self.validate()

    def validate(self) -> None:
        if not self.narrative_id:
            raise ValueError("narrative_id is required")
        if self.source_kind not in KNOWN_KINDS:
            raise ValueError(
                f"unknown source_kind: {self.source_kind!r} "
                f"(expected one of {KNOWN_KINDS})"
            )
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.source_hash:
            raise ValueError("source_hash is required")
        if not self.model:
            raise ValueError("model is required")
        if not self.prompt_hash:
            raise ValueError("prompt_hash is required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "narrative_id": self.narrative_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "narrative": self.narrative,
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
        }

    def stable_hash(self) -> str:
        data = self.to_dict()
        data.pop("generated_at", None)
        return stable_hash(data)


# ---------------------------------------------------------------------------
# ResearchAnalyst
# ---------------------------------------------------------------------------


class ResearchAnalyst:
    """Composes prompts and produces narratives from Phase 3/4 evidence.

    Accepts any client with a ``chat(prompt, system_prompt, model=None)``
    method and an ``effective_model(explicit=None)`` accessor.  In
    production callers supply a :class:`LocalLLMClient`; tests inject a
    fake with fixed responses for byte-identical rerun assertions.
    """

    def __init__(
        self,
        llm_client: Any,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        if not system_prompt:
            raise ValueError("system_prompt is required")
        # Belt-and-suspenders: refuse to run with a system prompt that
        # does not contain the "recommend a trade" prohibition clause.
        if "recommend a trade" not in system_prompt.lower():
            raise ValueError(
                "system_prompt must forbid trading recommendations "
                "(missing 'recommend a trade' clause)"
            )
        self._client = llm_client
        self._system_prompt = system_prompt

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def analyze(
        self,
        source_kind: str,
        source_id: str,
        source_hash: str,
        source_payload: Mapping[str, Any],
        explicit_model: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> LLMNarrative:
        prompt = compose_analyst_prompt(
            source_kind, source_id, source_hash, source_payload
        )
        prompt_hex = _prompt_hash(prompt)
        narrative_text = self._client.chat(
            prompt=prompt,
            system_prompt=self._system_prompt,
            model=explicit_model,
        )
        warnings = _detect_forbidden_output(narrative_text)
        model_used = self._client.effective_model(explicit=explicit_model)
        return LLMNarrative(
            narrative_id=_narrative_id(prompt_hex, source_hash),
            source_kind=source_kind,
            source_id=source_id,
            source_hash=source_hash,
            model=model_used,
            prompt_hash=prompt_hex,
            narrative=narrative_text,
            generated_at=generated_at or _utc_now_iso(),
            warnings=tuple(warnings),
        )

    # ----- convenience methods per source kind -----

    def analyze_comparison(
        self,
        comparison: Any,
        explicit_model: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> LLMNarrative:
        return self.analyze(
            source_kind=KIND_COMPARISON,
            source_id=comparison.metadata.run_id,
            source_hash=comparison.stable_hash(),
            source_payload=comparison.to_dict(),
            explicit_model=explicit_model,
            generated_at=generated_at,
        )

    def analyze_walk_forward(
        self,
        wf_report: Any,
        explicit_model: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> LLMNarrative:
        return self.analyze(
            source_kind=KIND_WALK_FORWARD,
            source_id=wf_report.report_id,
            source_hash=wf_report.stable_hash(),
            source_payload=wf_report.to_dict(),
            explicit_model=explicit_model,
            generated_at=generated_at,
        )

    def analyze_learning_report(
        self,
        report: Any,
        explicit_model: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> LLMNarrative:
        return self.analyze(
            source_kind=KIND_LEARNING,
            source_id=report.report_id,
            source_hash=report.stable_hash(),
            source_payload=report.payload,
            explicit_model=explicit_model,
            generated_at=generated_at,
        )

    def analyze_research_report(
        self,
        report: Any,
        explicit_model: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> LLMNarrative:
        return self.analyze(
            source_kind=KIND_RESEARCH_REPORT,
            source_id=report.report_id,
            source_hash=report.stable_hash(),
            source_payload=report.payload,
            explicit_model=explicit_model,
            generated_at=generated_at,
        )

    def analyze_promotion_report(
        self,
        promotion_report: Any,
        explicit_model: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> LLMNarrative:
        return self.analyze(
            source_kind=KIND_PROMOTION,
            source_id=promotion_report.report_id,
            source_hash=promotion_report.stable_hash(),
            source_payload=promotion_report.to_dict(),
            explicit_model=explicit_model,
            generated_at=generated_at,
        )


# ---------------------------------------------------------------------------
# Report bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchAnalystReportPaths:
    output_dir: str
    report_id: str

    @property
    def report_dir(self) -> str:
        return str(Path(self.output_dir) / self.report_id)

    @property
    def markdown_path(self) -> str:
        return str(Path(self.report_dir) / ANALYST_REPORT_FILENAME_MARKDOWN)

    @property
    def json_path(self) -> str:
        return str(Path(self.report_dir) / ANALYST_REPORT_FILENAME_JSON)

    @property
    def narrative_path(self) -> str:
        return str(Path(self.report_dir) / ANALYST_REPORT_FILENAME_NARRATIVE)

    @property
    def manifest_path(self) -> str:
        return str(Path(self.report_dir) / ANALYST_REPORT_FILENAME_MANIFEST)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "report_id": self.report_id,
            "report_dir": self.report_dir,
            "markdown_path": self.markdown_path,
            "json_path": self.json_path,
            "narrative_path": self.narrative_path,
            "manifest_path": self.manifest_path,
        }


@dataclass
class ResearchAnalystReport:
    report_id: str
    title: str
    narrative: LLMNarrative
    markdown: str
    payload: Dict[str, Any]
    manifest: Dict[str, Any]
    generated_at: str

    def to_markdown(self) -> str:
        return self.markdown

    def to_json(self) -> str:
        return stable_json(self.payload)

    def narrative_json(self) -> str:
        return stable_json(self.narrative.to_dict())

    def manifest_json(self) -> str:
        return stable_json(self.manifest)

    def stable_hash(self) -> str:
        data = {
            "title": self.title,
            "payload": self.payload,
            "narrative": {
                key: value
                for key, value in self.narrative.to_dict().items()
                if key != "generated_at"
            },
            "manifest": {
                key: value
                for key, value in self.manifest.items()
                if key != "generated_at"
            },
        }
        return stable_hash(data)

    def paths_for(self, output_dir: str) -> ResearchAnalystReportPaths:
        return ResearchAnalystReportPaths(
            output_dir=output_dir, report_id=self.report_id
        )

    def write(self, output_dir: str) -> ResearchAnalystReportPaths:
        paths = self.paths_for(output_dir)
        Path(paths.report_dir).mkdir(parents=True, exist_ok=True)
        Path(paths.markdown_path).write_text(self.markdown, encoding="utf-8")
        Path(paths.json_path).write_text(
            json.dumps(self.payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        Path(paths.narrative_path).write_text(
            json.dumps(self.narrative.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        Path(paths.manifest_path).write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return paths


def _render_markdown(
    report_id: str,
    title: str,
    generated_at: str,
    narrative: LLMNarrative,
) -> str:
    lines: List[str] = [
        f"# {title}",
        "",
        f"_Report ID: `{report_id}`_",
        f"_Generated: {generated_at}_",
        "",
        "Read-only research narrative. Does not recommend trades, enable feature flags, or approve promotions.",
        "",
        "## Provenance",
        f"- Source kind: `{narrative.source_kind}`",
        f"- Source id: `{narrative.source_id}`",
        f"- Source hash: `{narrative.source_hash}`",
        f"- Model: `{narrative.model}`",
        f"- Prompt hash: `{narrative.prompt_hash}`",
        f"- Narrative id: `{narrative.narrative_id}`",
        "",
    ]
    if narrative.warnings:
        lines.append("## Analyst Warnings")
        for warning in narrative.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    lines.extend(
        [
            "## Narrative",
            "",
            narrative.narrative.strip(),
            "",
        ]
    )
    return "\n".join(lines)


def render_analyst_report(
    narrative: LLMNarrative,
    title: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> ResearchAnalystReport:
    generated = generated_at or _utc_now_iso()
    resolved_title = title or f"Research Analyst — {narrative.source_kind}"
    report_id = _analyst_report_id(
        narrative.narrative_id, narrative.source_hash
    )
    manifest = {
        "report_id": report_id,
        "narrative_id": narrative.narrative_id,
        "source_kind": narrative.source_kind,
        "source_id": narrative.source_id,
        "source_hash": narrative.source_hash,
        "model": narrative.model,
        "prompt_hash": narrative.prompt_hash,
        "warnings_count": len(narrative.warnings),
        "generated_at": generated,
    }
    payload = {
        "report_id": report_id,
        "title": resolved_title,
        "narrative": narrative.to_dict(),
        "manifest": {
            key: value
            for key, value in manifest.items()
            if key != "generated_at"
        },
    }
    markdown = _render_markdown(
        report_id=report_id,
        title=resolved_title,
        generated_at=generated,
        narrative=narrative,
    )
    return ResearchAnalystReport(
        report_id=report_id,
        title=resolved_title,
        narrative=narrative,
        markdown=markdown,
        payload=payload,
        manifest=manifest,
        generated_at=generated,
    )
