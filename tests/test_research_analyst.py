"""Tests for strategy/research_analyst.py."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from strategy.config import reset_feature_flags
from strategy.research_analyst import (
    ANALYST_REPORT_FILENAME_JSON,
    ANALYST_REPORT_FILENAME_MANIFEST,
    ANALYST_REPORT_FILENAME_MARKDOWN,
    ANALYST_REPORT_FILENAME_NARRATIVE,
    ANALYST_REPORT_ID_PREFIX,
    CLOUD_MODEL_TOKENS,
    DEFAULT_ANALYST_OUTPUT_DIR,
    DEFAULT_LOCAL_ENDPOINT,
    FORBIDDEN_OUTPUT_HEADINGS,
    FORBIDDEN_OUTPUT_PATTERNS,
    KIND_COMPARISON,
    KIND_LEARNING,
    KIND_PROMOTION,
    KIND_RESEARCH_REPORT,
    KIND_WALK_FORWARD,
    KNOWN_KINDS,
    LOOPBACK_HOSTS,
    LocalLLMClient,
    LocalLLMConfigError,
    LocalLLMEndpointError,
    LocalLLMRequestError,
    LLMNarrative,
    MODEL_LISTING_PATHS,
    ModelDiscoveryError,
    ModelNotAvailableError,
    NARRATIVE_ID_PREFIX,
    REQUIRED_TEMPERATURE,
    RESEARCH_LLM_ENDPOINT_ENV,
    ResearchAnalyst,
    ResearchAnalystReport,
    ResearchAnalystReportPaths,
    SYSTEM_PROMPT,
    compose_analyst_prompt,
    render_analyst_report,
    verify_model_available,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    """Deterministic LLM stand-in for tests."""

    response: str = "## Executive Summary\nfake analyst output"
    model_id: str = "local-model:latest"
    calls: List[Dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.calls = []

    def effective_model(self, explicit: Optional[str] = None) -> str:
        return explicit or self.model_id

    def chat(
        self,
        prompt: str,
        system_prompt: str,
        model: Optional[str] = None,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "model": model}
        )
        return self.response


class _StubSource:
    """Duck-typed source object for the convenience analyzers."""

    def __init__(self, run_id="run_fixture", stable="src_hash", payload=None):
        self._run_id = run_id
        self._stable = stable
        self._payload = payload or {"metric": "score_delta_mean", "n": 40}

    @property
    def report_id(self):
        return self._run_id

    @property
    def metadata(self):
        class M:
            pass

        m = M()
        m.run_id = self._run_id
        return m

    @property
    def payload(self):
        return self._payload

    def to_dict(self):
        return self._payload

    def stable_hash(self):
        return self._stable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_endpoint_is_loopback(self):
        assert DEFAULT_LOCAL_ENDPOINT.startswith("http://")
        assert "127.0.0.1" in DEFAULT_LOCAL_ENDPOINT

    def test_loopback_hosts_cover_expected(self):
        assert "127.0.0.1" in LOOPBACK_HOSTS
        assert "localhost" in LOOPBACK_HOSTS
        assert "::1" in LOOPBACK_HOSTS

    def test_temperature_pinned_to_zero(self):
        assert REQUIRED_TEMPERATURE == 0.0

    def test_cloud_model_tokens_cover_expected(self):
        for token in ("openai", "anthropic", "google", "azure", "aws"):
            assert token in CLOUD_MODEL_TOKENS

    def test_known_kinds(self):
        assert KIND_COMPARISON in KNOWN_KINDS
        assert KIND_WALK_FORWARD in KNOWN_KINDS
        assert KIND_LEARNING in KNOWN_KINDS
        assert KIND_RESEARCH_REPORT in KNOWN_KINDS
        assert KIND_PROMOTION in KNOWN_KINDS


class TestSystemPrompt:
    def test_forbids_recommending_a_trade(self):
        assert "recommend a trade" in SYSTEM_PROMPT.lower()

    def test_forbids_flag_advocacy(self):
        assert "feature flag" in SYSTEM_PROMPT.lower()

    def test_forbids_promotion_advocacy(self):
        assert "promotion" in SYSTEM_PROMPT.lower()

    def test_mandates_the_six_sections(self):
        for section in (
            "Executive Summary",
            "What the Evidence Says",
            "Strengths",
            "Weaknesses",
            "Statistical Caveats",
            "What This Evidence Cannot Answer",
        ):
            assert section in SYSTEM_PROMPT

    def test_forbids_recommendation_section(self):
        lower = SYSTEM_PROMPT.lower()
        assert "do not add a \"recommendation\" section" in lower


# ---------------------------------------------------------------------------
# LocalLLMClient — endpoint / model / temperature
# ---------------------------------------------------------------------------


class TestLocalLLMEndpointGuard:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://127.0.0.1:11434",
            "http://localhost:11434",
            "https://127.0.0.1:8080",
            "http://[::1]:11434",
        ],
    )
    def test_loopback_endpoints_accepted(self, endpoint):
        LocalLLMClient(
            endpoint=endpoint, model="local-model", http_post=lambda u, b, t: b"{}"
        )

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://8.8.8.8:11434",
            "https://api.openai.com/v1",
            "http://myserver.example.com",
            "http://0.0.0.0:11434",  # "any" is not a loopback
            "http://192.168.1.10",
        ],
    )
    def test_non_loopback_endpoints_rejected(self, endpoint):
        with pytest.raises(LocalLLMEndpointError):
            LocalLLMClient(
                endpoint=endpoint, model="local-model"
            )

    def test_missing_scheme_rejected(self):
        with pytest.raises(LocalLLMEndpointError):
            LocalLLMClient(endpoint="127.0.0.1:11434", model="local-model")

    def test_endpoint_env_var_used_when_no_explicit(self, monkeypatch):
        monkeypatch.setenv(
            RESEARCH_LLM_ENDPOINT_ENV, "http://localhost:9000"
        )
        client = LocalLLMClient(model="local-model")
        assert client.endpoint == "http://localhost:9000"

    def test_default_endpoint_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(RESEARCH_LLM_ENDPOINT_ENV, raising=False)
        client = LocalLLMClient(model="local-model", env={})
        assert client.endpoint == DEFAULT_LOCAL_ENDPOINT


class TestLocalLLMModelGuard:
    @pytest.mark.parametrize(
        "model",
        [
            "openai/gpt-4",
            "anthropic-claude-3",
            "google-gemini",
            "azure-something",
            "aws-titan",
            "OPENAI_MODEL",
        ],
    )
    def test_cloud_tokens_rejected_at_construction(self, model):
        with pytest.raises(LocalLLMConfigError, match="cloud token"):
            LocalLLMClient(
                endpoint="http://127.0.0.1:11434", model=model
            )

    def test_cloud_tokens_rejected_at_effective_model(self):
        client = LocalLLMClient(endpoint="http://127.0.0.1:11434")
        with pytest.raises(LocalLLMConfigError):
            client.effective_model(explicit="openai/gpt-4")

    def test_no_model_resolved_raises(self, monkeypatch):
        monkeypatch.delenv("RESEARCH_AI_MODEL", raising=False)
        client = LocalLLMClient(endpoint="http://127.0.0.1:11434", env={})
        with pytest.raises(LocalLLMConfigError, match="no model resolved"):
            client.effective_model()

    def test_explicit_model_wins_over_default_and_env(self):
        env = {"RESEARCH_AI_MODEL": "env-model"}
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="default-model",
            env=env,
        )
        assert client.effective_model(explicit="hermes-model") == "hermes-model"

    def test_env_model_wins_over_default(self):
        env = {"RESEARCH_AI_MODEL": "env-model"}
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="default-model",
            env=env,
        )
        assert client.effective_model() == "env-model"

    def test_default_used_when_no_env_or_explicit(self):
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="default-model",
            env={},
        )
        assert client.effective_model() == "default-model"


class TestLocalLLMTemperatureAndTimeout:
    @pytest.mark.parametrize("temp", [0.1, 0.5, -0.1, 1.0])
    def test_non_zero_temperature_rejected(self, temp):
        with pytest.raises(LocalLLMConfigError, match="temperature"):
            LocalLLMClient(
                endpoint="http://127.0.0.1:11434",
                model="local-model",
                temperature=temp,
            )

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_non_positive_timeout_rejected(self, timeout):
        with pytest.raises(LocalLLMConfigError, match="timeout"):
            LocalLLMClient(
                endpoint="http://127.0.0.1:11434",
                model="local-model",
                timeout=timeout,
            )


# ---------------------------------------------------------------------------
# LocalLLMClient — chat wire behaviour
# ---------------------------------------------------------------------------


class TestLocalLLMChat:
    def _http_returning(self, response):
        calls = []

        def post(url, body, timeout):
            calls.append({"url": url, "body": body, "timeout": timeout})
            return json.dumps(response).encode("utf-8")

        return post, calls

    def test_ollama_shape_response(self):
        post, calls = self._http_returning({"message": {"content": "hi"}})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="local-model",
            http_post=post,
        )
        assert client.chat("prompt", "system") == "hi"
        assert calls[0]["url"] == "http://127.0.0.1:11434/api/chat"
        # temperature enforced in the wire body
        assert calls[0]["body"]["options"]["temperature"] == 0.0
        assert calls[0]["body"]["stream"] is False
        assert calls[0]["body"]["messages"][0]["role"] == "system"

    def test_openai_compat_shape_response(self):
        post, _ = self._http_returning(
            {"choices": [{"message": {"content": "hello"}}]}
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="local-model",
            http_post=post,
        )
        assert client.chat("p", "s") == "hello"

    def test_missing_content_raises(self):
        post, _ = self._http_returning({"message": {"role": "assistant"}})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="local-model",
            http_post=post,
        )
        with pytest.raises(LocalLLMRequestError, match="content"):
            client.chat("p", "s")

    def test_non_json_body_raises(self):
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="local-model",
            http_post=lambda u, b, t: b"not json",
        )
        with pytest.raises(LocalLLMRequestError, match="JSON"):
            client.chat("p", "s")

    def test_transport_exception_wrapped(self):
        def post(url, body, timeout):
            raise RuntimeError("boom")

        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="local-model",
            http_post=post,
        )
        with pytest.raises(LocalLLMRequestError, match="failed"):
            client.chat("p", "s")

    def test_explicit_model_overrides_default(self):
        post, calls = self._http_returning({"message": {"content": "ok"}})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="default-model",
            http_post=post,
        )
        client.chat("p", "s", model="alt-model")
        assert calls[0]["body"]["model"] == "alt-model"


# ---------------------------------------------------------------------------
# Prompt composer
# ---------------------------------------------------------------------------


class TestComposeAnalystPrompt:
    def test_prompt_is_deterministic_across_calls(self):
        payload = {"a": 1, "b": {"c": 2}}
        first = compose_analyst_prompt(
            KIND_COMPARISON, "run-1", "hash-1", payload
        )
        second = compose_analyst_prompt(
            KIND_COMPARISON, "run-1", "hash-1", payload
        )
        assert first == second

    def test_prompt_carries_source_metadata(self):
        prompt = compose_analyst_prompt(
            KIND_WALK_FORWARD, "wf-run", "wf-hash", {"foo": "bar"}
        )
        assert KIND_WALK_FORWARD in prompt
        assert "wf-run" in prompt
        assert "wf-hash" in prompt

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="unknown source_kind"):
            compose_analyst_prompt("mystery", "id", "hash", {})

    def test_missing_source_id_rejected(self):
        with pytest.raises(ValueError, match="source_id"):
            compose_analyst_prompt(KIND_COMPARISON, "", "hash", {})

    def test_missing_source_hash_rejected(self):
        with pytest.raises(ValueError, match="source_hash"):
            compose_analyst_prompt(KIND_COMPARISON, "id", "", {})


# ---------------------------------------------------------------------------
# LLMNarrative
# ---------------------------------------------------------------------------


class TestLLMNarrative:
    def _narrative(self, **overrides):
        base = dict(
            narrative_id="nar_abc",
            source_kind=KIND_COMPARISON,
            source_id="cc_123",
            source_hash="s_hash",
            model="local-model",
            prompt_hash="p_hash",
            narrative="## Executive Summary\nrecap",
            generated_at="2026-07-03T12:00:00+00:00",
        )
        base.update(overrides)
        return LLMNarrative(**base)

    def test_roundtrip(self):
        n = self._narrative()
        d = n.to_dict()
        assert d["source_kind"] == KIND_COMPARISON
        json.dumps(d)

    def test_stable_hash_excludes_generated_at(self):
        first = self._narrative(generated_at="2026-07-03T12:00:00+00:00")
        second = self._narrative(generated_at="2027-01-01T00:00:00+00:00")
        assert first.stable_hash() == second.stable_hash()

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"narrative_id": ""}, "narrative_id"),
            ({"source_kind": "bogus"}, "unknown source_kind"),
            ({"source_id": ""}, "source_id"),
            ({"source_hash": ""}, "source_hash"),
            ({"model": ""}, "model"),
            ({"prompt_hash": ""}, "prompt_hash"),
        ],
    )
    def test_validation_errors(self, overrides, error):
        with pytest.raises(ValueError, match=error):
            self._narrative(**overrides)


# ---------------------------------------------------------------------------
# ResearchAnalyst
# ---------------------------------------------------------------------------


class TestResearchAnalystConstruction:
    def test_default_system_prompt_used(self):
        analyst = ResearchAnalyst(FakeLLMClient())
        assert analyst.system_prompt is SYSTEM_PROMPT

    def test_empty_system_prompt_rejected(self):
        with pytest.raises(ValueError, match="system_prompt"):
            ResearchAnalyst(FakeLLMClient(), system_prompt="")

    def test_permissive_system_prompt_rejected(self):
        # A prompt that omits the "recommend a trade" clause must be
        # refused so a caller can't accidentally strip the guardrail.
        loose_prompt = "You are a research analyst. Be helpful."
        with pytest.raises(ValueError, match="recommend a trade"):
            ResearchAnalyst(FakeLLMClient(), system_prompt=loose_prompt)


class TestResearchAnalystAnalyze:
    def test_analyze_returns_narrative_with_source_metadata(self):
        client = FakeLLMClient(response="## Executive Summary\nok")
        analyst = ResearchAnalyst(client)
        narrative = analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc_abc",
            source_hash="s_hash",
            source_payload={"metric": "score"},
            generated_at="2026-07-03T12:00:00+00:00",
        )
        assert narrative.source_kind == KIND_COMPARISON
        assert narrative.source_id == "cc_abc"
        assert narrative.source_hash == "s_hash"
        assert narrative.narrative_id.startswith(f"{NARRATIVE_ID_PREFIX}_")
        assert narrative.narrative == "## Executive Summary\nok"

    def test_prompt_matches_compose_helper(self):
        client = FakeLLMClient()
        analyst = ResearchAnalyst(client)
        analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc_abc",
            source_hash="s_hash",
            source_payload={"metric": "score"},
        )
        expected_prompt = compose_analyst_prompt(
            KIND_COMPARISON, "cc_abc", "s_hash", {"metric": "score"}
        )
        assert client.calls[0]["prompt"] == expected_prompt

    def test_system_prompt_delivered_to_client(self):
        client = FakeLLMClient()
        analyst = ResearchAnalyst(client)
        analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc_abc",
            source_hash="s_hash",
            source_payload={"metric": "score"},
        )
        assert client.calls[0]["system_prompt"] is SYSTEM_PROMPT

    def test_narrative_id_deterministic_across_calls(self):
        client = FakeLLMClient()
        analyst = ResearchAnalyst(client)
        first = analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc",
            source_hash="h",
            source_payload={"x": 1},
            generated_at="2026-07-03T12:00:00+00:00",
        )
        second = analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc",
            source_hash="h",
            source_payload={"x": 1},
            generated_at="2027-01-01T00:00:00+00:00",
        )
        assert first.narrative_id == second.narrative_id
        assert first.prompt_hash == second.prompt_hash
        assert first.stable_hash() == second.stable_hash()

    def test_explicit_model_propagates_to_client(self):
        client = FakeLLMClient(model_id="fallback")
        analyst = ResearchAnalyst(client)
        narrative = analyst.analyze(
            source_kind=KIND_LEARNING,
            source_id="lr",
            source_hash="lh",
            source_payload={"a": 1},
            explicit_model="hermes-model",
        )
        assert client.calls[0]["model"] == "hermes-model"
        assert narrative.model == "hermes-model"

    def test_forbidden_output_flagged_as_warning(self):
        # LLM tried to recommend an action; must be caught in warnings.
        client = FakeLLMClient(
            response="## Executive Summary\nyou should buy the AAPL breakout."
        )
        analyst = ResearchAnalyst(client)
        narrative = analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc",
            source_hash="h",
            source_payload={"x": 1},
        )
        assert any(
            "you should buy" in w.lower() for w in narrative.warnings
        )

    def test_forbidden_heading_flagged_as_warning(self):
        client = FakeLLMClient(
            response=(
                "## Executive Summary\nrecap\n\n"
                "## Recommendation\nenable enable_relative_strength"
            )
        )
        analyst = ResearchAnalyst(client)
        narrative = analyst.analyze(
            source_kind=KIND_LEARNING,
            source_id="lr",
            source_hash="h",
            source_payload={"a": 1},
        )
        assert any("## recommendation" in w.lower() for w in narrative.warnings)

    def test_clean_output_has_no_warnings(self):
        clean = (
            "## Executive Summary\nThe evidence shows a moderate positive "
            "score delta with wide confidence bounds.\n\n"
            "## What the Evidence Says\n...\n\n"
            "## Strengths\n...\n\n"
            "## Weaknesses\nSmall sample.\n\n"
            "## Statistical Caveats\nMultiple-testing risk.\n\n"
            "## What This Evidence Cannot Answer\nOut-of-sample durability."
        )
        client = FakeLLMClient(response=clean)
        analyst = ResearchAnalyst(client)
        narrative = analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc",
            source_hash="h",
            source_payload={"x": 1},
        )
        assert narrative.warnings == ()


class TestResearchAnalystConvenience:
    def test_analyze_comparison_unpacks_metadata(self):
        analyst = ResearchAnalyst(FakeLLMClient())
        source = _StubSource(run_id="cc_run", stable="cc_hash")
        narrative = analyst.analyze_comparison(source)
        assert narrative.source_kind == KIND_COMPARISON
        assert narrative.source_id == "cc_run"
        assert narrative.source_hash == "cc_hash"

    def test_analyze_walk_forward_uses_report_id(self):
        analyst = ResearchAnalyst(FakeLLMClient())
        source = _StubSource(run_id="wf_report", stable="wf_hash")
        narrative = analyst.analyze_walk_forward(source)
        assert narrative.source_kind == KIND_WALK_FORWARD
        assert narrative.source_id == "wf_report"

    def test_analyze_learning_report_uses_payload_attribute(self):
        analyst = ResearchAnalyst(FakeLLMClient())
        source = _StubSource(run_id="lr", stable="lh", payload={"summary": True})
        narrative = analyst.analyze_learning_report(source)
        assert narrative.source_kind == KIND_LEARNING

    def test_analyze_research_report_uses_payload_attribute(self):
        analyst = ResearchAnalyst(FakeLLMClient())
        source = _StubSource(run_id="rr", stable="rh", payload={"foo": "bar"})
        narrative = analyst.analyze_research_report(source)
        assert narrative.source_kind == KIND_RESEARCH_REPORT

    def test_analyze_promotion_report_uses_report_id(self):
        analyst = ResearchAnalyst(FakeLLMClient())
        source = _StubSource(run_id="pg_run", stable="pg_hash")
        narrative = analyst.analyze_promotion_report(source)
        assert narrative.source_kind == KIND_PROMOTION


# ---------------------------------------------------------------------------
# Report bundle
# ---------------------------------------------------------------------------


class TestReportPaths:
    def test_paths_derive_from_output_dir_and_report_id(self):
        paths = ResearchAnalystReportPaths(
            output_dir="/tmp/out", report_id="ra_abc"
        )
        d = paths.to_dict()
        assert d["report_dir"].endswith("ra_abc")
        assert d["markdown_path"].endswith(f"ra_abc/{ANALYST_REPORT_FILENAME_MARKDOWN}")
        assert d["json_path"].endswith(f"ra_abc/{ANALYST_REPORT_FILENAME_JSON}")
        assert d["narrative_path"].endswith(
            f"ra_abc/{ANALYST_REPORT_FILENAME_NARRATIVE}"
        )
        assert d["manifest_path"].endswith(f"ra_abc/{ANALYST_REPORT_FILENAME_MANIFEST}")
        json.dumps(d)


class TestRenderAnalystReport:
    def _narrative(self, warnings=(), narrative="## Executive Summary\nrecap"):
        return LLMNarrative(
            narrative_id="nar_abc",
            source_kind=KIND_COMPARISON,
            source_id="cc_123",
            source_hash="s_hash",
            model="local-model",
            prompt_hash="p_hash",
            narrative=narrative,
            generated_at="2026-07-03T12:00:00+00:00",
            warnings=warnings,
        )

    def test_report_id_prefix(self):
        report = render_analyst_report(self._narrative())
        assert report.report_id.startswith(f"{ANALYST_REPORT_ID_PREFIX}_")
        assert len(report.report_id) == 3 + 12  # prefix + 12-char hash

    def test_default_title_names_source_kind(self):
        report = render_analyst_report(self._narrative())
        assert KIND_COMPARISON in report.title

    def test_custom_title(self):
        report = render_analyst_report(self._narrative(), title="Custom")
        assert report.title == "Custom"

    def test_json_payload_schema(self):
        report = render_analyst_report(self._narrative())
        payload = json.loads(report.to_json())
        assert payload["report_id"] == report.report_id
        assert "title" in payload
        assert "narrative" in payload
        assert "manifest" in payload
        assert payload["narrative"]["source_kind"] == KIND_COMPARISON

    def test_manifest_carries_reproducibility_metadata(self):
        report = render_analyst_report(self._narrative())
        manifest = json.loads(report.manifest_json())
        for key in (
            "report_id",
            "narrative_id",
            "source_kind",
            "source_id",
            "source_hash",
            "model",
            "prompt_hash",
        ):
            assert key in manifest

    def test_markdown_contains_key_sections(self):
        report = render_analyst_report(self._narrative())
        md = report.to_markdown()
        assert "# Research Analyst" in md
        assert "## Provenance" in md
        assert "## Narrative" in md
        assert "Read-only research narrative" in md

    def test_markdown_shows_warnings_when_present(self):
        report = render_analyst_report(
            self._narrative(warnings=("LLM tried to recommend buying",))
        )
        assert "## Analyst Warnings" in report.to_markdown()

    def test_markdown_omits_warnings_section_when_clean(self):
        report = render_analyst_report(self._narrative())
        assert "## Analyst Warnings" not in report.to_markdown()

    def test_stable_hash_ignores_generated_at(self):
        first = render_analyst_report(
            self._narrative(), generated_at="2026-07-03T12:00:00+00:00"
        )
        second = render_analyst_report(
            self._narrative(), generated_at="2027-01-01T00:00:00+00:00"
        )
        assert first.stable_hash() == second.stable_hash()
        assert first.report_id == second.report_id


class TestReportWrite:
    def _narrative(self):
        return LLMNarrative(
            narrative_id="nar_abc",
            source_kind=KIND_COMPARISON,
            source_id="cc_123",
            source_hash="s_hash",
            model="local-model",
            prompt_hash="p_hash",
            narrative="## Executive Summary\nrecap",
            generated_at="2026-07-03T12:00:00+00:00",
        )

    def test_write_creates_four_files(self, tmp_path):
        report = render_analyst_report(
            self._narrative(), generated_at="2026-07-03T12:00:00+00:00"
        )
        paths = report.write(str(tmp_path))
        assert Path(paths.markdown_path).is_file()
        assert Path(paths.json_path).is_file()
        assert Path(paths.narrative_path).is_file()
        assert Path(paths.manifest_path).is_file()

    def test_write_files_are_valid_json(self, tmp_path):
        report = render_analyst_report(self._narrative())
        paths = report.write(str(tmp_path))
        for p in (paths.json_path, paths.narrative_path, paths.manifest_path):
            json.loads(Path(p).read_text(encoding="utf-8"))

    def test_write_is_idempotent(self, tmp_path):
        report = render_analyst_report(
            self._narrative(), generated_at="2026-07-03T12:00:00+00:00"
        )
        report.write(str(tmp_path))
        report.write(str(tmp_path))
        paths = report.paths_for(str(tmp_path))
        assert (
            Path(paths.markdown_path).read_text(encoding="utf-8")
            == report.markdown
        )

    def test_write_lands_under_report_id_dir(self, tmp_path):
        report = render_analyst_report(self._narrative())
        paths = report.write(str(tmp_path))
        assert Path(paths.report_dir).name == report.report_id


class TestByteIdenticalReruns:
    def test_repeat_render_produces_byte_identical_output(self):
        narrative = LLMNarrative(
            narrative_id="nar_abc",
            source_kind=KIND_COMPARISON,
            source_id="cc",
            source_hash="h",
            model="local-model",
            prompt_hash="ph",
            narrative="## Executive Summary\ntext",
            generated_at="2026-07-03T12:00:00+00:00",
        )
        first = render_analyst_report(
            narrative, generated_at="2026-07-03T12:00:00+00:00"
        )
        second = render_analyst_report(
            narrative, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert first.to_json() == second.to_json()
        assert first.to_markdown() == second.to_markdown()
        assert first.narrative_json() == second.narrative_json()
        assert first.manifest_json() == second.manifest_json()


# ---------------------------------------------------------------------------
# Source-safety guarantees
# ---------------------------------------------------------------------------


class TestSourceSafety:
    def _source(self) -> str:
        import strategy.research_analyst as module

        return Path(module.__file__).read_text(encoding="utf-8")

    def test_no_order_path_references(self):
        source = self._source()
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
                f"research_analyst must not reference {token!r}"
            )

    def test_no_cloud_llm_sdk_imports(self):
        source = self._source()
        for token in [
            "import openai",
            "from openai",
            "import anthropic",
            "from anthropic",
            "import google.generativeai",
            "from google.generativeai",
        ]:
            assert token not in source, (
                f"research_analyst must not import cloud LLM SDK: {token!r}"
            )

    def test_never_imports_strategy_config(self):
        source = self._source()
        assert "from strategy.config" not in source
        assert "import strategy.config" not in source

    def test_never_imports_live_runner_modules(self):
        source = self._source()
        for token in [
            "from trader import",
            "from crypto_trader import",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ]:
            assert token not in source

    def test_terminology_avoids_training(self):
        source = self._source()
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 5 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.research_analyst", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.research_analyst  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {"trader_cli", "trader", "crypto_trader", "telegram_approvals"}
        assert not (added & forbidden)

    def test_module_never_mutates_global_feature_flags(self):
        flags = reset_feature_flags()
        analyst = ResearchAnalyst(FakeLLMClient())
        narrative = analyst.analyze(
            source_kind=KIND_COMPARISON,
            source_id="cc",
            source_hash="h",
            source_payload={"a": 1},
        )
        render_analyst_report(narrative)
        assert flags.all_disabled is True
        assert flags.enabled_flags == []


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def _http_get_returning(paths_to_responses):
    """Build a fake http_get that returns bytes per requested path."""
    calls: List[str] = []

    def _get(url: str, timeout: float) -> bytes:
        calls.append(url)
        for suffix, response in paths_to_responses.items():
            if url.endswith(suffix):
                if isinstance(response, Exception):
                    raise response
                return response
        raise urllib.error.URLError(f"no fake response for {url}")

    return _get, calls


import urllib.error  # noqa: E402 (placed here for the helper above)


class TestListModelsPaths:
    def test_openai_compat_shape(self):
        response = json.dumps(
            {"data": [{"id": "llama3.1"}, {"id": "gpt-oss-20b"}]}
        ).encode()
        get, calls = _http_get_returning({"/v1/models": response})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="llama3.1",
            http_get=get,
        )
        assert client.list_models() == ["gpt-oss-20b", "llama3.1"]
        assert calls == ["http://127.0.0.1:11434/v1/models"]

    def test_ollama_shape_fallback_when_v1_fails(self):
        get, calls = _http_get_returning(
            {
                "/v1/models": urllib.error.HTTPError(
                    "http://127.0.0.1:11434/v1/models",
                    404,
                    "not found",
                    {},
                    None,
                ),
                "/api/tags": json.dumps(
                    {"models": [{"name": "mistral:7b"}, {"name": "phi3:mini"}]}
                ).encode(),
            }
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="mistral:7b",
            http_get=get,
        )
        assert client.list_models() == ["mistral:7b", "phi3:mini"]
        assert calls[0].endswith("/v1/models")
        assert calls[1].endswith("/api/tags")

    def test_returns_empty_when_endpoint_exposes_no_listing(self):
        response = json.dumps({"data": []}).encode()
        get, _ = _http_get_returning({"/v1/models": response})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="anything",
            http_get=get,
        )
        assert client.list_models() == []

    def test_all_paths_failing_raises_discovery_error(self):
        error = urllib.error.URLError("connection refused")
        get, calls = _http_get_returning(
            {"/v1/models": error, "/api/tags": error}
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="anything",
            http_get=get,
        )
        with pytest.raises(ModelDiscoveryError):
            client.list_models()
        # Both paths were attempted before giving up.
        assert any(url.endswith("/v1/models") for url in calls)
        assert any(url.endswith("/api/tags") for url in calls)

    def test_non_json_response_causes_next_path(self):
        # First path returns garbage, second returns a valid list.
        get, calls = _http_get_returning(
            {
                "/v1/models": b"not json",
                "/api/tags": json.dumps(
                    {"models": [{"name": "phi3:mini"}]}
                ).encode(),
            }
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="phi3:mini",
            http_get=get,
        )
        assert client.list_models() == ["phi3:mini"]

    def test_list_is_deterministic_sorted(self):
        response = json.dumps(
            {"data": [{"id": "z-model"}, {"id": "a-model"}, {"id": "a-model"}]}
        ).encode()
        get, _ = _http_get_returning({"/v1/models": response})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="a-model",
            http_get=get,
        )
        # Sorted + deduped
        assert client.list_models() == ["a-model", "z-model"]


class TestListModelsSchemas:
    def test_extract_model_names_handles_missing_dicts(self):
        # Payload not shaped like either OpenAI-compat or Ollama.
        get, _ = _http_get_returning(
            {"/v1/models": json.dumps({"foo": "bar"}).encode()}
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="m",
            http_get=get,
        )
        assert client.list_models() == []

    def test_extract_model_names_skips_bad_entries(self):
        payload = {
            "data": [
                {"id": "good-1"},
                "not a dict",
                {"name": "good-2"},  # fallback name key
                {"id": ""},
                {"id": None},
            ]
        }
        get, _ = _http_get_returning(
            {"/v1/models": json.dumps(payload).encode()}
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="good-1",
            http_get=get,
        )
        # Only the two well-formed entries survive.
        assert client.list_models() == ["good-1", "good-2"]


class TestListModelsLoopbackOnly:
    def test_non_loopback_endpoint_never_constructs_client(self):
        # Endpoint guard triggers before list_models can be reached.
        with pytest.raises(LocalLLMEndpointError):
            LocalLLMClient(
                endpoint="http://8.8.8.8", model="local-model"
            )


class TestVerifyModelAvailable:
    def _client(self, response_map):
        get, _ = _http_get_returning(response_map)
        return LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="configured-model",
            http_get=get,
        )

    def test_returns_available_and_none_when_configured_present(self):
        client = self._client(
            {
                "/v1/models": json.dumps(
                    {"data": [{"id": "configured-model"}, {"id": "other"}]}
                ).encode()
            }
        )
        available, closest = verify_model_available(client, "configured-model")
        assert available == ["configured-model", "other"]
        assert closest is None

    def test_raises_with_configured_endpoint_available_when_absent(self):
        client = self._client(
            {
                "/v1/models": json.dumps(
                    {"data": [{"id": "llama3.1"}, {"id": "mistral:7b"}]}
                ).encode()
            }
        )
        with pytest.raises(ModelNotAvailableError) as excinfo:
            verify_model_available(client, "llama3-1")  # near miss
        exc = excinfo.value
        assert exc.configured == "llama3-1"
        assert exc.endpoint == "http://127.0.0.1:11434"
        assert exc.available == ("llama3.1", "mistral:7b")
        # Closest is the numerically-closest model name
        assert exc.closest == "llama3.1"
        # Message includes all four fields the operator needs
        text = str(exc)
        assert "llama3-1" in text
        assert "http://127.0.0.1:11434" in text
        assert "llama3.1" in text
        assert "closest match" in text
        # Explicit "will NOT silently substitute" clause
        assert "NOT silently substitute" in text

    def test_never_silently_substitutes(self):
        """Even when a very close match exists, verify raises."""
        client = self._client(
            {"/v1/models": json.dumps({"data": [{"id": "llama3.1"}]}).encode()}
        )
        with pytest.raises(ModelNotAvailableError):
            verify_model_available(client, "llama-3.1")

    def test_falls_back_when_discovery_fails(self):
        """If every listing path fails at transport, verify returns
        gracefully instead of raising, so the caller falls back to
        the configured model without behavior change.
        """
        error = urllib.error.URLError("connection refused")
        get, _ = _http_get_returning(
            {"/v1/models": error, "/api/tags": error}
        )
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="anything",
            http_get=get,
        )
        available, closest = verify_model_available(client, "anything")
        assert available == []
        assert closest is None

    def test_falls_back_when_endpoint_lists_no_models(self):
        client = self._client(
            {"/v1/models": json.dumps({"data": []}).encode()}
        )
        available, closest = verify_model_available(client, "anything")
        assert available == []
        assert closest is None

    def test_refuses_cloud_model_names(self):
        client = self._client(
            {"/v1/models": json.dumps({"data": [{"id": "llama"}]}).encode()}
        )
        with pytest.raises(LocalLLMConfigError, match="cloud token"):
            verify_model_available(client, "openai-gpt-4")

    def test_available_list_is_deterministic(self):
        response = json.dumps(
            {"data": [{"id": "z-model"}, {"id": "a-model"}]}
        ).encode()
        get, _ = _http_get_returning({"/v1/models": response})
        client = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="a-model",
            http_get=get,
        )
        available_1, _ = verify_model_available(client, "a-model")
        get2, _ = _http_get_returning({"/v1/models": response})
        client2 = LocalLLMClient(
            endpoint="http://127.0.0.1:11434",
            model="a-model",
            http_get=get2,
        )
        available_2, _ = verify_model_available(client2, "a-model")
        assert available_1 == available_2


class TestModelListingPathsConstant:
    def test_expected_paths(self):
        assert MODEL_LISTING_PATHS[0] == "/v1/models"
        assert MODEL_LISTING_PATHS[1] == "/api/tags"
