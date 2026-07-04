"""Tests for strategy/compact_analyst_payloads.py."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from strategy.compact_analyst_payloads import (
    DEFAULT_MAX_DISAGREEMENTS,
    DEFAULT_MAX_FINDINGS,
    DEFAULT_MAX_PROMPT_CHARS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    CompactAnalystSource,
    CompactPayloadLimits,
    compact_bundle_payload,
    compact_comparison_payload,
    compact_learning_payload,
    compact_walk_forward_payload,
    enforce_prompt_size,
)


# ---------------------------------------------------------------------------
# Limits validation
# ---------------------------------------------------------------------------


class TestLimits:
    def test_defaults_match_documented_targets(self):
        limits = CompactPayloadLimits()
        assert limits.max_disagreements == DEFAULT_MAX_DISAGREEMENTS
        assert limits.max_findings == DEFAULT_MAX_FINDINGS
        assert limits.max_prompt_chars == DEFAULT_MAX_PROMPT_CHARS
        assert limits.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
        # Card DoD: max_prompt_chars around 20k, output tokens 1200
        assert limits.max_prompt_chars <= 25_000
        assert limits.max_output_tokens <= 1_500

    @pytest.mark.parametrize(
        "field",
        [
            "max_disagreements",
            "max_findings",
            "max_prompt_chars",
            "max_output_tokens",
            "max_warning_sample",
        ],
    )
    def test_non_positive_rejected(self, field):
        with pytest.raises(ValueError, match=field):
            CompactPayloadLimits(**{field: 0})


# ---------------------------------------------------------------------------
# Comparison compaction
# ---------------------------------------------------------------------------


def _big_disagreements(n: int) -> List[Dict[str, Any]]:
    return [
        {
            "event_timestamp": f"2020-05-{(i % 30) + 1:02d}T14:30:00+00:00",
            "symbol": f"S{i}",
            "kind": "score_delta",
            "champion_score": 0.5,
            "challenger_score": 0.5 - 0.01 * i,
            "score_delta": -0.01 * i,
            "rank_delta": 0,
            "champion_explanation": "long champion explanation " * 20,
            "challenger_explanation": "long challenger explanation " * 20,
        }
        for i in range(n)
    ]


class TestCompactComparison:
    def test_top_n_disagreements_deterministic(self):
        full = {
            "metadata": {"run_id": "cc_x", "champion_id": "champ", "challenger_id": "chal"},
            "disagreements": _big_disagreements(100),
            "warnings": ["warn a", "warn b"],
        }
        compact = compact_comparison_payload(
            full, artifact_id="cc_x", limits=CompactPayloadLimits(max_disagreements=5)
        )
        assert compact["total_disagreements"] == 100
        assert len(compact["top_disagreements"]) == 5
        # Ordering: highest |score_delta| first
        # i=99 -> |-0.99|, i=98 -> |-0.98|, ...
        assert compact["top_disagreements"][0]["symbol"] == "S99"
        # Second call returns identical output
        again = compact_comparison_payload(
            full, artifact_id="cc_x", limits=CompactPayloadLimits(max_disagreements=5)
        )
        assert again == compact

    def test_artifact_id_preserved_not_body(self):
        full = {
            "metadata": {"run_id": "cc_x"},
            "disagreements": _big_disagreements(3),
            "score_tables": [{"huge": "body"}],  # would inflate
        }
        compact = compact_comparison_payload(full, artifact_id="cc_x")
        assert compact["artifact_id"] == "cc_x"
        # Full score_tables NOT embedded
        assert "score_tables" not in compact

    def test_warnings_summarized_not_verbatim(self):
        full = {
            "metadata": {},
            "disagreements": [],
            "warnings": ["rs data missing"] * 40,
        }
        compact = compact_comparison_payload(full, artifact_id="cc_x")
        assert compact["warnings"]["total"] == 40
        assert compact["warnings"]["unique"] == 1

    def test_dataset_provenance_preserved(self):
        full = {
            "metadata": {},
            "disagreements": [],
            "dataset_provenance": {
                "source": "warehouse",
                "datasets": [{"dataset_id": "d", "version": "1"}],
            },
        }
        compact = compact_comparison_payload(full, artifact_id="cc_x")
        assert compact["dataset_provenance"]["source"] == "warehouse"


# ---------------------------------------------------------------------------
# Walk-forward compaction
# ---------------------------------------------------------------------------


class TestCompactWalkForward:
    def test_pulls_summary_and_top_disagreements(self):
        full = {
            "report_id": "wf_x",
            "summary": {
                "split_count": 2,
                "total_events": 42,
                "total_disagreements": 63,
                "in_sample_days": 30,
                "out_of_sample_days": 15,
                "score_delta_threshold": 0.01,
                "disagreement_counts": {"score_delta": 63},
                "warnings": [],
            },
            "split_results": [
                {
                    "comparison": {
                        "disagreements": _big_disagreements(30),
                    }
                },
                {
                    "comparison": {
                        "disagreements": _big_disagreements(30),
                    }
                },
            ],
        }
        compact = compact_walk_forward_payload(
            full, artifact_id="wf_x",
            limits=CompactPayloadLimits(max_disagreements=5),
        )
        assert compact["artifact_id"] == "wf_x"
        assert compact["summary"]["split_count"] == 2
        assert len(compact["top_disagreements"]) == 5


# ---------------------------------------------------------------------------
# Learning compaction
# ---------------------------------------------------------------------------


class TestCompactLearning:
    def test_top_findings_by_sample_size(self):
        full = {
            "report_id": "lr_x",
            "title": "Learning Report — x",
            "summary": {
                "finding_count": 20, "hypothesis_count": 0,
                "importance_count": 0, "recommendation_count": 0,
                "finding_labels": {"validated": 20, "hypothesis": 0},
            },
            "statistical_findings": [
                {
                    "metric": f"m{i}",
                    "label": "validated",
                    "effect_size": 0.0,
                    "sample_size": i,
                    "ci_low": 0.0,
                    "ci_high": 1.0,
                    "detail": "long detail " * 100,
                }
                for i in range(20)
            ],
        }
        compact = compact_learning_payload(
            full, artifact_id="lr_x",
            limits=CompactPayloadLimits(max_findings=3),
        )
        assert len(compact["top_findings"]) == 3
        # Highest sample_size first
        assert compact["top_findings"][0]["metric"] == "m19"

    def test_preserves_explanation_summary(self):
        full = {
            "report_id": "lr_x",
            "title": "x",
            "summary": {},
            "statistical_findings": [],
            "explanation_summary": {"champion": {"observation_count": 126}},
        }
        compact = compact_learning_payload(full, artifact_id="lr_x")
        assert compact["explanation_summary"]["champion"]["observation_count"] == 126


# ---------------------------------------------------------------------------
# Bundle summary
# ---------------------------------------------------------------------------


class TestCompactBundle:
    def test_bundle_summary_covers_key_fields(self):
        bundle_dict = {
            "config_hash": "abc",
            "dataset_manifest_path": "research_data/manifests/x.json",
            "comparison_report_id": "rr_a",
            "walk_forward_report_id": "wf_a",
            "walk_forward_research_report_id": "rr_b",
            "learning_report_id": "lr_a",
            "analyst_report_ids": ["ra_1", "ra_2", "ra_3"],
            "warnings": ["w1", "w2"],
            "live_fetch_used": True,
            "dataset_provenance": {"source": "warehouse"},
            "promotion_entry": {
                "flag_name": "enable_relative_strength",
                "current_state": "disabled",
                "evidence": {"dataset_provenance_id": "warehouse:d@1"},
                "approvals": [],
                "notes": "x",
            },
            "generated_at": "2026-07-04T00:00:00+00:00",
        }
        compact = compact_bundle_payload(bundle_dict)
        assert compact["promotion_evidence"]["current_state"] == "disabled"
        assert compact["promotion_evidence"]["approvals"] == 0
        assert compact["analyst_report_ids"] == ["ra_1", "ra_2", "ra_3"]
        assert (
            compact["promotion_evidence"]["dataset_provenance_id"]
            == "warehouse:d@1"
        )


# ---------------------------------------------------------------------------
# Prompt size enforcement
# ---------------------------------------------------------------------------


class TestEnforcePromptSize:
    def test_truncates_when_over_limit(self):
        full = {
            "kind": "champion_challenger_comparison_compact",
            "artifact_id": "cc_x",
            "top_disagreements": _big_disagreements(50),
            "top_findings": [],
        }
        limits = CompactPayloadLimits(
            max_disagreements=50, max_findings=1,
            max_prompt_chars=5_000,
        )
        result = enforce_prompt_size(full, limits)
        assert len(json.dumps(result)) <= limits.max_prompt_chars + 200  # small overshoot allowed for warning notes
        assert result.get("truncation_warnings")

    def test_no_change_when_under_limit(self):
        full = {
            "kind": "champion_challenger_comparison_compact",
            "artifact_id": "cc_x",
            "top_disagreements": [],
            "top_findings": [],
        }
        limits = CompactPayloadLimits(max_prompt_chars=50_000)
        result = enforce_prompt_size(full, limits)
        assert "truncation_warnings" not in result
        assert result == full

    def test_default_bundle_under_20k(self):
        # Realistic full comparison payload
        full = {
            "metadata": {"run_id": "cc_x", "champion_id": "c", "challenger_id": "d"},
            "disagreements": _big_disagreements(200),
            "warnings": ["rs data missing"] * 42,
            "dataset_provenance": {"source": "warehouse", "datasets": []},
        }
        compact = compact_comparison_payload(full, artifact_id="cc_x")
        enforced = enforce_prompt_size(compact, CompactPayloadLimits())
        size = len(json.dumps(enforced, sort_keys=True))
        # DoD: default limit target ≈ 20k
        assert size < 25_000


# ---------------------------------------------------------------------------
# CompactAnalystSource shim
# ---------------------------------------------------------------------------


class _FakeComparison:
    """Minimal double for ChampionChallengerComparison covering the
    surface the shim delegates.
    """

    def __init__(self, disagreements: List[Dict[str, Any]]) -> None:
        self._d = disagreements
        self.metadata = "opaque-metadata-object"

    def stable_hash(self) -> str:
        return "aaaa" * 16

    def to_analyst_payload(self) -> Dict[str, Any]:
        return {
            "metadata": {"run_id": "cc_x"},
            "disagreements": self._d,
            "warnings": [],
            "score_tables": [{"long": "body"} for _ in range(100)],
        }


class TestCompactAnalystSource:
    def test_shim_returns_compact_via_to_analyst_payload(self):
        src = _FakeComparison(_big_disagreements(30))
        wrapped = CompactAnalystSource(
            src, kind="comparison",
            artifact_id="cc_x",
            artifact_path="reports/validation/backtests/cc_x",
            limits=CompactPayloadLimits(max_disagreements=5),
        )
        payload = wrapped.to_analyst_payload()
        assert payload["kind"] == "champion_challenger_comparison_compact"
        assert payload["artifact_id"] == "cc_x"
        assert "score_tables" not in payload

    def test_shim_delegates_metadata_and_hash(self):
        src = _FakeComparison([])
        wrapped = CompactAnalystSource(
            src, kind="comparison", artifact_id="cc_x",
        )
        assert wrapped.metadata == "opaque-metadata-object"
        assert wrapped.stable_hash() == "aaaa" * 16

    def test_unknown_kind_rejected(self):
        src = _FakeComparison([])
        wrapped = CompactAnalystSource(
            src, kind="unknown-kind", artifact_id="cc_x",
        )
        with pytest.raises(ValueError, match="compact kind"):
            wrapped.to_analyst_payload()


# ---------------------------------------------------------------------------
# Forbidden-output detection preserved
# ---------------------------------------------------------------------------


class TestForbiddenOutputStillDetected:
    def test_forbidden_output_detection_lives_downstream(self):
        # This card doesn't modify research_analyst.py.  Prove the
        # existing forbidden-output pipeline is untouched by
        # importing the constants and confirming their presence.
        from strategy.research_analyst import (
            FORBIDDEN_OUTPUT_PATTERNS,
            FORBIDDEN_OUTPUT_HEADINGS,
            _detect_forbidden_output,
        )
        assert len(FORBIDDEN_OUTPUT_PATTERNS) > 0
        assert len(FORBIDDEN_OUTPUT_HEADINGS) > 0
        # And detection actually flags a canonical forbidden phrase
        assert _detect_forbidden_output(
            "you should buy AAPL immediately"
        )


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.compact_analyst_payloads as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        source = _module_source()
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import", "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_order_path_tokens(self):
        source = _module_source()
        for token in (
            "submit_order", "place_order", "cancel_order",
            "TradingClient",
        ):
            assert token not in source

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source
