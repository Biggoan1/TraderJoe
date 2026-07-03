"""Model resolution helpers.

Resolves an AI model identifier for a given execution context via a
three-tier precedence chain:

    1. Explicit caller-provided value
    2. Context-specific environment variable
    3. Caller-provided default (may be ``None``)

The helper is pure — it never assumes a specific model name and never
mutates ``os.environ``.  Callers that want to preserve legacy
behavior supply their own default.

This module exists so future Hermes integration can pass an explicit
model argument without requiring an "active model" environment
variable.  Do not assume Hermes exports one.

Terminology: this module uses "validation", "replay", and "research".
It does not call this "training" — Phase 5 evaluation work is
validation, replay, or research.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, Optional, Tuple


CONTEXT_PAPER = "paper"
CONTEXT_CRYPTO = "crypto"
CONTEXT_RESEARCH = "research"
CONTEXT_PRODUCTION = "production"

KNOWN_CONTEXTS: Tuple[str, ...] = (
    CONTEXT_PAPER,
    CONTEXT_CRYPTO,
    CONTEXT_RESEARCH,
    CONTEXT_PRODUCTION,
)

# Context → env-var name mapping.  Each context reads exactly one env
# var — never falls back to another context's variable.
CONTEXT_MODEL_ENV: Dict[str, str] = {
    CONTEXT_PAPER: "PAPER_AI_MODEL",
    CONTEXT_CRYPTO: "CRYPTO_AI_MODEL",
    CONTEXT_RESEARCH: "RESEARCH_AI_MODEL",
    CONTEXT_PRODUCTION: "PRODUCTION_AI_MODEL",
}


class UnknownContextError(ValueError):
    """Raised when an unknown execution context is passed to a resolver."""


def env_var_for(context: str) -> str:
    """Return the model env-var name for ``context``."""
    if context not in CONTEXT_MODEL_ENV:
        raise UnknownContextError(
            f"unknown context: {context!r} "
            f"(expected one of {KNOWN_CONTEXTS})"
        )
    return CONTEXT_MODEL_ENV[context]


def resolve_model(
    context: str,
    explicit: Optional[str] = None,
    default: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve an AI model identifier for ``context``.

    Precedence:

    1. ``explicit`` if truthy
    2. ``env[CONTEXT_MODEL_ENV[context]]`` if present and truthy
    3. ``default`` (may be ``None``)

    ``env`` defaults to :data:`os.environ`.  Passing a mapping is the
    idiomatic way to keep tests hermetic — the resolver never mutates
    the supplied env.
    """
    if context not in CONTEXT_MODEL_ENV:
        raise UnknownContextError(
            f"unknown context: {context!r} "
            f"(expected one of {KNOWN_CONTEXTS})"
        )
    if explicit:
        return explicit
    env_source = env if env is not None else os.environ
    env_var = CONTEXT_MODEL_ENV[context]
    value = env_source.get(env_var)
    if value:
        return value
    return default
