"""Tests for strategy/morning_intelligence.py."""

import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.morning_intelligence import (
    DEFAULT_TOP_RS_COUNT,
    MorningIntelligenceAgent,
    MorningIntelligenceRenderer,
    MorningIntelligenceReport,
    get_agent,
)


@pytest.fixture(autouse=True)
def _clean_flags():
    reset_feature_flags()


@dataclass
class StubResult:
    data: dict

    def to_dict(self):
        return dict(self.data)


def _regime_provider(regime="bullish", confidence=0.75):
    provider = MagicMock()
    provider.classify.return_value = StubResult({
        "regime": regime,
        "confidence": confidence,
        "regime_scores": {
            "bullish": confidence if regime == "bullish" else 0.1,
            "bearish": confidence if regime == "bearish" else 0.1,
            "volatile": confidence if regime == "volatile" else 0.1,
            "neutral": 0.1,
        },
        "signals": [],
    })
    return provider


def _overnight_provider(significant=1, missing=0):
    provider = MagicMock()
    provider.assess.return_value = StubResult({
        "symbols_analyzed": 3,
        "symbols_with_data": 3 - missing,
        "symbols_missing_data": missing,
        "significant_gaps": significant,
        "avg_risk_score": 25.0,
        "observations": [],
    })
    return provider


def _rs_provider(results=None):
    provider = MagicMock()
    provider.calculate_watchlist_rs.return_value = [
        StubResult(item) for item in (results or [
            {
                "symbol": "AAPL",
                "rs_score": 82.0,
                "trend_direction": "improving",
                "rs_vs_benchmark": {},
                "percentile_rank": {},
            },
            {
                "symbol": "TSLA",
                "rs_score": 45.0,
                "trend_direction": "declining",
                "rs_vs_benchmark": {},
                "percentile_rank": {},
            },
        ])
    ]
    return provider


class TestMorningIntelligenceReport:
    def test_to_dict_defaults(self):
        report = MorningIntelligenceReport(
            date="2026-07-02",
            timestamp="2026-07-02T08:00:00+00:00",
        )
        d = report.to_dict()

        assert d["date"] == "2026-07-02"
        assert d["watchlist"] == []
        assert d["feature_flag_enabled"] is False
        assert d["data_quality"] == "missing"

    def test_to_dict_is_json_serializable(self):
        report = MorningIntelligenceReport(
            date="2026-07-02",
            timestamp="2026-07-02T08:00:00+00:00",
            watchlist=["AAPL"],
            market_regime={"regime": "bullish"},
            overnight_risk={"significant_gaps": 0},
            relative_strength=[{"symbol": "AAPL", "rs_score": 75.0}],
            key_points=["Market regime: bullish"],
        )
        json.dumps(report.to_dict())

    def test_to_dict_copies_lists(self):
        report = MorningIntelligenceReport(
            date="2026-07-02",
            timestamp="2026-07-02T08:00:00+00:00",
            watchlist=["AAPL"],
            errors=["gap"],
        )
        d = report.to_dict()
        d["watchlist"].append("TSLA")
        d["errors"].append("other")

        assert report.watchlist == ["AAPL"]
        assert report.errors == ["gap"]


class TestMorningIntelligenceRenderer:
    def test_render_includes_sections(self):
        report = MorningIntelligenceReport(
            date="2026-07-02",
            timestamp="2026-07-02T08:00:00+00:00",
            headline="Morning context: bullish",
            key_points=["Market regime: bullish"],
            risks=["1 significant overnight gap"],
            opportunities=["AAPL relative strength improving"],
            errors=["QQQ unavailable"],
            data_quality="partial",
        )
        text = MorningIntelligenceRenderer.render(report)

        assert "# Trade Joe Morning Intelligence" in text
        assert "## Key Points" in text
        assert "## Risks To Watch" in text
        assert "## Areas To Watch" in text
        assert "## Data Gaps" in text
        assert "Data quality: partial" in text

    def test_render_observational_only_notice(self):
        report = MorningIntelligenceReport(
            date="2026-07-02",
            timestamp="2026-07-02T08:00:00+00:00",
        )
        text = MorningIntelligenceRenderer.render(report)
        assert "Observational only" in text
        assert "trading decisions" in text


class TestMorningIntelligenceAgent:
    def test_generate_complete_report_with_mocked_providers(self):
        regime = _regime_provider()
        overnight = _overnight_provider(significant=1)
        rs = _rs_provider()

        agent = MorningIntelligenceAgent(
            watchlist=["AAPL", "TSLA", "MSFT"],
            regime_provider=regime,
            overnight_provider=overnight,
            relative_strength_provider=rs,
        )
        report = agent.generate()

        assert report.watchlist == ["AAPL", "TSLA", "MSFT"]
        assert report.market_regime["regime"] == "bullish"
        assert report.overnight_risk["significant_gaps"] == 1
        assert report.relative_strength[0]["symbol"] == "AAPL"
        assert report.data_quality == "complete"
        assert "Morning context" in report.headline
        regime.classify.assert_called_once_with()
        overnight.assess.assert_called_once_with()
        rs.calculate_watchlist_rs.assert_called_once_with(["AAPL", "TSLA", "MSFT"])

    def test_feature_flag_recorded_but_not_required(self):
        flags = get_feature_flags()
        flags.enable("enable_morning_intelligence")

        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=_rs_provider(),
        )
        report = agent.generate()

        assert report.feature_flag_enabled is True

    def test_feature_flag_default_disabled(self):
        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=_rs_provider(),
        )
        report = agent.generate()

        assert report.feature_flag_enabled is False

    def test_relative_strength_sorted_and_limited(self):
        rs = _rs_provider([
            {"symbol": "LOW", "rs_score": 20.0, "trend_direction": "stable"},
            {"symbol": "HIGH", "rs_score": 90.0, "trend_direction": "improving"},
            {"symbol": "MID", "rs_score": 60.0, "trend_direction": "stable"},
        ])
        agent = MorningIntelligenceAgent(
            watchlist=["LOW", "HIGH", "MID"],
            regime_provider=_regime_provider(),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=rs,
            top_rs_count=2,
        )
        report = agent.generate()

        assert [item["symbol"] for item in report.relative_strength] == ["HIGH", "MID"]

    def test_empty_watchlist_skips_relative_strength(self):
        rs = _rs_provider()
        agent = MorningIntelligenceAgent(
            watchlist=[],
            regime_provider=_regime_provider(),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=rs,
        )
        report = agent.generate()

        assert report.relative_strength == []
        assert "empty watchlist" in report.errors[0]
        rs.calculate_watchlist_rs.assert_not_called()
        assert report.data_quality == "partial"

    def test_provider_failures_are_captured_as_data_gaps(self):
        regime = MagicMock()
        regime.classify.side_effect = RuntimeError("regime offline")
        overnight = MagicMock()
        overnight.assess.side_effect = RuntimeError("overnight offline")
        rs = MagicMock()
        rs.calculate_watchlist_rs.side_effect = RuntimeError("rs offline")

        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=regime,
            overnight_provider=overnight,
            relative_strength_provider=rs,
        )
        report = agent.generate()

        assert report.data_quality == "missing"
        assert len(report.errors) == 3
        assert any("regime offline" in err for err in report.errors)
        assert any("overnight offline" in err for err in report.errors)
        assert any("rs offline" in err for err in report.errors)

    def test_bearish_regime_creates_risk_context(self):
        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(regime="bearish", confidence=0.8),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=_rs_provider(),
        )
        report = agent.generate()

        assert any("Bearish market regime" in risk for risk in report.risks)

    def test_volatile_regime_creates_risk_context(self):
        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(regime="volatile", confidence=0.8),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=_rs_provider(),
        )
        report = agent.generate()

        assert any("Volatile market regime" in risk for risk in report.risks)

    def test_significant_gaps_create_risk_context(self):
        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(),
            overnight_provider=_overnight_provider(significant=2, missing=1),
            relative_strength_provider=_rs_provider(),
        )
        report = agent.generate()

        assert "2 significant overnight gap(s)" in report.risks
        assert "1 symbol(s) missing overnight data" in report.risks

    def test_bullish_regime_and_improving_rs_create_watch_context(self):
        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(regime="bullish", confidence=0.8),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=_rs_provider(),
        )
        report = agent.generate()

        assert "Bullish regime context" in report.opportunities
        assert any("AAPL relative strength improving" in item for item in report.opportunities)

    def test_render_method_generates_text(self):
        agent = MorningIntelligenceAgent(
            watchlist=["AAPL"],
            regime_provider=_regime_provider(),
            overnight_provider=_overnight_provider(significant=0),
            relative_strength_provider=_rs_provider(),
        )
        text = agent.render()

        assert "Trade Joe Morning Intelligence" in text
        assert "Data quality: complete" in text

    def test_get_agent_defaults(self):
        agent = get_agent(watchlist=["AAPL"])

        assert isinstance(agent, MorningIntelligenceAgent)
        assert agent.watchlist == ["AAPL"]
        assert agent.top_rs_count == DEFAULT_TOP_RS_COUNT

    def test_top_rs_count_cannot_be_negative(self):
        agent = MorningIntelligenceAgent(top_rs_count=-5)

        assert agent.top_rs_count == 0

    def test_no_trading_methods_or_decision_fields(self):
        agent = MorningIntelligenceAgent(watchlist=["AAPL"])

        assert not hasattr(agent, "buy")
        assert not hasattr(agent, "sell")
        assert not hasattr(agent, "execute")
        assert not hasattr(agent, "place_order")

        report = MorningIntelligenceReport(
            date="2026-07-02",
            timestamp="2026-07-02T08:00:00+00:00",
        )
        d = report.to_dict()
        assert "recommendation" not in d
        assert "action" not in d
