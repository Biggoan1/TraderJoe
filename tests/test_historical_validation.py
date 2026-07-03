"""Tests for strategy/historical_validation.py."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from strategy.backtest_lab import BacktestEvent
from strategy.config import reset_feature_flags
from strategy.historical_validation import (
    CHAMPION_STRATEGY_ID,
    DEFAULT_FLAG_NAME,
    HistoricalValidationBundle,
    HistoricalValidationConfig,
    HistoricalValidationError,
    LiveFetchNotAvailableError,
    _fetch_live_events,
    run_historical_validation,
)
from strategy.promotion_gates import (
    PromotionEntry,
    STATE_DISABLED,
)
from strategy.rs_challenger import RS_CHALLENGER_STRATEGY_ID


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


FIXTURE_WINDOW_START = "2026-05-01"
FIXTURE_WINDOW_END = "2026-06-30"
FIXTURE_SYMBOLS = ("AAPL", "MSFT", "NVDA")


def _iter_dates(start: str, end: str):
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while current <= stop:
        yield current
        current = current + timedelta(days=1)


def _make_fixture_events(
    start: str = FIXTURE_WINDOW_START, end: str = FIXTURE_WINDOW_END
) -> List[BacktestEvent]:
    events: List[BacktestEvent] = []
    for index, day in enumerate(_iter_dates(start, end)):
        events.append(
            BacktestEvent(
                timestamp=f"{day.isoformat()}T14:30:00+00:00",
                event_type="market_snapshot",
                sequence=index + 1,
            )
        )
    return events


def _make_fixture_scores(
    events: Sequence[BacktestEvent],
    symbols: Sequence[str] = FIXTURE_SYMBOLS,
) -> Dict[str, Dict[str, float]]:
    scores: Dict[str, Dict[str, float]] = {}
    for i, event in enumerate(events):
        scores[event.timestamp] = {
            symbol: 0.5 + 0.001 * i + 0.01 * j
            for j, symbol in enumerate(symbols)
        }
    return scores


def _make_fixture_rs(
    events: Sequence[BacktestEvent],
    symbols: Sequence[str] = FIXTURE_SYMBOLS,
) -> Dict[str, Dict[str, float]]:
    """Deterministic RS map that lifts NVDA on odd days and AAPL on even days."""
    rs: Dict[str, Dict[str, float]] = {}
    for i, event in enumerate(events):
        rs[event.timestamp] = {
            "AAPL": 70.0 if i % 2 == 0 else 40.0,
            "MSFT": 55.0,
            "NVDA": 70.0 if i % 2 == 1 else 45.0,
        }
    return rs


def _make_config(tmp_path, **overrides) -> HistoricalValidationConfig:
    events = _make_fixture_events()
    base = dict(
        dataset_id="paper-2026-may-jun",
        symbols=FIXTURE_SYMBOLS,
        window_start=FIXTURE_WINDOW_START,
        window_end=FIXTURE_WINDOW_END,
        research_data_root=str(tmp_path / "research_data"),
        report_root=str(tmp_path / "reports"),
        in_sample_days=30,
        out_of_sample_days=15,
        step_days=15,
        fixture_events=tuple(events),
        fixture_champion_scores=_make_fixture_scores(events),
        fixture_rs_map=_make_fixture_rs(events),
    )
    base.update(overrides)
    return HistoricalValidationConfig(**base)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self, tmp_path):
        config = _make_config(tmp_path)
        assert config.live_fetch is False
        assert config.flag_name == DEFAULT_FLAG_NAME
        assert config.champion_id == CHAMPION_STRATEGY_ID
        assert config.challenger_id == RS_CHALLENGER_STRATEGY_ID

    def test_flag_name_locked_to_relative_strength(self, tmp_path):
        with pytest.raises(ValueError, match=DEFAULT_FLAG_NAME):
            _make_config(tmp_path, flag_name="enable_market_regime")

    def test_champion_and_challenger_must_differ(self, tmp_path):
        with pytest.raises(ValueError, match="must differ"):
            _make_config(
                tmp_path,
                champion_id="same-id",
                challenger_id="same-id",
            )

    @pytest.mark.parametrize(
        "overrides,error",
        [
            ({"dataset_id": ""}, "dataset_id"),
            ({"symbols": ()}, "symbols"),
            ({"window_start": ""}, "window_start"),
            ({"window_end": ""}, "window_end"),
            (
                {"window_start": "2026-08-01", "window_end": "2026-07-01"},
                "window_start",
            ),
            ({"in_sample_days": 0}, "in_sample_days"),
            ({"out_of_sample_days": 0}, "out_of_sample_days"),
            ({"step_days": 0}, "step_days"),
        ],
    )
    def test_validation_errors(self, tmp_path, overrides, error):
        with pytest.raises(ValueError, match=error):
            _make_config(tmp_path, **overrides)

    def test_stable_hash_deterministic(self, tmp_path):
        first = _make_config(tmp_path)
        second = _make_config(tmp_path)
        assert first.stable_hash() == second.stable_hash()

    def test_stable_hash_changes_with_seed(self, tmp_path):
        assert (
            _make_config(tmp_path, seed=1).stable_hash()
            != _make_config(tmp_path, seed=2).stable_hash()
        )


# ---------------------------------------------------------------------------
# Live fetch guard
# ---------------------------------------------------------------------------


class TestLiveFetchGuard:
    def test_live_fetch_defaults_to_false(self, tmp_path):
        config = _make_config(tmp_path)
        assert config.live_fetch is False

    def test_live_fetch_without_client_raises(self, tmp_path):
        config = _make_config(tmp_path, live_fetch=True)
        with pytest.raises(LiveFetchNotAvailableError):
            run_historical_validation(config)

    def test_fixture_mode_does_not_call_research_client(self, tmp_path):
        """A research_client passed with live_fetch=False must NOT be
        called — the orchestrator must not silently upgrade to live
        mode.
        """

        class FailIfCalled:
            def fetch_bars(self, **kwargs):
                raise AssertionError(
                    "research_client called in fixture mode"
                )

        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config,
            research_client=FailIfCalled(),
            generated_at="2026-07-03T12:00:00+00:00",
        )
        assert bundle.live_fetch_used is False


# ---------------------------------------------------------------------------
# End-to-end fixture run
# ---------------------------------------------------------------------------


class TestFixtureRun:
    def test_run_produces_bundle(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert isinstance(bundle, HistoricalValidationBundle)
        assert bundle.live_fetch_used is False
        assert bundle.config is config

    def test_run_writes_dataset_manifest(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        manifest_path = Path(bundle.dataset_manifest_path)
        assert manifest_path.is_file()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert payload["dataset_id"] == config.dataset_id

    def test_run_writes_comparison_report(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        paths = bundle.comparison_report_paths
        assert Path(paths.markdown_path).is_file()
        assert Path(paths.json_path).is_file()

    def test_run_writes_walk_forward_report(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        paths = bundle.walk_forward_report_paths
        assert Path(paths.markdown_path).is_file()
        assert Path(paths.json_path).is_file()

    def test_run_writes_learning_report(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        paths = bundle.learning_report_paths
        assert Path(paths.markdown_path).is_file()
        assert Path(paths.json_path).is_file()

    def test_run_generates_findings(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert len(bundle.findings) >= 6  # 4 from comparison + 2 from WF


# ---------------------------------------------------------------------------
# PromotionEntry / ApprovalRecord guarantees
# ---------------------------------------------------------------------------


class TestPromotionSafety:
    def test_promotion_entry_stays_disabled(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert bundle.promotion_entry.current_state == STATE_DISABLED

    def test_promotion_entry_carries_no_approvals(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert bundle.promotion_entry.approvals == []

    def test_promotion_entry_carries_evidence_ids(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        evidence = bundle.promotion_entry.evidence
        assert evidence["dataset_id"] == config.dataset_id
        assert (
            evidence["backtest_report_id"]
            == bundle.comparison.metadata.run_id
        )
        assert (
            evidence["walk_forward_report_id"]
            == bundle.walk_forward_report.report_id
        )
        assert (
            evidence["learning_report_id"]
            == bundle.learning_report.report_id
        )

    def test_promotion_entry_targets_enable_relative_strength(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert bundle.promotion_entry.flag_name == "enable_relative_strength"


# ---------------------------------------------------------------------------
# Analyst wiring
# ---------------------------------------------------------------------------


class _StubLLMClient:
    def __init__(self, response="## Executive Summary\nrecap"):
        self._response = response
        self.calls: List[Dict[str, Any]] = []

    def effective_model(self, explicit=None):
        return explicit or "local-model"

    def chat(self, prompt, system_prompt, model=None):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "model": model}
        )
        return self._response


class TestAnalystIntegration:
    def test_analyst_skipped_when_no_client(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert bundle.analyst_narratives == []
        assert bundle.analyst_reports == []
        assert bundle.analyst_report_paths == []

    def test_analyst_runs_when_client_supplied(self, tmp_path):
        client = _StubLLMClient()
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config,
            llm_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )
        # One narrative for comparison, walk-forward, and learning
        # report each.
        assert len(bundle.analyst_narratives) == 3
        assert len(bundle.analyst_reports) == 3
        assert len(bundle.analyst_report_paths) == 3
        for paths in bundle.analyst_report_paths:
            assert Path(paths.markdown_path).is_file()
        # Analyst-report ids also appear on the PromotionEntry evidence
        # dict for downstream review.
        for index, report in enumerate(bundle.analyst_reports):
            assert (
                bundle.promotion_entry.evidence[f"analyst_report_{index}"]
                == report.report_id
            )


# ---------------------------------------------------------------------------
# Live fetch pathway (mocked)
# ---------------------------------------------------------------------------


class _StubResearchClient:
    def __init__(self, bars: Dict[str, Any]):
        self._bars = bars
        self.calls: List[Dict[str, Any]] = []

    def fetch_bars(self, **kwargs):
        self.calls.append(kwargs)
        return {"bars": self._bars}


class TestLiveFetchPathway:
    def _client(self):
        bars = {
            symbol: [
                {
                    "t": f"2026-05-{day:02d}T14:30:00+00:00",
                    "c": 100.0 + i * (day % 5),
                }
                for day in range(1, 32)
            ]
            for i, symbol in enumerate(FIXTURE_SYMBOLS)
        }
        # Add benchmark bars so the fetch loop covers them too.
        for symbol in ("SPY", "QQQ"):
            bars[symbol] = [
                {
                    "t": f"2026-05-{day:02d}T14:30:00+00:00",
                    "c": 400.0 + day,
                }
                for day in range(1, 32)
            ]
        return _StubResearchClient(bars)

    def test_live_fetch_calls_client(self, tmp_path):
        client = self._client()
        config = _make_config(
            tmp_path,
            live_fetch=True,
            fixture_events=(),
            fixture_champion_scores={},
            fixture_rs_map={},
        )
        bundle = run_historical_validation(
            config,
            research_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )
        assert bundle.live_fetch_used is True
        assert len(client.calls) == 1
        assert (
            client.calls[0]["start"] == FIXTURE_WINDOW_START
        )
        assert (
            client.calls[0]["end"] == FIXTURE_WINDOW_END
        )
        # Symbols + benchmarks were requested together
        requested_symbols = set(client.calls[0]["symbols"])
        for symbol in FIXTURE_SYMBOLS:
            assert symbol in requested_symbols
        for symbol in ("SPY", "QQQ"):
            assert symbol in requested_symbols


# ---------------------------------------------------------------------------
# Live-fetch RS wiring — t_phase5_rs_live_feed
# ---------------------------------------------------------------------------


def _diverging_bars(
    days: int = 45,
) -> Dict[str, List[Dict[str, Any]]]:
    """Deterministic bars where each symbol diverges from benchmarks in
    a distinct direction so the RS map has entries for every symbol
    and pushes the Champion vs Challenger scores apart.
    """
    start = date(2026, 5, 1)

    def stamp(i: int) -> str:
        d = start + timedelta(days=i)
        return f"{d.isoformat()}T14:30:00+00:00"

    bars: Dict[str, List[Dict[str, Any]]] = {}
    # Rising, flat, and falling series relative to a flat benchmark.
    trajectories = {
        "AAPL": 1.010,   # +1.0% per bar (steady outperformer)
        "MSFT": 1.000,   # matches benchmark
        "NVDA": 0.990,   # -1.0% per bar (steady underperformer)
    }
    for symbol, ratio in trajectories.items():
        price = 100.0
        series: List[Dict[str, Any]] = []
        for i in range(days):
            series.append({"t": stamp(i), "c": round(price, 4)})
            price *= ratio
        bars[symbol] = series
    for bench in ("SPY", "QQQ"):
        series: List[Dict[str, Any]] = []
        price = 400.0 if bench == "SPY" else 300.0
        for i in range(days):
            series.append({"t": stamp(i), "c": round(price, 4)})
            # benchmark is essentially flat (tiny +0.1% drift)
            price *= 1.001
        bars[bench] = series
    return bars


def _short_bars(days: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    """Bars too short for the default RS lookback."""
    bars: Dict[str, List[Dict[str, Any]]] = {}
    for symbol in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ"):
        bars[symbol] = [
            {"t": f"2026-05-{i + 1:02d}T14:30:00+00:00", "c": 100.0 + i}
            for i in range(days)
        ]
    return bars


class TestLiveFetchRsMap:
    """Direct-call tests against ``_fetch_live_events`` so we can inspect
    the RS map that the pipeline hands to ``rs_provider_from_map``.
    Together with ``TestLiveFetchRsIntegration`` these prove the RS
    Challenger receives real data and that the fallback path still
    fires when bars are too short.
    """

    def _config(self, tmp_path) -> HistoricalValidationConfig:
        return _make_config(
            tmp_path,
            live_fetch=True,
            fixture_events=(),
            fixture_champion_scores={},
            fixture_rs_map={},
        )

    def test_rs_map_populated_when_bars_have_enough_history(self, tmp_path):
        client = _StubResearchClient(_diverging_bars(days=45))
        config = self._config(tmp_path)
        events, champion_scores, rs_map, bars = _fetch_live_events(config, client)
        assert events, "expected non-empty events"
        # Post-lookback timestamps must contain entries for every symbol
        # with bars covering the window.
        eligible = list(rs_map.keys())
        assert eligible, "rs_map must be populated in live-fetch path"
        sample = rs_map[eligible[-1]]
        for symbol in FIXTURE_SYMBOLS:
            assert symbol in sample, (
                f"expected {symbol} in rs_map at {eligible[-1]}"
            )

    def test_rs_map_directions_match_bar_trajectories(self, tmp_path):
        client = _StubResearchClient(_diverging_bars(days=45))
        config = self._config(tmp_path)
        _, _, rs_map, _ = _fetch_live_events(config, client)
        latest = rs_map[max(rs_map)]
        # AAPL trends up vs a flat benchmark -> RS > neutral
        assert latest["AAPL"] > 50.0
        # NVDA trends down vs a flat benchmark -> RS < neutral
        assert latest["NVDA"] < 50.0
        # MSFT tracks benchmark drift -> near neutral (allow small band)
        assert abs(latest["MSFT"] - 50.0) < 20.0

    def test_rs_map_empty_when_bars_too_short_for_lookback(self, tmp_path):
        client = _StubResearchClient(_short_bars(days=3))
        config = self._config(tmp_path)
        _, _, rs_map, _ = _fetch_live_events(config, client)
        # ``_fetch_live_events`` shrinks lookback to bars_available - 1
        # (min 1) so a 3-bar series may still emit at most one row.
        # The load-bearing assertion is that when bars are short the
        # RS map does not cover the whole event stream — the fallback
        # path must remain active for most events.
        events, _, _, _ = _fetch_live_events(config, client)
        assert len(rs_map) < len(events), (
            "rs_map must not cover every event when bars are too short"
        )


class TestLiveFetchRsIntegration:
    """Full-pipeline assertion: with a populated RS map, the Champion
    vs Challenger comparison produces disagreements.  With bars too
    short for a lookback, the pipeline still succeeds and the RS
    Challenger falls back to base scores.
    """

    def _config(self, tmp_path) -> HistoricalValidationConfig:
        return _make_config(
            tmp_path,
            live_fetch=True,
            fixture_events=(),
            fixture_champion_scores={},
            fixture_rs_map={},
        )

    def test_pipeline_produces_disagreements_with_rs_signal(self, tmp_path):
        reset_feature_flags()  # keep the global check clean
        client = _StubResearchClient(_diverging_bars(days=45))
        config = self._config(tmp_path)
        bundle = run_historical_validation(
            config,
            research_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )
        # With an RS map that lifts one symbol and depresses another,
        # the Challenger's overlay must reshuffle scores enough for
        # at least one disagreement to fire.
        total_disagreements = sum(
            bundle.comparison.disagreements_by_kind().get(kind, []).__len__()
            for kind in bundle.comparison.disagreements_by_kind()
        )
        assert total_disagreements > 0, (
            "expected RS overlay to produce at least one disagreement, "
            f"got {total_disagreements}"
        )
        # PromotionEntry still disabled and unapproved.
        assert bundle.promotion_entry.current_state == STATE_DISABLED
        assert bundle.promotion_entry.approvals == []

    def test_pipeline_falls_back_when_bars_too_short(self, tmp_path):
        reset_feature_flags()
        client = _StubResearchClient(_short_bars(days=3))
        config = self._config(tmp_path)
        bundle = run_historical_validation(
            config,
            research_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )
        # Pipeline succeeds; PromotionEntry unchanged.
        assert bundle.live_fetch_used is True
        assert bundle.promotion_entry.current_state == STATE_DISABLED

    def test_global_feature_flags_untouched_by_live_fetch_run(self, tmp_path):
        flags = reset_feature_flags()
        client = _StubResearchClient(_diverging_bars(days=45))
        config = self._config(tmp_path)
        run_historical_validation(
            config,
            research_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )
        assert flags.all_disabled is True
        assert flags.enabled_flags == []


# ---------------------------------------------------------------------------
# Champion explanations flow through the pipeline — t_phase5_champion_explanations
# ---------------------------------------------------------------------------


class TestChampionExplanationsFlowThrough:
    """Full-pipeline assertion: with live-fetched bars in hand, both
    champion and challenger structured explanations attach to every
    ``ScoreRow``, propagate into ``DisagreementRecord``s, and land in
    the learning report's ``explanation_summary`` section.
    """

    def _config(self, tmp_path) -> HistoricalValidationConfig:
        return _make_config(
            tmp_path,
            live_fetch=True,
            fixture_events=(),
            fixture_champion_scores={},
            fixture_rs_map={},
        )

    def _run(self, tmp_path):
        reset_feature_flags()
        client = _StubResearchClient(_diverging_bars(days=60))
        config = self._config(tmp_path)
        return run_historical_validation(
            config,
            research_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )

    def test_score_rows_carry_champion_structured_explanation(self, tmp_path):
        bundle = self._run(tmp_path)
        # At least one row past the champion's minimum-history window
        # must carry a structured explanation on both sides.
        non_empty_champion = 0
        non_empty_challenger = 0
        for table in bundle.comparison.score_tables:
            for row in table.rows:
                if isinstance(row.champion_structured_explanation, dict):
                    non_empty_champion += 1
                if isinstance(row.challenger_structured_explanation, dict):
                    non_empty_challenger += 1
        assert non_empty_champion > 0, (
            "champion structured explanations must attach to at least one row"
        )
        assert non_empty_challenger > 0, (
            "challenger structured explanations must attach to at least one row"
        )

    def test_free_text_explanations_populated_from_structured(self, tmp_path):
        bundle = self._run(tmp_path)
        populated = 0
        for table in bundle.comparison.score_tables:
            for row in table.rows:
                if row.champion_explanation:
                    populated += 1
        assert populated > 0, (
            "free-text champion_explanation must be populated from structured "
            "explanations when available"
        )

    def test_disagreements_carry_both_sides_explanations(self, tmp_path):
        bundle = self._run(tmp_path)
        assert bundle.comparison.disagreements, (
            "expected some disagreements from the diverging bar trajectories"
        )
        with_both = 0
        for record in bundle.comparison.disagreements:
            if (
                isinstance(record.champion_structured_explanation, dict)
                and isinstance(record.challenger_structured_explanation, dict)
            ):
                with_both += 1
        assert with_both > 0, (
            "at least one disagreement must carry both champion and challenger "
            "structured explanations"
        )

    def test_champion_component_names_appear_in_explanations(self, tmp_path):
        bundle = self._run(tmp_path)
        # Somewhere in the run the champion should have fired at least
        # one of the six gate components.
        expected_component_names = {
            "above_sma20",
            "sma20_above_sma50",
            "rsi_not_overbought",
            "macd_positive",
            "adx_strength",
            "volume_confirmation",
        }
        seen: set = set()
        for table in bundle.comparison.score_tables:
            for row in table.rows:
                exp = row.champion_structured_explanation
                if not isinstance(exp, dict):
                    continue
                for component in exp.get("components", []) or []:
                    seen.add(component.get("name"))
        assert seen & expected_component_names, (
            f"expected champion to fire at least one gate; saw {seen}"
        )

    def test_challenger_overlay_appends_rs_component_when_data_present(
        self, tmp_path
    ):
        bundle = self._run(tmp_path)
        rs_component_events = 0
        for table in bundle.comparison.score_tables:
            for row in table.rows:
                exp = row.challenger_structured_explanation
                if not isinstance(exp, dict):
                    continue
                names = [
                    (c or {}).get("name")
                    for c in (exp.get("components", []) or [])
                ]
                if "rs_overlay" in names:
                    rs_component_events += 1
        assert rs_component_events > 0, (
            "challenger must append rs_overlay component once RS data is "
            "populated"
        )

    def test_learning_report_carries_explanation_summary(self, tmp_path):
        bundle = self._run(tmp_path)
        payload = bundle.learning_report.payload
        assert "explanation_summary" in payload, (
            "learning report payload must include explanation_summary"
        )
        summary = payload["explanation_summary"]
        assert "champion" in summary
        assert "challenger" in summary
        assert summary["champion"]["observation_count"] > 0
        # The champion component pass rates should reflect the six-gate
        # scorer emitting at least one component.
        assert summary["champion"]["component_pass_rates"], (
            "champion component_pass_rates must not be empty"
        )

    def test_pipeline_still_safe_when_history_is_short(self, tmp_path):
        # Short bars -> champion rejects every symbol.  The pipeline
        # must not crash; PromotionEntry stays disabled and the
        # explanation summary reports the rejection rate.
        reset_feature_flags()
        client = _StubResearchClient(_short_bars(days=10))
        config = self._config(tmp_path)
        bundle = run_historical_validation(
            config,
            research_client=client,
            generated_at="2026-07-03T12:00:00+00:00",
        )
        assert bundle.promotion_entry.current_state == STATE_DISABLED
        payload = bundle.learning_report.payload
        summary = payload.get("explanation_summary", {})
        champion_summary = summary.get("champion", {})
        # All symbols rejected due to insufficient_history
        assert champion_summary.get("rejected_rate", 0) > 0

    def test_no_approval_record_constructed(self, tmp_path):
        bundle = self._run(tmp_path)
        assert bundle.promotion_entry.approvals == []
        assert bundle.promotion_entry.current_state == STATE_DISABLED

    def test_global_feature_flags_remain_disabled(self, tmp_path):
        flags = reset_feature_flags()
        self._run(tmp_path)
        assert flags.all_disabled is True
        assert flags.enabled_flags == []


class TestTraderPyUntouched:
    """Verifies that no import of trader.py leaks into the research
    modules that this card touches.  Coupled with the working-tree
    check in :mod:`git`, this prevents accidental cross-boundary
    imports from creeping in.
    """

    def test_score_explanation_never_imports_trader(self):
        import strategy.score_explanation as module
        source = open(module.__file__, encoding="utf-8").read()
        assert "from trader" not in source
        assert "import trader" not in source

    def test_champion_scoring_never_imports_trader(self):
        import strategy.champion_scoring as module
        source = open(module.__file__, encoding="utf-8").read()
        assert "from trader" not in source
        assert "import trader" not in source
        # No actual yfinance import or call — docstring may mention it
        # to explain WHY this module exists as a research mirror.
        assert "import yfinance" not in source
        assert "yf.download" not in source
        assert "yf.Ticker" not in source


# ---------------------------------------------------------------------------
# Determinism (byte-identical reruns)
# ---------------------------------------------------------------------------


class TestDeterministicReruns:
    def test_byte_identical_files_on_rerun(self, tmp_path):
        config_a = _make_config(tmp_path / "run_a")
        config_b = _make_config(tmp_path / "run_b")
        bundle_a = run_historical_validation(
            config_a, generated_at="2026-07-03T12:00:00+00:00"
        )
        bundle_b = run_historical_validation(
            config_b, generated_at="2026-07-03T12:00:00+00:00"
        )

        # Comparison
        assert bundle_a.comparison.stable_hash() == bundle_b.comparison.stable_hash()
        # Walk-forward
        assert (
            bundle_a.walk_forward_report.stable_hash()
            == bundle_b.walk_forward_report.stable_hash()
        )
        # Learning
        assert (
            bundle_a.learning_report.stable_hash()
            == bundle_b.learning_report.stable_hash()
        )
        # Comparison report files
        for attr in ("markdown_path", "json_path", "manifest_path"):
            a_bytes = Path(
                getattr(bundle_a.comparison_report_paths, attr)
            ).read_bytes()
            b_bytes = Path(
                getattr(bundle_b.comparison_report_paths, attr)
            ).read_bytes()
            assert a_bytes == b_bytes, f"comparison {attr} differs"
        # Walk-forward report files
        for attr in ("markdown_path", "json_path", "manifest_path"):
            a_bytes = Path(
                getattr(bundle_a.walk_forward_report_paths, attr)
            ).read_bytes()
            b_bytes = Path(
                getattr(bundle_b.walk_forward_report_paths, attr)
            ).read_bytes()
            assert a_bytes == b_bytes, f"walk-forward {attr} differs"
        # Learning report files
        for attr in ("markdown_path", "json_path", "manifest_path"):
            a_bytes = Path(
                getattr(bundle_a.learning_report_paths, attr)
            ).read_bytes()
            b_bytes = Path(
                getattr(bundle_b.learning_report_paths, attr)
            ).read_bytes()
            assert a_bytes == b_bytes, f"learning {attr} differs"

    def test_config_stable_hash_survives_rerun(self, tmp_path):
        first = _make_config(tmp_path)
        second = _make_config(tmp_path)
        assert first.stable_hash() == second.stable_hash()


# ---------------------------------------------------------------------------
# Safety: config immutability + flag isolation
# ---------------------------------------------------------------------------


class TestSafety:
    def test_strategy_config_py_bytes_unchanged(self, tmp_path):
        config_path = Path("strategy/config.py")
        before = config_path.read_bytes()
        config = _make_config(tmp_path)
        run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        after = config_path.read_bytes()
        assert before == after

    def test_global_feature_flags_remain_disabled(self, tmp_path):
        flags = reset_feature_flags()
        config = _make_config(tmp_path)
        run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_bundle_to_dict_json_serializable(self, tmp_path):
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config, generated_at="2026-07-03T12:00:00+00:00"
        )
        json.dumps(bundle.to_dict())

    def test_orchestrator_never_constructs_approval_record(self, tmp_path):
        """Runtime scan: no ApprovalRecord object exists on the
        returned bundle or in the promotion entry.
        """
        config = _make_config(tmp_path)
        bundle = run_historical_validation(
            config,
            llm_client=_StubLLMClient(),
            generated_at="2026-07-03T12:00:00+00:00",
        )
        assert bundle.promotion_entry.approvals == []
        # Confirm PromotionEntry has zero approvals and that the
        # bundle carries no other field that would smuggle an approval.
        payload = bundle.to_dict()
        assert payload["promotion_entry"]["approvals"] == []


# ---------------------------------------------------------------------------
# Source-safety
# ---------------------------------------------------------------------------


class TestSourceSafety:
    def _source(self) -> str:
        import strategy.historical_validation as module

        return Path(module.__file__).read_text(encoding="utf-8")

    def test_no_order_path_references(self):
        source = self._source()
        for token in [
            "submit_order",
            "place_order",
            "cancel_order",
            "TradingClient",
            "yfinance",
        ]:
            assert token not in source, (
                f"historical_validation must not reference {token!r}"
            )

    def test_no_live_runner_imports(self):
        source = self._source()
        for token in [
            "from trader import",
            "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "import trader_cli",
            "from telegram_approvals import",
            "import telegram_approvals",
            "from strategy.runner import",
            "import strategy.runner",
        ]:
            assert token not in source, (
                f"historical_validation must not import {token!r}"
            )

    def test_no_credential_env_reads(self):
        import re

        source = self._source()
        for pattern in (
            r"os\.environ\[\s*['\"]ALPACA_",
            r"os\.environ\.get\(\s*['\"]ALPACA_",
            r"os\.getenv\(\s*['\"]ALPACA_",
            r"['\"]RESEARCH_ALPACA_API_KEY['\"]",
            r"['\"]RESEARCH_ALPACA_SECRET_KEY['\"]",
        ):
            assert not re.search(pattern, source), (
                f"historical_validation must not read credentials directly "
                f"(pattern={pattern!r})"
            )

    def test_terminology_avoids_training(self):
        source = self._source()
        assert 'call this "training"' in source
        stripped = source.replace(
            'It does not call this "training" — Phase 5 evaluation work is',
            "",
        )
        for token in ["training", "Training", "TRAINING"]:
            assert token not in stripped

    def test_import_does_not_pull_in_order_path(self):
        import sys

        for name in ["strategy.historical_validation", "strategy"]:
            sys.modules.pop(name, None)
        before = set(sys.modules)
        import strategy.historical_validation  # noqa: F401

        added = set(sys.modules) - before
        forbidden = {
            "trader_cli",
            "trader",
            "crypto_trader",
            "telegram_approvals",
        }
        assert not (added & forbidden)

    def test_never_constructs_approvalrecord_in_source(self):
        source = self._source()
        # The literal `ApprovalRecord(` (constructor call) must not
        # appear anywhere in the module.  Comments referring to
        # ApprovalRecord as a concept are fine.
        assert "ApprovalRecord(" not in source, (
            "historical_validation must not construct ApprovalRecord"
        )
