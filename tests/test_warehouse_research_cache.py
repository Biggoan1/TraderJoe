"""Tests for strategy/warehouse/research_cache.py.

Twelfth Phase 5.6 card ``t_phase56_research_cache``.

Load-bearing tests (per the card's Definition of Done):

* Warehouse-first: when local coverage exists, no provider call
  is made — verified against a call counter on a stub that
  mirrors ``ResearchAccountClient.fetch_bars`` shape.
* Offline path: with the provider list empty, a fetch that
  hits complete warehouse coverage completes without raising.
* Provider fallback: with coverage missing, the reader falls
  through the priority list.
* Priority order: earlier providers get called before later
  ones.
* Pinned versions: pinned dataset_id wins over
  latest_validated.

Plus all the usual read-only guarantees.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.data_catalog import DataCatalog, DatasetFile, DatasetManifest
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    WarehouseLayout,
    write_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    MarketDataProvider,
    MarketDataRequestError,
    ProviderCapabilities,
    ProviderResponse,
)
from strategy.warehouse.parquet_io import write_bars
from strategy.warehouse.research_cache import (
    CacheHit,
    KNOWN_SOURCES,
    ResearchCacheError,
    SOURCE_MANUAL,
    SOURCE_PROVIDER,
    SOURCE_WAREHOUSE,
    WarehouseReader,
)


# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------


class TrackingProvider:
    """Stub MarketDataProvider that counts every fetch_daily_bars
    call.  Mirrors the surface ``ResearchAccountClient.fetch_bars``
    exposes for regression testing.
    """

    def __init__(self, name: str = "tracker", empty: bool = False) -> None:
        self.name = name
        self.calls = 0
        self._empty = empty

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name=self.name,
            supported_intervals=(BarInterval.DAILY,),
            supported_adjustments=(AdjustmentMode.RAW,),
            supported_asset_classes=(AssetClass.EQUITY,),
            supports_corporate_actions=False,
            supports_symbol_metadata=False,
            supports_calendar=False,
        )

    def fetch_daily_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[Sequence[Bar]]:
        self.calls += 1
        if self._empty:
            return ProviderResponse(provider=self.name, batch=[])
        return ProviderResponse(
            provider=self.name,
            batch=[
                _bar(sym, "2020-05-04T14:30:00+00:00") for sym in symbols
            ],
        )

    def fetch_intraday_bars(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_corporate_actions(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_symbol_metadata(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_calendar(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])


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


def _seed_dataset(
    layout: WarehouseLayout,
    dataset_id: str,
    bars: List[Bar],
    corporate_action_version: str = "1",
    validation_status: str = STATUS_VALIDATED,
) -> DatasetManifest:
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
    m = DatasetManifest(
        dataset_id=dataset_id,
        kind="historical_bars",
        symbols=tuple(sorted({b.symbol for b in bars})),
        start_date=min(b.timestamp[:10] for b in bars),
        end_date=max(b.timestamp[:10] for b in bars),
        files=files,
        provider="alpaca",
        interval="1Day",
        asset_class="equity",
        adjustment_mode="raw",
        corporate_action_version=corporate_action_version,
        validation_status=validation_status,
    )
    write_manifest(layout, dataset_id, m.to_dict(), force=True)
    return m


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    layout = WarehouseLayout(root=tmp_path / "wh")
    layout.create()
    return layout


# ---------------------------------------------------------------------------
# Warehouse-first behavior
# ---------------------------------------------------------------------------


class TestWarehouseFirst:
    def test_warehouse_hit_when_coverage_complete(self, layout):
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00")],
        )
        provider = TrackingProvider()
        with WarehouseReader(
            layout=layout, provider_priority=(provider,)
        ) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )
        assert hit.source == SOURCE_WAREHOUSE
        assert hit.dataset_id == "aapl-2020"
        assert len(hit.bars) == 1
        # Load-bearing: provider was NOT called
        assert provider.calls == 0

    def test_regression_no_provider_call_when_coverage_complete(self, layout):
        # This is the mandated regression per the card: prove that
        # ResearchAccountClient.fetch_bars is NOT called when
        # warehouse coverage is complete.  The TrackingProvider
        # counts every call; assert 0.
        _seed_dataset(
            layout, "aapl-2020",
            [
                _bar("AAPL", "2020-05-04T14:30:00+00:00"),
                _bar("AAPL", "2020-05-05T14:30:00+00:00"),
                _bar("MSFT", "2020-05-04T14:30:00+00:00"),
                _bar("MSFT", "2020-05-05T14:30:00+00:00"),
            ],
        )
        provider = TrackingProvider(name="research-account-client")
        with WarehouseReader(
            layout=layout, provider_priority=(provider,)
        ) as reader:
            reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL", "MSFT"], "2020-05-04", "2020-05-05",
            )
        assert provider.calls == 0


# ---------------------------------------------------------------------------
# Offline byte-identical read
# ---------------------------------------------------------------------------


class TestOfflineBehavior:
    def test_reads_bars_offline_with_no_providers(self, layout):
        # No providers configured; warehouse must satisfy alone.
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00", 100.0)],
        )
        with WarehouseReader(layout=layout) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )
        assert hit.source == SOURCE_WAREHOUSE
        assert hit.bars[0].close == 100.0

    def test_two_reader_instances_return_byte_identical_bars(self, layout):
        # Reading through two separate WarehouseReader instances
        # must return byte-identical Bar dicts — the warehouse is
        # the source of truth, independent of any provider state.
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00", 100.0)],
        )
        with WarehouseReader(layout=layout) as r1:
            hit_a = r1.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )
        with WarehouseReader(layout=layout) as r2:
            hit_b = r2.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )
        assert [b.to_dict() for b in hit_a.bars] == [
            b.to_dict() for b in hit_b.bars
        ]


# ---------------------------------------------------------------------------
# Provider fallback
# ---------------------------------------------------------------------------


class TestProviderFallback:
    def test_falls_through_when_coverage_missing(self, layout):
        # Warehouse has AAPL only; request MSFT
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00")],
        )
        provider = TrackingProvider()
        with WarehouseReader(
            layout=layout, provider_priority=(provider,)
        ) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["MSFT"], "2020-05-04", "2020-05-04",
            )
        assert hit.source == SOURCE_PROVIDER
        assert hit.provider_name == "tracker"
        assert provider.calls == 1

    def test_priority_order_respected(self, layout):
        primary = TrackingProvider(name="primary")
        secondary = TrackingProvider(name="secondary")
        with WarehouseReader(
            layout=layout, provider_priority=(primary, secondary)
        ) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["MSFT"], "2020-05-04", "2020-05-04",
            )
        assert hit.provider_name == "primary"
        assert primary.calls == 1
        assert secondary.calls == 0

    def test_empty_primary_falls_through_to_secondary(self, layout):
        primary = TrackingProvider(name="primary", empty=True)
        secondary = TrackingProvider(name="secondary")
        with WarehouseReader(
            layout=layout, provider_priority=(primary, secondary)
        ) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["MSFT"], "2020-05-04", "2020-05-04",
            )
        assert hit.provider_name == "secondary"

    def test_raise_when_nothing_returns_data(self, layout):
        primary = TrackingProvider(name="primary", empty=True)
        with WarehouseReader(
            layout=layout, provider_priority=(primary,)
        ) as reader:
            with pytest.raises(ResearchCacheError, match="no source"):
                reader.fetch_bars(
                    AssetClass.EQUITY, BarInterval.DAILY,
                    ["MSFT"], "2020-05-04", "2020-05-04",
                )

    def test_provider_failure_falls_through_with_warning(self, layout):
        class FailingProvider:
            name = "failing"
            def provider_capabilities(self):
                return ProviderCapabilities(
                    provider_name="failing",
                    supported_intervals=(BarInterval.DAILY,),
                    supported_adjustments=(AdjustmentMode.RAW,),
                    supported_asset_classes=(AssetClass.EQUITY,),
                    supports_corporate_actions=False,
                    supports_symbol_metadata=False,
                    supports_calendar=False,
                )
            def fetch_daily_bars(self, *a, **kw):
                raise MarketDataRequestError("boom")
            def fetch_intraday_bars(self, *a, **kw):
                return ProviderResponse(provider="failing", batch=[])
            def fetch_corporate_actions(self, *a, **kw):
                return ProviderResponse(provider="failing", batch=[])
            def fetch_symbol_metadata(self, *a, **kw):
                return ProviderResponse(provider="failing", batch=[])
            def fetch_calendar(self, *a, **kw):
                return ProviderResponse(provider="failing", batch=[])
        secondary = TrackingProvider(name="secondary")
        with WarehouseReader(
            layout=layout,
            provider_priority=(FailingProvider(), secondary),
        ) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["MSFT"], "2020-05-04", "2020-05-04",
            )
        assert hit.provider_name == "secondary"


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------


class TestHasCompleteCoverage:
    def test_true_when_dataset_covers_symbol_and_window(self, layout):
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00")],
        )
        with WarehouseReader(layout=layout) as reader:
            assert reader.has_complete_coverage(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )

    def test_false_for_symbol_outside_dataset(self, layout):
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00")],
        )
        with WarehouseReader(layout=layout) as reader:
            assert not reader.has_complete_coverage(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["MSFT"], "2020-05-04", "2020-05-04",
            )

    def test_false_for_window_outside_dataset(self, layout):
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00")],
        )
        with WarehouseReader(layout=layout) as reader:
            assert not reader.has_complete_coverage(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-06-01", "2020-06-30",
            )

    def test_false_for_empty_symbol_list(self, layout):
        with WarehouseReader(layout=layout) as reader:
            assert not reader.has_complete_coverage(
                AssetClass.EQUITY, BarInterval.DAILY,
                [], "2020-05-04", "2020-05-04",
            )


# ---------------------------------------------------------------------------
# Pinned versions
# ---------------------------------------------------------------------------


class TestPinnedVersions:
    def test_pinned_version_wins_over_latest(self, layout):
        # Two versions of the same dataset; pin v1 explicitly
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00", 100.0)],
            corporate_action_version="1",
        )
        _seed_dataset(
            layout, "aapl-2020-v2",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00", 25.0)],
            corporate_action_version="2",
        )
        with WarehouseReader(
            layout=layout,
            pinned_versions={"aapl-2020": "1"},
        ) as reader:
            hit = reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )
        assert hit.bars[0].close == 100.0  # v1
        assert "aapl-2020" in hit.dataset_id


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.research_cache as module
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_or_live_runner_imports(self):
        source = _module_source()
        for token in (
            "submit_order", "place_order", "cancel_order",
            "close_position", "TradingClient",
            "from trader import", "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_credential_env_reads(self):
        source = _module_source()
        for pattern in (
            r'os\.environ\[\s*[\'"]ALPACA_',
            r'os\.getenv\(\s*[\'"]ALPACA_',
            r'os\.environ\[\s*[\'"]APCA_',
            r'os\.environ\[\s*[\'"]RESEARCH_ALPACA_',
        ):
            assert not re.search(pattern, source)

    def test_no_approval_or_promotion_construction(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        assert "PromotionEntry(" not in source


class TestFeatureFlagsUntouched:
    def test_flags_stay_disabled(self, layout):
        flags = reset_feature_flags()
        _seed_dataset(
            layout, "aapl-2020",
            [_bar("AAPL", "2020-05-04T14:30:00+00:00")],
        )
        with WarehouseReader(layout=layout) as reader:
            reader.fetch_bars(
                AssetClass.EQUITY, BarInterval.DAILY,
                ["AAPL"], "2020-05-04", "2020-05-04",
            )
        assert flags.all_disabled is True

    def test_known_sources_include_expected_values(self):
        assert set(KNOWN_SOURCES) == {
            SOURCE_WAREHOUSE, SOURCE_PROVIDER, SOURCE_MANUAL,
        }
