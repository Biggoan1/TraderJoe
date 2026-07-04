"""Bridge card ``t_phase56_historical_validation_wiring``.

Wires ``WarehouseReader`` into
``strategy.historical_validation.run_historical_validation``.
Tests here prove:

* Warehouse-first: when the warehouse has complete coverage,
  ``ResearchAccountClient.fetch_bars`` (via a call-counting stub)
  is NOT called.
* Cache miss: when warehouse coverage is incomplete and
  ``live_fetch=True``, the pipeline falls through to the
  provider client.
* Offline mode: with warehouse coverage present, an empty /
  absent ``research_client`` still lets the pipeline complete.
* Dataset provenance propagates through the bundle,
  ``comparison.to_analyst_payload`` (via the shim), and
  ``PromotionEntry.evidence``.
* Byte-identical replay: two independent warehouse-backed runs
  produce identical bundle dicts modulo timestamps.
* PromotionEntry stays ``disabled`` regardless of source.

All read-only guarantees are preserved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from strategy.config import reset_feature_flags
from strategy.data_catalog import DatasetFile, DatasetManifest
from strategy.historical_validation import (
    HistoricalValidationConfig,
    LiveFetchNotAvailableError,
    _with_provenance,
    run_historical_validation,
)
from strategy.local_warehouse import (
    STATUS_VALIDATED,
    WarehouseLayout,
    write_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
)
from strategy.promotion_gates import STATE_DISABLED
from strategy.warehouse.parquet_io import write_bars
from strategy.warehouse.research_cache import WarehouseReader


# ---------------------------------------------------------------------------
# Stubs and helpers
# ---------------------------------------------------------------------------


class TrackingResearchClient:
    """Stub with the exact ResearchAccountClient.fetch_bars shape.

    Counts every call so tests can prove warehouse-first behavior.
    """

    def __init__(self, bars_by_symbol: Dict[str, List[Dict[str, Any]]]) -> None:
        self._bars_by_symbol = bars_by_symbol
        self.fetch_bars_calls: List[Dict[str, Any]] = []

    def fetch_bars(self, **kwargs) -> Dict[str, Any]:
        self.fetch_bars_calls.append(kwargs)
        return {"bars": dict(self._bars_by_symbol)}


def _bar(sym: str, ts: str, close: float = 100.0) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=ts,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000_000,
        interval=BarInterval.DAILY,
        adjustment_mode=AdjustmentMode.RAW,
    )


def _seed_warehouse(
    layout: WarehouseLayout,
    dataset_id: str,
    bars: List[Bar],
    corporate_action_version: str = "1",
    start_date: str = "",
    end_date: str = "",
) -> DatasetManifest:
    layout.create()
    written = write_bars(
        layout, bars, AssetClass.EQUITY, dataset_id=dataset_id
    )
    files = tuple(
        DatasetFile(
            path=w.relative_path,
            sha256=w.sha256,
            size_bytes=w.size_bytes,
            row_count=w.row_count,
        )
        for w in written
    )
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        kind="historical_bars",
        symbols=tuple(sorted({b.symbol for b in bars})),
        start_date=start_date or min(b.timestamp[:10] for b in bars),
        end_date=end_date or max(b.timestamp[:10] for b in bars),
        files=files,
        provider="alpaca",
        interval="1Day",
        asset_class="equity",
        adjustment_mode="raw",
        corporate_action_version=corporate_action_version,
        validation_status=STATUS_VALIDATED,
    )
    write_manifest(layout, dataset_id, manifest.to_dict(), force=True)
    return manifest


def _daily_bars_for(
    symbols: Sequence[str], days: int = 60
) -> List[Bar]:
    """Generate `days` trading days of bars for each symbol."""
    from datetime import date, timedelta

    start = date(2020, 5, 1)
    bars: List[Bar] = []
    for symbol in symbols:
        for i in range(days):
            d = start + timedelta(days=i)
            bars.append(
                _bar(
                    symbol,
                    f"{d.isoformat()}T14:30:00+00:00",
                    close=100.0 + i * 0.1,
                )
            )
    return bars


def _bar_dict_for_provider(
    symbols: Sequence[str], days: int = 60
) -> Dict[str, List[Dict[str, Any]]]:
    """Same shape as an Alpaca /v2/stocks/bars response."""
    from datetime import date, timedelta

    start = date(2020, 5, 1)
    result: Dict[str, List[Dict[str, Any]]] = {}
    for symbol in symbols:
        rows: List[Dict[str, Any]] = []
        for i in range(days):
            d = start + timedelta(days=i)
            close = 100.0 + i * 0.1
            rows.append(
                {
                    "t": f"{d.isoformat()}T14:30:00+00:00",
                    "o": close - 0.5,
                    "h": close + 0.5,
                    "l": close - 1.0,
                    "c": close,
                    "v": 1_000_000,
                }
            )
        result[symbol] = rows
    return result


def _mk_config(
    tmp_path: Path, dataset_id: str = "run-2020-05"
) -> HistoricalValidationConfig:
    return HistoricalValidationConfig(
        dataset_id=dataset_id,
        symbols=("AAPL", "MSFT", "NVDA"),
        window_start="2020-05-01",
        window_end="2020-06-30",
        research_data_root=str(tmp_path / "research_data"),
        report_root=str(tmp_path / "reports"),
        live_fetch=True,
    )


@pytest.fixture
def warehouse_layout(tmp_path: Path) -> WarehouseLayout:
    return WarehouseLayout(root=tmp_path / "warehouse")


@pytest.fixture
def seeded_warehouse(warehouse_layout: WarehouseLayout) -> WarehouseLayout:
    """Warehouse populated with 60 days of bars for the three
    watchlist symbols + both benchmarks.  Manifest declares
    coverage through 2020-06-30 to exceed the standard test window.
    """
    _seed_warehouse(
        warehouse_layout,
        "watchlist-2020",
        _daily_bars_for(
            ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"], days=61
        ),
        start_date="2020-05-01",
        end_date="2020-06-30",
    )
    return warehouse_layout


# ---------------------------------------------------------------------------
# Warehouse cache hit — regression: no provider call
# ---------------------------------------------------------------------------


class TestWarehouseCacheHit:
    def test_provider_not_called_when_warehouse_has_full_coverage(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        client = TrackingResearchClient(
            bars_by_symbol=_bar_dict_for_provider(
                ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"], days=60
            )
        )
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle = run_historical_validation(
                config,
                research_client=client,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        # Load-bearing regression: provider was NOT called
        assert client.fetch_bars_calls == []
        # Bars flowed from the warehouse
        assert bundle.dataset_provenance["source"] == "warehouse"
        # Bundle carries live_fetch_used=True even though the
        # provider never fired
        assert bundle.live_fetch_used is True

    def test_dataset_provenance_lists_warehouse_datasets(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle = run_historical_validation(
                config,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        assert bundle.dataset_provenance["source"] == "warehouse"
        datasets = bundle.dataset_provenance["datasets"]
        assert any(
            d.get("dataset_id") == "watchlist-2020" for d in datasets
        )


# ---------------------------------------------------------------------------
# Cache miss — provider fallback
# ---------------------------------------------------------------------------


class TestCacheMiss:
    def test_provider_called_when_warehouse_empty(
        self, tmp_path, warehouse_layout
    ):
        # Empty warehouse — coverage check returns False
        warehouse_layout.create()
        reset_feature_flags()
        client = TrackingResearchClient(
            bars_by_symbol=_bar_dict_for_provider(
                ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"], days=60
            )
        )
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=warehouse_layout) as reader:
            bundle = run_historical_validation(
                config,
                research_client=client,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        # Provider WAS called
        assert len(client.fetch_bars_calls) == 1
        assert bundle.dataset_provenance["source"] == "provider"


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


class TestOfflineMode:
    def test_pipeline_completes_without_research_client_when_warehouse_covers(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle = run_historical_validation(
                config,
                warehouse_reader=reader,
                # no research_client at all
                generated_at="2026-07-04T00:00:00+00:00",
            )
        assert bundle.dataset_provenance["source"] == "warehouse"
        # PromotionEntry still disabled
        assert bundle.promotion_entry.current_state == STATE_DISABLED

    def test_live_fetch_without_source_still_rejected(self, tmp_path):
        reset_feature_flags()
        config = _mk_config(tmp_path)
        with pytest.raises(LiveFetchNotAvailableError):
            run_historical_validation(
                config,
                # neither warehouse_reader nor research_client
                generated_at="2026-07-04T00:00:00+00:00",
            )


# ---------------------------------------------------------------------------
# Dataset provenance in analyst payload
# ---------------------------------------------------------------------------


class TestProvenanceInPromotionEvidence:
    def test_promotion_evidence_carries_dataset_provenance_id(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle = run_historical_validation(
                config,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        evidence = bundle.promotion_entry.evidence
        assert "dataset_provenance_id" in evidence
        prov_id = evidence["dataset_provenance_id"]
        assert prov_id.startswith("warehouse:")
        assert "watchlist-2020" in prov_id


class TestProvenanceInAnalystShim:
    def test_shim_injects_dataset_provenance(self):
        # Direct unit test on the wrapper
        class _Fake:
            def to_analyst_payload(self):
                return {"metadata": {"run_id": "cc_x"}, "score_tables": []}

            def to_dict(self):
                return {"metadata": {"run_id": "cc_x"}, "score_tables": []}

        wrapped = _with_provenance(
            _Fake(),
            {"source": "warehouse", "datasets": [{"dataset_id": "d", "version": "1"}]},
        )
        payload = wrapped.to_analyst_payload()
        assert payload["dataset_provenance"]["source"] == "warehouse"
        assert payload["dataset_provenance"]["datasets"][0]["dataset_id"] == "d"

    def test_shim_delegates_metadata_attribute(self):
        class _Fake:
            metadata = "held-by-shim"
            def to_analyst_payload(self):
                return {}
            def to_dict(self):
                return {}

        wrapped = _with_provenance(_Fake(), {"source": "warehouse"})
        assert wrapped.metadata == "held-by-shim"

    def test_shim_returns_source_unchanged_when_provenance_empty(self):
        source = object()
        assert _with_provenance(source, {}) is source


# ---------------------------------------------------------------------------
# Byte-identical replay
# ---------------------------------------------------------------------------


class TestByteIdenticalReplay:
    def test_two_warehouse_backed_runs_produce_identical_comparison_hash(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        # Two runs with identical config -> byte-identical
        # comparison hashes.  Different research_data / report
        # roots are fine — those only affect where artifacts
        # land, not the comparison content.
        config_a = _mk_config(tmp_path / "a", dataset_id="run-shared")
        config_b = _mk_config(tmp_path / "b", dataset_id="run-shared")

        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle_a = run_historical_validation(
                config_a,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle_b = run_historical_validation(
                config_b,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        # Comparison content is stable across independent runs
        assert (
            bundle_a.comparison.stable_hash()
            == bundle_b.comparison.stable_hash()
        )


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


class TestReadOnlyGuarantees:
    def test_promotion_entry_disabled_regardless_of_source(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle = run_historical_validation(
                config,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        assert bundle.promotion_entry.current_state == STATE_DISABLED
        assert bundle.promotion_entry.approvals == []

    def test_feature_flags_remain_disabled_after_warehouse_run(
        self, tmp_path, seeded_warehouse
    ):
        flags = reset_feature_flags()
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            run_historical_validation(
                config,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        assert flags.all_disabled is True
        assert flags.enabled_flags == []

    def test_bundle_dict_carries_dataset_provenance(
        self, tmp_path, seeded_warehouse
    ):
        reset_feature_flags()
        config = _mk_config(tmp_path)
        with WarehouseReader(layout=seeded_warehouse) as reader:
            bundle = run_historical_validation(
                config,
                warehouse_reader=reader,
                generated_at="2026-07-04T00:00:00+00:00",
            )
        d = bundle.to_dict()
        assert "dataset_provenance" in d
        assert d["dataset_provenance"]["source"] == "warehouse"
