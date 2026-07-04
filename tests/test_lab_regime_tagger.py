"""Tests for strategy/lab/regime_tagger.py — t_lab_regime_tagger."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence

import pytest

from strategy.comparison_harness import (
    ChampionChallengerComparison,
    ChampionChallengerRunMetadata,
    DisagreementRecord,
    ScoreRow,
    ScoreTable,
)
from strategy.lab.experiment_runner import ExperimentBundle
from strategy.lab.regime_tagger import (
    DEFAULT_QUANTILES,
    KNOWN_MODES,
    MODE_QUANTILE,
    MODE_RATIO_QUANTILE,
    MODE_SMA_TREND,
    MODE_THRESHOLD,
    RegimeMetrics,
    RegimeSpec,
    RegimeTag,
    RegimeTaggerError,
    _compute_quantile_boundaries,
    _label_for,
    default_regime_specs,
    regime_conditioned_metrics,
    tag_events,
)
from strategy.lab.strategy import StrategyIdentity


# ---------------------------------------------------------------------------
# Warehouse fixture — write parquet files that _load_closes reads
# ---------------------------------------------------------------------------


@pytest.fixture
def wh(tmp_path: Path) -> Path:
    """Build a tiny fake warehouse under tmp_path with three
    synthetic ETFs: VXX (volatility), HYG + LQD (ratio), TLT (trend).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "wh"
    root.mkdir()
    ds = root / "equities" / "daily" / "regime-pack-test"
    ds.mkdir(parents=True)

    def _write_parquet(symbol: str, dates: Sequence[str], closes: Sequence[float]):
        sym_dir = ds / symbol
        sym_dir.mkdir()
        # Bucket by year
        by_year: Dict[str, List[Any]] = {}
        for d, c in zip(dates, closes):
            by_year.setdefault(d[:4], []).append((d, c))
        for year, rows in by_year.items():
            ts_col = pa.array([f"{d}T14:30:00+00:00" for d, _ in rows])
            close_col = pa.array([float(c) for _, c in rows])
            open_col = pa.array([float(c) for _, c in rows])
            high_col = pa.array([float(c) + 1.0 for _, c in rows])
            low_col = pa.array([float(c) - 1.0 for _, c in rows])
            vol_col = pa.array([1_000_000 for _ in rows])
            tbl = pa.table({
                "timestamp": ts_col,
                "open": open_col,
                "high": high_col,
                "low": low_col,
                "close": close_col,
                "volume": vol_col,
            })
            pq.write_table(tbl, sym_dir / f"{year}.parquet")

    # 60 sequential business-day dates starting 2020-01-06 (Mon)
    dates = []
    d = date(2020, 1, 6)
    while len(dates) < 60:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d = d + timedelta(days=1)

    # VXX: monotonic rise (25 → 40 over 60 days)
    vxx = [25.0 + i * (15.0 / 59.0) for i in range(60)]
    _write_parquet("VXX", dates, vxx)

    # HYG: modest rise 85 → 90; LQD: modest rise 120 → 125.
    # HYG/LQD ratio: mostly stable, small oscillation
    hyg = [85.0 + i * (5.0 / 59.0) + (0.5 if i % 5 == 0 else 0) for i in range(60)]
    lqd = [120.0 + i * (5.0 / 59.0) for i in range(60)]
    _write_parquet("HYG", dates, hyg)
    _write_parquet("LQD", dates, lqd)

    # TLT: up trend then down trend
    tlt = [140.0 + i * 0.5 for i in range(30)] + [155.0 - i * 0.3 for i in range(30)]
    _write_parquet("TLT", dates, tlt)

    return root


# ---------------------------------------------------------------------------
# RegimeSpec validation
# ---------------------------------------------------------------------------


class TestRegimeSpecValidation:
    def test_name_required(self):
        with pytest.raises(RegimeTaggerError, match="name"):
            RegimeSpec.quantile("", dataset_id="d", symbol="s")

    def test_unknown_mode_rejected(self):
        with pytest.raises(RegimeTaggerError, match="unknown mode"):
            RegimeSpec(name="x", mode="wibble", dataset_id="d")

    def test_quantile_needs_symbol(self):
        with pytest.raises(RegimeTaggerError, match="symbol"):
            RegimeSpec(name="v", mode=MODE_QUANTILE, dataset_id="d")

    def test_ratio_needs_two_symbols(self):
        with pytest.raises(RegimeTaggerError, match="ratio_quantile"):
            RegimeSpec(name="c", mode=MODE_RATIO_QUANTILE, dataset_id="d",
                       numerator_symbol="HYG")

    def test_quantile_label_count_mismatch(self):
        with pytest.raises(RegimeTaggerError, match="labels"):
            RegimeSpec.quantile("v", dataset_id="d", symbol="s",
                                quantiles=(0.5,), labels=("a", "b", "c"))

    def test_quantile_out_of_range(self):
        with pytest.raises(RegimeTaggerError, match="quantile"):
            RegimeSpec.quantile("v", dataset_id="d", symbol="s",
                                quantiles=(0.0, 1.0),
                                labels=("a", "b", "c"))

    def test_quantile_not_ascending(self):
        with pytest.raises(RegimeTaggerError, match="ascending"):
            RegimeSpec.quantile("v", dataset_id="d", symbol="s",
                                quantiles=(0.7, 0.3),
                                labels=("a", "b", "c"))

    def test_sma_period_too_small(self):
        with pytest.raises(RegimeTaggerError, match="sma_period"):
            RegimeSpec.sma_trend("t", dataset_id="d", symbol="s", sma_period=1)


# ---------------------------------------------------------------------------
# Deterministic empirical quantile boundaries
# ---------------------------------------------------------------------------


class TestQuantileBoundaries:
    def test_terciles_on_sorted_range(self):
        # values 0..29 with quantiles (1/3, 2/3) → boundaries at rank 10 and 20
        values = [float(i) for i in range(30)]
        boundaries = _compute_quantile_boundaries(values, (1.0/3.0, 2.0/3.0))
        assert boundaries == [10.0, 20.0]

    def test_empty_values(self):
        assert _compute_quantile_boundaries([], (0.5,)) == []

    def test_stable_across_input_order(self):
        a = _compute_quantile_boundaries([5.0, 1.0, 3.0, 2.0, 4.0], (0.5,))
        b = _compute_quantile_boundaries([1.0, 2.0, 3.0, 4.0, 5.0], (0.5,))
        assert a == b


# ---------------------------------------------------------------------------
# tag_events — deterministic labelling
# ---------------------------------------------------------------------------


class TestTagEventsVxxQuantile:
    def test_low_mid_high_buckets(self, wh):
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
            labels=("low_vol", "mid_vol", "high_vol"),
        )
        # 3 events spanning early / middle / late window
        events = [
            "2020-01-06T14:30:00Z",  # first day, VXX=25
            "2020-02-14T14:30:00Z",  # middle
            "2020-03-27T14:30:00Z",  # last day, VXX=40
        ]
        tags = tag_events(events, [spec], warehouse_root=wh)
        assert len(tags) == 3
        assert tags["2020-01-06"].labels["vol"] == "low_vol"
        assert tags["2020-03-27"].labels["vol"] == "high_vol"
        # Middle event lands in mid_vol (VXX around 33)
        assert tags["2020-02-14"].labels["vol"] == "mid_vol"

    def test_deterministic_repeatable(self, wh):
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
            labels=("low", "mid", "high"),
        )
        events = ["2020-02-14T14:30:00Z"]
        a = tag_events(events, [spec], warehouse_root=wh)
        b = tag_events(events, [spec], warehouse_root=wh)
        assert a["2020-02-14"].labels == b["2020-02-14"].labels


class TestTagEventsHygLqdRatio:
    def test_ratio_bucketing(self, wh):
        spec = RegimeSpec.ratio_quantile(
            "credit", dataset_id="regime-pack-test",
            numerator_symbol="HYG", denominator_symbol="LQD",
            labels=("stress", "mid", "calm"),
        )
        events = ["2020-01-06T14:30:00Z", "2020-03-27T14:30:00Z"]
        tags = tag_events(events, [spec], warehouse_root=wh)
        # Both dates should be labeled from the tercile buckets
        assert tags["2020-01-06"].labels["credit"] in {"stress", "mid", "calm"}
        assert tags["2020-03-27"].labels["credit"] in {"stress", "mid", "calm"}


class TestTagEventsTltSmaTrend:
    def test_up_when_price_above_sma(self, wh):
        spec = RegimeSpec.sma_trend(
            "bond_trend", dataset_id="regime-pack-test", symbol="TLT",
            sma_period=20, labels=("tlt_down", "tlt_up"),
        )
        # Day 25 (index 24) is well into the upward leg
        events = ["2020-02-10T14:30:00Z"]
        tags = tag_events(events, [spec], warehouse_root=wh)
        assert tags["2020-02-10"].labels["bond_trend"] == "tlt_up"

    def test_down_when_price_below_sma(self, wh):
        # Very late in the down-leg (after day 30), price now below SMA20
        spec = RegimeSpec.sma_trend(
            "bond_trend", dataset_id="regime-pack-test", symbol="TLT",
            sma_period=20, labels=("tlt_down", "tlt_up"),
        )
        events = ["2020-03-27T14:30:00Z"]
        tags = tag_events(events, [spec], warehouse_root=wh)
        assert tags["2020-03-27"].labels["bond_trend"] == "tlt_down"


class TestTagEventsMissingData:
    def test_symbol_absent_yields_none(self, wh):
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="NONEXISTENT",
        )
        events = ["2020-01-06T14:30:00Z"]
        tags = tag_events(events, [spec], warehouse_root=wh)
        assert tags["2020-01-06"].labels["vol"] is None

    def test_event_date_outside_series_yields_none(self, wh):
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
        )
        # Way outside the 2020-Q1 range
        events = ["2015-01-01T14:30:00Z"]
        tags = tag_events(events, [spec], warehouse_root=wh)
        assert tags["2015-01-01"].labels["vol"] is None

    def test_partial_missing_across_specs(self, wh):
        specs = [
            RegimeSpec.quantile("vol", dataset_id="regime-pack-test", symbol="VXX"),
            RegimeSpec.quantile("bad", dataset_id="regime-pack-test", symbol="MISSING"),
        ]
        events = ["2020-01-06T14:30:00Z"]
        tags = tag_events(events, specs, warehouse_root=wh)
        assert tags["2020-01-06"].labels["vol"] is not None
        assert tags["2020-01-06"].labels["bad"] is None


class TestTagEventsThreshold:
    def test_threshold_mode(self, wh):
        spec = RegimeSpec.threshold(
            "high_vxx", dataset_id="regime-pack-test", symbol="VXX",
            threshold=30.0, labels=("calm", "spike"),
        )
        events = [
            "2020-01-06T14:30:00Z",  # VXX=25, below
            "2020-03-27T14:30:00Z",  # VXX=40, above
        ]
        tags = tag_events(events, [spec], warehouse_root=wh)
        assert tags["2020-01-06"].labels["high_vxx"] == "calm"
        assert tags["2020-03-27"].labels["high_vxx"] == "spike"


# ---------------------------------------------------------------------------
# regime_conditioned_metrics
# ---------------------------------------------------------------------------


def _make_bundle(timestamps, disagreements) -> ExperimentBundle:
    tables = []
    for i, ts in enumerate(timestamps):
        # Two rows per event so total_rows_in_bucket has structure
        rows = [
            ScoreRow(symbol="AAPL", champion_score=1.0, challenger_score=1.05,
                     champion_selected=True, challenger_selected=True,
                     score_delta=0.05),
            ScoreRow(symbol="MSFT", champion_score=0.5, challenger_score=0.3,
                     champion_selected=False, challenger_selected=False,
                     score_delta=-0.2),
        ]
        tables.append(ScoreTable(
            event_timestamp=ts, event_type="market_snapshot",
            event_sequence=i + 1,
            champion_id="c", challenger_id="ch", rows=rows,
        ))
    metadata = ChampionChallengerRunMetadata(
        run_id="rr_x", champion_id="c", challenger_id="ch", dataset_id="synth",
        event_count=len(timestamps), score_delta_threshold=0.0, seed=0,
        generated_at="2026-07-04T00:00:00Z",
    )
    comparison = ChampionChallengerComparison(
        metadata=metadata, score_tables=tables,
        disagreements=disagreements, warnings=[],
    )
    vb = SimpleNamespace(comparison=comparison)
    manifest = SimpleNamespace(experiment_id="exp", warnings=())
    return ExperimentBundle(manifest=manifest, validation_bundle=vb, manifest_path="")


def _dis(event_ts, symbol, score_delta, champion_exp=""):
    return DisagreementRecord(
        event_timestamp=event_ts,
        event_type="market_snapshot",
        symbol=symbol,
        kind="score_delta",
        champion_id="c",
        challenger_id="ch",
        champion_score=0.5,
        challenger_score=0.5 + score_delta,
        score_delta=score_delta,
        champion_explanation=champion_exp,
    )


class TestRegimeConditionedMetrics:
    def test_metrics_shape(self, wh):
        # Two events: one in low_vol, one in high_vol
        events = ["2020-01-06T14:30:00Z", "2020-03-27T14:30:00Z"]
        # One positive delta in low_vol, one negative in high_vol
        disagreements = [
            _dis("2020-01-06T14:30:00Z", "AAPL", 0.1),
            _dis("2020-03-27T14:30:00Z", "AAPL", -0.15),
        ]
        bundle = _make_bundle(events, disagreements)
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
            labels=("low_vol", "mid_vol", "high_vol"),
        )
        result = regime_conditioned_metrics(bundle, [spec], warehouse_root=wh)
        assert "vol" in result
        vol_metrics = result["vol"]
        # Both labels seen
        assert "low_vol" in vol_metrics
        assert "high_vol" in vol_metrics
        # Field shape
        m = vol_metrics["low_vol"]
        assert isinstance(m, RegimeMetrics)
        for field in ("n_events_tagged", "n_disagreements",
                      "disagreement_rate", "mean_score_delta",
                      "positive_pct", "negative_pct",
                      "max_abs_score_delta", "selection_agreement_rate"):
            assert hasattr(m, field)

    def test_deltas_bucketed_correctly(self, wh):
        # Two events, opposite regimes
        events = ["2020-01-06T14:30:00Z", "2020-03-27T14:30:00Z"]
        disagreements = [
            _dis("2020-01-06T14:30:00Z", "AAPL", 0.1),   # low_vol +
            _dis("2020-03-27T14:30:00Z", "AAPL", -0.15), # high_vol -
        ]
        bundle = _make_bundle(events, disagreements)
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
            labels=("low_vol", "mid_vol", "high_vol"),
        )
        result = regime_conditioned_metrics(bundle, [spec], warehouse_root=wh)
        assert result["vol"]["low_vol"].mean_score_delta == pytest.approx(0.1)
        assert result["vol"]["high_vol"].mean_score_delta == pytest.approx(-0.15)
        assert result["vol"]["low_vol"].positive_pct == pytest.approx(1.0)
        assert result["vol"]["high_vol"].negative_pct == pytest.approx(1.0)

    def test_champion_rejected_excluded_by_default(self, wh):
        events = ["2020-01-06T14:30:00Z"]
        disagreements = [
            _dis("2020-01-06T14:30:00Z", "AAPL", 0.1),
            _dis("2020-01-06T14:30:00Z", "MSFT", -0.5,
                 champion_exp="rejected(insufficient_history: have=5)"),
        ]
        bundle = _make_bundle(events, disagreements)
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
            labels=("low_vol", "mid_vol", "high_vol"),
        )
        result = regime_conditioned_metrics(bundle, [spec], warehouse_root=wh)
        # Only the +0.1 delta counts; the rejected-base one is dropped.
        assert result["vol"]["low_vol"].n_disagreements == 1
        assert result["vol"]["low_vol"].mean_score_delta == pytest.approx(0.1)

    def test_champion_rejected_included_when_flag_off(self, wh):
        events = ["2020-01-06T14:30:00Z"]
        disagreements = [
            _dis("2020-01-06T14:30:00Z", "AAPL", 0.1),
            _dis("2020-01-06T14:30:00Z", "MSFT", -0.5,
                 champion_exp="rejected(insufficient_history: have=5)"),
        ]
        bundle = _make_bundle(events, disagreements)
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
            labels=("low_vol", "mid_vol", "high_vol"),
        )
        result = regime_conditioned_metrics(
            bundle, [spec], warehouse_root=wh,
            exclude_champion_rejected=False,
        )
        assert result["vol"]["low_vol"].n_disagreements == 2

    def test_missing_data_bucketed_as_unknown(self, wh):
        events = ["2015-01-01T14:30:00Z"]  # outside warehouse range
        disagreements = [_dis("2015-01-01T14:30:00Z", "AAPL", 0.1)]
        bundle = _make_bundle(events, disagreements)
        spec = RegimeSpec.quantile(
            "vol", dataset_id="regime-pack-test", symbol="VXX",
        )
        result = regime_conditioned_metrics(bundle, [spec], warehouse_root=wh)
        # 'unknown' bucket present
        assert "unknown" in result["vol"]
        assert result["vol"]["unknown"].n_disagreements == 1

    def test_deterministic_repeat(self, wh):
        events = ["2020-01-06T14:30:00Z", "2020-03-27T14:30:00Z"]
        disagreements = [
            _dis("2020-01-06T14:30:00Z", "AAPL", 0.1),
            _dis("2020-03-27T14:30:00Z", "AAPL", -0.15),
        ]
        bundle = _make_bundle(events, disagreements)
        spec = RegimeSpec.quantile("vol", dataset_id="regime-pack-test",
                                   symbol="VXX")
        a = regime_conditioned_metrics(bundle, [spec], warehouse_root=wh)
        b = regime_conditioned_metrics(bundle, [spec], warehouse_root=wh)
        # Serialise both — same bytes
        import json
        assert json.dumps({k: {l: m.to_dict() for l, m in v.items()}
                           for k, v in a.items()}, sort_keys=True) \
            == json.dumps({k: {l: m.to_dict() for l, m in v.items()}
                           for k, v in b.items()}, sort_keys=True)


# ---------------------------------------------------------------------------
# default_regime_specs
# ---------------------------------------------------------------------------


class TestDefaultRegimeSpecs:
    def test_returns_three_specs(self):
        specs = default_regime_specs()
        names = [s.name for s in specs]
        assert names == ["vol", "credit", "bond_trend"]

    def test_specs_valid(self):
        for spec in default_regime_specs():
            # Just constructing them exercises __post_init__ validation
            assert spec.name


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _source() -> str:
    import strategy.lab.regime_tagger as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        s = _source()
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in s

    def test_no_order_path_tokens(self):
        s = _source()
        for token in ("submit_order", "place_order", "cancel_order",
                      "TradingClient"):
            assert token not in s

    def test_no_approval_or_promotion(self):
        s = _source()
        assert "ApprovalRecord(" not in s
        assert "PromotionEntry(" not in s
        assert "FeatureFlags(" not in s

    def test_no_credential_env_reads(self):
        s = _source()
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
        ):
            assert not re.search(pattern, s)
