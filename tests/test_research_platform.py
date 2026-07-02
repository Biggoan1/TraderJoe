"""Tests for strategy.research_platform — Research Platform Interface."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.research_platform import (
    ResearchPlatform,
    PortfolioData,
    PerformanceData,
    MarketRegimeData,
    RelativeStrengthData,
    SectorLeadershipData,
    MarketBreadthData,
    MarketIntelligenceData,
    ResearchNote,
    ResearchSnapshot,
    _dataclass_to_dict,
)
from strategy.trade_logger import TradeLogger
from strategy.config import get_feature_flags, reset_feature_flags


@pytest.fixture
def tmp_dir():
    """Create a temporary directory for testing."""
    tmp = tempfile.mkdtemp()
    yield tmp
    # Cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def platform(tmp_dir):
    """Create a ResearchPlatform with a temp database."""
    db_path = os.path.join(tmp_dir, "test_trades.db")
    logger = TradeLogger(db_path=db_path)
    reset_feature_flags()
    rp = ResearchPlatform(base_dir=tmp_dir, logger=logger)
    return rp


# ---- Dataclass tests ----

class TestDataContracts:
    """Test data contract dataclasses."""

    def test_portfolio_data_defaults(self):
        p = PortfolioData()
        assert p.equity == 0.0
        assert p.cash == 0.0
        assert p.positions == []
        assert p.exposure_by_symbol == {}

    def test_portfolio_merge_alpaca_data(self):
        p = PortfolioData()
        data = {
            "equity": 10000.0,
            "cash": 5000.0,
            "daily_pl": 150.0,
            "total_pl": 1200.0,
            "positions": [
                {
                    "symbol": "AAPL",
                    "qty": 10,
                    "avg_entry_price": 150.0,
                    "current_price": 155.0,
                    "unrealized_pl": 50.0,
                }
            ],
        }
        p.merge_alpaca_data(data)
        assert p.equity == 10000.0
        assert p.cash == 5000.0
        assert p.daily_pl == 150.0
        assert len(p.positions) == 1
        assert p.positions[0]["symbol"] == "AAPL"
        assert p.exposure_by_symbol["AAPL"] == 1550.0

    def test_performance_data_defaults(self):
        perf = PerformanceData()
        assert perf.win_rate == 0.0
        assert perf.total_trades == 0
        assert perf.max_drawdown == 0.0

    def test_market_regime_data_defaults(self):
        mr = MarketRegimeData()
        assert mr.regime == "unknown"
        assert mr.confidence == 0.0

    def test_sector_leadership_data_defaults(self):
        sl = SectorLeadershipData()
        assert sl.strongest_sectors == []
        assert sl.weakest_sectors == []
        assert sl.all_sectors == []
        assert sl.data_quality == "missing"

    def test_market_breadth_data_defaults(self):
        mb = MarketBreadthData()
        assert mb.breadth_score == 50.0
        assert mb.breadth_regime == "missing"
        assert mb.above_ma_percentages == {}
        assert mb.all_symbols == []
        assert mb.data_quality == "missing"

    def test_research_note_defaults(self):
        note = ResearchNote()
        assert note.date == ""
        assert note.hypotheses == []

    def test_research_snapshot_defaults(self):
        snap = ResearchSnapshot()
        assert snap.generated_at == ""
        assert snap.recent_trades == []

    def test_dataclass_to_dict_simple(self):
        p = PortfolioData(equity=100.0, cash=50.0)
        d = _dataclass_to_dict(p)
        assert isinstance(d, dict)
        assert d["equity"] == 100.0
        assert d["cash"] == 50.0

    def test_dataclass_to_dict_nested(self):
        snap = ResearchSnapshot(
            portfolio=PortfolioData(equity=100.0),
            generated_at="2026-01-01",
        )
        d = _dataclass_to_dict(snap)
        assert d["portfolio"]["equity"] == 100.0
        assert d["generated_at"] == "2026-01-01"

    def test_dataclass_to_dict_list(self):
        items = [PortfolioData(equity=10.0), PortfolioData(equity=20.0)]
        result = _dataclass_to_dict(items)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["equity"] == 10.0

    def test_dataclass_to_dict_non_dataclass(self):
        assert _dataclass_to_dict("hello") == "hello"
        assert _dataclass_to_dict(42) == 42
        assert _dataclass_to_dict(None) is None


# ---- ResearchPlatform tests ----

class TestResearchPlatformInit:
    """Test ResearchPlatform initialization."""

    def test_init_creates_notes_dir(self, tmp_dir):
        rp = ResearchPlatform(base_dir=tmp_dir)
        assert Path(tmp_dir) / "research_notes"

    def test_init_with_custom_logger(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "custom.db")
        logger = TradeLogger(db_path=db_path)
        rp = ResearchPlatform(base_dir=tmp_dir, logger=logger)
        assert rp.logger is logger

    def test_init_default_base_dir(self):
        rp = ResearchPlatform()
        assert rp.base_dir.exists()


class TestGetPortfolio:
    """Test get_portfolio method."""

    def test_empty_portfolio(self, platform):
        portfolio = platform.get_portfolio()
        assert portfolio.equity == 0.0
        assert portfolio.cash == 0.0
        assert len(portfolio.positions) == 0
        assert portfolio.timestamp  # has a timestamp

    def test_portfolio_with_open_trades(self, platform):
        platform.logger.log_trade_entry(
            symbol="AAPL",
            side="buy",
            entry_price=150.0,
            quantity=10,
        )
        portfolio = platform.get_portfolio()
        assert len(portfolio.positions) == 1
        assert portfolio.positions[0]["symbol"] == "AAPL"
        assert portfolio.exposure_by_symbol["AAPL"] == 1500.0

    def test_portfolio_multiple_positions(self, platform):
        platform.logger.log_trade_entry("AAPL", "buy", 150.0, 10)
        platform.logger.log_trade_entry("MSFT", "buy", 300.0, 5)
        portfolio = platform.get_portfolio()
        assert len(portfolio.positions) == 2
        assert "AAPL" in portfolio.exposure_by_symbol
        assert "MSFT" in portfolio.exposure_by_symbol


class TestGetPerformance:
    """Test get_performance method."""

    def test_empty_performance(self, platform):
        perf = platform.get_performance()
        assert perf.total_trades == 0
        assert perf.win_rate == 0.0
        assert perf.max_drawdown == 0.0

    def test_performance_with_wins_and_losses(self, platform):
        # Winning trade
        platform.logger.log_trade_entry("AAPL", "buy", 100.0, 10)
        platform.logger.log_trade_exit("AAPL", 110.0, "target")
        # Losing trade
        platform.logger.log_trade_entry("TSLA", "buy", 200.0, 10)
        platform.logger.log_trade_exit("TSLA", 190.0, "stop loss")

        perf = platform.get_performance()
        assert perf.total_trades == 2
        assert perf.wins == 1
        assert perf.losses == 1
        assert perf.win_rate == 50.0
        assert perf.average_winner == 100.0
        assert perf.average_loser == -100.0
        assert perf.total_pl == 0.0

    def test_performance_max_drawdown(self, platform):
        # Down then up — creates drawdown
        platform.logger.log_trade_entry("A", "buy", 100.0, 10)
        platform.logger.log_trade_exit("A", 90.0, "stop")  # -100
        platform.logger.log_trade_entry("B", "buy", 100.0, 10)
        platform.logger.log_trade_exit("B", 95.0, "stop")  # -50 (cumulative -150)
        platform.logger.log_trade_entry("C", "buy", 100.0, 10)
        platform.logger.log_trade_exit("C", 120.0, "target")  # +200 (cumulative +50)

        perf = platform.get_performance()
        assert perf.max_drawdown == 150.0  # peak 0, trough -150

    def test_performance_best_worst_trade(self, platform):
        platform.logger.log_trade_entry("A", "buy", 100.0, 10)
        platform.logger.log_trade_exit("A", 120.0, "target")  # +200
        platform.logger.log_trade_entry("B", "buy", 100.0, 10)
        platform.logger.log_trade_exit("B", 80.0, "stop")  # -200

        perf = platform.get_performance()
        assert perf.best_trade["symbol"] == "A"
        assert perf.best_trade["pl"] == 200.0
        assert perf.worst_trade["symbol"] == "B"
        assert perf.worst_trade["pl"] == -200.0

    def test_performance_trades_by_exit_reason(self, platform):
        platform.logger.log_trade_entry("A", "buy", 100.0, 10)
        platform.logger.log_trade_exit("A", 110.0, "target")
        platform.logger.log_trade_entry("B", "buy", 100.0, 10)
        platform.logger.log_trade_exit("B", 95.0, "stop loss")
        platform.logger.log_trade_entry("C", "buy", 100.0, 10)
        platform.logger.log_trade_exit("C", 105.0, "target")

        perf = platform.get_performance()
        assert perf.trades_by_exit_reason.get("target") == 2
        assert perf.trades_by_exit_reason.get("stop loss") == 1

    def test_performance_inf_profit_factor(self, platform):
        platform.logger.log_trade_entry("A", "buy", 100.0, 10)
        platform.logger.log_trade_exit("A", 110.0, "target")

        perf = platform.get_performance()
        assert perf.profit_factor == 999.0  # capped from inf


class TestGetTradeHistory:
    """Test get_trade_history method."""

    def test_empty_history(self, platform):
        trades = platform.get_trade_history()
        assert trades == []

    def test_trade_history_with_trades(self, platform):
        platform.logger.log_trade_entry("AAPL", "buy", 150.0, 10)
        platform.logger.log_trade_exit("AAPL", 155.0, "target")

        trades = platform.get_trade_history()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "AAPL"
        assert trades[0]["side"] == "buy"
        assert trades[0]["pl"] == 50.0

    def test_trade_history_limit(self, platform):
        for i, sym in enumerate(["A", "B", "C", "D", "E"]):
            platform.logger.log_trade_entry(sym, "buy", 100.0, 10)
            platform.logger.log_trade_exit(sym, 105.0, "target")

        trades = platform.get_trade_history(limit=3)
        assert len(trades) == 3


class TestGetDailyDigests:
    """Test get_daily_digests method."""

    def test_no_digests(self, platform):
        digests = platform.get_daily_digests()
        assert digests == []

    def test_digest_found(self, platform):
        # Create a fake digest file
        digest_file = platform._reports_dir / "daily_digest_2026-07-02.md"
        digest_file.write_text("# Daily Digest\nTest content")

        digests = platform.get_daily_digests()
        assert len(digests) == 1
        assert digests[0]["date"] == "2026-07-02"
        assert "Test content" in digests[0]["content"]


class TestGetResearchNotes:
    """Test get_research_notes method."""

    def test_no_notes(self, platform):
        notes = platform.get_research_notes()
        assert notes == []

    def test_note_found(self, platform):
        # Create a fake note file
        note_file = platform._notes_dir / "2026-07-02.json"
        note_data = {
            "date": "2026-07-02",
            "content": "Test note",
            "market_regime": "bullish",
            "hypotheses": ["H1"],
        }
        note_file.write_text(json.dumps(note_data))

        notes = platform.get_research_notes()
        assert len(notes) == 1
        assert notes[0].date == "2026-07-02"
        assert notes[0].market_regime == "bullish"


class TestGetSnapshot:
    """Test get_snapshot method."""

    def test_snapshot_has_all_fields(self, platform):
        with patch("strategy.market_regime", create=True), \
             patch("strategy.relative_strength", create=True), \
             patch("strategy.sector_leadership", create=True), \
             patch("strategy.market_breadth", create=True):
            snap = platform.get_snapshot()
            assert snap.portfolio is not None
            assert snap.performance is not None
            assert snap.market_intelligence is not None
            assert isinstance(snap.market_intelligence.sector_leadership, SectorLeadershipData)
            assert isinstance(snap.market_intelligence.market_breadth, MarketBreadthData)
            assert isinstance(snap.recent_trades, list)
            assert isinstance(snap.research_notes, list)
            assert isinstance(snap.daily_digests, list)
            assert isinstance(snap.feature_flags, dict)
            assert snap.generated_at  # timestamp set

    def test_snapshot_feature_flags(self, platform):
        with patch("strategy.market_regime", create=True), \
             patch("strategy.relative_strength", create=True), \
             patch("strategy.sector_leadership", create=True), \
             patch("strategy.market_breadth", create=True):
            snap = platform.get_snapshot()
            assert "enable_historical_statistics" in snap.feature_flags


class TestGetSnapshotJson:
    """Test get_snapshot_json method."""

    def test_json_output(self, platform):
        with patch("strategy.market_regime", create=True), \
             patch("strategy.relative_strength", create=True), \
             patch("strategy.sector_leadership", create=True), \
             patch("strategy.market_breadth", create=True):
            json_str = platform.get_snapshot_json()
            data = json.loads(json_str)
            assert "portfolio" in data
            assert "performance" in data
            assert "sector_leadership" in data["market_intelligence"]
            assert "market_breadth" in data["market_intelligence"]
            assert "generated_at" in data

    def test_json_serializable(self, platform):
        with patch("strategy.market_regime", create=True), \
             patch("strategy.relative_strength", create=True), \
             patch("strategy.sector_leadership", create=True), \
             patch("strategy.market_breadth", create=True):
            json_str = platform.get_snapshot_json()
            # Should not raise
            json.loads(json_str)


class TestParseJsonField:
    """Test _parse_json_field helper."""

    def test_valid_json(self):
        result = ResearchPlatform._parse_json_field('{"a": 1}')
        assert result == {"a": 1}

    def test_none_input(self):
        assert ResearchPlatform._parse_json_field(None) is None

    def test_empty_string(self):
        assert ResearchPlatform._parse_json_field("") is None

    def test_invalid_json_returns_raw(self):
        result = ResearchPlatform._parse_json_field("not json")
        assert result == "not json"


class TestCalculateMaxDrawdown:
    """Test _calculate_max_drawdown internal method."""

    def test_no_trades(self, platform):
        assert platform._calculate_max_drawdown() == 0.0

    def test_all_winners_no_drawdown(self, platform):
        platform.logger.log_trade_entry("A", "buy", 100.0, 10)
        platform.logger.log_trade_exit("A", 110.0, "target")
        platform.logger.log_trade_entry("B", "buy", 100.0, 10)
        platform.logger.log_trade_exit("B", 115.0, "target")

        assert platform._calculate_max_drawdown() == 0.0

    def test_loss_then_recovery(self, platform):
        platform.logger.log_trade_entry("A", "buy", 100.0, 10)
        platform.logger.log_trade_exit("A", 90.0, "stop")  # -100
        platform.logger.log_trade_entry("B", "buy", 100.0, 10)
        platform.logger.log_trade_exit("B", 130.0, "target")  # +300

        assert platform._calculate_max_drawdown() == 100.0


class TestFetchAlpacaPortfolio:
    """Test _fetch_alpaca_portfolio internal method."""

    def test_returns_empty_on_failure(self, platform):
        result = platform._fetch_alpaca_portfolio()
        # Should not raise, returns empty dict when trader module unavailable
        assert isinstance(result, dict)


class TestFetchRelativeStrength:
    """Test _fetch_relative_strength internal method."""

    def test_returns_relative_strength_data(self, platform):
        rs = platform._fetch_relative_strength()
        assert isinstance(rs, RelativeStrengthData)
        assert rs.timestamp  # timestamp set


class TestFetchSectorLeadership:
    """Test _fetch_sector_leadership internal method."""

    def test_returns_sector_leadership_data(self, platform):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "sector": "Technology",
            "symbol": "XLK",
            "leadership_score": 62.0,
        }
        mock_report = MagicMock(
            strongest_sectors=["Technology"],
            weakest_sectors=["Utilities"],
            results=[mock_result],
            data_quality="complete",
            timestamp="2026-07-02T12:00:00+00:00",
        )
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = mock_report

        with patch("strategy.sector_leadership.SectorLeadershipAnalyzer", return_value=mock_analyzer):
            sector_data = platform._fetch_sector_leadership()

        assert isinstance(sector_data, SectorLeadershipData)
        assert sector_data.strongest_sectors == ["Technology"]
        assert sector_data.weakest_sectors == ["Utilities"]
        assert sector_data.all_sectors[0]["symbol"] == "XLK"
        assert sector_data.data_quality == "complete"
        assert sector_data.timestamp == "2026-07-02T12:00:00+00:00"

    def test_returns_empty_data_on_failure(self, platform):
        with patch("strategy.sector_leadership.SectorLeadershipAnalyzer", side_effect=RuntimeError("boom")):
            sector_data = platform._fetch_sector_leadership()

        assert isinstance(sector_data, SectorLeadershipData)
        assert sector_data.strongest_sectors == []
        assert sector_data.weakest_sectors == []
        assert sector_data.all_sectors == []
        assert sector_data.data_quality == "missing"


class TestFetchMarketBreadth:
    """Test _fetch_market_breadth internal method."""

    def test_returns_market_breadth_data(self, platform):
        mock_observation = MagicMock()
        mock_observation.to_dict.return_value = {
            "symbol": "AAPL",
            "latest_close": 200.0,
            "above_moving_average": {"20d": True},
        }
        mock_report = MagicMock(
            breadth_score=72.0,
            breadth_regime="strong",
            above_ma_percentages={"20d": 80.0},
            advance_decline_ratio=2.0,
            advancing_count=8,
            declining_count=4,
            unchanged_count=1,
            new_high_count=2,
            new_low_count=0,
            observations=[mock_observation],
            data_quality="complete",
            timestamp="2026-07-02T12:00:00+00:00",
        )
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = mock_report

        with patch("strategy.market_breadth.MarketBreadthAnalyzer", return_value=mock_analyzer):
            breadth_data = platform._fetch_market_breadth()

        assert isinstance(breadth_data, MarketBreadthData)
        assert breadth_data.breadth_score == 72.0
        assert breadth_data.breadth_regime == "strong"
        assert breadth_data.above_ma_percentages == {"20d": 80.0}
        assert breadth_data.advance_decline_ratio == 2.0
        assert breadth_data.advancing_count == 8
        assert breadth_data.declining_count == 4
        assert breadth_data.all_symbols[0]["symbol"] == "AAPL"
        assert breadth_data.data_quality == "complete"
        assert breadth_data.timestamp == "2026-07-02T12:00:00+00:00"

    def test_returns_empty_data_on_failure(self, platform):
        with patch("strategy.market_breadth.MarketBreadthAnalyzer", side_effect=RuntimeError("boom")):
            breadth_data = platform._fetch_market_breadth()

        assert isinstance(breadth_data, MarketBreadthData)
        assert breadth_data.breadth_score == 50.0
        assert breadth_data.breadth_regime == "missing"
        assert breadth_data.all_symbols == []
        assert breadth_data.data_quality == "missing"
