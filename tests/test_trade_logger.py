"""Tests for strategy.trade_logger — Historical Trade Logger."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.trade_logger import TradeLogger


@pytest.fixture
def tmp_db():
    """Create a temporary database for testing."""
    db_path = tempfile.mktemp(suffix=".db")
    yield db_path
    # Cleanup
    if os.path.exists(db_path):
        os.remove(db_path)


class TestTradeLoggerInit:
    """Test TradeLogger initialization."""

    def test_creates_db_file(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        assert Path(tmp_db).exists()

    def test_creates_trades_table(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        tables = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = [row[0] for row in tables]
        assert "trades" in table_names

    def test_creates_indexes(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        indexes = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        conn.close()
        index_names = [row[0] for row in indexes]
        assert "idx_trades_symbol" in index_names
        assert "idx_trades_side" in index_names
        assert "idx_trades_entry_time" in index_names
        assert "idx_trades_exit_time" in index_names

    def test_migrates_existing_database_with_missing_metadata_columns(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                pl REAL,
                pl_percent REAL,
                holding_period_seconds REAL,
                entry_score REAL,
                exit_reason TEXT,
                market_regime TEXT,
                active_flags_entry TEXT,
                active_flags_exit TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        TradeLogger(db_path=tmp_db)

        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        columns = {row[1] for row in cur.execute("PRAGMA table_info(trades)").fetchall()}
        conn.close()
        assert "rs_at_entry" in columns
        assert "rs_at_exit" in columns
        assert "trade_metadata_entry" in columns
        assert "trade_metadata_exit" in columns


class TestLogTradeEntry:
    """Test logging trade entries."""

    def test_log_buy_entry(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        trade_id = logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=120.50,
            quantity=100,
            entry_score=7.5,
            active_flags=["historical_statistics"],
        )
        assert trade_id > 0

    def test_log_sell_entry(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        trade_id = logger.log_trade_entry(
            symbol="TSLA",
            side="sell",
            entry_price=250.00,
            quantity=50,
        )
        assert trade_id > 0

    def test_entry_has_no_exit_time(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="AAPL",
            side="buy",
            entry_price=150.00,
            quantity=200,
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT exit_time, exit_price FROM trades LIMIT 1").fetchone()
        conn.close()
        assert row[0] is None  # exit_time
        assert row[1] is None  # exit_price

    def test_entry_stores_flags(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="MSFT",
            side="buy",
            entry_price=300.00,
            quantity=100,
            active_flags=["flag_a", "flag_b"],
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT active_flags_entry FROM trades LIMIT 1").fetchone()
        conn.close()
        flags = json.loads(row[0])
        assert flags == ["flag_a", "flag_b"]

    def test_entry_stores_none_flags_by_default(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="GOOGL",
            side="buy",
            entry_price=140.00,
            quantity=50,
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT active_flags_entry FROM trades LIMIT 1").fetchone()
        conn.close()
        flags = json.loads(row[0])
        assert flags == []

    def test_multiple_entries_same_symbol(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        id1 = logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=120.00,
            quantity=100,
        )
        id2 = logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=125.00,
            quantity=100,
        )
        assert id1 != id2

    def test_entry_stores_rs_snapshot(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        rs_snapshot = {
            "symbol": "NVDA",
            "rs_vs_benchmark": {"SPY": {"5d": 2.5, "20d": -1.3, "60d": 5.2}},
            "rs_score": 75.0,
            "trend_direction": "improving",
        }
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=120.00,
            quantity=100,
            rs_snapshot=rs_snapshot,
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT rs_at_entry FROM trades LIMIT 1").fetchone()
        conn.close()
        rs = json.loads(row[0])
        assert rs["symbol"] == "NVDA"
        assert rs["rs_score"] == 75.0
        assert rs["trend_direction"] == "improving"

    def test_entry_rs_snapshot_none_by_default(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="AAPL",
            side="buy",
            entry_price=150.00,
            quantity=100,
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT rs_at_entry FROM trades LIMIT 1").fetchone()
        conn.close()
        assert row[0] is None

    def test_entry_stores_trade_metadata(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="AAPL",
            side="buy",
            entry_price=150.00,
            quantity=100,
            metadata={"setup_type": "orb", "watchlist_rank": 2},
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT trade_metadata_entry FROM trades LIMIT 1").fetchone()
        conn.close()
        metadata = json.loads(row[0])
        assert metadata["setup_type"] == "orb"
        assert metadata["watchlist_rank"] == 2


class TestLogTradeExitRS:
    """Test RS snapshot storage in trade exits."""

    def test_exit_stores_rs_snapshot(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=100.00,
            quantity=100,
        )
        rs_exit = {
            "symbol": "NVDA",
            "rs_vs_benchmark": {"SPY": {"5d": 1.2, "20d": 3.5, "60d": 4.0}},
            "rs_score": 80.0,
            "trend_direction": "improving",
        }
        result = logger.log_trade_exit(
            symbol="NVDA",
            exit_price=110.00,
            exit_reason="target reached",
            rs_snapshot=rs_exit,
        )
        assert result is not None
        assert result["rs_at_exit"] is not None
        rs = json.loads(result["rs_at_exit"])
        assert rs["rs_score"] == 80.0

    def test_exit_preserves_entry_rs(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        rs_entry = {
            "symbol": "NVDA",
            "rs_score": 60.0,
            "trend_direction": "stable",
        }
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=100.00,
            quantity=100,
            rs_snapshot=rs_entry,
        )
        logger.log_trade_exit(
            symbol="NVDA",
            exit_price=110.00,
            exit_reason="target reached",
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT rs_at_entry, rs_at_exit FROM trades LIMIT 1").fetchone()
        conn.close()
        assert json.loads(row[0])["rs_score"] == 60.0
        assert row[1] is None

    def test_exit_rs_both_entry_and_exit(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        rs_entry = {"rs_score": 65.0, "trend_direction": "stable"}
        rs_exit = {"rs_score": 72.0, "trend_direction": "improving"}
        logger.log_trade_entry(
            symbol="TSLA",
            side="buy",
            entry_price=200.00,
            quantity=50,
            rs_snapshot=rs_entry,
        )
        result = logger.log_trade_exit(
            symbol="TSLA",
            exit_price=210.00,
            rs_snapshot=rs_exit,
        )
        assert result is not None
        entry_rs = json.loads(result["rs_at_entry"])
        exit_rs = json.loads(result["rs_at_exit"])
        assert entry_rs["rs_score"] == 65.0
        assert exit_rs["rs_score"] == 72.0

    def test_exit_stores_trade_metadata(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="TSLA",
            side="buy",
            entry_price=200.00,
            quantity=50,
        )
        result = logger.log_trade_exit(
            symbol="TSLA",
            exit_price=210.00,
            metadata={"exit_signal": "target", "slippage_pct": 0.1},
        )

        assert result is not None
        metadata = json.loads(result["trade_metadata_exit"])
        assert metadata["exit_signal"] == "target"
        assert metadata["slippage_pct"] == 0.1


class TestLogTradeExit:
    """Test logging trade exits."""

    def test_close_profitable_buy(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=100.00,
            quantity=100,
        )
        result = logger.log_trade_exit(
            symbol="NVDA",
            exit_price=110.00,
            exit_reason="target reached",
        )
        assert result is not None
        assert result["pl"] == 1000.0  # (110 - 100) * 100
        assert result["pl_percent"] == 10.0

    def test_close_losing_buy(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="TSLA",
            side="buy",
            entry_price=250.00,
            quantity=50,
        )
        result = logger.log_trade_exit(
            symbol="TSLA",
            exit_price=240.00,
            exit_reason="stop loss",
        )
        assert result is not None
        assert result["pl"] == -500.0  # (240 - 250) * 50
        assert result["pl_percent"] == -4.0

    def test_close_profitable_sell(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="AAPL",
            side="sell",
            entry_price=150.00,
            quantity=100,
        )
        result = logger.log_trade_exit(
            symbol="AAPL",
            exit_price=140.00,
            exit_reason="short covered",
        )
        assert result is not None
        assert result["pl"] == 1000.0  # (150 - 140) * 100

    def test_exit_returns_none_if_no_entry(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        result = logger.log_trade_exit(
            symbol="NOENT",
            exit_price=100.00,
        )
        assert result is None

    def test_exit_sets_exit_time(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="MSFT",
            side="buy",
            entry_price=300.00,
            quantity=100,
        )
        logger.log_trade_exit(
            symbol="MSFT",
            exit_price=310.00,
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT exit_time, exit_price FROM trades LIMIT 1").fetchone()
        conn.close()
        assert row[0] is not None
        assert row[1] == 310.0

    def test_exit_stores_reason(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="AMZN",
            side="buy",
            entry_price=180.00,
            quantity=50,
        )
        logger.log_trade_exit(
            symbol="AMZN",
            exit_price=190.00,
            exit_reason="overbought RSI",
        )
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        row = cur.execute("SELECT exit_reason FROM trades LIMIT 1").fetchone()
        conn.close()
        assert row[0] == "overbought RSI"

    def test_exit_calculates_holding_period(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="META",
            side="buy",
            entry_price=500.00,
            quantity=20,
        )
        result = logger.log_trade_exit(
            symbol="META",
            exit_price=510.00,
        )
        assert result is not None
        assert result["holding_period_seconds"] is not None
        assert result["holding_period_seconds"] >= 0


class TestGetTrades:
    """Test retrieving trades."""

    def test_get_open_trades(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=120.00,
            quantity=100,
        )
        open_trades = logger.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["symbol"] == "NVDA"

    def test_get_closed_trades(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="AAPL",
            side="buy",
            entry_price=150.00,
            quantity=100,
        )
        logger.log_trade_exit(
            symbol="AAPL",
            exit_price=160.00,
        )
        closed_trades = logger.get_closed_trades()
        assert len(closed_trades) == 1
        assert closed_trades[0]["pl"] == 1000.0

    def test_get_symbol_trades(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=120.00,
            quantity=100,
        )
        logger.log_trade_entry(
            symbol="TSLA",
            side="buy",
            entry_price=250.00,
            quantity=50,
        )
        nvda_trades = logger.get_symbol_trades("NVDA")
        assert len(nvda_trades) == 1
        assert nvda_trades[0]["symbol"] == "NVDA"

    def test_get_all_trades(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="NVDA",
            side="buy",
            entry_price=120.00,
            quantity=100,
        )
        logger.log_trade_entry(
            symbol="TSLA",
            side="buy",
            entry_price=250.00,
            quantity=50,
        )
        all_trades = logger.get_all_trades()
        assert len(all_trades) == 2


class TestSummaryReport:
    """Test summary report generation."""

    def test_empty_report(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        report = logger.get_summary_report()
        assert report["total_trades"] == 0
        assert report["win_rate"] == 0.0

    def test_report_with_wins(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        # Two winning trades
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=110.0, exit_reason="target")
        logger.log_trade_entry(symbol="B", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="B", exit_price=120.0, exit_reason="target")
        report = logger.get_summary_report()
        assert report["total_trades"] == 2
        assert report["wins"] == 2
        assert report["losses"] == 0
        assert report["win_rate"] == 100.0
        assert report["total_pl"] == 300.0

    def test_report_with_losses(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        # One win, one loss
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=110.0, exit_reason="target")
        logger.log_trade_entry(symbol="B", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="B", exit_price=90.0, exit_reason="stop loss")
        report = logger.get_summary_report()
        assert report["total_trades"] == 2
        assert report["wins"] == 1
        assert report["losses"] == 1
        assert report["win_rate"] == 50.0
        assert report["total_pl"] == 0.0

    def test_profit_factor(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        # Win: +$100, Loss: -$50
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=110.0, exit_reason="target")
        logger.log_trade_entry(symbol="B", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="B", exit_price=95.0, exit_reason="stop loss")
        report = logger.get_summary_report()
        # profit_factor = 100 / 50 = 2.0
        assert report["profit_factor"] == 2.0

    def test_best_worst_trade(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=120.0, exit_reason="target")
        logger.log_trade_entry(symbol="B", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="B", exit_price=80.0, exit_reason="stop loss")
        report = logger.get_summary_report()
        assert report["best_trade"]["symbol"] == "A"
        assert report["best_trade"]["pl"] == 200.0
        assert report["worst_trade"]["symbol"] == "B"
        assert report["worst_trade"]["pl"] == -200.0

    def test_trades_by_exit_reason(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=110.0, exit_reason="overbought RSI")
        logger.log_trade_entry(symbol="B", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="B", exit_price=90.0, exit_reason="stop loss")
        logger.log_trade_entry(symbol="C", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="C", exit_price=115.0, exit_reason="overbought RSI")
        report = logger.get_summary_report()
        assert report["trades_by_exit_reason"]["overbought RSI"] == 2
        assert report["trades_by_exit_reason"]["stop loss"] == 1

    def test_format_report(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=110.0, exit_reason="target")
        report = logger.get_summary_report()
        formatted = logger.format_report(report)
        assert "Total Trades: 1" in formatted
        assert "Win Rate: 100.0%" in formatted
        assert "Total P/L: $100.00" in formatted


class TestExportJSONL:
    """Test JSONL export."""

    def test_export_creates_file(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        output = logger.export_jsonl()
        assert Path(output).exists()

    def test_export_valid_jsonl(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(symbol="A", side="buy", entry_price=100.0, quantity=10)
        logger.log_trade_exit(symbol="A", exit_price=110.0)
        output = logger.export_jsonl()
        with open(output) as f:
            lines = f.readlines()
        assert len(lines) == 1
        trade = json.loads(lines[0])
        assert trade["symbol"] == "A"
        assert trade["pl"] == 100.0

    def test_export_parsers_flags(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="A", side="buy", entry_price=100.0, quantity=10,
            active_flags=["flag_x"],
        )
        output = logger.export_jsonl()
        with open(output) as f:
            trade = json.loads(f.readline())
        assert trade["active_flags_entry"] == ["flag_x"]

    def test_export_parses_rs_and_metadata(self, tmp_db):
        logger = TradeLogger(db_path=tmp_db)
        logger.log_trade_entry(
            symbol="A",
            side="buy",
            entry_price=100.0,
            quantity=10,
            rs_snapshot={"symbol": "A", "rs_score": 70.0},
            metadata={"setup_type": "breakout"},
        )
        logger.log_trade_exit(
            symbol="A",
            exit_price=110.0,
            rs_snapshot={"symbol": "A", "rs_score": 82.0},
            metadata={"exit_signal": "target"},
        )

        output = logger.export_jsonl()
        with open(output) as f:
            trade = json.loads(f.readline())

        assert trade["rs_at_entry"]["rs_score"] == 70.0
        assert trade["rs_at_exit"]["rs_score"] == 82.0
        assert trade["trade_metadata_entry"]["setup_type"] == "breakout"
        assert trade["trade_metadata_exit"]["exit_signal"] == "target"
