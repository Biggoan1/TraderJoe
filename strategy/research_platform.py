"""Research Platform Interface
===============================

A clean, stable interface that exposes Trader Joe research data without
exposing implementation details. Consumers (dashboards, Telegram reports,
future APIs, Backtest Lab) read through this interface rather than
touching implementation-specific files directly.

Design principles:
  - Read-only — never modifies trading state
  - No secrets, no API keys, no trading controls
  - Stable contract — consumers depend on this interface
  - Backward compatible — new fields added without breaking existing ones
  - Single source of truth — dashboard, reports, and APIs all call the same methods

Usage:
    from strategy.research_platform import ResearchPlatform

    platform = ResearchPlatform()

    # Portfolio overview
    portfolio = platform.get_portfolio()

    # Performance metrics
    performance = platform.get_performance()

    # Market intelligence
    market = platform.get_market_intelligence()

    # Trade history
    trades = platform.get_trade_history(limit=50)

    # Research notes
    notes = platform.get_research_notes(limit=10)

    # Full snapshot (for dashboard generation)
    snapshot = platform.get_snapshot()
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from strategy.trade_logger import TradeLogger
from strategy.config import get_feature_flags


# ---------------------------------------------------------------------------
# Data contracts — these define what consumers can expect
# ---------------------------------------------------------------------------

@dataclass
class PortfolioData:
    """Read-only portfolio snapshot."""
    equity: float = 0.0
    cash: float = 0.0
    daily_pl: float = 0.0
    total_pl: float = 0.0
    positions: List[Dict[str, Any]] = field(default_factory=list)
    exposure_by_symbol: Dict[str, float] = field(default_factory=dict)
    exposure_by_sector: Dict[str, float] = field(default_factory=dict)
    timestamp: str = ""

    def merge_alpaca_data(self, data: Dict[str, Any]) -> None:
        """Merge Alpaca portfolio data into this snapshot."""
        if "equity" in data:
            self.equity = float(data["equity"])
        if "cash" in data:
            self.cash = float(data["cash"])
        if "daily_pl" in data:
            self.daily_pl = float(data["daily_pl"])
        if "total_pl" in data:
            self.total_pl = float(data["total_pl"])
        if "positions" in data and isinstance(data["positions"], list):
            for pos in data["positions"]:
                symbol = pos.get("symbol", "")
                if symbol:
                    self.positions.append({
                        "symbol": symbol,
                        "quantity": float(pos.get("qty", pos.get("quantity", 0))),
                        "entry_price": float(pos.get("avg_entry_price", 0)),
                        "current_price": float(pos.get("current_price", pos.get("last_price", 0))),
                        "unrealized_pl": float(pos.get("unrealized_pl", 0)),
                    })
                    exposure = float(pos.get("current_price", 0)) * float(pos.get("qty", 0))
                    self.exposure_by_symbol[symbol] = round(exposure, 2)


@dataclass
class PerformanceData:
    """Read-only performance metrics from closed trades."""
    win_rate: float = 0.0
    profit_factor: float = 0.0
    average_winner: float = 0.0
    average_loser: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    best_trade: Optional[Dict[str, Any]] = None
    worst_trade: Optional[Dict[str, Any]] = None
    total_pl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    avg_holding_seconds: float = 0.0
    max_drawdown: float = 0.0
    trades_by_exit_reason: Dict[str, int] = field(default_factory=dict)
    trades_by_side: Dict[str, int] = field(default_factory=dict)


@dataclass
class MarketRegimeData:
    """Current market regime classification."""
    regime: str = "unknown"
    confidence: float = 0.0
    spy_trend: str = "unknown"
    qqq_trend: str = "unknown"
    regime_scores: Dict[str, float] = field(default_factory=dict)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class RelativeStrengthData:
    """Relative strength leaders and laggards."""
    leaders: List[Dict[str, Any]] = field(default_factory=list)
    laggards: List[Dict[str, Any]] = field(default_factory=list)
    all_symbols: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class MarketIntelligenceData:
    """Aggregated market intelligence."""
    regime: MarketRegimeData = field(default_factory=MarketRegimeData)
    relative_strength: RelativeStrengthData = field(default_factory=RelativeStrengthData)
    timestamp: str = ""


@dataclass
class ResearchNote:
    """Single research note entry."""
    date: str = ""
    content: str = ""
    market_regime: str = ""
    strongest_sectors: List[str] = field(default_factory=list)
    weakest_sectors: List[str] = field(default_factory=list)
    rs_leaders: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    hypotheses: List[str] = field(default_factory=list)
    confidence: str = ""
    data_needed: List[str] = field(default_factory=list)


@dataclass
class ResearchSnapshot:
    """Complete research data snapshot for dashboard/API consumers."""
    portfolio: PortfolioData = field(default_factory=PortfolioData)
    performance: PerformanceData = field(default_factory=PerformanceData)
    market_intelligence: MarketIntelligenceData = field(default_factory=MarketIntelligenceData)
    recent_trades: List[Dict[str, Any]] = field(default_factory=list)
    research_notes: List[ResearchNote] = field(default_factory=list)
    daily_digests: List[Dict[str, Any]] = field(default_factory=list)
    feature_flags: Dict[str, bool] = field(default_factory=dict)
    generated_at: str = ""


# ---------------------------------------------------------------------------
# Research Platform — the single interface consumers use
# ---------------------------------------------------------------------------

class ResearchPlatform:
    """Read-only research data interface.

    Aggregates data from TradeLogger, MarketRegimeAnalyzer,
    RelativeStrengthCalculator, daily digests, and research notes.

    Consumers should depend on this interface, not on individual modules.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        logger: Optional[TradeLogger] = None,
    ):
        self.base_dir = Path(base_dir) if base_dir else Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.logger = logger or TradeLogger(str(self.base_dir / "trades_history.db"))
        self._reports_dir = self.base_dir / "reports"
        self._notes_dir = self.base_dir / "research_notes"
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        self._notes_dir.mkdir(parents=True, exist_ok=True)

    # ---- Public API -------------------------------------------------------

    def get_portfolio(self) -> PortfolioData:
        """Get current portfolio overview.

        Reads from Alpaca paper trading API if available, falls back to
        trade log data. No secrets exposed.
        """
        portfolio = PortfolioData()
        portfolio.timestamp = datetime.now(timezone.utc).isoformat()

        # Get open trades from our log
        open_trades = self.logger.get_open_trades()
        for trade in open_trades:
            pos = {
                "symbol": trade.get("symbol", ""),
                "quantity": trade.get("quantity", 0),
                "entry_price": trade.get("entry_price", 0),
                "entry_time": trade.get("entry_time", ""),
            }
            portfolio.positions.append(pos)
            symbol = trade.get("symbol", "")
            if symbol:
                exposure = (trade.get("entry_price", 0) or 0) * (trade.get("quantity", 0) or 0)
                portfolio.exposure_by_symbol[symbol] = round(exposure, 2)

        # Try to get live portfolio from trader module
        try:
            portfolio.merge_alpaca_data(self._fetch_alpaca_portfolio())
        except Exception:
            pass

        return portfolio

    def get_performance(self) -> PerformanceData:
        """Get performance metrics from closed trades."""
        report = self.logger.get_summary_report()

        performance = PerformanceData(
            win_rate=report.get("win_rate", 0.0),
            profit_factor=float(report.get("profit_factor", 0.0)) if report.get("profit_factor") != "inf" else 999.0,
            average_winner=report.get("average_winner", 0.0),
            average_loser=report.get("average_loser", 0.0),
            total_trades=report.get("total_trades", 0),
            wins=report.get("wins", 0),
            losses=report.get("losses", 0),
            best_trade=report.get("best_trade"),
            worst_trade=report.get("worst_trade"),
            total_pl=report.get("total_pl", 0.0),
            gross_profit=report.get("gross_profit", 0.0),
            gross_loss=report.get("gross_loss", 0.0),
            avg_holding_seconds=report.get("avg_holding_period_seconds", 0.0),
            trades_by_exit_reason=report.get("trades_by_exit_reason", {}),
            trades_by_side=report.get("trades_by_side", {}),
        )

        # Calculate max drawdown from trade sequence
        performance.max_drawdown = self._calculate_max_drawdown()

        return performance

    def get_market_intelligence(self) -> MarketIntelligenceData:
        """Get current market intelligence."""
        intelligence = MarketIntelligenceData()
        intelligence.timestamp = datetime.now(timezone.utc).isoformat()

        # Market regime
        try:
            from strategy.market_regime import MarketRegimeAnalyzer
            analyzer = MarketRegimeAnalyzer()
            regime_result = analyzer.classify()
            intelligence.regime = MarketRegimeData(
                regime=regime_result.regime,
                confidence=regime_result.confidence,
                regime_scores=regime_result.regime_scores,
                signals=[s.__dict__ if hasattr(s, '__dict__') else {} for s in regime_result.signals],
                timestamp=regime_result.date,
            )
            # Derive individual trends from signals
            for sig in regime_result.signals:
                name = sig.name if hasattr(sig, 'name') else ""
                if "SPY" in name and "price_vs_ma" in name:
                    intelligence.regime.spy_trend = "up" if sig.bullish_score > sig.bearish_score else "down"
                elif "QQQ" in name and "price_vs_ma" in name:
                    intelligence.regime.qqq_trend = "up" if sig.bullish_score > sig.bearish_score else "down"
        except Exception:
            pass

        # Relative strength
        try:
            intelligence.relative_strength = self._fetch_relative_strength()
        except Exception:
            pass

        return intelligence

    def get_trade_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent trade history."""
        all_trades = self.logger.get_all_trades()
        trades = []
        for t in all_trades[:limit]:
            trade = {
                "id": t.get("id"),
                "symbol": t.get("symbol", ""),
                "side": t.get("side", ""),
                "entry_time": t.get("entry_time", ""),
                "exit_time": t.get("exit_time"),
                "entry_price": t.get("entry_price", 0),
                "exit_price": t.get("exit_price"),
                "quantity": t.get("quantity", 0),
                "pl": t.get("pl"),
                "pl_percent": t.get("pl_percent"),
                "holding_period_seconds": t.get("holding_period_seconds"),
                "entry_score": t.get("entry_score"),
                "exit_reason": t.get("exit_reason"),
                "market_regime": t.get("market_regime"),
                "rs_at_entry": self._parse_json_field(t.get("rs_at_entry")),
            }
            trades.append(trade)
        return trades

    def get_daily_digests(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent daily digest files."""
        digests = []
        if not self._reports_dir.exists():
            return digests

        digest_files = sorted(
            [f for f in self._reports_dir.glob("daily_digest_*.md")],
            reverse=True,
        )[:limit]

        for f in digest_files:
            date_str = f.stem.replace("daily_digest_", "")
            try:
                content = f.read_text()
                digests.append({
                    "date": date_str,
                    "filename": f.name,
                    "content": content,
                })
            except Exception:
                pass

        return digests

    def get_research_notes(self, limit: int = 10) -> List[ResearchNote]:
        """Get research notes from the notebook."""
        notes = []
        if not self._notes_dir.exists():
            return notes

        note_files = sorted(
            [f for f in self._notes_dir.glob("*.json")],
            reverse=True,
        )[:limit]

        for f in note_files:
            try:
                data = json.loads(f.read_text())
                note = ResearchNote(
                    date=data.get("date", ""),
                    content=data.get("content", ""),
                    market_regime=data.get("market_regime", ""),
                    strongest_sectors=data.get("strongest_sectors", []),
                    weakest_sectors=data.get("weakest_sectors", []),
                    rs_leaders=data.get("rs_leaders", []),
                    patterns=data.get("patterns", []),
                    hypotheses=data.get("hypotheses", []),
                    confidence=data.get("confidence", ""),
                    data_needed=data.get("data_needed", []),
                )
                notes.append(note)
            except Exception:
                pass

        return notes

    def get_snapshot(self) -> ResearchSnapshot:
        """Get a complete research data snapshot.

        This is the primary method for dashboard generation.
        Returns all research data in a single call.
        """
        snapshot = ResearchSnapshot()
        snapshot.generated_at = datetime.now(timezone.utc).isoformat()
        snapshot.portfolio = self.get_portfolio()
        snapshot.performance = self.get_performance()
        snapshot.market_intelligence = self.get_market_intelligence()
        snapshot.recent_trades = self.get_trade_history(limit=50)
        snapshot.research_notes = self.get_research_notes(limit=10)
        snapshot.daily_digests = self.get_daily_digests(limit=10)
        snapshot.feature_flags = {
            name: value
            for name, value in get_feature_flags().__dict__.items()
        }
        return snapshot

    def get_snapshot_json(self) -> str:
        """Get complete snapshot as JSON string (for dashboard)."""
        snapshot = self.get_snapshot()
        data = _dataclass_to_dict(snapshot)
        return json.dumps(data, indent=2, default=str)

    # ---- Internal helpers -------------------------------------------------

    def _fetch_alpaca_portfolio(self) -> Dict[str, Any]:
        """Try to fetch live portfolio from trader module.

        Returns empty dict on failure — never raises.
        """
        try:
            # Import trader to get portfolio info
            import importlib
            trader_mod = importlib.import_module("trader")
            if hasattr(trader_mod, "get_portfolio"):
                return trader_mod.get_portfolio()
        except Exception:
            pass
        return {}

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown from trade sequence."""
        all_trades = self.logger.get_closed_trades()
        if not all_trades:
            return 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for trade in all_trades:
            pl = trade.get("pl", 0) or 0
            cumulative += pl
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_dd:
                max_dd = drawdown

        return round(max_dd, 2)

    def _fetch_relative_strength(self) -> RelativeStrengthData:
        """Fetch current relative strength data."""
        rs_data = RelativeStrengthData()
        rs_data.timestamp = datetime.now(timezone.utc).isoformat()

        try:
            from strategy.relative_strength import RelativeStrengthCalculator

            # Get watchlist symbols
            watchlist_file = self.base_dir / "trending_watchlist.json"
            symbols = []
            if watchlist_file.exists():
                wl = json.loads(watchlist_file.read_text())
                symbols = [s.get("symbol", "") for s in wl if s.get("symbol")]

            if not symbols:
                symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "META", "SPY", "QQQ"]

            calc = RelativeStrengthCalculator()
            results = calc.calculate_watchlist_rs(symbols)

            all_items = []
            for r in results:
                d = r.to_dict()
                all_items.append(d)

            rs_data.all_symbols = all_items

            # Sort by score
            sorted_items = sorted(all_items, key=lambda x: x.get("rs_score", 0), reverse=True)
            rs_data.leaders = sorted_items[:5]
            rs_data.laggards = sorted_items[-5:]

        except Exception:
            pass

        return rs_data

    @staticmethod
    def _parse_json_field(value: Optional[str]) -> Optional[Any]:
        """Parse a JSON field from the database."""
        if not value:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value


# ---------------------------------------------------------------------------
# Utility: dataclass to dict conversion
# ---------------------------------------------------------------------------

def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclass to dict."""
    if dataclass_fields := getattr(obj, '__dataclass_fields__', None):
        return {
            field_name: _dataclass_to_dict(getattr(obj, field_name))
            for field_name in dataclass_fields
        }
    elif isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj
