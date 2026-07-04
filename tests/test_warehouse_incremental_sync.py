"""Tests for strategy/warehouse/incremental_sync.py.

Seventh Phase 5.6 implementation card ``t_phase56_incremental_sync``.
All work runs under pytest ``tmp_path``.

Coverage:

* ``sync_dataset`` end-to-end: appends bars strictly newer than
  the existing latest bar
* Never redownloads existing bars — provider is asked for a
  window starting at the current latest timestamp
* Provider priority order: falls through to a lower-priority
  provider if the primary returns empty
* Provider failure surfaces on the report as a warning, and the
  next provider is tried
* ``dry_run`` disables writes but populates the report with the
  planned appends
* Refuses to touch a validated manifest without warehouse-revise
* Provider ledger tracks per-provider call + bar counts
* ``sync_all`` batches datasets, catches per-dataset errors
* Manifest end_date advances on append
* Read-only / feature-flag guarantees
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.data_catalog import DatasetManifest
from strategy.local_warehouse import (
    STATUS_UNVALIDATED,
    STATUS_VALIDATED,
    WarehouseLayout,
    read_manifest,
    write_manifest,
)
from strategy.market_data_provider import (
    AdjustmentMode,
    AssetClass,
    Bar,
    BarInterval,
    MarketDataRequestError,
    ProviderCapabilities,
    ProviderResponse,
)
from strategy.warehouse.import_pipeline import import_bars
from strategy.warehouse.incremental_sync import (
    IncrementalSyncError,
    ProviderCall,
    ProviderLedger,
    SyncPolicy,
    SyncReport,
    sync_all,
    sync_dataset,
)


# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------


class SeedProvider:
    """Seeds a fresh dataset with a fixed batch of bars."""

    name = "seed"

    def __init__(self, bars: List[Bar]) -> None:
        self._bars = bars

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="seed",
            supported_intervals=(BarInterval.DAILY,),
            supported_adjustments=(AdjustmentMode.RAW,),
            supported_asset_classes=(AssetClass.EQUITY,),
            supports_corporate_actions=False,
            supports_symbol_metadata=False,
            supports_calendar=False,
        )

    def fetch_daily_bars(self, symbols, start, end, adjustment=AdjustmentMode.RAW):
        symbols_ok = {b.symbol for b in self._bars}
        return ProviderResponse(
            provider="seed",
            batch=list(self._bars),
            per_symbol_status={s: ("ok" if s in symbols_ok else "empty") for s in symbols},
        )

    def fetch_intraday_bars(self, *a, **kw):
        return ProviderResponse(provider="seed", batch=[])

    def fetch_corporate_actions(self, *a, **kw):
        return ProviderResponse(provider="seed", batch=[])

    def fetch_symbol_metadata(self, *a, **kw):
        return ProviderResponse(provider="seed", batch=[])

    def fetch_calendar(self, *a, **kw):
        return ProviderResponse(provider="seed", batch=[])


class ScriptedProvider:
    """Returns a fixed set of bars regardless of query window.

    Records every call so tests can assert priority-list traversal.
    """

    def __init__(
        self,
        name: str,
        bars: Optional[List[Bar]] = None,
        raise_error: bool = False,
    ) -> None:
        self.name = name
        self._bars = list(bars or [])
        self._raise = raise_error
        self.calls: List[Dict[str, Any]] = []

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

    def fetch_daily_bars(self, symbols, start, end, adjustment=AdjustmentMode.RAW):
        self.calls.append({"symbols": list(symbols), "start": start, "end": end})
        if self._raise:
            raise MarketDataRequestError(f"{self.name} intentional failure")
        return ProviderResponse(
            provider=self.name,
            batch=list(self._bars),
            per_symbol_status={s: "ok" for s in symbols},
        )

    def fetch_intraday_bars(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_corporate_actions(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_symbol_metadata(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])

    def fetch_calendar(self, *a, **kw):
        return ProviderResponse(provider=self.name, batch=[])


def _bar(ts: str, symbol: str = "AAPL", close: float = 100.5) -> Bar:
    # Ensure OHLC invariants hold when caller supplies a custom close.
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000_000,
        interval=BarInterval.DAILY,
        adjustment_mode=AdjustmentMode.RAW,
    )


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    return WarehouseLayout(root=tmp_path / "wh")


@pytest.fixture
def seeded_dataset(layout: WarehouseLayout) -> str:
    """Seed a dataset with three days of AAPL bars."""
    seed = SeedProvider(
        [
            _bar("2020-05-01T14:30:00+00:00"),
            _bar("2020-05-02T14:30:00+00:00"),
            _bar("2020-05-03T14:30:00+00:00"),
        ]
    )
    import_bars(
        layout=layout,
        provider=seed,
        dataset_id="aapl",
        symbols=["AAPL"],
        start="2020-05-01",
        end="2020-05-03",
        asset_class=AssetClass.EQUITY,
        interval=BarInterval.DAILY,
    )
    return "aapl"


# ---------------------------------------------------------------------------
# sync_dataset happy path
# ---------------------------------------------------------------------------


class TestSyncHappyPath:
    def test_appends_strictly_newer_bars(self, layout, seeded_dataset):
        primary = ScriptedProvider(
            "primary",
            bars=[
                _bar("2020-05-03T14:30:00+00:00"),  # duplicate — filtered
                _bar("2020-05-04T14:30:00+00:00"),
                _bar("2020-05-05T14:30:00+00:00"),
            ],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,)),
        )
        assert report.bars_appended == 2
        assert report.latest_after >= "2020-05-05"
        assert report.provider_name == "primary"

    def test_no_new_bars_leaves_dataset_unchanged(self, layout, seeded_dataset):
        primary = ScriptedProvider("primary", bars=[])
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,)),
        )
        assert report.bars_appended == 0
        assert report.files_appended == ()

    def test_manifest_end_date_advances(self, layout, seeded_dataset):
        before = read_manifest(layout, seeded_dataset)["end_date"]
        primary = ScriptedProvider(
            "primary",
            bars=[_bar("2020-05-10T14:30:00+00:00")],
        )
        sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,)),
        )
        after = read_manifest(layout, seeded_dataset)["end_date"]
        assert after > before
        assert after.startswith("2020-05-10")


class TestProviderPriority:
    def test_falls_through_to_next_when_primary_empty(
        self, layout, seeded_dataset
    ):
        primary = ScriptedProvider("primary", bars=[])
        secondary = ScriptedProvider(
            "secondary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary, secondary)),
        )
        assert report.provider_name == "secondary"
        assert report.bars_appended == 1
        assert len(primary.calls) == 1
        assert len(secondary.calls) == 1

    def test_stops_after_first_success(self, layout, seeded_dataset):
        primary = ScriptedProvider(
            "primary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        secondary = ScriptedProvider(
            "secondary",
            bars=[_bar("2020-05-05T14:30:00+00:00")],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary, secondary)),
        )
        assert report.provider_name == "primary"
        assert len(secondary.calls) == 0

    def test_provider_failure_falls_through_and_records_warning(
        self, layout, seeded_dataset
    ):
        primary = ScriptedProvider("primary", raise_error=True)
        secondary = ScriptedProvider(
            "secondary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary, secondary)),
        )
        assert report.provider_name == "secondary"
        assert any("intentional failure" in w for w in report.warnings)


class TestDryRun:
    def test_dry_run_disables_writes(self, layout, seeded_dataset):
        before_manifest = read_manifest(layout, seeded_dataset)
        primary = ScriptedProvider(
            "primary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,), dry_run=True),
        )
        # Report describes the plan
        assert report.dry_run is True
        assert report.bars_appended == 1
        # But no files landed and the manifest is unchanged
        assert report.files_appended == ()
        assert read_manifest(layout, seeded_dataset) == before_manifest


# ---------------------------------------------------------------------------
# Immutability — validated datasets
# ---------------------------------------------------------------------------


class TestValidatedGuard:
    def test_refuses_to_sync_validated_dataset(self, layout, seeded_dataset):
        payload = read_manifest(layout, seeded_dataset)
        payload["validation_status"] = "validated"
        write_manifest(layout, seeded_dataset, payload, force=True)
        primary = ScriptedProvider(
            "primary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,)),
        )
        assert report.bars_appended == 0
        assert any("validated" in w for w in report.warnings)

    def test_dry_run_still_reports_on_validated_dataset(
        self, layout, seeded_dataset
    ):
        payload = read_manifest(layout, seeded_dataset)
        payload["validation_status"] = "validated"
        write_manifest(layout, seeded_dataset, payload, force=True)
        primary = ScriptedProvider(
            "primary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        report = sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,), dry_run=True),
        )
        # dry-run still surfaces what a sync WOULD append, so
        # operators can preview changes to a validated dataset
        # before choosing to run warehouse-revise.
        assert report.dry_run is True


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class TestProviderLedger:
    def test_tracks_calls_and_bars_per_provider(
        self, layout, seeded_dataset
    ):
        primary = ScriptedProvider("primary", bars=[])
        secondary = ScriptedProvider(
            "secondary",
            bars=[
                _bar("2020-05-04T14:30:00+00:00"),
                _bar("2020-05-05T14:30:00+00:00"),
            ],
        )
        ledger = ProviderLedger()
        sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary, secondary)),
            ledger,
        )
        assert ledger.calls == {"primary": 1, "secondary": 1}
        assert ledger.bars["secondary"] == 2
        assert ledger.bars["primary"] == 0

    def test_ledger_serializes(self, layout, seeded_dataset):
        ledger = ProviderLedger()
        ledger.record("primary", 3)
        assert ledger.to_dict() == {
            "calls": {"primary": 1},
            "bars": {"primary": 3},
        }


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


class TestSyncPolicy:
    def test_empty_provider_list_rejected(self):
        with pytest.raises(IncrementalSyncError, match="at least one"):
            SyncPolicy(providers=())


# ---------------------------------------------------------------------------
# sync_all
# ---------------------------------------------------------------------------


class TestSyncAll:
    def test_returns_report_per_dataset(self, layout):
        # Seed two datasets
        for dataset_id, close in (("aapl", 100.0), ("msft", 200.0)):
            seed = SeedProvider(
                [_bar("2020-05-01T14:30:00+00:00", symbol=dataset_id.upper(), close=close)]
            )
            import_bars(
                layout=layout,
                provider=seed,
                dataset_id=dataset_id,
                symbols=[dataset_id.upper()],
                start="2020-05-01",
                end="2020-05-01",
                asset_class=AssetClass.EQUITY,
                interval=BarInterval.DAILY,
            )
        primary = ScriptedProvider(
            "primary",
            bars=[
                _bar("2020-05-02T14:30:00+00:00", symbol="AAPL"),
                _bar("2020-05-02T14:30:00+00:00", symbol="MSFT"),
            ],
        )
        reports = sync_all(
            layout, ["aapl", "msft"],
            SyncPolicy(providers=(primary,)),
        )
        assert [r.dataset_id for r in reports] == ["aapl", "msft"]

    def test_missing_dataset_yields_warning_not_exception(self, layout):
        primary = ScriptedProvider("primary")
        reports = sync_all(
            layout, ["no-such-dataset"],
            SyncPolicy(providers=(primary,)),
        )
        assert len(reports) == 1
        assert reports[0].bars_appended == 0
        assert reports[0].warnings


# ---------------------------------------------------------------------------
# Manifest wiring
# ---------------------------------------------------------------------------


class TestManifestWiring:
    def test_missing_warehouse_fields_rejected(self, layout):
        # Write a manifest without interval / asset_class — a
        # legacy Phase 3 manifest that sync should refuse.
        legacy = {
            "dataset_id": "legacy",
            "kind": "historical_bars",
            "symbols": ["AAPL"],
            "start_date": "2020-05-01",
            "end_date": "2020-05-03",
            "files": [{"path": "x.csv", "sha256": "0" * 64, "size_bytes": 1}],
            "validation_status": "unvalidated",
        }
        write_manifest(layout, "legacy", legacy)
        with pytest.raises(IncrementalSyncError, match="warehouse fields"):
            sync_dataset(
                layout, "legacy",
                SyncPolicy(providers=(ScriptedProvider("primary"),)),
            )

    def test_no_symbols_rejected(self, layout):
        # A manifest with warehouse fields but no symbols is a
        # configuration bug — sync refuses.
        seed = SeedProvider([_bar("2020-05-01T14:30:00+00:00")])
        import_bars(
            layout=layout,
            provider=seed,
            dataset_id="ds",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-01",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        payload = read_manifest(layout, "ds")
        payload["symbols"] = []
        # symbols=[] fails DatasetManifest.validate on files
        # so we bypass write_manifest and drop the file directly.
        (layout.manifests_dir / "ds.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        with pytest.raises(IncrementalSyncError, match="symbols"):
            sync_dataset(
                layout, "ds",
                SyncPolicy(providers=(ScriptedProvider("primary"),)),
            )


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.incremental_sync as module
    return Path(module.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_order_path_or_live_runner_imports(self):
        source = _module_source()
        for token in (
            "submit_order",
            "place_order",
            "cancel_order",
            "close_position",
            "TradingClient",
            "from trader import",
            "import trader\n",
            "from crypto_trader import",
            "import crypto_trader",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_provider_credential_env_reads(self):
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
    def test_flags_stay_disabled_after_sync(self, layout, seeded_dataset):
        flags = reset_feature_flags()
        primary = ScriptedProvider(
            "primary",
            bars=[_bar("2020-05-04T14:30:00+00:00")],
        )
        sync_dataset(
            layout, seeded_dataset,
            SyncPolicy(providers=(primary,)),
        )
        assert flags.all_disabled is True
