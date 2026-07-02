"""Tests for strategy/sector_leadership.py."""

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from strategy.sector_leadership import (
    DEFAULT_BENCHMARK,
    DEFAULT_SECTOR_ETFS,
    SectorLeadershipAnalyzer,
    SectorLeadershipReport,
    SectorLeadershipResult,
    calculate_period_return,
    compute_leadership_score,
    determine_sector_trend,
    format_sector_leadership,
    get_analyzer,
)
from strategy.config import reset_feature_flags


def _close_df(prices):
    return pd.DataFrame(
        {"Close": [float(price) for price in prices]},
        index=pd.date_range("2026-01-01", periods=len(prices), freq="B"),
    )


class TestCalculations:
    def test_calculate_period_return(self):
        df = _close_df([100, 102, 104, 106, 108, 110])
        assert calculate_period_return(df, 5) == pytest.approx(10.0)

    def test_calculate_period_return_missing_data(self):
        assert calculate_period_return(None, 5) is None
        assert calculate_period_return(pd.DataFrame(), 5) is None
        assert calculate_period_return(pd.DataFrame({"Open": [1, 2]}), 1) is None

    def test_calculate_period_return_insufficient_history(self):
        df = _close_df([100, 101, 102])
        assert calculate_period_return(df, 5) is None

    def test_calculate_period_return_zero_start(self):
        df = _close_df([0, 101, 102, 103, 104, 105])
        assert calculate_period_return(df, 5) is None

    def test_compute_leadership_score_default(self):
        assert compute_leadership_score({}) == 50.0

    def test_compute_leadership_score_positive_and_negative(self):
        assert compute_leadership_score({"5d": 2.0, "20d": 3.0}) == 60.0
        assert compute_leadership_score({"5d": -2.0, "20d": -3.0}) == 40.0

    def test_compute_leadership_score_clamped(self):
        assert compute_leadership_score({"5d": 20.0}) == 100.0
        assert compute_leadership_score({"5d": -20.0}) == 0.0

    def test_determine_sector_trend(self):
        assert determine_sector_trend({"5d": 4.0, "20d": 1.0}) == "improving"
        assert determine_sector_trend({"5d": 0.0, "20d": 3.0}) == "declining"
        assert determine_sector_trend({"20d": 3.0, "60d": 2.0}) == "leading"
        assert determine_sector_trend({"20d": -3.0, "60d": -2.0}) == "lagging"
        assert determine_sector_trend({"5d": 0.2, "20d": 0.1}) == "stable"
        assert determine_sector_trend({}) == "stable"


class TestSerialization:
    def test_result_to_dict(self):
        result = SectorLeadershipResult(
            sector="Technology",
            symbol="XLK",
            returns={"5d": 1.23456},
            relative_returns={"5d": 2.34567},
            leadership_score=61.234,
            rank=1,
            trend_direction="leading",
            data_quality="complete",
            details={"benchmark": "SPY"},
        )
        d = result.to_dict()
        assert d["sector"] == "Technology"
        assert d["returns"]["5d"] == pytest.approx(1.2346)
        assert d["relative_returns"]["5d"] == pytest.approx(2.3457)
        assert d["leadership_score"] == pytest.approx(61.23)
        json.dumps(d)

    def test_report_to_dict(self):
        result = SectorLeadershipResult(sector="Technology", symbol="XLK", rank=1)
        report = SectorLeadershipReport(
            timestamp="2026-07-02T12:00:00+00:00",
            benchmark="SPY",
            lookback_periods=[5, 20],
            sectors_analyzed=1,
            sectors_with_data=1,
            strongest_sectors=["Technology"],
            weakest_sectors=["Technology"],
            results=[result],
            data_quality="complete",
        )
        d = report.to_dict()
        assert d["benchmark"] == "SPY"
        assert d["results"][0]["sector"] == "Technology"
        json.dumps(d)


class TestSectorLeadershipAnalyzer:
    def test_init_defaults(self):
        analyzer = SectorLeadershipAnalyzer()
        assert analyzer.sectors == DEFAULT_SECTOR_ETFS
        assert analyzer.benchmark == DEFAULT_BENCHMARK
        assert analyzer.lookback_periods == [5, 20, 60]

    def test_analyze_ranks_sectors_with_mocked_provider(self):
        provider = MagicMock()
        price_map = {
            "SPY": _close_df([100 + i for i in range(70)]),
            "XLK": _close_df([100 + i * 2 for i in range(70)]),
            "XLE": _close_df([100 + i * 0.5 for i in range(70)]),
            "XLU": _close_df([100 - i * 0.2 for i in range(70)]),
        }
        provider.fetch.side_effect = lambda symbol, period="6mo": price_map.get(symbol)

        analyzer = SectorLeadershipAnalyzer(
            sectors={"Technology": "XLK", "Energy": "XLE", "Utilities": "XLU"},
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.sectors_analyzed == 3
        assert report.sectors_with_data == 3
        assert report.sectors_missing_data == 0
        assert report.data_quality == "complete"
        assert report.results[0].sector == "Technology"
        assert report.results[0].rank == 1
        assert report.strongest_sectors[0] == "Technology"
        assert "Utilities" in report.weakest_sectors
        provider.fetch.assert_any_call("SPY", period="6mo")
        provider.fetch.assert_any_call("XLK", period="6mo")

    def test_analyze_handles_missing_sector_data(self):
        provider = MagicMock()
        provider.fetch.side_effect = lambda symbol, period="6mo": (
            _close_df([100 + i for i in range(70)]) if symbol in {"SPY", "XLK"} else None
        )

        analyzer = SectorLeadershipAnalyzer(
            sectors={"Technology": "XLK", "Energy": "XLE"},
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.sectors_analyzed == 2
        assert report.sectors_with_data == 1
        assert report.sectors_missing_data == 1
        assert report.data_quality == "partial"
        assert report.results[-1].sector == "Energy"
        assert report.results[-1].data_quality == "missing"

    def test_analyze_handles_missing_benchmark_as_partial(self):
        provider = MagicMock()
        provider.fetch.side_effect = lambda symbol, period="6mo": (
            _close_df([100 + i for i in range(70)]) if symbol == "XLK" else None
        )

        analyzer = SectorLeadershipAnalyzer(
            sectors={"Technology": "XLK"},
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.errors == ["Benchmark data unavailable: SPY"]
        assert report.sectors_with_data == 1
        assert report.results[0].relative_returns == {}
        assert report.results[0].data_quality == "partial"

    def test_analyze_handles_provider_exception(self):
        provider = MagicMock()
        provider.fetch.side_effect = RuntimeError("network down")

        analyzer = SectorLeadershipAnalyzer(
            sectors={"Technology": "XLK"},
            price_provider=provider,
        )
        report = analyzer.analyze()

        assert report.data_quality == "missing"
        assert report.sectors_missing_data == 1
        assert report.errors == ["Benchmark data unavailable: SPY"]

    def test_analyze_empty_sector_map(self):
        provider = MagicMock()
        provider.fetch.return_value = _close_df([100 + i for i in range(70)])

        analyzer = SectorLeadershipAnalyzer(sectors={}, price_provider=provider)
        report = analyzer.analyze()

        # Empty dict means use defaults by design.
        assert report.sectors_analyzed == len(DEFAULT_SECTOR_ETFS)

    def test_format_sector_leadership(self):
        report = SectorLeadershipReport(
            timestamp="2026-07-02T12:00:00+00:00",
            benchmark="SPY",
            lookback_periods=[5, 20],
            results=[
                SectorLeadershipResult(
                    sector="Technology",
                    symbol="XLK",
                    relative_returns={"20d": 2.0},
                    leadership_score=62,
                    rank=1,
                    trend_direction="leading",
                    data_quality="complete",
                )
            ],
        )
        text = format_sector_leadership(report)
        assert "Sector Leadership" in text
        assert "Observational only" in text
        assert "Technology" in text
        assert "20d vs SPY: +2.0%" in text

    def test_format_sector_leadership_empty(self):
        report = SectorLeadershipReport(
            timestamp="2026-07-02T12:00:00+00:00",
            benchmark="SPY",
            lookback_periods=[5, 20],
        )
        assert format_sector_leadership(report) == "No sector leadership data available."

    def test_get_analyzer(self):
        analyzer = get_analyzer(sectors={"Technology": "XLK"})
        assert isinstance(analyzer, SectorLeadershipAnalyzer)
        assert analyzer.sectors == {"Technology": "XLK"}

    def test_observational_only_contract(self):
        flags = reset_feature_flags()
        analyzer = SectorLeadershipAnalyzer(sectors={"Technology": "XLK"})
        assert flags.enable_sector_leadership is False
        assert "enable_sector_leadership" not in flags.enabled_flags
        assert not hasattr(analyzer, "buy")
        assert not hasattr(analyzer, "sell")
        assert not hasattr(analyzer, "execute")
        assert not hasattr(analyzer, "place_order")

        result = SectorLeadershipResult(sector="Technology", symbol="XLK")
        d = result.to_dict()
        assert "recommendation" not in d
        assert "action" not in d
