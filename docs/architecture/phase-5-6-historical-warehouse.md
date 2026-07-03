# Phase 5.6 — Historical Data Warehouse

**Status:** PLANNED (design only — no production code, no live-path changes)

**Sits between:** Phase 5.5 (Research & Learning Dashboard) and Phase 6 (Production Readiness)

**Card:** `t_phase56_historical_warehouse` (parent planning card, in Review)

---

## Motivation

The first live 60-day validation surfaced a concrete data-shape limitation. The Research Analyst narrative for commit `f46a548` observed:

> "both strategies are consistently rejected due to a hard sample-size floor. The system requires a minimum of 51 historical observations (`need>=51`), but the challenger only accumulates between 21 and 42 samples across the evaluation window."

Sixty calendar days of Alpaca daily bars yields ~42 trading-day bars, but SMA(50) needs 51+. Widening the fetch window solves one instance but not the shape of the problem: **every research question that requires more history than one Alpaca call returns forces a redesign** — of the driver, of the credential path, of the cache assumptions, of the reproducibility guarantees. The pipeline is a slave to whatever the provider chose to return today.

Two adjacent risks compound this:

1. **Provider lock-in.** `strategy/research_account.py` reaches straight into Alpaca. If Alpaca rate-limits, deprecates the endpoint, changes the adjustment policy, or bumps their subscription price, every downstream artifact silently changes with it. Research reproducibility becomes tied to a vendor's release cadence.
2. **Deterministic replay is only skin-deep.** Today's runs are reproducible only for the window Alpaca happens to serve on the day of the run. A rerun tomorrow may return subtly-different bars (post-market prints, adjustment revisions after a corporate action). Nothing in the pipeline notices.

Phase 5.6 fixes the shape of the problem, not the immediate lookback gap. The goal is a warehouse that treats historical data as an immutable, versioned, locally-owned research asset — with providers acting as acquisition tools, not runtime dependencies.

---

## Objectives

1. **Provider independence.** No research module names a specific provider. A `MarketDataProvider` interface abstracts every fetch. Adding a new provider means writing a plugin; every other layer stays unchanged.
2. **Local-first read path.** The replay engine always consults the warehouse first. External providers are contacted only when the warehouse has a gap AND policy permits.
3. **Immutable versioned datasets.** Once validated, a dataset is frozen. Corporate-action revisions produce new versions with lineage; existing versions are never overwritten.
4. **Deterministic replay independent of provider uptime.** Pulling the network cable never breaks a rerun.
5. **Scale headroom.** The design absorbs 20+ years × thousands of symbols × minute bars without a redesign or a lift-and-shift.

---

## Design principles / constraints (mandatory)

Every implementation card under Phase 5.6 must satisfy these before landing:

- **Research-only.** No warehouse module may be imported by `trader.py`, `crypto_trader.py`, `strategy/runner.py`, `trader_cli.py`, `telegram_approvals.py`, or any scheduler.
- **No live-path changes.** `trader.py` bytes unchanged; equivalence tests on the live runner's public surface pass byte-identically before and after.
- **No new order path.** No warehouse module places, submits, cancels, or replaces any order. Read-only source-level tests (mirroring `test_research_account.py::TestSourceSafety`) enforce this.
- **No feature-flag flips.** `FeatureFlags.all_disabled == True` before and after every warehouse test.
- **No `ApprovalRecord` construction.** No `PromotionEntry` state advances.
- **Credential isolation preserved.** Warehouse code reads from a dedicated `WAREHOUSE_*` env namespace, disjoint from `ALPACA_*`, `APCA_*`, `RESEARCH_ALPACA_*`, and `CRYPTO_ALPACA_*`.
- **Terminology.** "Validation", "replay", "research", "acquisition" — never "training".

---

## Architecture

### 1. `MarketDataProvider` interface

A `typing.Protocol` in a new module (`strategy/market_data_provider.py`, to be created by `t_phase56_provider_interface`). Every provider plugin implements the same shape; the replay engine talks only to this shape.

```python
class MarketDataProvider(Protocol):
    name: str  # e.g. "alpaca", "polygon", "csv", "parquet"

    def provider_capabilities(self) -> ProviderCapabilities: ...
    def fetch_daily_bars(
        self, symbols: Sequence[str], start: str, end: str,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[BarBatch]: ...
    def fetch_intraday_bars(
        self, symbols: Sequence[str], start: str, end: str,
        interval: BarInterval,
        adjustment: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResponse[BarBatch]: ...
    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: str, end: str,
    ) -> ProviderResponse[CorporateActionBatch]: ...
    def fetch_symbol_metadata(
        self, symbols: Sequence[str],
    ) -> ProviderResponse[SymbolMetadataBatch]: ...
    def fetch_calendar(
        self, start: str, end: str, exchange: str = "XNYS",
    ) -> ProviderResponse[CalendarBatch]: ...
```

Supporting types (all frozen dataclasses, deterministic, JSON-serializable):

- `ProviderCapabilities` — supported intervals, adjustment modes, corporate-action coverage, latest-data lag, rate-limit hints, cost model per fetch.
- `ProviderResponse[T]` — carries the batch, per-symbol status codes, provider trace ID, request URL (redacted), fetch cost estimate, warnings.
- `BarInterval` — enum: `DAILY`, `HOURLY`, `MINUTE_1`, `MINUTE_5`, `SECOND_1`, etc.
- `AdjustmentMode` — enum: `RAW`, `SPLIT`, `SPLIT_DIVIDEND`, `TOTAL_RETURN`.
- `Bar` — `(t, o, h, l, c, v, vw?, trade_count?, adjustment_version)`.
- `CorporateAction` — split ratio, cash amount, ex-date, pay-date, kind (`split`, `cash_dividend`, `special_dividend`, `ticker_change`, `delisting`, `merger`).

Supported providers (plugin roster, to be implemented under `t_phase56_provider_plugins`):

- Alpaca (equities + crypto)
- Polygon
- Databento
- Tiingo
- Financial Modeling Prep
- Alpha Vantage
- CSV importer (local files)
- Parquet importer (local files)
- Manual import (operator-supplied dataset with attestation)
- Future providers (interface is closed under addition)

Every plugin lives in `strategy/providers/<name>.py`. The registry lives in `strategy/providers/__init__.py`. Plugins are discovered by explicit import (no side-effect autoloading — reproducibility beats convenience).

**Boundary condition:** The replay engine never receives a provider handle. It receives a `WarehouseReader` (see §6), which internally consults providers only as a fallback.

### 2. Local Historical Warehouse

Permanent, on-disk repository. Layout:

```
market_data/
    equities/
        daily/
            <symbol>/<year>.parquet          # partitioned by symbol, year
        hourly/
            <symbol>/<year>-<month>.parquet
        minute/
            <symbol>/<year>-<month>-<day>.parquet
    crypto/
        <same shape, distinct partitioning>
    options/                                  # reserved for Phase 6+
    metadata/
        symbols.db          # SQLite: symbol → sector, exchange, listing dates, aliases
        calendars.db        # SQLite: exchange → trading calendar rows
        exchanges.db        # SQLite: exchange metadata, holiday overrides
        dividends.db        # SQLite: symbol → (ex-date, amount, currency, kind)
        splits.db           # SQLite: symbol → (ex-date, ratio, before, after)
    manifests/
        <dataset_id>.json   # immutable per-dataset manifests
    versions/
        <dataset_id>/
            v1/
            v2/             # created when a corporate-action revision arrives
```

**Storage format policy (see §Storage Architecture Recommendation below):**

- **Bars** live in **Parquet**. Columnar, per-column compression (Zstd), partitioned by symbol × time bucket. Immutable once written. Rewrites happen only on corporate-action revision, and the rewrite creates a new version, not an overwrite.
- **Metadata** (symbols, calendars, dividends, splits, exchanges) lives in **SQLite**. Small, transactional, familiar, ACID.
- **Query surface** is **DuckDB**, which reads the Parquet tree directly with SQL and joins metadata from the SQLite files via `ATTACH`. No ETL step needed.
- **Manifests** stay JSON, per the existing Phase 3 `DataCatalog` convention (`strategy/data_catalog.py:1`).

**Immutability rules:**

- Every Parquet file is content-addressed (sha256 of the file contents recorded in the manifest).
- After a file's manifest is marked `validated`, the file cannot be overwritten. Attempted overwrites raise `WarehouseIntegrityError`.
- A corporate-action revision that changes historical values creates a *new versioned dataset* under `versions/<dataset_id>/vN/` and the catalog records the lineage.

### 3. Data Catalog

Extends the existing `strategy/data_catalog.py`. Every warehouse dataset gets a manifest with fields:

```json
{
  "dataset_id": "equities-daily-AAPL-2015-2026-alpaca-v1",
  "provider": "alpaca",
  "provider_request_id": "trace-abc123",
  "download_started_at": "2026-07-15T04:00:00Z",
  "download_completed_at": "2026-07-15T04:00:34Z",
  "coverage": {
    "start": "2015-01-02",
    "end": "2026-07-14",
    "trading_days_expected": 2892,
    "trading_days_observed": 2892
  },
  "symbols": ["AAPL"],
  "interval": "DAILY",
  "adjustment_mode": "SPLIT_DIVIDEND",
  "adjustment_version": "alpaca:2026-07-15",
  "corporate_action_version": "3",
  "timezone": "America/New_York",
  "kind": "historical_bars",
  "files": [
    {
      "path": "equities/daily/AAPL/2015.parquet",
      "sha256": "…",
      "size_bytes": 12345,
      "row_count": 252
    }
  ],
  "checksum": "sha256:aggregate-of-file-checksums",
  "validation_status": "validated",
  "validation_report_id": "vr_abc123",
  "provider_capabilities_snapshot": {…},
  "notes": ""
}
```

Backward-compatible extension of the existing `DataCatalog`. The Phase 5 `historical_bars` kind stays valid — Phase 5.6 datasets carry the same kind with a superset of fields.

The catalog gains query methods:

- `find_coverage(symbol, interval, start, end) -> Iterable[Dataset]`
- `gaps(symbol, interval, start, end) -> Iterable[Gap]`
- `latest_validated(symbol, interval) -> Optional[Dataset]`
- `versions_of(dataset_id) -> List[DatasetVersion]`

### 4. Gap Detection

Automated integrity + coverage scanner, runnable on-demand and nightly. Detects:

- **Missing trading day** — expected per exchange calendar, absent in bars.
- **Partial day** — session boundary bar missing (open bar without close bar or vice versa in intraday).
- **Missing symbol** — symbol declared in a watchlist but no dataset covers the requested window.
- **Bad checksum** — file sha256 doesn't match manifest.
- **Corporate action mismatch** — split recorded in `splits.db` not reflected in bar values (raw vs adjusted).
- **DST anomalies** — timestamps that don't align with the exchange's DST rules for the date.
- **Holiday mismatch** — bars present on an exchange holiday, or missing on a half-day session.
- **Duplicate bars** — two bars for the same (symbol, timestamp, interval).
- **Impossible OHLC** — `l > o | c`, `h < o | c`, `v < 0`, `l > h`, `o == 0` on non-void days.

Every scan produces a `GapReport` — a research artifact stored under `reports/warehouse/gaps/<run_id>/`. Gap reports become promotion evidence and are cited by the Research Analyst.

### 5. Incremental Sync

Nightly `warehouse-sync` runner (cron-driven — but the cron infra itself is out of scope for Phase 5.6). Workflow per (symbol, interval):

1. Look up the latest validated bar's timestamp in the warehouse.
2. Ask the highest-priority available provider for bars *strictly newer* than that timestamp.
3. Run integrity validation on the new bars.
4. Write to Parquet.
5. Compute file sha256 + row count.
6. Append a new manifest OR update the existing manifest's coverage window (never rewrite historical bars).
7. Log the fetch in a per-provider ledger with cost estimate + rate-limit consumption.

**Sync is never destructive.** It never redownloads existing history. `--force` exists for operator-initiated repulls but always writes to a new dataset version.

Corporate-action-driven revisions (a split announced late) are handled by a *separate* `warehouse-revise` runner that:

- Fetches the revised bars into a new version directory (`versions/<dataset_id>/vN+1/`).
- Emits a diff report showing which bars changed.
- The catalog records the lineage.
- Existing research runs pinned to `vN` continue to be reproducible.

### 6. Provider Priority

Configuration-driven per-context. Example:

```yaml
# .env.warehouse (new namespace)
WAREHOUSE_PRIORITY_EQUITIES_DAILY: [warehouse, alpaca, polygon, csv, manual]
WAREHOUSE_PRIORITY_EQUITIES_MINUTE: [warehouse, polygon, databento, manual]
WAREHOUSE_PRIORITY_CRYPTO_DAILY: [warehouse, alpaca, tiingo]
```

Resolution rule: the `WarehouseReader` walks the priority list; the first entry that satisfies the request (has coverage AND passes validation) wins. `warehouse` is always first — the local copy is authoritative once validated.

Fallback is opt-in per context. Research runs default to `[warehouse]` only. Sync runs use the full priority chain.

**Every read path returns a normalized data model** — a `BarBatch` with `AdjustmentMode`, `provider`, `dataset_id`, and `dataset_version` attached. The replay engine cannot tell which provider produced the underlying bars.

### 7. Import Pipeline

Bulk-import workflows for operator-driven onboarding:

- **`import-provider`** — pull a 10-year window for a symbol universe from a provider. Chunks the pull to respect rate limits. Resumable via a `pending_downloads` SQLite queue.
- **`import-zip`** — extract a vendor-supplied ZIP archive (Polygon flat-file exports, Databento bundles) into the warehouse, running validation as it goes.
- **`import-parquet`** — accept externally-generated Parquet files, verify schema compatibility, register in the catalog.
- **`import-csv`** — normalize CSV imports into the canonical bar schema. Common for legacy datasets.
- **`rebuild-manifests`** — scan the warehouse tree and regenerate manifests from file contents. Safe to run anytime; validates checksums.
- **`resume`** — every pipeline is checkpointed. An interrupted 10-year backfill resumes from the last successful (symbol, chunk) pair.

Each pipeline writes a `WarehouseImportReport` — a research artifact.

### 8. Research Cache

Once a dataset is marked `validated`, it is immutable and cached forever. Research runs pin their fixture references to specific `(dataset_id, dataset_version)` pairs. A rerun months later produces byte-identical bars regardless of what the provider is serving today.

**Cache eviction:** none by default. The warehouse is the source of truth; disk is cheap; provenance is expensive. Operators may prune old versions via `warehouse-prune`, but the CLI refuses to prune a dataset that has an outstanding `PromotionEntry` referencing it.

### 9. Corporate Actions

Explicit handling for:

- **Forward splits** and **reverse splits** — ratio, before/after CUSIP.
- **Cash dividends** and **special dividends** — ex-date, pay-date, amount, currency.
- **Ticker changes** — old symbol → new symbol, effective date, retained history mapping.
- **Delistings** — reason (M&A, bankruptcy, exchange transfer), last trading date.
- **Merged companies** — surviving entity, ratio, effective date.

Every bar dataset stores **both raw and adjusted variants**. Downstream research code chooses the mode via the `AdjustmentMode` enum. The adjustment version string on the manifest ties the adjusted values to a specific corporate-action revision — so an old research run pinned to `alpaca:2025-12-01` sees the adjusted bars *as they were computed on that date*, even if Alpaca later revised the adjustment.

### 10. Validation

Every dataset transitions through states: `unvalidated → validating → validated` (or `→ quarantined`). The validator runs:

- Manifest schema check.
- File-level sha256 verification.
- Row-count vs manifest declaration.
- Schema compatibility (canonical column order + dtypes).
- OHLCV sanity (`l ≤ o,c ≤ h`, `v ≥ 0`, no NaN in required fields).
- Timezone consistency (every timestamp interpretable in the declared TZ).
- Trading-calendar alignment (per exchange).
- Cross-check with `splits.db` / `dividends.db` for the covered symbols.
- Duplicate detection.

A quarantined dataset is retained but excluded from reads. The `WarehouseReader` refuses to serve from quarantined datasets and falls back through the priority chain.

### 11. Future Scale

The design accommodates without redesign:

- **20+ years of history** — Parquet + Zstd keeps daily equities under ~10 MB per symbol per decade. A 3000-symbol universe × 20 years ≈ 60 GB. DuckDB queries this fluidly on a laptop.
- **Minute bars** — same partition strategy at `<year>-<month>` granularity. 3000 symbols × 20 years × ~98,000 minute bars/year ≈ few TB of Zstd Parquet; still tractable on a modern NAS.
- **Second bars** — separate partitioning (`<year>-<month>-<day>`); operators opt in per-symbol.
- **Multiple providers, multiple exchanges** — orthogonal to the interval partition. Datasets tagged with `provider` and `exchange`; catalog joins.
- **Options** and **futures** — reserved directory paths. Contract identifiers (OSI, root+expiry+strike) added to the metadata schema when needed. Interval partitioning unchanged.

The bottleneck is not the storage layer — it's the analyst's context window. Phase 5.6 sizes for hardware, not for token budgets.

### 12. Integration

How Phase 5.6 wires into existing modules:

- **`ResearchAccountClient`** (Phase 5) — becomes one *provider plugin* among many. Existing `RESEARCH_ALPACA_*` env vars stay isolated. Existing tests keep passing. When the warehouse is populated for the requested window, `ResearchAccountClient` is not called.
- **`HistoricalValidation`** — `_fetch_live_events` gains a `WarehouseReader` alongside the current `research_client`. Live-fetch pathway consults the warehouse first; falls through to the client per policy. The fixture-persistence step continues to write a fixture snapshot, but now the fixture references `(dataset_id, dataset_version)` in the warehouse rather than embedding events inline.
- **`WalkForward`** — no change. Consumes events from the harness; doesn't know where they came from.
- **`Learning Pipeline`** — gains explanation-summary rollups per (provider, dataset_version). Learning findings can now compare regime effects across warehouse versions.
- **`Research Analyst`** — payload gains a `dataset_provenance` section citing the exact warehouse datasets and versions the run consumed. Analyst narratives can cite this in the "provenance" line they already emit.
- **`Dashboard`** (Phase 5.5) — gains a new warehouse view: dataset coverage grid, gap reports, provider comparison, version lineage. Phase 5.5 tables just add rows.
- **`Promotion evidence`** — a `PromotionEntry`'s evidence dict gains a `dataset_provenance_id` pointing at the specific warehouse snapshot the promotion was evaluated against. Reproducing the promotion is now a matter of pinning the version.

### 13. Operational goal

The intended day-to-day workflow becomes:

1. **Acquire once.** `warehouse-import` pulls a decade of history from Alpaca into the warehouse, resumable.
2. **Validate once.** The validator marks each dataset `validated`.
3. **Store forever.** Immutable Parquet under `market_data/`. The catalog remembers.
4. **Research forever.** Every subsequent research run reads from the warehouse. Providers become acquisition tools only.
5. **Nightly delta.** `warehouse-sync` picks up yesterday's close, appends, marks the new range validated.
6. **Providers may vanish.** Alpaca could shut down tomorrow. Every past research run remains reproducible; every future research run against known-good history remains executable.

Research becomes independent of provider uptime, provider pricing, provider API stability, and provider adjustment revisions.

---

## Storage Architecture Discussion

The card asks for a specific recommendation. Here is the comparison and the call.

### Option A: SQLite alone

Pros:

- Ships with Python. Zero dependency footprint.
- ACID guarantees are load-bearing for the metadata layer regardless.
- Simple backup: copy the `.db` file.
- Familiar operational shape.

Cons:

- **Not columnar.** Analytical scans across 20 years × 3000 symbols × 250 bars/year are row-oriented and slow.
- **Compression is coarse.** Whole-DB `VACUUM` or file-system-level compression; no per-column encoding.
- **Concurrency ceiling.** Single writer. Multi-reader is fine but the writer becomes a serialization point during sync.
- **Analytical query surface is weak.** Window functions and time-series joins work but pay a big row-scan tax.
- **Poor at scale.** A 60 GB SQLite file with billions of rows is technically possible but operationally miserable — VACUUMs stall, `.dump` is slow, backups are painful.

Verdict: correct for **metadata** (symbols, calendars, splits, dividends). Wrong for **bars** at Phase 5.6 target scale.

### Option B: DuckDB alone

Pros:

- Columnar, vectorized, analytical-first.
- Excellent SQL surface — window functions, ASOF joins, time-series aggregations natively fast.
- Single-file DB or query external Parquet directly.
- ACID, zero-server.
- Python-native via `duckdb-python`.
- Similar operational shape to SQLite.

Cons:

- **Single-writer limitation** — like SQLite. Fine for our workload (sync runs at night, one process).
- **Backup discipline** — need `CHECKPOINT` before file copy; not as trivial as SQLite.
- **Ecosystem younger** than SQLite, though maturing fast.
- **File format is proprietary** — a `.duckdb` file is portable across DuckDB versions but not to other engines.

Verdict: excellent query engine. Not quite the right *storage* medium for immutable bar archives — because the bars are naturally external Parquet files that any future engine can read.

### Option C: Parquet alone (with DuckDB as query engine)

Pros:

- **Columnar, compressed** — Zstd typically gets 5-10× compression on OHLCV.
- **Widely supported** — Arrow, Pandas, Polars, DuckDB, Spark, Presto, ClickHouse. Portable to any future analytical engine.
- **Perfect fit for immutable historical archives** — the format is designed for write-once, read-many.
- **Naturally partitioned** by directory structure (`symbol/year.parquet`).
- **Version-friendly** — a new dataset version is a new directory, no schema migration.

Cons:

- **Not transactional** — writing 500 files in a batch has no atomic guarantee. Solution: write to `tmp/`, atomic rename per file, transactional manifest update in SQLite.
- **Slow for point queries** — retrieving one bar for one symbol at one timestamp involves reading (part of) a Parquet file. Fine for our workload (research reads scan ranges, not single bars). For symbol metadata lookups, we use SQLite.
- **Not great for updates** — corporate-action revisions rewrite an affected Parquet file. Since we version datasets, the rewrite becomes a new file in a new version directory. This is a feature.

Verdict: correct medium for **bar archives**. Combined with DuckDB as the query engine, we get the best of both worlds.

### Option D: Hybrid (recommended)

**Bars** → Parquet, partitioned by symbol × time bucket, Zstd compression, immutable.
**Metadata** → SQLite (symbols, calendars, splits, dividends, exchanges).
**Query engine** → DuckDB, reads Parquet directly, `ATTACH`es SQLite for metadata joins.
**Manifests** → JSON on disk, keyed by `dataset_id`, per existing Phase 3 convention.
**Import queue / sync ledger** → SQLite (transactional; small; concurrent-safe with one writer).

### Comparison table

| Concern | SQLite alone | DuckDB alone | Parquet alone | Hybrid (rec.) |
|---|---|---|---|---|
| Compression on 20 yrs × 3k symbols daily | ~200 GB | ~40 GB | ~30 GB | ~30 GB |
| Analytical query speed (walk-forward scan) | slow | fast | fast (via DuckDB) | fast |
| Point-query speed (single bar lookup) | fast | fast | slow | fast (via SQLite metadata + Parquet scan) |
| Portability across engines | SQLite-only | DuckDB-only | universal | universal (bars) + SQLite-familiar (meta) |
| Backup ergonomics | trivial | needs `CHECKPOINT` | trivial (files) | trivial |
| Concurrency (multi-reader, single writer) | supported | supported | supported | supported |
| Python ecosystem fit | strong | strong | strong | strong |
| Dashboard integration (Phase 5.5) | works | works | works via Arrow/DuckDB | works, best variety |
| Future 20+ yr minute-level scale | painful | good | excellent | excellent |
| Corporate-action revisioning | file rewrite | table rewrite | new version dir | new version dir |
| Cost to onboard operators | familiar | new but similar | libraries needed | mixed familiar |

### Recommendation

**Adopt DuckDB + Parquet + SQLite (hybrid).**

Rationale, in one paragraph: the market-data warehouse's dominant workload is *columnar range scans over immutable append-only history* — Parquet was designed for exactly this. DuckDB gives us a modern analytical query surface with zero server ops, and it reads Parquet natively so there is no ETL step. SQLite handles the metadata that Parquet handles poorly (transactional updates, small tables, point lookups) and is already in the repository's operational vocabulary (Kanban DB, trades DB). This split lets each layer do what it's best at, avoids lock-in to any single format (Parquet is universal), and scales to Phase 5.6's stated targets on commodity hardware without a re-architecture. The alternative — SQLite alone — would work for a year and then require a painful migration once minute bars enter the mix. Adopting DuckDB now, before we have a decade of data to move, is the low-cost decision.

---

## Kanban breakdown

Twelve implementation cards, each independently testable, reversible, and read-only. Dependencies noted; unblocked cards can proceed in parallel.

| Card | Depends on | Deliverable |
|---|---|---|
| `t_phase56_provider_interface` | — | `strategy/market_data_provider.py` with Protocol, dataclasses, enum, and tests. No plugin implementations. |
| `t_phase56_local_warehouse` | provider_interface | Directory layout under `market_data/`, immutability enforcement, `WarehouseIntegrityError`, unit tests on a scratch tree. |
| `t_phase56_catalog` | local_warehouse | Extension of `strategy/data_catalog.py` with warehouse-aware fields and query methods. Backward-compat tests for existing Phase 5 manifests. |
| `t_phase56_parquet_storage` | local_warehouse | Bar-writer + bar-reader in the canonical schema. Zstd config. Roundtrip and schema tests. |
| `t_phase56_duckdb_queries` | parquet_storage, catalog | DuckDB query layer joining Parquet + SQLite metadata. `WarehouseReader` shell (no provider fallback yet). |
| `t_phase56_data_versioning` | catalog | Version chain (`v1`, `v2`, …) with lineage. Corporate-action revision test. |
| `t_phase56_validation` | parquet_storage, catalog | Integrity validator + `GapReport` producer + `validation_status` transitions. |
| `t_phase56_gap_detection` | validation, catalog | Scanner + `GapReport` artifact writer. Cross-checks against `splits.db` / `dividends.db`. |
| `t_phase56_provider_plugins` | provider_interface | Alpaca plugin (wraps `ResearchAccountClient`), CSV plugin, Parquet plugin. Others as follow-up cards. |
| `t_phase56_import_pipeline` | provider_plugins, parquet_storage, catalog | `import-provider`, `import-zip`, `import-csv`, `rebuild-manifests` runners. Resumable via SQLite queue. |
| `t_phase56_incremental_sync` | import_pipeline, catalog | `warehouse-sync` runner, per-provider ledger, dry-run mode. |
| `t_phase56_research_cache` | data_versioning, catalog | `WarehouseReader` with provider-priority fallback, pin-by-version support, cache immutability enforcement. |

Each card's Definition of Done includes:

- Suite passes green.
- `git diff --check` clean.
- `trader.py`, `crypto_trader.py`, `strategy/runner.py` bytes unchanged (byte-comparison test).
- `FeatureFlags.all_disabled == True` after every test.
- No `ApprovalRecord` construction.
- No `PromotionEntry` state change.
- No cross-namespace credential read (`WAREHOUSE_*` isolated from `ALPACA_*`, `APCA_*`, `RESEARCH_ALPACA_*`, `CRYPTO_ALPACA_*`).

## Dependencies

External:

- `duckdb-python` — new dependency, single wheel, no C compiler required.
- `pyarrow` — Parquet I/O; already Pandas-adjacent; adds ~30 MB.

Internal:

- Existing `strategy/data_catalog.py` — extended, not replaced.
- Existing `strategy/research_account.py` — becomes one provider plugin, unchanged in behavior.
- Existing `strategy/backtest_lab.py::stable_hash` — used by manifest hashes.

The two new external deps are added by `t_phase56_provider_interface` (first card in the chain), locked to specific versions, and pinned to the research context only. Neither is imported by any live-path module (source-safety tests enforce this).

## Validation criteria for Phase 5.6 as a whole

The phase is complete when:

1. All twelve implementation cards are in Review or Done.
2. The warehouse is populated with at least one provider's full daily history for the current watchlist + benchmarks (SPY, QQQ).
3. `HistoricalValidation` live-fetch consumes the warehouse first; a follow-up unit test asserts `ResearchAccountClient.fetch_bars` is not called when warehouse coverage is complete.
4. Gap detection produces a zero-unexplained-gap report for the populated coverage.
5. **Offline test.** With the network cable metaphorically pulled (`RESEARCH_ALPACA_*` env vars unset, provider plugins disabled), a full 60-day validation completes successfully and produces byte-identical artifacts to the online version.
6. Analyst narrative includes a `dataset_provenance` block citing the warehouse dataset IDs and versions.
7. Every read-only guarantee holds: `FeatureFlags.all_disabled == True`, no `ApprovalRecord`, `PromotionEntry` stays `disabled`, `trader.py` byte-identical to Phase 5.5 exit.

## Explicitly out of scope for Phase 5.6

- Options data (Phase 6+).
- Futures data (Phase 6+).
- Real-time streaming / websocket adapters (Phase 6+).
- Cross-provider comparison studies (a valuable follow-up card, but Phase 5.6 is about *acquisition and storage*, not comparison).
- Automated cost optimization across providers (manual for now).
- Dashboard UI additions — Phase 5.5's territory. Phase 5.6 provides the data; Phase 5.5 renders it.
- Populating `.env.production` — Phase 6.

## Terminology

Consistent with prior phases: "validation", "replay", "research", "acquisition". Never "training". A follow-up terminology-audit test (mirroring `test_research_account.py::TestSourceSafety::test_terminology_avoids_training`) will apply to every warehouse module.

## Read-only safety review

Before Phase 5.6 begins, the following invariants hold and must continue to hold after every implementation card lands:

- No warehouse module imports `trader`, `crypto_trader`, `trader_cli`, `telegram_approvals`, or `strategy.runner`.
- No warehouse module places, submits, cancels, or replaces any order — verified by source-level substring tests.
- No warehouse module reads or writes any file inside `.env`, `.env.paper`, `.env.crypto`, or `.env.production`.
- `WAREHOUSE_*` env namespace is disjoint from `ALPACA_*`, `APCA_*`, `RESEARCH_ALPACA_*`, `CRYPTO_ALPACA_*` — verified by explicit test.
- `FeatureFlags.all_disabled == True` before and after every warehouse test.
- No `ApprovalRecord` construction anywhere under `strategy/providers/`, `strategy/warehouse/`, or `strategy/market_data_provider.py`.
- `PromotionEntry.current_state` never advances as a side effect of a warehouse operation.

These are the same guardrails Phase 5 shipped under. Phase 5.6 inherits them wholesale.

---

## Follow-up cards this design generates

Not part of Phase 5.6 itself, but flagged for later planning:

- **Warehouse dashboard view** (Phase 5.5 extension) — dataset coverage grid, gap heatmap, version lineage tree.
- **Cross-provider validation** — pull the same window from two providers, run integrity diff. Yields provider-quality findings for the learning report.
- **Warehouse-driven promotion evidence** — extend `PromotionEntry.evidence` with a `dataset_provenance_id` field.
- **Options data warehouse** (Phase 6+).
- **Realtime tap** for research-quality intraday recording (Phase 6+).

Each becomes its own Kanban card at the appropriate phase.

---

**Card summary:** Phase 5.6 is planning-only. This document, the roadmap section, and the twelve Kanban cards are the deliverables. No production code lands under this parent card. Implementation begins only when child cards are claimed.
