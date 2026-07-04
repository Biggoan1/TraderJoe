"""Tests for strategy/research_run_manifest.py."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, List

import pytest

from strategy.config import reset_feature_flags
from strategy.research_run_manifest import (
    ArtifactRef,
    ResearchRunManifest,
    _redact_url,
    build_from_bundle,
    write,
)


# ---------------------------------------------------------------------------
# ResearchRunManifest — roundtrip + hash
# ---------------------------------------------------------------------------


def _make_manifest(**overrides: Any) -> ResearchRunManifest:
    base = dict(
        run_id="run_x_2026_07_04",
        dataset_id="watchlist-2020",
        dataset_version="1",
        symbols=("AAPL", "MSFT", "NVDA"),
        benchmarks=("SPY", "QQQ"),
        window_start="2020-05-04",
        window_end="2020-07-03",
        provenance_source="warehouse",
        provenance_summary="watchlist-2020@1",
        config_hash="abc" * 20,
        git_commit="deadbeef",
        model_id="qwen3.6-27b",
        llm_endpoint_redacted="http://10.100.0.13:8080",
        feature_flags_all_disabled=True,
        feature_flags_enabled=(),
        promotion_state="disabled",
        promotion_approvals=0,
        comparison_artifact=ArtifactRef(
            kind="comparison", artifact_id="rr_x", path="backtests/rr_x",
        ),
        walk_forward_artifact=ArtifactRef(
            kind="walk_forward", artifact_id="wf_x", path="",
        ),
        learning_artifact=ArtifactRef(
            kind="learning", artifact_id="lr_x", path="learning/lr_x",
        ),
        analyst_artifacts=(
            ArtifactRef(kind="analyst", artifact_id="ra_1", path="analyst/ra_1"),
            ArtifactRef(kind="analyst", artifact_id="ra_2", path="analyst/ra_2"),
        ),
        dataset_manifest_path="research_data/manifests/watchlist-2020.json",
        warnings=(),
        started_at="2026-07-04T00:00:00+00:00",
        completed_at="2026-07-04T00:20:00+00:00",
    )
    base.update(overrides)
    return ResearchRunManifest(**base)


class TestRoundtrip:
    def test_to_dict_shape(self):
        m = _make_manifest()
        d = m.to_dict()
        assert d["run_id"] == "run_x_2026_07_04"
        assert d["comparison_artifact"]["artifact_id"] == "rr_x"
        assert d["analyst_artifacts"][1]["artifact_id"] == "ra_2"

    def test_json_serializes_deterministically(self):
        m = _make_manifest()
        a = m.to_json()
        b = m.to_json()
        assert a == b


class TestStableHash:
    def test_hash_stable_across_timestamp_changes(self):
        a = _make_manifest(started_at="2026-07-04T00:00:00+00:00")
        b = _make_manifest(started_at="2026-08-15T12:34:56+00:00")
        assert a.stable_hash() == b.stable_hash()

    def test_hash_changes_with_content(self):
        a = _make_manifest()
        b = _make_manifest(run_id="run_y_2026_07_04")
        assert a.stable_hash() != b.stable_hash()


# ---------------------------------------------------------------------------
# Secrets never in serialization
# ---------------------------------------------------------------------------


class TestNoSecrets:
    def test_llm_endpoint_stripped_of_credentials(self):
        raw = "http://user:supersecret@10.100.0.13:8080/v1?api_key=leaked"
        redacted = _redact_url(raw)
        assert "supersecret" not in redacted
        assert "leaked" not in redacted
        assert "***" in redacted

    def test_manifest_dict_contains_no_credential_shapes(self):
        m = _make_manifest(
            llm_endpoint_redacted=_redact_url(
                "http://user:supersecret@10.100.0.13:8080"
            )
        )
        blob = json.dumps(m.to_dict())
        # Alpaca-style key shape refuse
        assert not re.search(r"PK[A-Z0-9]{18,32}", blob)
        assert "supersecret" not in blob

    def test_markdown_never_shows_credentials(self):
        m = _make_manifest(
            llm_endpoint_redacted=_redact_url(
                "https://user:mypass@endpoint.example/v1?api_key=leak"
            )
        )
        text = m.to_markdown()
        assert "mypass" not in text
        assert "leak" not in text


# ---------------------------------------------------------------------------
# write() + latest pointer
# ---------------------------------------------------------------------------


class TestWrite:
    def test_writes_json_and_markdown(self, tmp_path):
        m = _make_manifest()
        paths = write(m, tmp_path)
        assert paths["json"].is_file()
        assert paths["markdown"].is_file()
        loaded = json.loads(paths["json"].read_text())
        assert loaded["run_id"] == m.run_id

    def test_writes_latest_pointer_by_default(self, tmp_path):
        m = _make_manifest()
        paths = write(m, tmp_path)
        assert paths["latest_json"].is_file()
        assert paths["latest_markdown"].is_file()
        # Latest content matches the versioned copy
        assert paths["latest_json"].read_bytes() == paths["json"].read_bytes()

    def test_disable_latest_pointer(self, tmp_path):
        m = _make_manifest()
        paths = write(m, tmp_path, write_latest_pointer=False)
        assert "latest_json" not in paths
        assert not (tmp_path / "latest.json").exists()


# ---------------------------------------------------------------------------
# build_from_bundle — realistic + missing artifact handling
# ---------------------------------------------------------------------------


class _FakePaths:
    def __init__(self, output_dir: str, report_id: str) -> None:
        self.output_dir = output_dir
        self.report_id = report_id


class _FakeReport:
    def __init__(self, report_id: str) -> None:
        self.report_id = report_id


class _FakePromotion:
    def __init__(self) -> None:
        self.current_state = "disabled"
        self.approvals: List[Any] = []


class _FakeConfig:
    symbols = ("AAPL", "MSFT")
    benchmarks = ("SPY",)
    window_start = "2020-05-01"
    window_end = "2020-05-31"
    def stable_hash(self) -> str:
        return "cfg-hash"


class _FakeBundle:
    def __init__(self, provenance=None, with_analyst=True):
        self.config = _FakeConfig()
        self.dataset_provenance = provenance or {
            "source": "warehouse",
            "datasets": [{"dataset_id": "d", "version": "1"}],
        }
        self.dataset_manifest_path = "research_data/manifests/d.json"
        self.comparison_report = _FakeReport("rr_a")
        self.comparison_report_paths = _FakePaths(
            "reports/validation/backtests", "rr_a"
        )
        self.walk_forward_report = _FakeReport("wf_a")
        self.walk_forward_research_report = _FakeReport("rr_b")
        self.walk_forward_report_paths = _FakePaths(
            "reports/validation/walk_forward", "rr_b"
        )
        self.learning_report = _FakeReport("lr_a")
        self.learning_report_paths = _FakePaths(
            "reports/validation/learning", "lr_a"
        )
        if with_analyst:
            self.analyst_reports = [_FakeReport("ra_1"), _FakeReport("ra_2")]
            self.analyst_report_paths = [
                _FakePaths("reports/validation/analyst", "ra_1"),
                _FakePaths("reports/validation/analyst", "ra_2"),
            ]
        else:
            self.analyst_reports = []
            self.analyst_report_paths = []
        self.promotion_entry = _FakePromotion()
        self.warnings: List[str] = []


class TestBuildFromBundle:
    def test_records_all_artifacts(self):
        reset_feature_flags()
        m = build_from_bundle(
            _FakeBundle(),
            run_id="run_x",
            started_at="2026-07-04T00:00:00+00:00",
            completed_at="2026-07-04T00:20:00+00:00",
            git_commit="abcd1234",
            model_id="qwen3.6-27b",
            llm_endpoint="http://user:pass@endpoint/v1",
        )
        assert m.comparison_artifact.artifact_id == "rr_a"
        assert m.walk_forward_artifact.artifact_id == "wf_a"
        assert m.walk_forward_research_artifact.artifact_id == "rr_b"
        assert m.learning_artifact.artifact_id == "lr_a"
        assert [a.artifact_id for a in m.analyst_artifacts] == ["ra_1", "ra_2"]
        assert m.provenance_source == "warehouse"
        assert m.dataset_id == "d"
        assert m.dataset_version == "1"
        assert m.git_commit == "abcd1234"
        # Endpoint redacted
        assert "pass" not in m.llm_endpoint_redacted
        # Promotion left disabled
        assert m.promotion_state == "disabled"
        assert m.promotion_approvals == 0
        # Feature flags all disabled
        assert m.feature_flags_all_disabled is True

    def test_handles_missing_analyst_gracefully(self):
        reset_feature_flags()
        m = build_from_bundle(
            _FakeBundle(with_analyst=False),
            run_id="run_x",
            started_at="2026-07-04T00:00:00+00:00",
        )
        assert m.analyst_artifacts == ()

    def test_provider_source_when_provenance_says_so(self):
        reset_feature_flags()
        m = build_from_bundle(
            _FakeBundle(
                provenance={"source": "provider", "provider_name": "alpaca"}
            ),
            run_id="run_x",
            started_at="2026-07-04T00:00:00+00:00",
        )
        assert m.provenance_source == "provider"
        assert m.dataset_id == "alpaca"


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.research_run_manifest as m
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

    def test_no_credential_env_reads(self):
        source = _module_source()
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
        ):
            assert not re.search(pattern, source)

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source
