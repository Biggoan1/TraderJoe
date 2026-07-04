"""Tests for strategy/warehouse/import_pipeline.py.

Sixth Phase 5.6 implementation card ``t_phase56_import_pipeline``.
All filesystem work runs under pytest ``tmp_path``; the provider
is a deterministic stub — no real HTTP.

Coverage:

* ``import_bars`` end-to-end: chunks, sorts, writes Parquet,
  computes manifest with sha256 / row_count / warehouse fields
* Chunk-size validation
* Rate-limit friendliness: fewer symbols per chunk yields the
  same total output
* ``per_symbol_status`` propagation into the report
* Resumable queue: interrupted runs can be replayed and
  ``PendingDownloadsQueue`` records status transitions
* Provider failure surfaces as ``MarketDataRequestError``
* ``rebuild_manifest`` regenerates from disk with correct
  checksums + symbol coverage + date bounds
* Immutability guard: refuses to overwrite validated manifest
  unless ``force``
* No live-runner imports, feature flags untouched
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.data_catalog import DataCatalog, DatasetManifest
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
    MarketDataRequestError,
    MarketDataValidationError,
    ProviderCapabilities,
    ProviderResponse,
)
from strategy.warehouse.import_pipeline import (
    DEFAULT_CHUNK_SIZE,
    ImportPipelineError,
    ImportReport,
    KNOWN_QUEUE_STATUSES,
    PendingDownloadsQueue,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INFLIGHT,
    STATUS_PENDING,
    import_bars,
    rebuild_manifest,
)


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------


class StubProvider:
    """Deterministic provider stub — never touches the network.

    Emits three daily bars per symbol per call by default.  Can be
    configured to raise on the Nth call (for failure tests) or to
    return an empty batch for specific symbols.
    """

    name = "stub"

    def __init__(
        self,
        empty_symbols: Optional[Sequence[str]] = None,
        raise_after: Optional[int] = None,
    ) -> None:
        self._empty = set(empty_symbols or ())
        self.calls: List[Dict[str, Any]] = []
        self._raise_after = raise_after

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="stub",
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
        self.calls.append(
            {
                "kind": "daily",
                "symbols": list(symbols),
                "start": start,
                "end": end,
                "adjustment": adjustment,
            }
        )
        if (
            self._raise_after is not None
            and len(self.calls) > self._raise_after
        ):
            raise MarketDataRequestError("simulated provider failure")
        bars: List[Bar] = []
        status: Dict[str, str] = {}
        for sym in symbols:
            if sym in self._empty:
                status[sym] = "empty"
                continue
            status[sym] = "ok"
            for day in ("05-01", "05-02", "05-03"):
                bars.append(
                    Bar(
                        symbol=sym,
                        timestamp=f"2020-{day}T14:30:00+00:00",
                        open=100.0,
                        high=101.0,
                        low=99.5,
                        close=100.5,
                        volume=1_000_000,
                        interval=BarInterval.DAILY,
                        adjustment_mode=AdjustmentMode.RAW,
                    )
                )
        return ProviderResponse(
            provider="stub",
            batch=bars,
            per_symbol_status=status,
        )

    def fetch_intraday_bars(self, *a, **kw):
        return ProviderResponse(provider="stub", batch=[])

    def fetch_corporate_actions(self, *a, **kw):
        return ProviderResponse(provider="stub", batch=[])

    def fetch_symbol_metadata(self, *a, **kw):
        return ProviderResponse(provider="stub", batch=[])

    def fetch_calendar(self, *a, **kw):
        return ProviderResponse(provider="stub", batch=[])


@pytest.fixture
def layout(tmp_path: Path) -> WarehouseLayout:
    return WarehouseLayout(root=tmp_path / "warehouse")


# ---------------------------------------------------------------------------
# import_bars — happy path
# ---------------------------------------------------------------------------


class TestImportBarsHappyPath:
    def test_writes_files_and_manifest(self, layout):
        provider = StubProvider()
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id="stub-2020",
            symbols=["AAPL", "MSFT", "NVDA"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        # 3 symbols × 3 bars = 9 bars total
        assert report.total_bars == 9
        # One file per (symbol, year) partition
        assert len(report.files) == 3
        # Manifest exists on disk
        assert Path(report.manifest_path).is_file()
        # Manifest is loadable via the catalog
        payload = json.loads(Path(report.manifest_path).read_text())
        assert payload["provider"] == "stub"
        assert payload["interval"] == "1Day"
        assert payload["asset_class"] == "equity"
        assert payload["validation_status"] == "unvalidated"

    def test_report_carries_per_symbol_status(self, layout):
        provider = StubProvider(empty_symbols=["MSFT"])
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id="stub-2020",
            symbols=["AAPL", "MSFT", "NVDA"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        assert set(report.symbols_ok) == {"AAPL", "NVDA"}
        assert report.symbols_empty == ("MSFT",)

    def test_chunk_size_controls_provider_calls(self, layout):
        provider = StubProvider()
        symbols = ["S1", "S2", "S3", "S4", "S5"]
        import_bars(
            layout=layout,
            provider=provider,
            dataset_id="ds",
            symbols=symbols,
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
            chunk_size=2,
        )
        # 5 symbols / chunk_size=2 = 3 calls (2, 2, 1)
        assert len(provider.calls) == 3
        assert provider.calls[0]["symbols"] == ["S1", "S2"]
        assert provider.calls[-1]["symbols"] == ["S5"]

    def test_chunk_size_output_invariant(self, layout):
        # Same input, different chunk_size, same total bars
        provider_a = StubProvider()
        report_a = import_bars(
            layout=layout,
            provider=provider_a,
            dataset_id="ds-a",
            symbols=["AAPL", "MSFT", "NVDA"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
            chunk_size=1,
        )
        provider_b = StubProvider()
        report_b = import_bars(
            layout=WarehouseLayout(root=layout.root.parent / "wh2"),
            provider=provider_b,
            dataset_id="ds-b",
            symbols=["AAPL", "MSFT", "NVDA"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
            chunk_size=100,
        )
        assert report_a.total_bars == report_b.total_bars

    def test_files_land_under_dataset_dir(self, layout):
        provider = StubProvider()
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id="stub",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        expected_prefix = "equities/daily/stub/AAPL/"
        for f in report.files:
            assert f.relative_path.startswith(expected_prefix)

    def test_default_chunk_size_positive(self):
        assert DEFAULT_CHUNK_SIZE > 0


# ---------------------------------------------------------------------------
# Manifest thread-through
# ---------------------------------------------------------------------------


class TestManifestThreadThrough:
    def test_manifest_files_carry_sha256_and_size(self, layout):
        provider = StubProvider()
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id="ds",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        payload = json.loads(Path(report.manifest_path).read_text())
        for entry in payload["files"]:
            assert len(entry["sha256"]) == 64
            assert entry["size_bytes"] > 0
            assert entry["row_count"] == 3

    def test_manifest_is_loadable_by_catalog(self, layout):
        provider = StubProvider()
        import_bars(
            layout=layout,
            provider=provider,
            dataset_id="ds",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        catalog = DataCatalog.from_warehouse_layout(layout)
        assert catalog.has("ds")
        manifest = catalog.get("ds")
        assert manifest.provider == "stub"
        assert manifest.validation_status == "unvalidated"

    def test_symbols_empty_stashed_in_metadata(self, layout):
        provider = StubProvider(empty_symbols=["MSFT"])
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id="ds",
            symbols=["AAPL", "MSFT"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        payload = json.loads(Path(report.manifest_path).read_text())
        assert payload["metadata"]["symbols_empty"] == ["MSFT"]

    def test_refuses_to_overwrite_validated_manifest(self, layout):
        provider = StubProvider()
        import_bars(
            layout=layout,
            provider=provider,
            dataset_id="ds",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        # Promote to validated
        payload = json.loads(layout.manifest_path("ds").read_text())
        payload["validation_status"] = "validated"
        write_manifest(layout, "ds", payload, force=True)

        # Second import must refuse
        with pytest.raises(Exception, match="validated"):
            import_bars(
                layout=layout,
                provider=StubProvider(),
                dataset_id="ds",
                symbols=["AAPL"],
                start="2020-05-01",
                end="2020-05-31",
                asset_class=AssetClass.EQUITY,
                interval=BarInterval.DAILY,
            )


# ---------------------------------------------------------------------------
# Queue + resumability
# ---------------------------------------------------------------------------


class TestPendingDownloadsQueue:
    def test_status_transitions(self, tmp_path):
        with PendingDownloadsQueue(tmp_path / "q.db") as q:
            q.enqueue("ds", "AAPL", "2020-01-01", "2020-12-31")
            assert q.status("ds", "AAPL", "2020-01-01", "2020-12-31") == STATUS_PENDING
            q.mark("ds", "AAPL", "2020-01-01", "2020-12-31", STATUS_INFLIGHT)
            assert q.status("ds", "AAPL", "2020-01-01", "2020-12-31") == STATUS_INFLIGHT
            q.mark("ds", "AAPL", "2020-01-01", "2020-12-31", STATUS_DONE)
            assert q.status("ds", "AAPL", "2020-01-01", "2020-12-31") == STATUS_DONE

    def test_pending_lists_unfinished_only(self, tmp_path):
        with PendingDownloadsQueue(tmp_path / "q.db") as q:
            q.enqueue("ds", "AAPL", "2020-01-01", "2020-12-31")
            q.enqueue("ds", "MSFT", "2020-01-01", "2020-12-31")
            q.enqueue("ds", "NVDA", "2020-01-01", "2020-12-31")
            q.mark("ds", "AAPL", "2020-01-01", "2020-12-31", STATUS_DONE)
            pending = q.pending("ds")
            names = [row[0] for row in pending]
            assert "AAPL" not in names
            assert set(names) == {"MSFT", "NVDA"}

    def test_reject_unknown_status(self, tmp_path):
        with PendingDownloadsQueue(tmp_path / "q.db") as q:
            q.enqueue("ds", "AAPL", "2020-01-01", "2020-12-31")
            with pytest.raises(ImportPipelineError, match="unknown queue status"):
                q.mark(
                    "ds", "AAPL", "2020-01-01", "2020-12-31", "bogus"
                )

    def test_enqueue_is_idempotent(self, tmp_path):
        with PendingDownloadsQueue(tmp_path / "q.db") as q:
            q.enqueue("ds", "AAPL", "2020-01-01", "2020-12-31")
            q.enqueue("ds", "AAPL", "2020-01-01", "2020-12-31")
            # PRIMARY KEY prevents duplication
            assert len(q.pending("ds")) == 1


class TestImportBarsResumability:
    def test_queue_tracks_completed_chunks(self, tmp_path, layout):
        with PendingDownloadsQueue(tmp_path / "q.db") as q:
            import_bars(
                layout=layout,
                provider=StubProvider(),
                dataset_id="ds",
                symbols=["AAPL", "MSFT"],
                start="2020-05-01",
                end="2020-05-31",
                asset_class=AssetClass.EQUITY,
                interval=BarInterval.DAILY,
                queue=q,
                chunk_size=1,
            )
            # All symbols marked DONE
            assert q.pending("ds") == []
            for sym in ("AAPL", "MSFT"):
                assert (
                    q.status("ds", sym, "2020-05-01", "2020-05-31")
                    == STATUS_DONE
                )

    def test_provider_failure_marks_chunk_failed(self, tmp_path, layout):
        provider = StubProvider(raise_after=1)  # fail on second chunk
        with PendingDownloadsQueue(tmp_path / "q.db") as q:
            with pytest.raises(MarketDataRequestError):
                import_bars(
                    layout=layout,
                    provider=provider,
                    dataset_id="ds",
                    symbols=["AAPL", "MSFT", "NVDA"],
                    start="2020-05-01",
                    end="2020-05-31",
                    asset_class=AssetClass.EQUITY,
                    interval=BarInterval.DAILY,
                    queue=q,
                    chunk_size=1,
                )
            # AAPL completed (chunk 1), MSFT and NVDA marked FAILED
            assert (
                q.status("ds", "AAPL", "2020-05-01", "2020-05-31")
                == STATUS_DONE
            )
            assert (
                q.status("ds", "MSFT", "2020-05-01", "2020-05-31")
                == STATUS_FAILED
            )


# ---------------------------------------------------------------------------
# Chunk-size validation
# ---------------------------------------------------------------------------


class TestChunkValidation:
    def test_chunk_size_zero_rejected(self, layout):
        with pytest.raises(ImportPipelineError, match="chunk_size"):
            import_bars(
                layout=layout,
                provider=StubProvider(),
                dataset_id="ds",
                symbols=["AAPL"],
                start="2020-05-01",
                end="2020-05-31",
                asset_class=AssetClass.EQUITY,
                interval=BarInterval.DAILY,
                chunk_size=0,
            )

    def test_empty_symbols_rejected(self, layout):
        with pytest.raises(ImportPipelineError, match="symbols"):
            import_bars(
                layout=layout,
                provider=StubProvider(),
                dataset_id="ds",
                symbols=[],
                start="2020-05-01",
                end="2020-05-31",
                asset_class=AssetClass.EQUITY,
                interval=BarInterval.DAILY,
            )

    def test_empty_dataset_id_rejected(self, layout):
        with pytest.raises(ImportPipelineError, match="dataset_id"):
            import_bars(
                layout=layout,
                provider=StubProvider(),
                dataset_id="",
                symbols=["AAPL"],
                start="2020-05-01",
                end="2020-05-31",
                asset_class=AssetClass.EQUITY,
                interval=BarInterval.DAILY,
            )


# ---------------------------------------------------------------------------
# rebuild_manifest
# ---------------------------------------------------------------------------


class TestRebuildManifest:
    def test_regenerates_from_disk(self, layout):
        import_bars(
            layout=layout,
            provider=StubProvider(),
            dataset_id="ds",
            symbols=["AAPL", "MSFT"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        # Delete the manifest to simulate a corrupt / missing catalog
        layout.manifest_path("ds").unlink()
        manifest = rebuild_manifest(
            layout, "ds", AssetClass.EQUITY, BarInterval.DAILY
        )
        assert set(manifest.symbols) == {"AAPL", "MSFT"}
        assert manifest.provider == "operator"
        assert len(manifest.files) == 2

    def test_rebuild_refuses_missing_partition(self, layout):
        layout.create()
        with pytest.raises(ImportPipelineError, match="no Parquet files"):
            rebuild_manifest(
                layout, "no-such", AssetClass.EQUITY, BarInterval.DAILY
            )

    def test_rebuild_computes_correct_checksums(self, layout):
        report = import_bars(
            layout=layout,
            provider=StubProvider(),
            dataset_id="ds",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        expected_sha = report.files[0].sha256
        # Delete manifest, rebuild
        layout.manifest_path("ds").unlink()
        rebuilt = rebuild_manifest(
            layout, "ds", AssetClass.EQUITY, BarInterval.DAILY
        )
        assert rebuilt.files[0].sha256 == expected_sha


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.import_pipeline as module
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
    def test_flags_stay_disabled_across_import(self, layout):
        flags = reset_feature_flags()
        import_bars(
            layout=layout,
            provider=StubProvider(),
            dataset_id="ds",
            symbols=["AAPL"],
            start="2020-05-01",
            end="2020-05-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
        )
        assert flags.all_disabled is True
