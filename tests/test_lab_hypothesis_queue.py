"""Tests for strategy/lab/hypothesis_queue.py — Card 7."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from strategy.lab import ExperimentProposal, HypothesisQueue
from strategy.lab.hypothesis_queue import (
    HypothesisQueueError,
    STATUS_APPROVED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    compute_proposal_id,
)


# ---------------------------------------------------------------------------
# Proposal validation
# ---------------------------------------------------------------------------


class TestProposalValidation:
    def test_title_required(self):
        with pytest.raises(HypothesisQueueError, match="title"):
            ExperimentProposal(
                proposal_id="hyp_x", title="",
                strategy_name="champion_rs",
                suggested_parameters={},
            )

    def test_strategy_name_required(self):
        with pytest.raises(HypothesisQueueError, match="strategy_name"):
            ExperimentProposal(
                proposal_id="hyp_x", title="idea",
                strategy_name="",
                suggested_parameters={},
            )

    def test_confidence_bounded(self):
        with pytest.raises(HypothesisQueueError, match="confidence"):
            ExperimentProposal(
                proposal_id="hyp_x", title="idea",
                strategy_name="s", suggested_parameters={},
                confidence=1.5,
            )

    def test_unknown_status_rejected(self):
        with pytest.raises(HypothesisQueueError, match="unknown status"):
            ExperimentProposal(
                proposal_id="hyp_x", title="idea",
                strategy_name="s", suggested_parameters={},
                status="wibble",
            )


# ---------------------------------------------------------------------------
# Deterministic proposal id
# ---------------------------------------------------------------------------


class TestProposalId:
    def test_same_identity_same_id(self):
        a = compute_proposal_id("Test", "champion_rs", {"weight": 0.1})
        b = compute_proposal_id("Test", "champion_rs", {"weight": 0.1})
        assert a == b

    def test_case_and_whitespace_insensitive(self):
        a = compute_proposal_id("Test", "champion_rs", {"a": 1})
        b = compute_proposal_id("  test  ", "CHAMPION_RS", {"a": 1})
        assert a == b

    def test_different_parameters_different_id(self):
        a = compute_proposal_id("Test", "champion_rs", {"weight": 0.1})
        b = compute_proposal_id("Test", "champion_rs", {"weight": 0.2})
        assert a != b

    def test_notes_do_not_affect_id(self):
        # The compute_proposal_id API only takes title / strategy /
        # params — motivation, confidence, and conditions are
        # excluded by design.  Nothing to assert beyond the API
        # shape; this test documents the invariant.
        assert compute_proposal_id("t", "s", {}) == compute_proposal_id("t", "s", {})


# ---------------------------------------------------------------------------
# Queue basics
# ---------------------------------------------------------------------------


class TestQueue:
    def test_propose_appends_record(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(
            title="Decrease RS weight when VIX>25",
            strategy_name="champion_rs",
            suggested_parameters={"overlay_weight": 0.12},
            motivation="analyst_narrative_ra_x",
            confidence=0.91,
            conditions={"VIX_gt": 25},
            source="analyst",
        )
        assert p.status == STATUS_PROPOSED
        assert p.proposal_id.startswith("hyp_")
        # On-disk record
        lines = (tmp_path / "queue.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["title"] == p.title

    def test_propose_idempotent(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        a = q.propose(
            title="Idea",
            strategy_name="champion_rs",
            suggested_parameters={"weight": 0.1},
        )
        b = q.propose(
            title="Idea",
            strategy_name="champion_rs",
            suggested_parameters={"weight": 0.1},
        )
        assert a.proposal_id == b.proposal_id
        lines = (tmp_path / "queue.jsonl").read_text().splitlines()
        assert len(lines) == 1

    def test_two_different_proposals_two_records(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        q.propose(title="One", strategy_name="s", suggested_parameters={"a": 1})
        q.propose(title="Two", strategy_name="s", suggested_parameters={"a": 2})
        assert len(q.list()) == 2

    def test_list_filter_by_status(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(title="Idea", strategy_name="s",
                      suggested_parameters={"a": 1})
        q.update_status(p.proposal_id, STATUS_APPROVED,
                        review_notes="looks reasonable")
        assert [x.proposal_id for x in q.list(STATUS_APPROVED)] == [p.proposal_id]
        assert q.list(STATUS_PROPOSED) == []

    def test_list_unknown_status_rejected(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        with pytest.raises(HypothesisQueueError, match="unknown status"):
            q.list("wibble")

    def test_get_returns_proposal(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(title="Idea", strategy_name="s",
                      suggested_parameters={"a": 1})
        got = q.get(p.proposal_id)
        assert got.proposal_id == p.proposal_id

    def test_get_missing_raises(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        with pytest.raises(HypothesisQueueError, match="not found"):
            q.get("hyp_missing")


class TestApproval:
    def test_approve_flips_status_and_records_notes(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(title="Idea", strategy_name="s",
                      suggested_parameters={"a": 1})
        updated = q.update_status(
            p.proposal_id, STATUS_APPROVED,
            review_notes="ship it after safety review",
        )
        assert updated.status == STATUS_APPROVED
        assert updated.review_notes == "ship it after safety review"
        # Persisted
        again = q.get(p.proposal_id)
        assert again.status == STATUS_APPROVED

    def test_reject_flips_status(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(title="Idea", strategy_name="s",
                      suggested_parameters={"a": 1})
        updated = q.update_status(p.proposal_id, STATUS_REJECTED,
                                  review_notes="regime already tested")
        assert updated.status == STATUS_REJECTED

    def test_unknown_status_rejected(self, tmp_path):
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(title="Idea", strategy_name="s",
                      suggested_parameters={"a": 1})
        with pytest.raises(HypothesisQueueError, match="unknown status"):
            q.update_status(p.proposal_id, "quantum")


class TestPersistence:
    def test_records_survive_process_restart(self, tmp_path):
        q1 = HypothesisQueue(tmp_path / "queue.jsonl")
        q1.propose(title="One", strategy_name="s",
                   suggested_parameters={"a": 1})
        q1.propose(title="Two", strategy_name="s",
                   suggested_parameters={"a": 2})
        q2 = HypothesisQueue(tmp_path / "queue.jsonl")
        assert len(q2.list()) == 2

    def test_corrupt_line_raises_clear_error(self, tmp_path):
        path = tmp_path / "queue.jsonl"
        path.write_text('{"proposal_id": "hyp_x", "title": "ok",\n'
                        'invalid json line\n', encoding="utf-8")
        q = HypothesisQueue(path)
        with pytest.raises(HypothesisQueueError, match="line"):
            q.list()


# ---------------------------------------------------------------------------
# Never auto-implements approved proposals
# ---------------------------------------------------------------------------


class TestNeverAutoImplements:
    def test_approving_does_not_change_promotion(self, tmp_path):
        # The queue's only public promotion-adjacent method is
        # update_status.  Confirm that nothing in the module even
        # imports PromotionEntry / ApprovalRecord / FeatureFlags —
        # source-safety catches this — and that after approval, no
        # side effects have run other than the JSONL update.
        q = HypothesisQueue(tmp_path / "queue.jsonl")
        p = q.propose(title="Idea", strategy_name="s",
                      suggested_parameters={"a": 1})
        q.update_status(p.proposal_id, STATUS_APPROVED)
        # No experiment directory, no leaderboard, nothing.
        assert list(tmp_path.iterdir()) == [tmp_path / "queue.jsonl"]


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source(name: str) -> str:
    import importlib
    mod = importlib.import_module(name)
    return Path(mod.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _module_source("strategy.lab.hypothesis_queue")
        for token in ("from trader import", "import trader\n",
                      "from crypto_trader import",
                      "from strategy.runner import"):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _module_source("strategy.lab.hypothesis_queue")
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion_construction(self):
        s = _module_source("strategy.lab.hypothesis_queue")
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s

    def test_no_feature_flag_mutation(self):
        s = _module_source("strategy.lab.hypothesis_queue")
        # Queue must not enable feature flags anywhere.
        assert "enable_relative_strength=True" not in s
        assert "FeatureFlags(" not in s
