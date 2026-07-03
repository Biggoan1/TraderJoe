"""Relative Strength Analysis Module
====================================

Calculates relative performance of symbols versus benchmarks (SPY, QQQ)
over configurable lookback periods. Observational only — does NOT influence
trading decisions.

Key metrics per symbol:
  - Relative performance vs SPY (percentage outperformance/underperformance)
  - Relative performance vs QQQ
  - Percentile ranking across the watchlist
  - Trend direction (improving / declining / stable)
  - Composite RS score (0-100)

Usage:
    from strategy.relative_strength import RelativeStrengthCalculator

    calc = RelativeStrengthCalculator()
    results = calc.calculate_watchlist_rs(
        symbols=["AAPL", "MSFT", "NVDA"],
        lookback_periods=[5, 20, 60],
        benchmarks=["SPY", "QQQ"],
    )

    for result in results:
        print(f"{result.symbol}: RS vs SPY (20d) = {result.rs_vs_benchmark.get('SPY', {}).get('20d', 0):+.2f}%")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import yfinance as yf

import pandas as pd


def relative_return_pct(
    sym_closes: Sequence[float],
    bench_closes: Sequence[float],
    lookback_days: int,
) -> Optional[float]:
    """Pure-Python rolling relative return.

    Accepts two index-aligned close-price sequences and returns the
    percentage-point outperformance of the symbol vs the benchmark
    over ``lookback_days`` bars — ``sym_return_pct - bench_return_pct``
    where each ``return_pct`` is measured from the ``t - lookback``
    close to the final close of the sequence.

    Returns ``None`` if ``lookback_days`` is non-positive, if either
    sequence has fewer than ``lookback_days + 1`` entries, or if
    either start price is zero.

    ``RelativeStrengthCalculator._relative_return`` delegates to this
    helper after aligning DataFrames.  Extracting the primitive lets
    research/replay code compute RS from bar-lists without pulling in
    pandas or yfinance.
    """
    if lookback_days <= 0:
        return None
    if (
        len(sym_closes) < lookback_days + 1
        or len(bench_closes) < lookback_days + 1
    ):
        return None
    sym_start = float(sym_closes[-(lookback_days + 1)])
    sym_end = float(sym_closes[-1])
    bench_start = float(bench_closes[-(lookback_days + 1)])
    bench_end = float(bench_closes[-1])
    if sym_start == 0 or bench_start == 0:
        return None
    sym_return = ((sym_end - sym_start) / sym_start) * 100
    bench_return = ((bench_end - bench_start) / bench_start) * 100
    return round(sym_return - bench_return, 2)


# ---------------------------------------------------------------------------
# Configurable defaults
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACK_PERIODS: List[int] = [5, 20, 60]
DEFAULT_BENCHMARKS: List[str] = ["SPY", "QQQ"]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RelativeStrengthResult:
    """Relative strength analysis for a single symbol.

    Attributes:
        symbol: Ticker symbol
        rs_vs_benchmark: Dict mapping benchmark -> {lookback_key: pct_diff}
            e.g. {"SPY": {"5d": 2.5, "20d": -1.3}, "QQQ": {"5d": 1.8, ...}}
            Positive means symbol outperformed benchmark.
        percentile_rank: Dict mapping benchmark -> {lookback_key: percentile (0-100)}
            Higher = better relative to peer group.
        trend_direction: Overall trend: "improving" / "declining" / "stable"
        rs_score: Composite score 0-100 based on all lookbacks and benchmarks
    """
    symbol: str
    rs_vs_benchmark: Dict[str, Dict[str, float]] = field(default_factory=dict)
    percentile_rank: Dict[str, Dict[str, float]] = field(default_factory=dict)
    trend_direction: str = "stable"
    rs_score: float = 50.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON / database storage."""
        return {
            "symbol": self.symbol,
            "rs_vs_benchmark": self.rs_vs_benchmark,
            "percentile_rank": self.percentile_rank,
            "trend_direction": self.trend_direction,
            "rs_score": round(self.rs_score, 2),
        }


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class PriceFetcher:
    """Fetch historical prices via yfinance."""

    @staticmethod
    def fetch(symbol: str, period: str = "6mo", interval: str = "1d") -> Optional[pd.DataFrame]:
        """Fetch adjusted close prices for a symbol.

        Returns DataFrame with 'Close' column or None on failure.
        """
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.history(period=period, interval=interval, auto_adjust=True)
        except Exception:
            return None

        if data.empty:
            return None

        # Flatten MultiIndex columns
        if getattr(data.columns, "nlevels", 1) > 1:
            data.columns = data.columns.get_level_values(0)

        if "Close" not in data.columns:
            return None

        return data[["Close"]].dropna()


# ---------------------------------------------------------------------------
# Core calculator
# ---------------------------------------------------------------------------

class RelativeStrengthCalculator:
    """Calculate relative strength of symbols versus benchmarks.

    OBSERVATIONAL ONLY — results are for data collection and reporting.
    Do NOT use to influence trading decisions until validated.
    """

    def __init__(
        self,
        lookback_periods: Optional[List[int]] = None,
        benchmarks: Optional[List[str]] = None,
    ):
        self.lookback_periods = lookback_periods or DEFAULT_LOOKBACK_PERIODS
        self.benchmarks = benchmarks or DEFAULT_BENCHMARKS

    # ---- public API -------------------------------------------------------

    def calculate_watchlist_rs(
        self,
        symbols: List[str],
        period: str = "6mo",
    ) -> List[RelativeStrengthResult]:
        """Calculate relative strength for a list of symbols.

        Args:
            symbols: List of ticker symbols to analyze.
            period: yfinance history period (default "6mo" covers 60 trading days).

        Returns:
            List of RelativeStrengthResult, one per symbol.
        """
        # Fetch prices for all symbols + benchmarks in parallel
        all_symbols = list(symbols) + self.benchmarks
        prices = self._fetch_prices(all_symbols, period)

        if not prices:
            return []

        # Calculate RS for each symbol
        results: List[RelativeStrengthResult] = []
        for symbol in symbols:
            result = self._calculate_symbol_rs(symbol, prices)
            results.append(result)

        # Calculate percentile ranks across the watchlist
        self._calculate_percentiles(results)

        # Calculate trend direction and RS score
        for result in results:
            self._calculate_trend(result)
            self._calculate_score(result)

        return results

    def calculate_single_rs(
        self,
        symbol: str,
        watchlist_results: Optional[List[RelativeStrengthResult]] = None,
        period: str = "6mo",
    ) -> Optional[RelativeStrengthResult]:
        """Calculate RS for a single symbol.

        Args:
            symbol: Single ticker symbol.
            watchlist_results: Pre-calculated results for percentile ranking.
                If None, calculates only raw RS without percentile.
            period: yfinance history period.

        Returns:
            RelativeStrengthResult or None if data unavailable.
        """
        results = self.calculate_watchlist_rs([symbol], period)
        if results:
            return results[0]
        return None

    def get_rs_snapshot(
        self,
        symbol: str,
        period: str = "6mo",
    ) -> Optional[Dict[str, Any]]:
        """Get a compact RS snapshot for database storage.

        Returns a JSON-serializable dict suitable for storing in a trade
        log entry.
        """
        result = self.calculate_single_rs(symbol, period=period)
        if result is None:
            return None
        return result.to_dict()

    # ---- internal ---------------------------------------------------------

    def _fetch_prices(
        self, symbols: List[str], period: str
    ) -> Dict[str, pd.DataFrame]:
        """Fetch prices for multiple symbols.

        Returns dict mapping symbol -> DataFrame with 'Close' column.
        """
        prices: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            df = PriceFetcher.fetch(symbol, period)
            if df is not None:
                prices[symbol] = df
        return prices

    def _calculate_symbol_rs(
        self,
        symbol: str,
        prices: Dict[str, pd.DataFrame],
    ) -> RelativeStrengthResult:
        """Calculate relative performance of symbol vs each benchmark."""
        sym_df = prices.get(symbol)
        if sym_df is None:
            return RelativeStrengthResult(symbol=symbol)

        rs_vs_benchmark: Dict[str, Dict[str, float]] = {}

        for benchmark in self.benchmarks:
            bench_df = prices.get(benchmark)
            if bench_df is None:
                continue

            rs_dict: Dict[str, float] = {}
            for lookback in self.lookback_periods:
                key = f"{lookback}d"
                pct = self._relative_return(sym_df, bench_df, lookback)
                if pct is not None:
                    rs_dict[key] = round(pct, 2)

            rs_vs_benchmark[benchmark] = rs_dict

        return RelativeStrengthResult(
            symbol=symbol,
            rs_vs_benchmark=rs_vs_benchmark,
        )

    @staticmethod
    def _relative_return(
        sym_df: pd.DataFrame,
        bench_df: pd.DataFrame,
        lookback_days: int,
    ) -> Optional[float]:
        """Calculate relative return of symbol vs benchmark over lookback period.

        Returns percentage points of outperformance (positive = outperformed).
        E.g., if symbol returned 5% and benchmark returned 3%, returns 2.0.
        """
        aligned = sym_df.join(
            bench_df, how="inner", lsuffix="_sym", rsuffix="_bench"
        )
        if aligned.empty:
            return None
        return relative_return_pct(
            aligned["Close_sym"].tolist(),
            aligned["Close_bench"].tolist(),
            lookback_days,
        )

    def _calculate_percentiles(
        self, results: List[RelativeStrengthResult]
    ) -> None:
        """Calculate percentile rank for each symbol within the watchlist.

        For each benchmark and lookback, ranks symbols by their RS value.
        Percentile 0 = worst performer, 100 = best.
        """
        if not results:
            return

        for benchmark in self.benchmarks:
            for lookback in self.lookback_periods:
                key = f"{lookback}d"

                # Collect RS values for this benchmark/lookback
                rs_values: List[tuple] = []
                for i, result in enumerate(results):
                    val = result.rs_vs_benchmark.get(benchmark, {}).get(key)
                    if val is not None:
                        rs_values.append((i, val))

                if len(rs_values) <= 1:
                    # Solo or no data — default to 50th percentile
                    for i, val in rs_values:
                        results[i].percentile_rank.setdefault(benchmark, {})[key] = 50.0
                    continue

                # Sort by value ascending
                rs_values.sort(key=lambda x: x[1])

                # Calculate percentile rank (min-max within group)
                n = len(rs_values)
                for rank, (idx, val) in enumerate(rs_values):
                    if n == 1:
                        pct = 50.0
                    else:
                        pct = (rank / (n - 1)) * 100
                    pct = round(pct, 1)
                    results[idx].percentile_rank.setdefault(benchmark, {})[key] = pct

    def _calculate_trend(self, result: RelativeStrengthResult) -> None:
        """Determine trend direction based on short vs long lookback.

        If short-term RS (5d) > long-term RS (60d), symbol is improving.
        If short-term RS < long-term RS, symbol is declining.
        Otherwise stable.
        """
        # Collect all RS values across benchmarks
        short_vals: List[float] = []
        long_vals: List[float] = []

        for benchmark in self.benchmarks:
            rs_dict = result.rs_vs_benchmark.get(benchmark, {})
            val_5d = rs_dict.get("5d")
            val_60d = rs_dict.get("60d")
            val_20d = rs_dict.get("20d")

            if val_5d is not None:
                short_vals.append(val_5d)
            if val_60d is not None:
                long_vals.append(val_60d)
            elif val_20d is not None:
                # Use 20d as fallback for long-term if 60d unavailable
                long_vals.append(val_20d)

        if not short_vals or not long_vals:
            result.trend_direction = "stable"
            return

        avg_short = sum(short_vals) / len(short_vals)
        avg_long = sum(long_vals) / len(long_vals)

        diff = avg_short - avg_long

        # Threshold: 1.5 percentage points
        if diff > 1.5:
            result.trend_direction = "improving"
        elif diff < -1.5:
            result.trend_direction = "declining"
        else:
            result.trend_direction = "stable"

    def _calculate_score(self, result: RelativeStrengthResult) -> None:
        """Calculate composite RS score (0-100).

        Score is based on:
        - Average RS across all benchmarks and lookbacks (normalized to 0-50 points)
        - Average percentile rank (0-50 points)

        A score of 50 means average relative performance.
        A score of 80+ means strong outperformance.
        A score below 20 means significant underperformance.
        """
        # Collect all RS values
        all_rs: List[float] = []
        for benchmark in self.benchmarks:
            rs_dict = result.rs_vs_benchmark.get(benchmark, {})
            all_rs.extend(rs_dict.values())

        if not all_rs:
            result.rs_score = 50.0
            return

        # RS component: normalize around 0, cap at ±10 for 0-50 range
        avg_rs = sum(all_rs) / len(all_rs)
        # Map [-10, 10] -> [0, 50] with 0 RS = 25 points
        rs_component = 25 + (avg_rs * 2.5)
        rs_component = max(0.0, min(50.0, rs_component))

        # Percentile component: average percentile across all lookbacks
        all_percentiles: List[float] = []
        for benchmark in self.benchmarks:
            pct_dict = result.percentile_rank.get(benchmark, {})
            all_percentiles.extend(pct_dict.values())

        if all_percentiles:
            avg_pct = sum(all_percentiles) / len(all_percentiles)
            pct_component = (avg_pct / 100.0) * 50.0
        else:
            pct_component = 25.0

        result.rs_score = round(rs_component + pct_component, 1)


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

# Default calculator instance (lazy initialization)
_calculator: Optional[RelativeStrengthCalculator] = None


def get_calculator() -> RelativeStrengthCalculator:
    """Get the module-level calculator singleton."""
    global _calculator
    if _calculator is None:
        _calculator = RelativeStrengthCalculator()
    return _calculator


def calculate_watchlist_rs(
    symbols: List[str],
    lookback_periods: Optional[List[int]] = None,
    benchmarks: Optional[List[str]] = None,
    period: str = "6mo",
) -> List[RelativeStrengthResult]:
    """Convenience function to calculate RS for a watchlist."""
    calc = RelativeStrengthCalculator(
        lookback_periods=lookback_periods,
        benchmarks=benchmarks,
    )
    return calc.calculate_watchlist_rs(symbols, period=period)


def format_rs_result(
    result: RelativeStrengthResult,
    include_details: bool = True,
) -> str:
    """Format a RelativeStrengthResult as a human-readable string.

    Args:
        result: The result to format.
        include_details: If True, include all lookback details. If False,
            show only score and trend.
    """
    lines: List[str] = []

    if include_details:
        # RS vs SPY
        spy_rs = result.rs_vs_benchmark.get("SPY", {})
        if spy_rs:
            spy_parts = [f"{k}: {v:+.2f}%" for k, v in spy_rs.items()]
            lines.append(f"  vs SPY: {', '.join(spy_parts)}")

        # RS vs QQQ
        qqq_rs = result.rs_vs_benchmark.get("QQQ", {})
        if qqq_rs:
            qqq_parts = [f"{k}: {v:+.2f}%" for k, v in qqq_rs.items()]
            lines.append(f"  vs QQQ: {', '.join(qqq_parts)}")

        # Percentile ranks
        spy_pct = result.percentile_rank.get("SPY", {})
        if spy_pct:
            spy_pct_parts = [f"{k}: {v:.0f}th" for k, v in spy_pct.items()]
            lines.append(f"  SPY%:  {', '.join(spy_pct_parts)}")

        qqq_pct = result.percentile_rank.get("QQQ", {})
        if qqq_pct:
            qqq_pct_parts = [f"{k}: {v:.0f}th" for k, v in qqq_pct.items()]
            lines.append(f"  QQQ%:  {', '.join(qqq_pct_parts)}")

    # Score and trend
    trend_icon = {"improving": "📈", "declining": "📉", "stable": "➡️"}.get(
        result.trend_direction, "➡️"
    )
    lines.append(f"  Score: {result.rs_score:.0f}/100  {trend_icon} {result.trend_direction}")

    return "\n".join(lines)


def format_rs_summary(
    results: List[RelativeStrengthResult],
    top_n: int = 10,
) -> str:
    """Format a ranked summary of RS results.

    Returns a string showing the top and bottom performers.
    """
    if not results:
        return "  No relative strength data available."

    # Sort by RS score descending
    sorted_results = sorted(results, key=lambda r: r.rs_score, reverse=True)

    lines: List[str] = []
    lines.append(f"  {'Symbol':<8} {'Score':>6} {'Trend':<10}")
    lines.append(f"  {'-'*30}")

    # Top performers
    for r in sorted_results[:top_n]:
        trend_icon = {"improving": "📈", "declining": "📉", "stable": "➡️"}.get(
            r.trend_direction, "➡️"
        )
        lines.append(
            f"  {r.symbol:<8} {r.rs_score:>5.0f}  {trend_icon} {r.trend_direction:<10}"
        )

    return "\n".join(lines)
