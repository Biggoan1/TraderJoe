"""Tests for strategy/daily_digest.py"""
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from strategy.trade_logger import TradeLogger
from strategy.daily_digest import (
    DailyDigestBuilder,
    DigestRenderer,
    DigestSaver,
    DailyDigestService,
    DigestStats,
    DigestMetadataSummary,
    DailyDigest,
    OpenPosition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _make_logger():
    path = tempfile.mktemp(suffix=".db")
    return TradeLogger(db_path=path), path

def _seed_trades(logger: TradeLogger, date: str) -> list:
    """Seed 5 trades on the given date: 3 wins, 2 losses."""
    trades = []
    # Win 1: AAPL +$50
    entry = logger.log_trade_entry("AAPL", "buy", 150.0, 10, entry_score=8.0)
    exit_ = logger.log_trade_exit("AAPL", 155.0, "upper BB")
    trades.append(exit_)

    # Loss 1: TSLA -$30
    logger.log_trade_entry("TSLA", "buy", 200.0, 6, entry_score=6.5)
    exit_ = logger.log_trade_exit("TSLA", 195.0, "MACD death cross")
    trades.append(exit_)

    # Win 2: MSFT +$80
    logger.log_trade_entry("MSFT", "buy", 300.0, 20, entry_score=7.8)
    exit_ = logger.log_trade_exit("MSFT", 304.0, "overbought RSI")
    trades.append(exit_)

    # Win 3: GOOGL +$40
    logger.log_trade_entry("GOOGL", "buy", 140.0, 5, entry_score=7.2)
    exit_ = logger.log_trade_exit("GOOGL", 148.0, "upper BB")
    trades.append(exit_)

    # Loss 2: AMZN -$20
    logger.log_trade_entry("AMZN", "buy", 180.0, 5, entry_score=6.0)
    exit_ = logger.log_trade_exit("AMZN", 176.0, "MACD death cross")
    trades.append(exit_)

    return trades

# ---------------------------------------------------------------------------
# Builder tests
# ---------------------------------------------------------------------------

class TestDailyDigestBuilder:
    def test_build_with_no_trades(self):
        logger, path = _make_logger()
        try:
            builder = DailyDigestBuilder(logger, date=_utc_today())
            digest = builder.build()

            assert digest.stats.total_trades == 0
            assert digest.stats.daily_pl == 0.0
            assert digest.stats.win_rate == 0.0
            assert not digest.open_positions
        finally:
            os.unlink(path)

    def test_build_with_trades(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            _seed_trades(logger, date)

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()

            assert digest.stats.total_trades == 5
            assert digest.stats.closed_trades == 5
            assert digest.stats.wins == 3
            assert digest.stats.losses == 2
            assert digest.stats.win_rate == 60.0
            # 50 - 30 + 80 + 40 - 20 = 120
            assert digest.stats.daily_pl == 120.0
            assert digest.stats.date == date
        finally:
            os.unlink(path)

    def test_build_metadata_summary_from_trade_logger_rows(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            rs_entry = {"symbol": "AAPL", "rs_score": 72.0, "trend_direction": "improving"}
            rs_exit = {"symbol": "AAPL", "rs_score": 81.0, "trend_direction": "improving"}
            logger.log_trade_entry(
                "AAPL",
                "buy",
                100.0,
                10,
                entry_score=8.0,
                market_regime="bullish",
                active_flags=["enable_relative_strength"],
                rs_snapshot=rs_entry,
                metadata={"setup_type": "orb", "watchlist_rank": 1},
            )
            logger.log_trade_exit(
                "AAPL",
                110.0,
                "target",
                market_regime="bullish",
                active_flags=["enable_relative_strength"],
                rs_snapshot=rs_exit,
                metadata={"exit_signal": "target"},
            )

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()
            meta = digest.metadata_summary

            assert meta.symbols_traded == ["AAPL"]
            assert meta.exit_reason_counts == {"target": 1}
            assert meta.market_regime_counts == {"bullish": 1}
            assert meta.active_flag_counts == {"enable_relative_strength": 2}
            assert meta.avg_entry_score == 8.0
            assert meta.trades_with_entry_rs == 1
            assert meta.trades_with_exit_rs == 1
            assert meta.trades_with_metadata == 1
            assert meta.strongest_rs_symbol == "AAPL"
            assert meta.strongest_rs_score == 81.0
        finally:
            os.unlink(path)

    def test_metadata_summary_handles_malformed_json(self):
        trades = [
            {
                "symbol": "BAD",
                "exit_price": 10.0,
                "active_flags_entry": "{bad-json",
                "rs_at_entry": "[not-a-dict]",
                "trade_metadata_entry": "{bad-json",
            }
        ]

        meta = DailyDigestBuilder._compute_metadata_summary(trades)

        assert meta.symbols_traded == ["BAD"]
        assert meta.missing_exit_reasons == 1
        assert meta.active_flag_counts == {}
        assert meta.trades_with_entry_rs == 0
        assert meta.trades_with_metadata == 0

    def test_build_with_open_positions(self):
        logger, path = _make_logger()
        try:
            pos = OpenPosition("NVDA", 10, 120.0, 125.0, 50.0, "2025-01-15T14:00:00")
            builder = DailyDigestBuilder(logger, date=_utc_today())
            digest = builder.build(open_positions=[pos])

            assert len(digest.open_positions) == 1
            assert digest.open_positions[0].symbol == "NVDA"
        finally:
            os.unlink(path)

    def test_build_with_feature_flags(self):
        logger, path = _make_logger()
        try:
            flags = {"champion_mode": True, "orb_enabled": True, "rsi_exit": False}
            builder = DailyDigestBuilder(logger, date=_utc_today())
            digest = builder.build(feature_flags=flags)

            assert digest.feature_flags == flags
        finally:
            os.unlink(path)

    def test_build_with_runner_info(self):
        logger, path = _make_logger()
        try:
            info = {"watchlist_size": 15, "champion": "AAPL"}
            builder = DailyDigestBuilder(logger, date=_utc_today())
            digest = builder.build(runner_info=info)

            assert digest.runner_info == info
        finally:
            os.unlink(path)

    def test_compute_profit_factor(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            _seed_trades(logger, date)

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()

            # Gross profit: 50 + 80 + 40 = 170
            # Gross loss: 30 + 20 = 50
            # PF = 170/50 = 3.4
            assert digest.stats.profit_factor == 3.4
        finally:
            os.unlink(path)

    def test_compute_all_wins(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            logger.log_trade_entry("A", "buy", 100.0, 10)
            logger.log_trade_exit("A", 110.0, "target")
            logger.log_trade_entry("B", "buy", 50.0, 20)
            logger.log_trade_exit("B", 55.0, "target")

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()

            assert digest.stats.wins == 2
            assert digest.stats.losses == 0
            assert digest.stats.win_rate == 100.0
            # PF = inf -> handled as gross_profit
            assert digest.stats.profit_factor > 0
        finally:
            os.unlink(path)

    def test_compute_all_losses(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            logger.log_trade_entry("A", "buy", 100.0, 10)
            logger.log_trade_exit("A", 90.0, "stop")
            logger.log_trade_entry("B", "buy", 50.0, 20)
            logger.log_trade_exit("B", 45.0, "stop")

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()

            assert digest.stats.wins == 0
            assert digest.stats.losses == 2
            assert digest.stats.win_rate == 0.0
            assert digest.stats.profit_factor == 0.0
        finally:
            os.unlink(path)

    def test_avg_winner_averager(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            # +$50 and +$100
            logger.log_trade_entry("A", "buy", 100.0, 10)
            logger.log_trade_exit("A", 105.0, "target")  # +50
            logger.log_trade_entry("B", "buy", 100.0, 10)
            logger.log_trade_exit("B", 110.0, "target")  # +100

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()

            assert digest.stats.avg_winner == 75.0
        finally:
            os.unlink(path)

    def test_avg_loser(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            # -$20 and -$40
            logger.log_trade_entry("A", "buy", 100.0, 10)
            logger.log_trade_exit("A", 98.0, "stop")  # -20
            logger.log_trade_entry("B", "buy", 100.0, 10)
            logger.log_trade_exit("B", 96.0, "stop")  # -40

            builder = DailyDigestBuilder(logger, date=date)
            digest = builder.build()

            assert digest.stats.avg_loser == -30.0
        finally:
            os.unlink(path)

# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------

class TestDigestRenderer:
    def test_render_no_trades(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=0,
            closed_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            daily_pl=0.0,
            avg_winner=0.0,
            avg_loser=0.0,
            profit_factor=0.0,
            best_trade_pl=0.0,
            worst_trade_pl=0.0,
        )
        digest = DailyDigest(stats=stats, trades=[])
        content = DigestRenderer.render(digest)

        assert "No trades today" in content
        assert "TRADE JOE" in content

    def test_render_with_trades(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=5,
            closed_trades=5,
            wins=3,
            losses=2,
            win_rate=60.0,
            daily_pl=120.0,
            avg_winner=56.67,
            avg_loser=-25.0,
            profit_factor=3.4,
            best_trade_pl=80.0,
            worst_trade_pl=-30.0,
            avg_holding_seconds=3600,
        )
        trades = [
            {"symbol": "AAPL", "side": "buy", "entry_price": 150.0, "exit_price": 155.0, "qty": 10, "pl": 50.0, "score": 8.0, "exit_reason": "upper BB", "holding_period": 1800},
        ]
        digest = DailyDigest(stats=stats, trades=trades)
        content = DigestRenderer.render(digest)

        assert "DAILY DIGEST" in content
        assert "120.00" in content
        assert "60.0%" in content
        assert "AAPL" in content
        assert "Profit Factor" in content
        assert "Trade Joe out" in content

    def test_render_positive_pl_prefix(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=1,
            closed_trades=1,
            wins=1,
            losses=0,
            win_rate=100.0,
            daily_pl=50.0,
            avg_winner=50.0,
            avg_loser=0.0,
            profit_factor=50.0,
            best_trade_pl=50.0,
            worst_trade_pl=50.0,
        )
        digest = DailyDigest(stats=stats, trades=[{"symbol": "X", "side": "buy", "entry_price": 100.0, "exit_price": 105.0, "qty": 10, "pl": 50.0}])
        content = DigestRenderer.render(digest)

        assert "+$" in content

    def test_render_negative_pl(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=1,
            closed_trades=1,
            wins=0,
            losses=1,
            win_rate=0.0,
            daily_pl=-30.0,
            avg_winner=0.0,
            avg_loser=-30.0,
            profit_factor=0.0,
            best_trade_pl=-30.0,
            worst_trade_pl=-30.0,
        )
        digest = DailyDigest(stats=stats, trades=[{"symbol": "X", "side": "buy", "entry_price": 100.0, "exit_price": 97.0, "qty": 10, "pl": -30.0}])
        content = DigestRenderer.render(digest)

        assert "-$30.00" in content or "$-30.00" in content or "-30.00" in content

    def test_render_open_positions(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=0,
            closed_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            daily_pl=0.0,
            avg_winner=0.0,
            avg_loser=0.0,
            profit_factor=0.0,
            best_trade_pl=0.0,
            worst_trade_pl=0.0,
        )
        pos = OpenPosition("NVDA", 10, 120.0, 125.0, 50.0, "2025-01-15T14:00:00")
        digest = DailyDigest(stats=stats, trades=[], open_positions=[pos])
        content = DigestRenderer.render(digest)

        assert "NVDA" in content
        assert "OPEN POSITIONS" in content

    def test_render_feature_flags(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=0,
            closed_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            daily_pl=0.0,
            avg_winner=0.0,
            avg_loser=0.0,
            profit_factor=0.0,
            best_trade_pl=0.0,
            worst_trade_pl=0.0,
        )
        digest = DailyDigest(stats=stats, trades=[], feature_flags={"champion_mode": True, "orb_enabled": True})
        content = DigestRenderer.render(digest)

        assert "champion_mode" in content
        assert "orb_enabled" in content

    def test_render_holding_period_format(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=1,
            closed_trades=1,
            wins=1,
            losses=0,
            win_rate=100.0,
            daily_pl=50.0,
            avg_winner=50.0,
            avg_loser=0.0,
            profit_factor=50.0,
            best_trade_pl=50.0,
            worst_trade_pl=50.0,
            avg_holding_seconds=150,  # 2.5m
        )
        digest = DailyDigest(stats=stats, trades=[{"symbol": "X", "side": "buy", "entry_price": 100.0, "exit_price": 105.0, "qty": 10, "pl": 50.0}])
        content = DigestRenderer.render(digest)

        assert "2.5m" in content

    def test_render_long_holding(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=1,
            closed_trades=1,
            wins=1,
            losses=0,
            win_rate=100.0,
            daily_pl=50.0,
            avg_winner=50.0,
            avg_loser=0.0,
            profit_factor=50.0,
            best_trade_pl=50.0,
            worst_trade_pl=50.0,
            avg_holding_seconds=7200,  # 2h
        )
        digest = DailyDigest(stats=stats, trades=[{"symbol": "X", "side": "buy", "entry_price": 100.0, "exit_price": 105.0, "qty": 10, "pl": 50.0}])
        content = DigestRenderer.render(digest)

        assert "2.0h" in content

    def test_render_metadata_summary(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=1,
            closed_trades=1,
            wins=1,
            losses=0,
            win_rate=100.0,
            daily_pl=50.0,
            avg_winner=50.0,
            avg_loser=0.0,
            profit_factor=50.0,
            best_trade_pl=50.0,
            worst_trade_pl=50.0,
        )
        meta = DigestMetadataSummary(
            symbols_traded=["AAPL"],
            exit_reason_counts={"target": 1},
            market_regime_counts={"bullish": 1},
            active_flag_counts={"enable_relative_strength": 2},
            avg_entry_score=8.0,
            trades_with_entry_rs=1,
            trades_with_exit_rs=1,
            trades_with_metadata=1,
            strongest_rs_symbol="AAPL",
            strongest_rs_score=82.0,
            weakest_rs_symbol="AAPL",
            weakest_rs_score=72.0,
        )
        digest = DailyDigest(
            stats=stats,
            trades=[{"symbol": "AAPL", "side": "buy", "entry_price": 100.0, "quantity": 10}],
            metadata_summary=meta,
        )
        content = DigestRenderer.render(digest)

        assert "TRADE METADATA" in content
        assert "Observational only" in content
        assert "Avg Entry Score" in content
        assert "RELATIVE STRENGTH SNAPSHOTS" in content
        assert "Strongest: AAPL" in content

    def test_render_trade_details_supports_trade_logger_keys(self):
        stats = DigestStats(
            date="2025-01-15",
            total_trades=1,
            closed_trades=1,
            wins=1,
            losses=0,
            win_rate=100.0,
            daily_pl=50.0,
            avg_winner=50.0,
            avg_loser=0.0,
            profit_factor=50.0,
            best_trade_pl=50.0,
            worst_trade_pl=50.0,
        )
        digest = DailyDigest(
            stats=stats,
            trades=[
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "entry_price": 100.0,
                    "exit_price": 105.0,
                    "quantity": 10,
                    "pl": 50.0,
                    "entry_score": 8.0,
                    "holding_period_seconds": 90,
                }
            ],
        )
        content = DigestRenderer.render(digest)

        assert "10 @ $100.00" in content
        assert "Score: 8.0" in content
        assert "Held: 90s" in content

# ---------------------------------------------------------------------------
# Saver tests
# ---------------------------------------------------------------------------

class TestDigestSaver:
    def test_save_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stats = DigestStats(
                date="2025-01-15",
                total_trades=0,
                closed_trades=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                daily_pl=0.0,
                avg_winner=0.0,
                avg_loser=0.0,
                profit_factor=0.0,
                best_trade_pl=0.0,
                worst_trade_pl=0.0,
            )
            digest = DailyDigest(stats=stats, trades=[])
            content = DigestRenderer.render(digest)

            saver = DigestSaver(output_dir=tmpdir)
            filepath = saver.save(digest, content)

            assert os.path.exists(filepath)
            assert Path(filepath).name == "daily_digest_2025-01-15.md"
            assert Path(filepath).read_text() == content

    def test_save_creates_dir(self):
        tmpdir = tempfile.mkdtemp()
        nested = os.path.join(tmpdir, "reports", "sub")
        stats = DigestStats(
            date="2025-06-01",
            total_trades=0,
            closed_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            daily_pl=0.0,
            avg_winner=0.0,
            avg_loser=0.0,
            profit_factor=0.0,
            best_trade_pl=0.0,
            worst_trade_pl=0.0,
        )
        digest = DailyDigest(stats=stats, trades=[])
        saver = DigestSaver(output_dir=nested)
        saver.save(digest, "test")

        assert os.path.isdir(nested)

# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------

class TestDailyDigestService:
    def test_generate_and_deliver_no_telegram(self):
        logger, path = _make_logger()
        try:
            service = DailyDigestService(logger, telegram_sender=None)
            content, filepath = service.generate_and_deliver()

            assert "DAILY DIGEST" in content or "No trades" in content
            assert os.path.exists(filepath)
        finally:
            os.unlink(path)

    def test_generate_and_deliver_with_telegram(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            _seed_trades(logger, date)

            messages = []
            class FakeTelegram:
                def send_message(self, message: str):
                    messages.append(message)

            service = DailyDigestService(logger, telegram_sender=FakeTelegram())
            content, filepath = service.generate_and_deliver(date=date)

            assert len(messages) == 1
            assert "DAILY DIGEST" in messages[0]
            assert "120.00" in messages[0]
        finally:
            os.unlink(path)

    def test_full_pipeline_with_positions_and_flags(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            _seed_trades(logger, date)

            pos = OpenPosition("NVDA", 10, 120.0, 125.0, 50.0, "2025-01-15T14:00:00")
            flags = {"champion_mode": True, "orb_enabled": False}
            info = {"watchlist_size": 15, "champion": "AAPL"}

            messages = []
            class FakeTelegram:
                def send_message(self, msg: str):
                    messages.append(msg)

            service = DailyDigestService(logger, telegram_sender=FakeTelegram())
            content, filepath = service.generate_and_deliver(
                date=date,
                open_positions=[pos],
                feature_flags=flags,
                runner_info=info,
            )

            assert "NVDA" in content
            assert "champion_mode" in content
            assert "watchlist_size" in content
            assert os.path.exists(filepath)
        finally:
            os.unlink(path)

    def test_generate_and_deliver_passes_rs_data(self):
        logger, path = _make_logger()
        try:
            date = _utc_today()
            _seed_trades(logger, date)
            rs_data = {
                "results": [
                    {
                        "symbol": "AAPL",
                        "rs_score": 82.0,
                        "trend_direction": "improving",
                    }
                ]
            }

            service = DailyDigestService(logger, telegram_sender=None)
            content, filepath = service.generate_and_deliver(date=date, rs_data=rs_data)

            assert "RELATIVE STRENGTH" in content
            assert "AAPL: Score 82" in content
            assert os.path.exists(filepath)
        finally:
            os.unlink(path)
