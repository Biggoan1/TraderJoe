"""Tests for strategy/model_config.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from strategy.model_config import (
    CONTEXT_CRYPTO,
    CONTEXT_MODEL_ENV,
    CONTEXT_PAPER,
    CONTEXT_PRODUCTION,
    CONTEXT_RESEARCH,
    KNOWN_CONTEXTS,
    UnknownContextError,
    env_var_for,
    resolve_model,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_known_contexts_covers_expected_values(self):
        assert set(KNOWN_CONTEXTS) == {
            "paper",
            "crypto",
            "research",
            "production",
        }

    def test_context_model_env_maps_each_context(self):
        assert CONTEXT_MODEL_ENV[CONTEXT_PAPER] == "PAPER_AI_MODEL"
        assert CONTEXT_MODEL_ENV[CONTEXT_CRYPTO] == "CRYPTO_AI_MODEL"
        assert CONTEXT_MODEL_ENV[CONTEXT_RESEARCH] == "RESEARCH_AI_MODEL"
        assert CONTEXT_MODEL_ENV[CONTEXT_PRODUCTION] == "PRODUCTION_AI_MODEL"

    def test_context_model_env_covers_all_known_contexts(self):
        assert set(CONTEXT_MODEL_ENV) == set(KNOWN_CONTEXTS)


class TestEnvVarFor:
    @pytest.mark.parametrize(
        "context,expected",
        [
            (CONTEXT_PAPER, "PAPER_AI_MODEL"),
            (CONTEXT_CRYPTO, "CRYPTO_AI_MODEL"),
            (CONTEXT_RESEARCH, "RESEARCH_AI_MODEL"),
            (CONTEXT_PRODUCTION, "PRODUCTION_AI_MODEL"),
        ],
    )
    def test_returns_matching_env_var(self, context, expected):
        assert env_var_for(context) == expected

    def test_unknown_context_raises(self):
        with pytest.raises(UnknownContextError, match="unknown context"):
            env_var_for("staging")


# ---------------------------------------------------------------------------
# resolve_model precedence
# ---------------------------------------------------------------------------


class TestResolveModelPrecedence:
    def test_explicit_arg_wins_over_env_and_default(self):
        env = {"PAPER_AI_MODEL": "env-model"}
        assert (
            resolve_model(
                CONTEXT_PAPER,
                explicit="explicit-model",
                default="default-model",
                env=env,
            )
            == "explicit-model"
        )

    def test_env_var_wins_over_default(self):
        env = {"PAPER_AI_MODEL": "env-model"}
        assert (
            resolve_model(
                CONTEXT_PAPER, default="default-model", env=env
            )
            == "env-model"
        )

    def test_default_returned_when_env_unset(self):
        assert (
            resolve_model(CONTEXT_PAPER, default="default-model", env={})
            == "default-model"
        )

    def test_none_returned_when_nothing_supplied(self):
        assert resolve_model(CONTEXT_PAPER, env={}) is None

    def test_empty_string_env_treated_as_unset(self):
        env = {"PAPER_AI_MODEL": ""}
        assert (
            resolve_model(CONTEXT_PAPER, default="fallback", env=env)
            == "fallback"
        )

    def test_empty_explicit_treated_as_unset(self):
        env = {"PAPER_AI_MODEL": "env-model"}
        assert (
            resolve_model(
                CONTEXT_PAPER, explicit="", default="d", env=env
            )
            == "env-model"
        )


# ---------------------------------------------------------------------------
# Context isolation — each context reads only its own env var
# ---------------------------------------------------------------------------


class TestContextIsolation:
    def test_paper_does_not_read_crypto_env(self):
        env = {"CRYPTO_AI_MODEL": "crypto-model"}
        assert (
            resolve_model(CONTEXT_PAPER, default="paper-default", env=env)
            == "paper-default"
        )

    def test_crypto_does_not_read_paper_env(self):
        env = {"PAPER_AI_MODEL": "paper-model"}
        assert (
            resolve_model(CONTEXT_CRYPTO, default="crypto-default", env=env)
            == "crypto-default"
        )

    def test_research_does_not_read_paper_or_crypto(self):
        env = {"PAPER_AI_MODEL": "p", "CRYPTO_AI_MODEL": "c"}
        assert (
            resolve_model(
                CONTEXT_RESEARCH, default="research-default", env=env
            )
            == "research-default"
        )

    def test_production_does_not_read_other_contexts(self):
        env = {
            "PAPER_AI_MODEL": "p",
            "CRYPTO_AI_MODEL": "c",
            "RESEARCH_AI_MODEL": "r",
        }
        assert (
            resolve_model(
                CONTEXT_PRODUCTION, default="prod-default", env=env
            )
            == "prod-default"
        )

    def test_research_uses_its_own_env(self):
        env = {"RESEARCH_AI_MODEL": "research-model"}
        assert (
            resolve_model(CONTEXT_RESEARCH, default="d", env=env)
            == "research-model"
        )


class TestResolveModelValidation:
    def test_unknown_context_raises(self):
        with pytest.raises(UnknownContextError):
            resolve_model("staging", default="x")


# ---------------------------------------------------------------------------
# Module source safety
# ---------------------------------------------------------------------------


class TestModelConfigSource:
    def _source(self) -> str:
        import strategy.model_config as module

        return Path(module.__file__).read_text(encoding="utf-8")

    def test_no_hardcoded_model_names(self):
        source = self._source()
        # The resolver must not hardcode any specific model name.
        assert "gpt-5-mini" not in source
        assert "gpt-4" not in source
        assert "gpt-3" not in source

    def test_terminology_avoids_training(self):
        source = self._source()
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 5 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped
