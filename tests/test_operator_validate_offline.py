"""Tests for strategy/warehouse/operator/validate_offline.py."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from strategy.config import get_feature_flags, reset_feature_flags
from strategy.data_catalog import DatasetFile, DatasetManifest
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
from strategy.warehouse.operator import validate_offline
from strategy.warehouse.operator.validate_offline import (
    OfflineValidationError,
    build_parser,
    main,
    run,
)
from strategy.warehouse.parquet_io import write_bars
from strategy.warehouse.research_cache import WarehouseReader


# ---------------------------------------------------------------------------
# Warehouse seed
# ---------------------------------------------------------------------------


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


def _seed_watchlist(
    layout: WarehouseLayout,
    dataset_id: str = "watchlist-2020",
    days: int = 61,
    end_date: str = "2020-06-30",
) -> None:
    from datetime import date, timedelta
    layout.create()
    start = date(2020, 5, 1)
    bars: List[Bar] = []
    for symbol in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ"):
        for i in range(days):
            d = start + timedelta(days=i)
            bars.append(
                _bar(symbol, f"{d.isoformat()}T14:30:00+00:00", 100.0 + i * 0.1)
            )
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
        symbols=("AAPL", "MSFT", "NVDA", "QQQ", "SPY"),
        start_date="2020-05-01",
        end_date=end_date,
        files=files,
        provider="alpaca",
        interval="1Day",
        asset_class="equity",
        adjustment_mode="raw",
        validation_status=STATUS_VALIDATED,
    )
    write_manifest(layout, dataset_id, manifest.to_dict(), force=True)


class TrackingResearchClient:
    """Provider stub whose fetch_bars is counted for the
    Alpaca-not-contacted regression.
    """

    def __init__(self) -> None:
        self.fetch_bars_calls: List[Dict[str, Any]] = []

    def fetch_bars(self, **kwargs) -> Dict[str, Any]:
        self.fetch_bars_calls.append(kwargs)
        return {"bars": {}}


@pytest.fixture
def layout(tmp_path):
    return WarehouseLayout(root=tmp_path / "wh")


@pytest.fixture
def seeded(layout):
    _seed_watchlist(layout)
    return layout


# ---------------------------------------------------------------------------
# Warehouse hit — provider not contacted
# ---------------------------------------------------------------------------


class TestWarehouseHit:
    def _args(self, tmp_path, allow_fallback=False):
        parser = build_parser()
        return parser.parse_args(
            [
                "--dataset-id", "offline-run",
                "--start", "2020-05-01",
                "--end", "2020-06-30",
                "--report-root", str(tmp_path / "reports"),
                "--research-data-root", str(tmp_path / "rd"),
            ] + (["--allow-provider-fallback"] if allow_fallback else [])
        )

    def test_provider_not_contacted_on_coverage(self, tmp_path, seeded):
        reset_feature_flags()
        args = self._args(tmp_path)
        client = TrackingResearchClient()
        printed: List[str] = []
        with WarehouseReader(layout=seeded) as reader:
            bundle = run(
                args,
                env={},
                warehouse_reader=reader,
                layout=seeded,
                research_client=client,
                printer=printed.append,
            )
        assert client.fetch_bars_calls == []
        haystack = "\n".join(printed)
        assert "provider_contacted: False" in haystack
        assert bundle.dataset_provenance["source"] == "warehouse"

    def test_promotion_stays_disabled(self, tmp_path, seeded):
        reset_feature_flags()
        args = self._args(tmp_path)
        with WarehouseReader(layout=seeded) as reader:
            bundle = run(
                args, env={}, warehouse_reader=reader, layout=seeded,
                printer=lambda *a, **k: None,
            )
        assert bundle.promotion_entry.current_state == STATE_DISABLED
        assert bundle.promotion_entry.approvals == []

    def test_feature_flags_disabled_after_run(self, tmp_path, seeded):
        flags = reset_feature_flags()
        args = self._args(tmp_path)
        with WarehouseReader(layout=seeded) as reader:
            run(
                args, env={}, warehouse_reader=reader, layout=seeded,
                printer=lambda *a, **k: None,
            )
        assert flags.all_disabled is True


# ---------------------------------------------------------------------------
# Incomplete coverage — refuses by default, requires explicit flag
# ---------------------------------------------------------------------------


class TestIncompleteCoverageRefused:
    def test_missing_coverage_fails_without_flag(self, tmp_path, layout):
        # Warehouse empty
        layout.create()
        parser = build_parser()
        args = parser.parse_args(
            [
                "--dataset-id", "offline-run",
                "--start", "2020-05-01",
                "--end", "2020-06-30",
                "--report-root", str(tmp_path / "reports"),
                "--research-data-root", str(tmp_path / "rd"),
            ]
        )
        with WarehouseReader(layout=layout) as reader:
            with pytest.raises(OfflineValidationError, match="incomplete"):
                run(
                    args, env={}, warehouse_reader=reader, layout=layout,
                    printer=lambda *a, **k: None,
                )


# ---------------------------------------------------------------------------
# Deterministic replay
# ---------------------------------------------------------------------------


class TestDeterministicReplay:
    def test_two_offline_runs_produce_identical_comparison_hash(
        self, tmp_path, seeded
    ):
        reset_feature_flags()
        parser = build_parser()
        args_a = parser.parse_args(
            [
                "--dataset-id", "offline-run",
                "--start", "2020-05-01",
                "--end", "2020-06-30",
                "--report-root", str(tmp_path / "reports_a"),
                "--research-data-root", str(tmp_path / "rd_a"),
            ]
        )
        args_b = parser.parse_args(
            [
                "--dataset-id", "offline-run",
                "--start", "2020-05-01",
                "--end", "2020-06-30",
                "--report-root", str(tmp_path / "reports_b"),
                "--research-data-root", str(tmp_path / "rd_b"),
            ]
        )
        with WarehouseReader(layout=seeded) as r1:
            bundle_a = run(
                args_a, env={}, warehouse_reader=r1, layout=seeded,
                now_iso="2026-07-04T00:00:00+00:00",
                printer=lambda *a, **k: None,
            )
        with WarehouseReader(layout=seeded) as r2:
            bundle_b = run(
                args_b, env={}, warehouse_reader=r2, layout=seeded,
                now_iso="2026-07-04T00:00:00+00:00",
                printer=lambda *a, **k: None,
            )
        assert (
            bundle_a.comparison.stable_hash()
            == bundle_b.comparison.stable_hash()
        )


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    def test_start_after_end_rejected(self, tmp_path, seeded):
        reset_feature_flags()
        parser = build_parser()
        args = parser.parse_args(
            [
                "--dataset-id", "offline-run",
                "--start", "2020-06-30", "--end", "2020-05-01",
                "--report-root", str(tmp_path / "reports"),
                "--research-data-root", str(tmp_path / "rd"),
            ]
        )
        with WarehouseReader(layout=seeded) as reader:
            with pytest.raises(OfflineValidationError, match="start"):
                run(
                    args, env={}, warehouse_reader=reader, layout=seeded,
                    printer=lambda *a, **k: None,
                )


# ---------------------------------------------------------------------------
# Main + exit codes
# ---------------------------------------------------------------------------


class TestMainExitCode:
    def test_incomplete_coverage_returns_2(self, tmp_path, monkeypatch):
        # Point WAREHOUSE_ROOT at an empty tree
        monkeypatch.setenv("WAREHOUSE_ROOT", str(tmp_path / "empty"))
        rc = main(
            [
                "--dataset-id", "offline-run",
                "--start", "2020-05-01",
                "--end", "2020-06-30",
                "--report-root", str(tmp_path / "reports"),
                "--research-data-root", str(tmp_path / "rd"),
            ]
        )
        assert rc == 2


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


def _module_source() -> str:
    import strategy.warehouse.operator.validate_offline as m
    return Path(m.__file__).read_text(encoding="utf-8")


class TestSourceSafety:
    def test_no_live_runner_imports(self):
        source = _module_source()
        for token in (
            "from trader import", "import trader\n",
            "from crypto_trader import",
            "from trader_cli import",
            "from telegram_approvals import",
            "from strategy.runner import",
        ):
            assert token not in source

    def test_no_order_path_references(self):
        source = _module_source()
        for token in (
            "submit_order", "place_order", "cancel_order",
            "TradingClient",
        ):
            assert token not in source

    def test_no_approval_or_promotion_state_advance(self):
        source = _module_source()
        assert "ApprovalRecord(" not in source
        # PromotionEntry may appear in read-only assertions but not
        # as a constructor.  Check the module never advances state.
        assert "current_state=" not in source or 'STATE_DISABLED' in source
