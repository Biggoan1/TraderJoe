"""End-to-end regression test for the multi-symbol pagination bug.

Reproduces the operator's observed failure:

    ./scripts/research-import-watchlist --symbols AAPL MSFT NVDA SPY QQQ \\
      --start 2020-01-01

    symbols_ok:    ['AAPL']
    symbols_empty: ['MSFT', 'NVDA', 'SPY', 'QQQ']
    total_bars:    1000
    files_written: 5

Root cause: ``ResearchAccountClient.fetch_bars`` returned page 1
only.  Alpaca packs the ``limit=1000`` response sorted by symbol,
so AAPL alone consumed all 1000 slots and the remaining symbols
never appeared in the payload.

Fix: paginate ``fetch_bars`` by following ``next_page_token``
until the server signals no more pages (or the safety cap fires).

This test bolts a fake paginated Alpaca server underneath the
real ``ResearchAccountClient`` / ``AlpacaProvider`` /
``import_bars`` stack and asserts every requested symbol ends up
in ``symbols_ok``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from strategy.local_warehouse import WarehouseLayout
from strategy.market_data_provider import AdjustmentMode, AssetClass, BarInterval
from strategy.providers.alpaca import AlpacaProvider
from strategy.research_account import (
    ResearchAccountClient,
    ResearchAccountConfig,
    ResearchAccountRequest,
)
from strategy.warehouse.import_pipeline import PendingDownloadsQueue, import_bars


TEST_ENV: Dict[str, str] = {
    "RESEARCH_ALPACA_API_KEY": "test-api-key",
    "RESEARCH_ALPACA_SECRET_KEY": "test-secret-key",
    "RESEARCH_ALPACA_ENDPOINT": "https://research-paper.alpaca.example",
    "RESEARCH_ALPACA_DATA_ENDPOINT": "https://research-data.alpaca.example",
}


class _PaginatedAlpacaFake:
    """HTTP fake that mimics Alpaca's ``/v2/stocks/bars``
    pagination behavior — packs one symbol's data per page when
    the request's ``limit`` is smaller than the total row count.
    """

    def __init__(self, per_symbol_bars: Dict[str, List[Dict[str, Any]]],
                 rows_per_page: int = 30):
        self._per_symbol = per_symbol_bars
        self._rows_per_page = rows_per_page
        self.requests: List[ResearchAccountRequest] = []

    def __call__(self, request: ResearchAccountRequest, timeout: float) -> bytes:
        self.requests.append(request)
        # Which page was requested?
        page_index = 0
        if "page_token=" in request.url:
            token = request.url.split("page_token=")[1].split("&")[0]
            if token.startswith("page-"):
                page_index = int(token[len("page-"):])
        # Enumerate every (symbol, bar) tuple sorted by symbol
        flat: List[Tuple[str, Dict[str, Any]]] = []
        for sym in sorted(self._per_symbol):
            for row in self._per_symbol[sym]:
                flat.append((sym, row))
        start = page_index * self._rows_per_page
        end = start + self._rows_per_page
        window = flat[start:end]
        page_bars: Dict[str, List[Dict[str, Any]]] = {}
        for sym, row in window:
            page_bars.setdefault(sym, []).append(row)
        next_token = None
        if end < len(flat):
            next_token = f"page-{page_index + 1}"
        payload = {"bars": page_bars, "next_page_token": next_token}
        return json.dumps(payload).encode("utf-8")


def _make_bar(symbol: str, day: int) -> Dict[str, Any]:
    return {
        "t": f"2020-01-{day:02d}T14:30:00+00:00",
        "o": 100.0 + day * 0.1,
        "h": 101.0 + day * 0.1,
        "l": 99.5 + day * 0.1,
        "c": 100.5 + day * 0.1,
        "v": 1_000_000,
    }


def test_five_symbols_pagination_all_symbols_ok(tmp_path: Path):
    """The scenario from the operator's bug report.

    Five symbols × 60 daily bars = 300 rows total.  The fake
    server returns 30 rows per page, so page 1 fills with AAPL
    only, page 2 finishes AAPL + starts MSFT, and so on across
    ten pages.  Every symbol must appear in ``symbols_ok`` at the
    end — the bug was that only AAPL did.
    """
    per_symbol = {
        sym: [_make_bar(sym, d) for d in range(1, 61)]  # 60 daily bars
        for sym in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ")
    }
    http_get = _PaginatedAlpacaFake(per_symbol, rows_per_page=30)
    client = ResearchAccountClient(
        config=ResearchAccountConfig(),
        env=TEST_ENV,
        http_get=http_get,
    )
    provider = AlpacaProvider(client=client)

    layout = WarehouseLayout(root=tmp_path / "wh")
    with PendingDownloadsQueue(layout.root / "queue" / "regression.db") as q:
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id="regression-multi-symbol",
            symbols=("AAPL", "MSFT", "NVDA", "SPY", "QQQ"),
            start="2020-01-01",
            end="2020-01-31",
            asset_class=AssetClass.EQUITY,
            interval=BarInterval.DAILY,
            adjustment=AdjustmentMode.RAW,
            queue=q,
        )

    # Every symbol got data
    assert set(report.symbols_ok) == {"AAPL", "MSFT", "NVDA", "SPY", "QQQ"}, (
        f"expected all five symbols in symbols_ok; got {report.symbols_ok!r}"
    )
    assert report.symbols_empty == (), (
        f"expected no empty symbols; got {report.symbols_empty!r}"
    )
    # Total bars = 5 × 60 (may double if provider chunk revisits data,
    # but with default chunk_size=50 the five symbols are one chunk)
    assert report.total_bars == 300, (
        f"expected 300 bars; got {report.total_bars}"
    )
    # Every Parquet file must have >0 rows
    assert all(f.row_count > 0 for f in report.files)
    # Pagination happened — 300 rows / 30 rows-per-page = 10 pages.
    assert len(http_get.requests) == 10


def test_pagination_does_not_leak_credentials(tmp_path: Path):
    """Every request in the paginated loop must carry credentials
    in redacted form when repr'd — the fix must not accidentally
    reintroduce a plain-text path.
    """
    per_symbol = {"AAPL": [_make_bar("AAPL", d) for d in range(1, 91)]}
    http_get = _PaginatedAlpacaFake(per_symbol, rows_per_page=30)
    client = ResearchAccountClient(
        config=ResearchAccountConfig(),
        env=TEST_ENV,
        http_get=http_get,
    )
    provider = AlpacaProvider(client=client)
    layout = WarehouseLayout(root=tmp_path / "wh")
    import_bars(
        layout=layout,
        provider=provider,
        dataset_id="regression-credentials",
        symbols=("AAPL",),
        start="2020-01-01",
        end="2020-01-31",
        asset_class=AssetClass.EQUITY,
        interval=BarInterval.DAILY,
        adjustment=AdjustmentMode.RAW,
    )
    # Multiple pages issued (90 rows / 30 per page = 3 pages)
    assert len(http_get.requests) == 3
    for req in http_get.requests:
        text = repr(req)
        assert "test-api-key" not in text
        assert "test-secret-key" not in text
        assert "***REDACTED***" in text
