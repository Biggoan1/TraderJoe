# CHANGELOG

Trader Joe release history.

---

## Unreleased — Phase 5.6: Gap detection reports

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_gap_detection` (`t_b06ec41d`)

### Added
- `strategy/warehouse/gap_detection.py` — coverage-gap
  scanner + report producer.
- `detect_gaps(layout, dataset_id, window_start, window_end,
  calendar=None, exclude_weekends=True, expected_symbols=None,
  minimum_bars_per_day=1)` — returns a deterministic
  `GapReport`.  Kinds detected: `missing_trading_day`,
  `missing_symbol`, `no_coverage`, `partial_day` (sub-daily
  intervals), `weekend_bars_present`, `holiday_bars_present`
  (calendar-aware).
- `Gap`, `GapReport` — frozen dataclasses for the result
  surface.  Gaps sorted by `(symbol, date, kind)` ascending.
- `write_report(report, output_dir)` — persists JSON with a
  unique per-pass filename.
- `GapDetectionError` — subclass of `WarehouseIntegrityError`.

### Read-only guarantees (enforced by tests)
- No live-runner imports; no order-path tokens; no credential
  env-var reads; no `ApprovalRecord` / `PromotionEntry`
  construction.  `FeatureFlags.all_disabled == True` after
  every scan.

### Testing
- +18 tests in `tests/test_warehouse_gap_detection.py`.  Full
  suite: **1779 passing** (was 1761; +18 net new).
- Coverage: fully-covered dataset (zero gaps), missing weekdays
  flagged, weekends excluded by default, weekend-bars-present
  detection, holiday-aware detection via CalendarDay input,
  no_coverage for symbols with no bars, expected_symbols
  override, partial_day for sub-daily intervals, empty /
  reversed bounds rejected, missing dataset raises,
  deterministic ordering, serialization, write_report creates
  output dir, source safety, feature-flag invariance.

---

## Unreleased — Phase 5.6: Warehouse integrity validation

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_validation` (`t_ababb2f7`)
**Commit:** `7612084`

### Added
- `strategy/warehouse/validation.py` — integrity validator +
  report producer + lifecycle transition helper.  Validation
  checks: manifest existence + schema; per-file existence,
  sha256, size, row count; Parquet schema matches
  `CANONICAL_SCHEMA`; OHLCV invariants (l ≤ o,c ≤ h);
  volume ≥ 0; no NaN; timestamp ISO 8601 prefix; no
  duplicate (symbol, timestamp) across files.
- `validate_dataset(layout, dataset_id)` — pure function
  returning a `ValidationReport`.  Never mutates the manifest.
- `apply_validation_report(layout, report)` — lifecycle
  transition: `ok=True → validated`, `ok=False → quarantined`.
  Explicit — the caller decides whether to apply.
- `write_report(report, output_dir)` — persists the report as
  JSON under a filesystem path, with a unique filename per pass
  so repeated runs on the same dataset don't overwrite each
  other.
- `ValidationFinding`, `ValidationReport`, `ValidationError`,
  severity constants (`SEVERITY_ERROR` / `SEVERITY_WARNING`).

### Read-only guarantees (enforced by tests)
- No live-runner imports; no order-path tokens; no credential
  env-var reads; no `ApprovalRecord` / `PromotionEntry`
  construction.  `FeatureFlags.all_disabled == True` after
  every validation call.  `validate_dataset` never mutates
  the manifest — mutation happens only through
  `apply_validation_report`, and only with `force=True` since
  the caller has explicitly chosen the transition.

### Testing
- +18 tests in `tests/test_warehouse_validation.py`.  Full
  suite: **1761 passing** (was 1743; +18 net new).
- Coverage: clean dataset validates ok; missing file /
  checksum / size / row_count mismatch; Parquet schema
  mismatch; negative volume (or unreadable_parquet from the
  writer's own guard); duplicate bar across files; missing
  manifest raises; apply_validation_report promotes / quarantines
  per report.ok; write_report drops JSON and creates output
  dir; source safety; feature-flag invariance.

---

## Unreleased — Phase 5.6: Dataset versioning + lineage

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_data_versioning` (`t_c32b8416`)
**Commit:** `c398a92`

### Added
- `strategy/warehouse/versioning.py` — lineage traversal +
  version-diff reports over the `parent_dataset_id` /
  `corporate_action_version` / `adjustment_version` fields the
  catalog already carries.
- `VersionNode`, `BarDiff`, `VersionDiff` — frozen dataclasses
  for the result surface.
- `version_chain(catalog, dataset_id)` — deterministic ordering
  (root first, then numeric ascending on
  `corporate_action_version`).
- `latest_version(catalog, dataset_id)` — most recent version
  in the chain.
- `parent_manifest(catalog, dataset_id)` — parent manifest or
  `None` for roots.
- `derive_next_version(parent_manifest, revision_source="",
  adjustment_version="", corporate_action_version="")` — pure
  manifest builder: increments the version, points
  `parent_dataset_id` at the parent, resets
  `validation_status` to `unvalidated`, clears `files` for the
  caller to attach.  **Never mutates the parent manifest.**
- `compare_versions(layout, catalog, parent_dataset_id,
  child_dataset_id)` — reads both sides' Parquet files,
  classifies row differences as `added` / `removed` /
  `changed`, and returns a deterministic `VersionDiff`.
- `refuse_if_validated(layout, dataset_id)` — guardrail every
  in-place mutation call must pass through.
- `VersioningError` — subclass of `WarehouseIntegrityError`.

### Read-only guarantees (enforced by tests)
- No live-runner imports; no order-path tokens; no credential
  env-var reads; no `ApprovalRecord` / `PromotionEntry`
  construction.  `FeatureFlags.all_disabled == True` after
  chain / latest / diff calls.
- `derive_next_version` never mutates the parent manifest.

### Testing
- +26 tests in `tests/test_warehouse_versioning.py`.  Full
  suite: **1743 passing** (was 1717; +26 net new).
- Coverage: lineage traversal (chain, latest, parent + missing
  cases), derive_next_version (root → v2, v2 → v3, explicit
  corporate_action_version, provider field carryover, notes
  from revision_source, immutability of parent),
  compare_versions (changed / added / removed classification,
  no-diff, deterministic ordering, interval-mismatch reject,
  missing-dataset reject), refuse_if_validated for validated /
  unvalidated / missing datasets, source safety, feature-flag
  invariance.

---

## Unreleased — Phase 5.6: DuckDB query layer

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_duckdb_queries` (`t_e9626fc3`)
**Commit:** `f96d086`

### Added
- `strategy/warehouse/duckdb_query.py` — analytical read layer.
  Embedded DuckDB (no server); reads the Parquet tree written by
  `strategy.warehouse.parquet_io` directly via `read_parquet(?)`.
- `WarehouseQueryReader(layout, db_path=None)` — in-memory by
  default; file-backed for cross-session reuse.  Context-manager
  friendly.
- `scan_bars(asset_class, interval, symbols, start, end, dataset_id="")`
  — canonical `Bar` records sorted by `(symbol, timestamp)`.
- `coverage_summary(asset_class, interval, dataset_id)` — first /
  last timestamp, row count, sorted symbol list, file count.
  Returns `None` when the partition has no files.
- `latest_bar(asset_class, interval, symbol, dataset_id="")` —
  most recent bar per symbol.
- `row_counts_by_symbol(asset_class, interval, dataset_id="")` —
  `{symbol: row_count}` for the partition.
- `ohlcv_aggregate(asset_class, interval, symbols, start, end,
  dataset_id="")` — per-symbol min low / max high / mean close /
  mean volume / total volume over the window.  Symbols with no
  rows in-window omitted from the result.
- `CoverageSummary`, `OHLCVAggregate` — frozen dataclasses for
  the summary/aggregate returns.
- `DuckDBQueryError` — subclass of `WarehouseIntegrityError`
  for query-side violations (missing bounds, reversed window,
  malformed row on read).
- `open_reader(layout, db_path=None)` — context-manager helper.

### Read-only guarantees (enforced by tests)
- No live-runner imports; no order-path tokens; no credential
  env-var reads; no `ApprovalRecord` / `PromotionEntry`
  construction.  `FeatureFlags.all_disabled == True` after every
  query.

### Testing
- +26 new tests in `tests/test_warehouse_duckdb_query.py`.
  Full suite: **1717 passing** (was 1691; +26 net new).
- Coverage: sorted scan, symbol / window filters, canonical Bar
  materialisation, empty partition, bound validation, coverage
  summary (missing, populated, serialization), latest_bar
  (present, missing symbol, missing partition, empty-symbol
  guard), row_counts, OHLCVAggregate arithmetic parity with
  hand-computed values, symbol-list filter, empty-window
  handling, context-manager lifecycle, file-backed persistence
  across sessions, source safety, feature-flag invariance.

### Dependency
- New: `duckdb` (1.5.4).  Anticipated in the Phase 5.6 design
  doc.  Not imported by any live-path module.

---

## Unreleased — Phase 5.6: Incremental sync runner

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_incremental_sync` (`t_cf80bf36`)
**Commit:** `7b6b62f`

### Added
- `strategy/warehouse/incremental_sync.py` — nightly append-only
  runner that reads a dataset's manifest + on-disk partitions
  for the latest observed bar timestamp, asks the highest-
  priority provider for bars strictly newer than that
  timestamp, and appends them to the existing partition tree.
- `SyncPolicy(providers=(...), dry_run=False, end="")` — the
  priority-ordered provider list plus a dry-run toggle.  Empty
  provider list rejected on construction.
- `SyncReport` — per-run summary: `latest_before` → `latest_after`,
  `bars_appended`, `files_appended` (WrittenFile entries), the
  chosen provider, per-call trace, warnings, dry-run flag.
- `ProviderLedger` — per-provider call + bar counts accumulated
  across a sync session.  Research artifact only; does not
  influence provider selection.
- `sync_dataset(layout, dataset_id, policy, ledger=None)` —
  never redownloads existing bars; strictly-newer filter drops
  duplicates.  Falls through the priority list if the primary
  returns empty or raises.  Refuses to mutate a validated
  manifest (returns a warning telling the operator to run
  `warehouse-revise` instead).  Dry-run mode reports planned
  appends without writing.
- `sync_all(layout, dataset_ids, policy, ledger=None)` — batch
  runner.  Per-dataset failures caught and surfaced on the
  matching report entry instead of aborting the batch.
- `IncrementalSyncError` — subclass of
  `WarehouseIntegrityError` for sync-specific violations
  (missing warehouse fields, empty symbols, empty provider
  list).

### Read-only guarantees (enforced by tests)
- No live-runner imports.
- No order-path token references.
- No credential env-var reads.
- No `ApprovalRecord` / `PromotionEntry` construction.
- `FeatureFlags.all_disabled == True` after sync runs.

### Testing
- +20 new tests in
  `tests/test_warehouse_incremental_sync.py` against
  deterministic stub providers.  Full suite: **1691 passing**
  (was 1671; +20 net new).
- Coverage: appends strictly-newer bars, no-op when provider
  returns empty, manifest `end_date` advances on append,
  provider priority (falls through empty / raising primary,
  stops after first success), dry-run leaves manifest and disk
  unchanged while report still describes the plan, refuses to
  sync validated dataset, ledger tracks per-provider calls +
  bar counts, SyncPolicy validation, `sync_all` returns
  per-dataset report + surfaces missing dataset as warning
  rather than exception, missing-warehouse-fields rejection,
  no-symbols rejection, source safety, feature-flag invariance.

### Not in this card (deferred)
- Corporate-action revision replay — `warehouse-revise` under
  `t_phase56_data_versioning` follow-up.
- Automated cron scheduling — operator concern outside the
  Kanban surface.
- Dashboard rendering of `SyncReport` /
  `ProviderLedger` — `t_phase55_dashboard_plan`'s territory.

---

## Unreleased — Phase 5.6: Import pipeline

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_import_pipeline` (`t_31308fc2`)
**Commit:** `a898d6e`

### Added
- `strategy/warehouse/import_pipeline.py` — bulk provider fetch,
  chunked writes, resumable via SQLite queue, manifest thread-
  through, rebuild-from-disk workflow.
- `import_bars(layout, provider, dataset_id, symbols, …)` —
  chunks symbols to respect provider rate limits, calls
  `MarketDataProvider.fetch_daily_bars` / `fetch_intraday_bars`,
  writes bars via `strategy.warehouse.parquet_io.write_bars`,
  and lands a `DatasetManifest` (Phase 5.6 warehouse fields
  populated: `provider`, `interval`, `asset_class`,
  `adjustment_mode`, `validation_status=unvalidated`).
  Immutability guard: refuses to overwrite a validated manifest
  without `force=True`.
- `PendingDownloadsQueue` — SQLite-backed per-chunk status
  tracking (`pending → inflight → done | failed`).  Idempotent
  enqueue.  Resumable across process restarts.
- `rebuild_manifest(layout, dataset_id, …)` — regenerates a
  manifest from on-disk Parquet files.  Computes sha256 + row
  count + symbol coverage + date bounds by reading each file
  once.  Useful after operator backfills or warehouse moves.
- `ImportReport` — frozen dataclass summarizing one
  `import_bars` run: symbols requested / ok / empty, per-file
  metadata, warnings, manifest path.
- `ImportPipelineError` — subclass of `WarehouseIntegrityError`
  for pipeline-specific violations (bad chunk size, empty
  symbols, missing partition).

### Read-only guarantees (enforced by tests)
- No live-runner imports.
- No order-path token references.
- No credential env-var reads.
- No `ApprovalRecord` / `PromotionEntry` construction.
- `FeatureFlags.all_disabled == True` after import runs.

### Testing
- +26 new tests in `tests/test_warehouse_import_pipeline.py`
  against a deterministic `StubProvider` — no network.  Full
  suite: **1671 passing** (was 1645; +26 net new).
- Coverage: end-to-end write + manifest, per-symbol status
  propagation, chunk-size validation and output invariance,
  file placement under `dataset_dir`, sha256 / row_count /
  size fields threaded into manifest, catalog integration
  through `DataCatalog.from_warehouse_layout`, symbols_empty
  stashed in metadata, immutability guard refuses to overwrite
  validated manifest, queue status transitions, pending-list
  semantics, unknown status rejection, enqueue idempotence,
  resumability (queue tracks completed chunks; provider
  failure marks remaining chunks FAILED), chunk validation
  (zero, empty symbols, empty dataset_id), rebuild manifest
  from disk, missing-partition rejection, checksum parity vs
  first import, source safety.

### Not in this card (deferred)
- Incremental (append-only) sync — `t_phase56_incremental_sync`.
- Gap detection reports — `t_phase56_gap_detection`.
- DuckDB analytical query surface — `t_phase56_duckdb_queries`.

---

## Unreleased — Phase 5.6: Provider plugins

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_provider_plugins` (`t_22210825`)
**Commit:** `4c41212`

### Added
- `strategy/providers/` subpackage with three plugins:
  * `alpaca.py` — wraps the existing `ResearchAccountClient` as
    a `MarketDataProvider`.  Translates
    `AdjustmentMode` <-> Alpaca vocab; maps
    `/v2/stocks/bars` and `/v2/calendar` payloads onto `Bar` /
    `CalendarDay`.  Corporate actions + symbol metadata return
    empty batches with warnings — Alpaca doesn't expose them at
    this layer.  Client-level failures surface as
    `MarketDataRequestError`.
  * `csv.py` — reads local CSVs with canonical columns
    (`symbol, timestamp, open, high, low, close, volume,
    [vwap], [trade_count]`).  Configured with an interval and
    adjustment mode; refuses fetch calls whose interval differs.
    Rejects missing files and missing columns.
  * `parquet.py` — reads canonical-schema Parquet files through
    `strategy.warehouse.parquet_io.read_bars`, then filters by
    symbol / start / end / interval.
- Plugin registry (`strategy.providers.register` /
  `get_plugin` / `registered`).  Idempotent same-factory
  re-registration; refuses to shadow an existing name with a
  different factory.

### Read-only guarantees (enforced by tests)
- No live-runner imports.
- No order-path token references.
- No credential env-var reads at plugin level (Alpaca creds
  stay inside `ResearchAccountClient`).
- No `ApprovalRecord` / `PromotionEntry` construction.
- `FeatureFlags.all_disabled == True` after import + fetch.

### Testing
- +44 new tests in `tests/test_providers.py`.  Full suite:
  **1645 passing** (was 1601; +44 net new).
- Coverage: registry semantics; Alpaca capabilities + bar
  mapping + adjustment translation + intraday guards + client
  failure translation + bad-row skip + bad-response shape
  rejection + calendar mapping + holiday flag + unsupported
  surfaces; CSV canonical parsing + per-column filtering +
  missing file / column rejection; Parquet Protocol conformance
  + read via warehouse layout + filter by symbol / window /
  interval; source-safety (order path, live-runner, credential
  env, ApprovalRecord, PromotionEntry).  Every plugin passes
  the `isinstance(prov, MarketDataProvider)` conformance check.

### Not in this card (deferred)
- Polygon, Databento, Tiingo, Financial Modeling Prep, Alpha
  Vantage plugins — follow-up cards.
- Manual-import attestation flow — Phase 5.6 gap-detection card.
- Provider priority resolution / `WarehouseReader` fallback —
  `t_phase56_research_cache`.

---

## Unreleased — Phase 5.6: Parquet bar storage

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_parquet_storage` (`t_6168af8e`)
**Commit:** `e287c50`

### Added
- `strategy/warehouse/` — new subpackage for Phase 5.6 warehouse
  implementation modules.  Reserved for the parquet writer/reader,
  DuckDB query layer, versioning, validation, and import pipeline.
- `strategy/warehouse/parquet_io.py` — canonical Parquet bar
  writer / reader.  Uses `pyarrow`; no DuckDB, no provider
  plugins, no import pipeline.
- `CANONICAL_SCHEMA` / `CANONICAL_COLUMNS` — 12-column contract
  every warehouse-persisted bar row satisfies.  Column order is
  fixed on read: symbol, timestamp, OHLCV, vwap (nullable),
  trade_count (nullable), interval, adjustment_mode,
  adjustment_version.
- Zstandard (level 3) compression at write time; deterministic
  file bodies under a fixed pyarrow / libparquet version.
- Partition strategy per bar interval — daily -> `{yyyy}.parquet`,
  hourly -> `{yyyy}-{mm}.parquet`, minute/second ->
  `{yyyy}-{mm}-{dd}.parquet`.  All sub-hourly intervals share the
  daily partition.
- `write_bars(layout, bars, asset_class, dataset_id="")` — atomic
  writer (tempfile + `os.replace`) that sorts by
  `(symbol, timestamp)` before batching, refuses mixed-interval
  / mixed-adjustment_mode batches, and returns a list of
  `WrittenFile` records carrying relative + absolute paths, row
  count, byte count, and sha256 for the catalog to thread into
  its manifest.
- `read_bars(layout, relative_paths)` — schema-validated reader
  that reconstructs `Bar` instances via
  `strategy.market_data_provider` and returns them in
  `(symbol, timestamp)` order.  Refuses files whose on-disk
  schema does not match `CANONICAL_SCHEMA`.
- `scan_parquet_files(layout, asset_class, interval, dataset_id,
  symbol=None)` — deterministic filesystem enumeration under a
  partition, returning portable relative paths.
- `ParquetStorageError` — subclass of `WarehouseIntegrityError`
  for storage-specific violations (mixed intervals, schema
  mismatch, missing file, malformed timestamp).
- `WrittenFile` — frozen dataclass returned by `write_bars`;
  contains everything the catalog / manifest layer needs to
  register the file.

### Read-only guarantees (enforced by tests)
- No live-runner imports.
- No order-path token references.
- No provider plugin imports (`strategy.providers`,
  `alpaca_trade_api`, `polygon`, `databento`, `tiingo`).
- No yfinance / pandas.
- No provider credential env reads.
- No `ApprovalRecord` / `PromotionEntry` construction.
- `FeatureFlags.all_disabled == True` after module import,
  write, read, scan.
- Terminology: validation / replay / research / acquisition.

### Testing
- +42 new tests in `tests/test_warehouse_parquet_io.py` under
  pytest `tmp_path` (no touching of the repo's `market_data/`
  tree).  Total suite: **1601 passing** (was 1559; +42 net new).
- Coverage: canonical schema shape + null constraints,
  write/read roundtrip for daily/hourly/minute/second, per-symbol
  and per-time-bucket file naming, sha256 / size / row count
  vs disk, mixed-interval / mixed-adjustment_mode / bad-timestamp
  rejection, atomic-write leaves no stray temp files, asset-class
  routing (ETF -> equities/, CRYPTO -> crypto/), `dataset_id=""`
  fallback, `scan_parquet_files` with and without symbol filter,
  schema-mismatch refusal on read, warehouse integration via
  `layout.dataset_dir`, path-traversal rejection through the
  warehouse guard, feature-flag invariance, source-safety scan.

### Dependencies
- New dependency: `pyarrow` (24.0.0 installed).  Anticipated in
  the Phase 5.6 design doc.  Not imported by any live-path
  module.

### Not in this card (deferred to subsequent Phase 5.6 cards)
- DuckDB analytical query layer — `t_phase56_duckdb_queries`.
- Integrity + gap validation on the written files —
  `t_phase56_validation` / `t_phase56_gap_detection`.
- Provider plugin implementations — `t_phase56_provider_plugins`.
- Import pipelines — `t_phase56_import_pipeline`.

---

## Unreleased — Phase 5.6: Data catalog extension

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_catalog` (`t_56f319a9`)
**Commit:** `0fd96dd`
**Validation commit:** pending (this update)

### Added
- `strategy/data_catalog.py` — extended the existing Phase 3
  catalog with Phase 5.6 warehouse-aware metadata and query
  helpers.  Backward-compatible: legacy Phase 3 manifests round-
  trip byte-identically through `to_dict` / `from_dict`, and
  their `stable_hash` is unchanged.
- `DatasetManifest` gains the Phase 5.6 field surface: `provider`,
  `provider_request_id`, `interval` (validated against
  `BarInterval`), `asset_class` (validated against `AssetClass`),
  `adjustment_mode` (validated against `AdjustmentMode`),
  `adjustment_version`, `corporate_action_version`, `timezone`,
  `validation_status` (validated against
  `strategy.local_warehouse.KNOWN_STATUSES`), `parent_dataset_id`
  (for versioned datasets), `provider_capabilities_snapshot`,
  `warehouse_paths`.  Every field defaults to empty and serializes
  only when populated.
- `DatasetManifest.covers_symbol(symbol)` and
  `.covers_window(start, end)` — small predicates the coverage
  queries share.
- `DataCatalog.from_warehouse_layout(layout)` — construct a catalog
  from a `WarehouseLayout` (uses the layout's `manifests_dir`).
- `DataCatalog.find_coverage(symbol, interval, start, end)` —
  returns manifests whose symbol coverage, interval, and declared
  window overlap the query, sorted by `(start_date, dataset_id)`.
- `DataCatalog.gaps(symbol, interval, start, end)` — returns the
  uncovered sub-ranges within `[start, end]` as
  `[(gap_start, gap_end), …]`.  Rejects empty bounds and reversed
  ranges.  Day-level granularity (intra-day gap detection is the
  concern of `t_phase56_gap_detection`).
- `DataCatalog.latest_validated(symbol, interval)` — the most
  recent `validation_status='validated'` manifest covering a
  symbol at an interval; ordered by `end_date` descending.
  Ignores quarantined / unvalidated datasets.
- `DataCatalog.versions(dataset_id)` — returns the full version
  chain (the v1 root when present, plus every manifest whose
  `parent_dataset_id` matches).  Ordered by
  `corporate_action_version` (numeric ascending), with v1 root
  first.
- New enum-vocabulary constants exposed through
  `market_data_provider` and `local_warehouse` are consulted for
  invariant validation without duplicating enum bodies.

### Read-only guarantees (unchanged from Phase 3)
- No live-runner imports.
- No order-path token references.
- No provider plugin imports.
- No yfinance / pandas.
- No credential env-var reads.
- No `ApprovalRecord` or `PromotionEntry` construction.
- `FeatureFlags.all_disabled == True` after every query.

### Testing
- +40 new tests in `tests/test_data_catalog.py`
  (`TestWarehouseFieldsDefaultToEmpty`, `TestWarehouseFieldsPopulated`,
  `TestWarehouseFieldValidation`, `TestFindCoverage`, `TestGaps`,
  `TestLatestValidated`, `TestVersions`, `TestFromWarehouseLayout`,
  `TestWarehouseExtensionFeatureFlagInvariance`).
- Existing 45 Phase 3 tests still pass (backward compat).
- Total suite: **1559 passing** (was 1519; +40 net new).
- Coverage: legacy roundtrip byte-identity, hash invariance under
  the extension, warehouse field serialization, enum validation
  rejects (interval / asset_class / adjustment_mode /
  validation_status), parent-dataset invariants, coverage
  matching + interval filter + benchmark symbols + sort order,
  gap computation (no gap / full gap / interior gap / end gap /
  start gap / overlap merging / bad bounds), latest_validated
  ignores quarantined + filters by symbol/interval, version chain
  ordering + missing root + invalid id rejection,
  `from_warehouse_layout` roundtrip, feature-flag invariance.

### Not in this card (deferred to subsequent Phase 5.6 cards)
- Parquet writer / reader — `t_phase56_parquet_storage`.
- DuckDB analytical query surface — `t_phase56_duckdb_queries`.
- Provider plugin implementations — `t_phase56_provider_plugins`.
- Intra-day / per-bar gap detection — `t_phase56_gap_detection`.
- Dataset version lineage tracking beyond `parent_dataset_id` —
  `t_phase56_data_versioning`.

---

## Unreleased — Phase 5.6: Local historical warehouse layout

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_local_warehouse` (`t_b2a75ee8`)
**Commit:** `c2173c9`
**Validation commit:** pending (this update)

### Added
- `strategy/local_warehouse.py` — foundation for the Historical
  Data Warehouse: canonical directory tree, layout model, path
  helpers, immutable-manifest guarantee.  Layout only — no bar
  I/O, no Parquet, no DuckDB, no provider plugins.
- `WarehouseLayout` (frozen dataclass) — derives every canonical
  subdirectory from a single `root` path.  Class methods
  `default()` and `from_env()` for zero-config bootstrap; the
  `WAREHOUSE_ROOT` env var (disjoint from every provider
  credential namespace) selects the root location.
- Canonical subdirectory constants (`REQUIRED_SUBDIRS`):
  `equities/{daily,hourly,minute}`, `crypto`, `options`,
  `metadata`, `manifests`, `versions`.  `create()` builds them
  idempotently; `verify()` refuses to run against a tree missing
  or malformed a required subdirectory.
- Deterministic path helpers: `dataset_dir(dataset_id,
  asset_class, interval)`, `manifest_path(dataset_id)`,
  `version_dir(dataset_id, version)`.  ETFs route to equities;
  the four minute-scale intervals plus `SECOND_1` route to the
  minute partition; unsupported partitions raise.
- Manifest lifecycle vocabulary: `STATUS_UNVALIDATED`,
  `STATUS_VALIDATING`, `STATUS_VALIDATED`, `STATUS_QUARANTINED`.
- `write_manifest(layout, dataset_id, manifest, force=False)` —
  atomic (temp file + `os.replace`) write that refuses to
  overwrite a validated manifest without an explicit `force=True`.
  Unknown status values are rejected on write.  Serialization is
  deterministic (`sort_keys=True`, trailing newline) so a rerun
  produces a byte-identical file.
- `read_manifest`, `is_validated`, `refuse_overwrite_of_validated`
  — the immutability guardrail every downstream write path calls
  before touching disk.
- `WarehouseIntegrityError` — the module's single error class for
  every violated invariant (missing directory, wrong type,
  malformed JSON, forbidden overwrite, path traversal in
  `dataset_id`).

### Read-only guarantees (enforced by tests)
- No live-runner imports (`trader`, `crypto_trader`,
  `trader_cli`, `telegram_approvals`, `strategy.runner`).
- No order-path token references.
- No provider plugin imports (Alpaca, Polygon, Databento, Tiingo,
  `strategy.providers`, `alpaca_trade_api`).
- No yfinance / pandas dependency.
- No provider credential env reads (`ALPACA_*`, `APCA_*`,
  `RESEARCH_ALPACA_*`, `CRYPTO_ALPACA_*`).  Only the
  `WAREHOUSE_*` namespace is consulted.
- No `ApprovalRecord` or `PromotionEntry` construction.
- Global `FeatureFlags.all_disabled == True` after module
  import, layout construction, manifest writes.
- Terminology: validation / replay / research / acquisition.

### Testing
- +65 new tests in `tests/test_local_warehouse.py` under
  `pytest.tmp_path` — no touching of the repo's `market_data/`
  tree.
- Total suite: **1519 passing** (was 1454; +65 net new).
- Coverage: canonical constants, `WarehouseLayout` accessors,
  `create()` idempotency, `verify()` positive + missing-dir +
  wrong-type paths, `dataset_dir` routing (equity / ETF / crypto,
  daily / hourly / minute / second), path-traversal rejection in
  `dataset_id`, deterministic byte-identical manifest writes,
  read/write/status round-trips, immutability enforcement
  (validated cannot overwrite without `force`; unvalidated /
  quarantined can), `is_validated` including corrupt-manifest
  case, `refuse_overwrite_of_validated` gate on every write
  boundary, atomic-write leaves no stray temp files, source
  safety, feature-flag invariance.

### Not in this card (deferred to subsequent Phase 5.6 cards)
- Bar I/O (Parquet writer + reader) — `t_phase56_parquet_storage`.
- Analytical queries (DuckDB) — `t_phase56_duckdb_queries`.
- Provider plugin implementations — `t_phase56_provider_plugins`.
- Import pipelines / incremental sync / gap detection.
- Dataset versioning lineage tracking (uses `version_dir()` but
  the lineage catalog fields land in `t_phase56_data_versioning`).

---

## Unreleased — Phase 5.6: MarketDataProvider interface

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase56_provider_interface` (`t_1c8a70da`)
**Commit:** `808ec02`
**Validation commit:** pending (this update)

### Added
- `strategy/market_data_provider.py` — provider-agnostic market
  data interface, foundation card for Phase 5.6 Historical Data
  Warehouse.  Read-only, no plugin implementations.
- Normalized data models: `Bar`, `CorporateAction`, `SymbolMetadata`,
  `CalendarDay`, `ProviderCapabilities`, `ProviderResponse[T]`.  All
  frozen dataclasses with deterministic `to_dict()` serialization
  and content-level invariant validation on construction.
- Enums: `BarInterval` (DAILY / HOURLY / MINUTE_{1,5,15,30} / SECOND_1
  with Alpaca-shaped string values), `AdjustmentMode` (RAW / SPLIT /
  SPLIT_DIVIDEND / TOTAL_RETURN), `CorporateActionKind` (7 kinds
  covering splits, reverse splits, cash/special dividends, ticker
  changes, delistings, mergers), `CalendarSessionKind`, `AssetClass`.
- `MarketDataProvider` `typing.Protocol` (runtime_checkable) with
  the five documented fetch methods (`fetch_daily_bars`,
  `fetch_intraday_bars`, `fetch_corporate_actions`,
  `fetch_symbol_metadata`, `fetch_calendar`) plus
  `provider_capabilities`.  Structural per-symbol issues report
  via `ProviderResponse.per_symbol_status`; wholesale failures
  raise `MarketDataRequestError`.
- Error classes: `MarketDataValidationError` (dataclass invariants),
  `MarketDataRequestError` (provider transport failures).

### Read-only guarantees (enforced by tests)
- No live-runner imports (`trader`, `crypto_trader`, `trader_cli`,
  `telegram_approvals`, `strategy.runner`).
- No order-path token references (`submit_order`, `place_order`,
  `cancel_order`, `TradingClient`, …).
- No yfinance / pandas dependency.
- No credential env-var reads at the interface layer (plugins own
  their own credential namespaces).
- No `ApprovalRecord` or `PromotionEntry` construction.
- No file writes.
- Global `FeatureFlags.all_disabled == True` after module import
  and every stub-provider use.
- Terminology: validation / replay / research / acquisition.

### Testing
- +65 new tests in `tests/test_market_data_provider.py`.
- Total suite: 1454 passing (was 1389; +65 net new).
- Coverage: enum values, data-model invariants,
  serialization round-trips, credential-leak guardrail on
  `ProviderResponse.request_url_redacted`, protocol conformance
  via a `FakeProvider` stub, source-safety scan, feature-flag
  invariance.

### Not in this card (deferred to subsequent Phase 5.6 cards)
- Any provider plugin implementation (Alpaca, Polygon, Databento,
  Tiingo, FMP, Alpha Vantage, CSV, Parquet, manual).
- Warehouse storage (Parquet / DuckDB / SQLite).
- Import pipelines, incremental sync, gap detection.
- New dependency additions (`pyarrow`, `duckdb-python`).

---

## v0.26.0 — Phase 5: Two-Month Historical Validation

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase5_two_month_validation_run`
**Commit:** `5f03320`, plus validation board update

### Validation
- 1198 tests passing (0 failing); working tree clean prior to
  validation commit
- Fixture/offline mode is the default; a research_client passed
  with `live_fetch=False` is verifiably never called
- `live_fetch=True` without a research_client raises
  `LiveFetchNotAvailableError` — the orchestrator refuses to
  silently fall through
- `PromotionEntry.current_state == "disabled"` and `.approvals ==
  []` after every run; targets `enable_relative_strength` only
- `strategy/config.py` bytes byte-identical before and after a
  full run
- Determinism verified: two independent runs of the same config
  produce identical comparison / walk-forward / learning
  `stable_hash` values
- Global `FeatureFlags` singleton remains `all_disabled`
  throughout
- Source-safety scan confirms:
  no `submit_order` / `place_order` / `cancel_order` /
  `TradingClient` / `yfinance` references;
  no `from trader` / `from crypto_trader` / `from trader_cli` /
  `from telegram_approvals` / `from strategy.runner` imports;
  no `ApprovalRecord(` construction anywhere in the source;
  no `ALPACA_*` / `APCA_*` / `RESEARCH_ALPACA_*` env reads;
  terminology audit passes
- Card `t_phase5_two_month_validation_run` moved to Done
- **Phase 5 is complete — 4/4 cards Done**

### Added
- `strategy/historical_validation.py` — end-to-end orchestrator
  that ties the Research Account Client, Data Catalog, Backtest
  Lab, Relative Strength Challenger, Walk-Forward pipeline, Stats
  Engine, Learning Report generator, Research Analyst, and
  Promotion Gate evidence pipeline into a single
  `run_historical_validation(config)` call
- `HistoricalValidationConfig` frozen dataclass carrying dataset
  id, symbols, benchmarks, calendar window, in-sample /
  out-of-sample / step sizing, champion / challenger ids, seed,
  score-delta threshold, `live_fetch` flag (default `False`), and
  fixture inputs (`fixture_events`, `fixture_champion_scores`,
  `fixture_rs_map`); locks `flag_name` to
  `enable_relative_strength`
- `HistoricalValidationBundle` result with `dataset_manifest_path`,
  `ChampionChallengerComparison`, `ResearchReport` bundles for the
  comparison and walk-forward, `LearningReport`,
  optional analyst narratives / reports / paths, `PromotionEntry`
  evidence, warnings, and `live_fetch_used`
- Live-fetch pathway (opt-in): reads `RESEARCH_ALPACA_*` only via
  the caller-supplied `research_client` — the module itself
  never reads credentials from `os.environ`; converts bars to
  deterministic events and a simple percent-change Champion score
  map (real Champion scoring integration is a follow-up card)
- `LiveFetchNotAvailableError` raised when `live_fetch=True` and
  no `research_client` is supplied — the orchestrator refuses to
  silently fall through to fixture mode
- Fixture mode is the default: writes a lightweight DataCatalog
  manifest under `<research_data_root>/manifests/<dataset_id>.json`
  and persists a JSON events snapshot per dataset
- Champion is a deterministic `_FixtureChampion` (score lookup by
  timestamp); Challenger is `RelativeStrengthChallenger` wrapping
  it with a locally-scoped
  `FeatureFlags(enable_relative_strength=True)` so the global
  `FeatureFlags` singleton stays disabled

### Promotion evidence — read-only
- `PromotionEntry` targets `enable_relative_strength` at
  `STATE_DISABLED` with evidence keys `dataset_id`,
  `experiment_manifest`, `backtest_report_id`,
  `walk_forward_report_id`, `learning_report_id`, and
  `analyst_report_<i>`
- Runtime asserts refuse to return a bundle whose entry has
  advanced past `disabled` or carries any `ApprovalRecord`
- Source-level scan confirms the module never contains an
  `ApprovalRecord(` construction

### Tests
- `tests/test_historical_validation.py` — 41 tests covering:
  - Config: defaults, flag-name lock to
    `enable_relative_strength`, champion / challenger id
    disjointness, every validation error, stable-hash determinism
    and seed sensitivity
  - Live-fetch guard: default `False`; `live_fetch=True` without
    `research_client` raises `LiveFetchNotAvailableError`;
    passing a `research_client` in fixture mode NEVER calls it
    (research_client raises AssertionError if invoked;
    bundle.live_fetch_used is False)
  - Fixture end-to-end: bundle returned; dataset manifest
    written; comparison / walk-forward / learning reports each
    produce `.md` + `.json` files; findings list is populated
  - Promotion safety: entry stays `STATE_DISABLED`; no approvals;
    evidence carries dataset_id + backtest_report_id +
    walk_forward_report_id + learning_report_id; entry targets
    `enable_relative_strength`
  - Analyst integration: skipped when no `llm_client`; when a
    client is supplied, three narratives + three reports written
    (one each for comparison, walk-forward, learning); analyst
    report ids appear on the PromotionEntry evidence
  - Live-fetch pathway (mocked): `research_client.fetch_bars` is
    called exactly once with the configured window and requests
    symbols + benchmarks together; live_fetch_used is True
  - Determinism: two independent config instances produce
    byte-identical `report.md`, `report.json`, and `manifest.json`
    for every report type
  - Safety: `strategy/config.py` bytes byte-identical before and
    after a run; global `FeatureFlags` remains `all_disabled`;
    `HistoricalValidationBundle.to_dict()` is JSON-serializable;
    orchestrator never constructs `ApprovalRecord`
  - Source safety: no `submit_order` / `place_order` /
    `cancel_order` / `TradingClient` / `yfinance` references; no
    `from trader import` / `import trader` /
    `from crypto_trader` / `import crypto_trader` /
    `from trader_cli` / `import trader_cli` /
    `from telegram_approvals` / `import telegram_approvals` /
    `from strategy.runner` / `import strategy.runner`; no
    `os.environ[\"ALPACA_...` / `os.getenv(\"ALPACA_...` /
    `RESEARCH_ALPACA_API_KEY` / `RESEARCH_ALPACA_SECRET_KEY`
    literal env reads; terminology audit passes; import-time
    exclusion of order-path modules; `ApprovalRecord(`
    construction absent from source
- 1198 passing total (0 failures)

### Notes
- No live trading impact.  Runner, scheduler, Telegram, CLI, and
  plugin behavior unchanged.
- No feature flags enabled globally; the challenger's
  `enable_relative_strength` flag lives on a locally-scoped
  `FeatureFlags` instance passed to the challenger constructor
- No credentials read from `os.environ` inside the module — the
  caller supplies the research_client, which was independently
  validated to read `RESEARCH_ALPACA_*` only
- Historical validation paper account remains isolated per the
  Phase 5 rules; the orchestrator refuses to silently upgrade to
  live mode
- **Phase 5 backlog is now empty pending Hermes validation of
  this card**

---

## v0.25.0 — Phase 5: Local LLM Research Assistant

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase5_local_llm_research_assistant`
**Commit:** `c846f51`, plus validation board update

### Validation
- 1157 tests passing (0 failing)
- Working tree clean prior to validation commit
- **Local LLM endpoint loopback-only:** runtime check accepts
  `http://127.0.0.1:*`, `http://localhost:*`, `http://[::1]:*`;
  rejects public IPs (`8.8.8.8`), public hostnames
  (`api.openai.com`), `0.0.0.0`, RFC1918 (`192.168.1.10`), and
  missing schemes
- **Cloud model names refused:** `openai/*`, `anthropic-*`,
  `google-*`, `azure-*`, `aws-*`, `gemini`, `claude`, `chatgpt`
  refused at construction time and at `effective_model`
  resolution
- **Temperature pinned to 0.0:** any non-zero value rejected at
  construction
- **Output is read-only narrative/report data:** `LLMNarrative`
  and `ResearchAnalystReport` are the only outputs; write path
  only touches `<output_dir>/<report_id>/` under a caller-supplied
  root
- **No trade recommendations allowed:** system prompt explicitly
  forbids trade / entry / exit / position-size / hold / sell / buy
  advocacy; forbidden-output detector flags phrases like
  `you should buy` and `recommend selling`
- **No feature-flag advocacy allowed:** system prompt forbids
  "enable this flag" advocacy; forbidden-output detector catches
  it in LLM responses
- **No promotion advocacy allowed:** system prompt forbids
  "approve this promotion" / "promote to production" advocacy;
  detector catches `## Recommendation`, `## Next Steps`, and
  `## Action Items` headings
- **No credentials exposed:** module source contains no
  `ALPACA_*` / `APCA_*` / `TELEGRAM_*` / `OPENAI_*` reads; no
  credential values persisted anywhere
- **No broker/order-path imports:** module source has no
  `submit_order` / `place_order` / `cancel_order` /
  `TradingClient` / `yfinance` / `alpaca` references
- **No feature flags enabled:** module never imports
  `strategy.config`; runtime check confirms
  `FeatureFlags.all_disabled` after analyst run
- **No buy/sell logic changed:** runner, scheduler, Telegram,
  CLI, plugin behavior untouched
- **Deterministic outputs:** narrative id, prompt hash, source
  hash, and report id are stable across `generated_at`
  differences (verified by
  `TestRenderAnalystReport::test_stable_hash_ignores_generated_at`
  and
  `TestResearchAnalystAnalyze::test_narrative_id_deterministic_across_calls`);
  given the same LLM response, the four-file bundle is
  byte-identical
- Card `t_phase5_local_llm_research_assistant` moved to Done
- Card `t_phase5_two_month_validation_run` unblocked and moved
  to Ready

### Added
- `strategy/research_analyst.py` — read-only Research Analyst that
  consumes Phase 3 / Phase 4 artifacts and produces a research
  narrative
- `LocalLLMClient`:
  - Loopback-only endpoint check (`127.0.0.1`, `localhost`, `::1`);
    rejects `0.0.0.0`, public IPs, remote hostnames, and non-http
    schemes at construction time
  - `temperature=0.0` enforced; positive timeout enforced
  - Cloud model tokens (`openai` / `anthropic` / `google` / `azure`
    / `aws` / `gemini` / `claude` / `chatgpt`) refused at
    construction and at `effective_model` resolution
  - Model resolution via `strategy.model_config.resolve_model` with
    the `CONTEXT_RESEARCH` context; precedence
    `explicit > RESEARCH_AI_MODEL > client default`
  - Endpoint resolution from `RESEARCH_LLM_ENDPOINT`; defaults to
    `http://127.0.0.1:11434`
  - Accepts Ollama (`{"message":{"content":...}}`) and OpenAI-compat
    (`{"choices":[{"message":{"content":...}}]}`) response shapes
- `LLMNarrative` frozen dataclass with `narrative_id`, `prompt_hash`,
  `source_hash`, `model`, `warnings`, and a `stable_hash` that
  excludes `generated_at`
- `ResearchAnalyst` orchestrator:
  - Refuses at construction if the system prompt does not contain
    the "recommend a trade" prohibition clause
  - Convenience methods for each source kind:
    `analyze_comparison`, `analyze_walk_forward`,
    `analyze_learning_report`, `analyze_research_report`,
    `analyze_promotion_report`
  - Forbidden-output detection: scans the LLM response for
    trade-recommendation phrases (`you should buy`, `recommend
    buying`, ...) and forbidden section headings
    (`## Recommendation`, `## Next Steps`, `## Action Items`) and
    surfaces them as `LLMNarrative.warnings` for human review
- `SYSTEM_PROMPT` explicitly forbids trade recommendations, flag
  advocacy, and promotion advocacy; mandates the six analytical
  sections and forbids Recommendation / Next Steps / Action Items
- `ResearchAnalystReport` + `ResearchAnalystReportPaths` mirroring
  the Phase 4 `LearningReport` layout:
  `<output_dir>/<report_id>/report.md`, `report.json`,
  `narrative.json`, `manifest.json`; deterministic `report_id`
  derived from `narrative_id + source_hash`
- `compose_analyst_prompt` deterministic prompt composer
- Model / endpoint / kind / filename / prefix constants exported

### Tests
- `tests/test_research_analyst.py` — 96 tests covering:
  - Loopback endpoint acceptance (`127.0.0.1`, `localhost`, `::1`,
    `[::1]`); non-loopback rejection (public IP, public host,
    `0.0.0.0`, `192.168.1.10`); missing-scheme rejection; env-var
    endpoint resolution; default-endpoint fallback
  - Cloud model token rejection at construction and at
    `effective_model`; no-model-resolved error;
    explicit > env > default precedence
  - Non-zero temperature rejected; non-positive timeout rejected
  - Ollama and OpenAI-compat response shapes; missing content
    raises; non-JSON body raises; transport exception wrapped;
    explicit model override propagates into the wire body
  - `compose_analyst_prompt` determinism, source-metadata carrying,
    unknown-kind rejection, empty source id/hash rejection
  - `LLMNarrative` roundtrip; `stable_hash` excludes `generated_at`;
    all field validation errors
  - `ResearchAnalyst` refuses empty and permissive system prompts;
    analyze returns narrative with source metadata; delivered
    prompt matches the composer; system prompt is `SYSTEM_PROMPT`;
    `narrative_id` and `prompt_hash` deterministic across
    `generated_at` differences; explicit model propagates; forbidden
    output phrase and heading detected as warnings; clean output
    has no warnings
  - Convenience methods for every source kind (comparison /
    walk-forward / learning / research / promotion) unpack the
    correct metadata
  - `ResearchAnalystReportPaths` derives all four artifact paths
  - `render_analyst_report`: `rr_`-family prefix (`ra_`);
    default-title includes source kind; custom title; payload
    schema; manifest reproducibility metadata; markdown key
    sections; warnings section conditional; `stable_hash`
    independent of `generated_at`
  - `write` persists all four files; JSON files parse; is
    idempotent; lands under `<output_dir>/<report_id>/`
  - Byte-identical reruns: repeat render with same `generated_at`
    produces identical `to_json` / `to_markdown` /
    `narrative_json` / `manifest_json`
  - Source safety: no order-path references; no cloud LLM SDK
    imports; no `strategy.config` import; no live-runner imports;
    terminology audit passes; import-time exclusion of
    `trader_cli`, `trader`, `crypto_trader`, `telegram_approvals`;
    module never mutates global `FeatureFlags`
- 1157 passing total (0 failures)

### Notes
- No trading behavior changed; runner, scheduler, Telegram, CLI,
  plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- Localhost-only by construction; refuses cloud endpoints; refuses
  cloud model names
- Deterministic: report ids and prompt hashes are stable given the
  same source; given a deterministic LLM response the entire
  four-file bundle is byte-identical
- Follow-up card `t_phase5_two_month_validation_run` remains in
  Backlog

---

## v0.24.0 — Phase 5: Isolated Research Alpaca Client

**Date:** 2026-07-03
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase5_research_account_api`
**Commits:** `e5db721` (implementation) → `de8ea00` (credential-
namespace security refactor + `.env.example` + `.gitignore`
whitelist) → `645e259` (env-isolation docs + `PAPER = True`
safeguard comments) → `021351f` (launchers + systemd examples +
model config), plus this validation board update

### Validation
- 1061 tests passing (0 failing) on 2026-07-03
- Working tree clean prior to validation commit
- **Read-only client:** enumeration test guards against
  `submit_order`, `place_order`, `cancel_order`, `close_position`,
  `create_order`, `buy`, `sell`, `replace_order` attributes; only
  `fetch_bars`, `list_calendar`, `paper_account_info` exposed
- **Credentials isolated to `RESEARCH_ALPACA_*`:** config rejects
  any env-var name that does not start with `RESEARCH_ALPACA`;
  `FORBIDDEN_ENV_FALLBACKS` covers `ALPACA_*` and `APCA_*` and is
  disjoint from `REQUIRED_ENV_VARS`; `from_env()` fails fast when
  the namespace is missing; `resolve_credentials` refuses to fall
  back to `ALPACA_*` / `APCA_*`
- **No secrets tracked:** repository-wide `git grep` on tracked
  files finds no live Alpaca `PK*` credentials, no rotated-key
  literals, no AWS `AKIA*`, no `Bearer <token>`; test
  `test_env_example_holds_no_real_credentials` now scans by
  Alpaca / AWS credential shape (not by literal), so no
  credential-shaped string ever needs to be tracked
- **`.env.*` remain ignored:** `.gitignore` ignores `.env` and
  every `.env.*` variant while explicitly whitelisting
  `.env.example`; verified via `git check-ignore` for all four
  context env files
- **Launchers load only matching env files:** parametric tests
  confirm `run-paper` / `run-crypto` / `run-research` each
  reference their own env file, refuse to source `.env` or
  `.env.production` outside a documentation comment, and refuse
  to run when the target env file is missing; there is no
  `scripts/run-production`
- **Production remains disabled/guarded:** both live-runner
  `PAPER = True` guards are preserved; no `.env.production`
  loading in any live path; no `scripts/run-production`;
  `traderjoe-production.service.example` ships with layered
  guards (`.example` suffix + `ExecStart=/bin/false` +
  `ConditionPathExists=` + omitted `[Install]` section);
  live-runner code (comments/docstrings stripped) contains no
  `RESEARCH_ALPACA` / `RESEARCH_AI_MODEL` / `.env.research` /
  `.env.production` / `PRODUCTION_AI_MODEL` references
- **Model config is context-aware:** `strategy/model_config.py`
  implements a three-tier precedence (`explicit > env > default`)
  across `PAPER_AI_MODEL` / `CRYPTO_AI_MODEL` /
  `RESEARCH_AI_MODEL` / `PRODUCTION_AI_MODEL`; parametric tests
  confirm each context reads only its own env var; the helper
  never hardcodes a model name; `trader.py` and `crypto_trader.py`
  preserve their pre-env-var default via a local
  `_LEGACY_MODEL_DEFAULT` so behavior is unchanged when env vars
  are unset
- **No feature flags enabled:** global `FeatureFlags` singleton
  remains `all_disabled` after client + config runs; module
  never imports `strategy.config`
- **No buy/sell logic changed:** runner, scheduler, Telegram,
  CLI, and plugin behavior unchanged; broker/order paths untouched
  beyond the two-line AI_MODEL wiring and the safeguard comment
  blocks at `PAPER = True`
- **Working tree clean:** confirmed pre- and post-commit
- Card `t_phase5_research_account_api` moved to Done
- Card `t_phase5_local_llm_research_assistant` unblocked and moved
  to Ready

### Added
- `strategy/research_account.py` — read-only client for the
  dedicated Research Alpaca paper account
- `ResearchAccountConfig` frozen dataclass storing env-var *names*
  only; refuses any name that does not start with
  `RESEARCH_ALPACA`
- `ResearchAccountClient` exposing only read methods:
  `fetch_bars`, `list_calendar`, `paper_account_info`; deterministic
  request construction; pluggable `http_get` for testing
- `ResearchAccountRequest` frozen record with `redacted_dict()`
  masking credential headers before logging or serialization
- `ResearchAccountConfigError` / `ResearchAccountRequestError`
  for typed failures
- `REQUIRED_ENV_VARS` (`RESEARCH_ALPACA_API_KEY`,
  `RESEARCH_ALPACA_SECRET_KEY`, `RESEARCH_ALPACA_ENDPOINT`) and
  `FORBIDDEN_ENV_FALLBACKS` (`ALPACA_*` and `APCA_*`) — the two
  sets are disjoint by construction and audited by a test
- Default HTTP transport uses `urllib.request.urlopen`; tests
  inject a fake to avoid the network

### Behavior
- Credentials are resolved at call time via
  `ResearchAccountConfig.resolve_credentials(env)`; the config
  object never stores credential values
- `resolve_credentials` refuses to fall back to `ALPACA_*` /
  `APCA_*`; an env supplying only those raises
  `ResearchAccountConfigError`
- Empty-string env values are treated as missing
- Endpoint must start with `http://` or `https://`; trailing slash
  is stripped
- Query parameters are sorted alphabetically for reproducible URLs
- `to_dict` and `redacted_dict` never leak credential values
- Response shape checked: bars → object, calendar → list,
  account → object; anything else raises `ResearchAccountRequestError`

### Tests
- `tests/test_research_account.py` — 62 tests covering:
  - `REQUIRED_ENV_VARS` all in `RESEARCH_ALPACA_` namespace;
    `FORBIDDEN_ENV_FALLBACKS` covers `ALPACA_*` and `APCA_*`; the
    two sets are disjoint; API paths are the expected read
    endpoints
  - `ResearchAccountConfig`: defaults use `RESEARCH_ALPACA_*`;
    rejects `ALPACA_*`, `APCA_*`, and any non-namespaced env; each
    of the three names validated; `to_dict` returns names only
    (no values); `resolve_credentials` reads from supplied env,
    raises on missing keys, treats empty strings as missing,
    never falls back to `ALPACA_*`; defaults to `os.environ` when
    no env supplied
  - Bars request: URL composition; deterministic query-string
    ordering; credential headers; `redacted_dict` masks
    credentials; empty symbols rejected; non-positive limit
    rejected; optional start/end omitted when `None`
  - Calendar request: URL with dates; URL without params;
    credential headers
  - Account request: exact URL; credential headers
  - Endpoint validation: rejects `ftp://`, missing scheme, empty
    string; strips trailing slash; positive timeout required
  - `fetch_bars`: returns decoded JSON object; HTTP exception
    wrapped as `ResearchAccountRequestError`; non-JSON raises;
    non-object bars body raises; timeout forwarded to `http_get`
  - `list_calendar`: returns list; non-list raises
  - `paper_account_info`: returns dict; non-dict raises
  - **Read-only API surface:** no `submit_order`, `place_order`,
    `cancel_order`, `close_position`, `close_all_positions`,
    `create_order`, `buy`, `sell`, `replace_order` attributes;
    only `fetch_bars`, `list_calendar`, `paper_account_info`
    exposed; no public method starts with `submit_` / `place_` /
    `create_` / `cancel_` / `delete_`
  - **Source safety:** no `submit_order`, `place_order`,
    `cancel_order`, `close_position`, `create_order`,
    `TradingClient`, `yfinance` references; no
    `from trader import` / `import trader` /
    `from crypto_trader import` / `import crypto_trader` /
    `from trader_cli import` / `import trader_cli` /
    `from telegram_approvals import` /
    `import telegram_approvals` / `from strategy.runner import` /
    `import strategy.runner`; no direct `ALPACA_*` or `APCA_*`
    env reads; no `write_text` / `write_bytes` / `open(...,"w")` /
    `with open`; module never imports `strategy.config`;
    terminology audit passes; import-time exclusion of
    `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`; module never mutates global
    `FeatureFlags`
  - **Env-namespace isolation:** with only `ALPACA_*` / `APCA_*`
    set, the client refuses to resolve credentials;
    `ResearchAccountConfig.to_dict` output never contains
    credential values; the client exposes no `api_key`,
    `secret_key`, or `credentials` attribute
  - **Deterministic request construction:** two clients with the
    same env produce byte-equal URLs; `ResearchAccountRequest` is
    frozen; passing an env dict does not mutate it
- 981 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is
  unchanged; runner, scheduler, Telegram, CLI, and plugin behavior
  untouched
- No feature flags enabled; module never imports `strategy.config`
- No file writes; credentials never touch disk
- Historical validation paper account remains isolated: env-var
  namespace disjoint from `ALPACA_*`; no cross-import into
  Phase 1-4 modules
- Follow-up cards remain in Backlog:
  `t_phase5_local_llm_research_assistant`,
  `t_phase5_two_month_validation_run`

---

## v0.23.0 — Phase 4: End-to-End Learning System Validation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_validation`
**Commit:** `6d886b2`, plus validation board update

### Validation
- 919 tests passing (0 failing)
- Working tree clean prior to validation commit
- Two independent `LearningPipeline` runs sharing the same
  fixtures and `generated_at` produce byte-identical files
- `stable_hash` and `report_id` independent of `generated_at`
- `output_root` auto-created if missing; no files land outside the
  configured root (verified by inspecting the parent directory)
- Symlinked root resolves into the real target and stays inside it
- Global `FeatureFlags` singleton remains disabled after pipeline
  runs
- `strategy/config.py` bytes byte-identical before and after a
  pipeline run
- No order-path modules (`trader_cli`, `trader`, `crypto_trader`,
  `telegram_approvals`) pulled into `sys.modules` during pipeline
  execution
- Forbidden-import audit passes across every Phase 4 module
  (`stats_engine`, `pattern_discovery`, `feature_importance`,
  `weight_recommender`, `learning_reports`, `learning_pipeline`)
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_validation` moved to Done
- **Phase 4 is complete — 7 / 7 cards Done**

### Added
- `strategy/learning_pipeline.py` — thin orchestrator that renders
  and persists a `LearningReport` under a statically-configured
  output root
- `LearningPipeline` frozen dataclass with an immutable
  `output_root` (defaults to `DEFAULT_LEARNING_REPORT_OUTPUT_DIR`);
  no per-run override
- `LearningPipeline.run(...)` calls `render_learning_report`, checks
  that the resolved report directory is inside `output_root`, then
  persists via `LearningReport.write`
- `LearningPipelineError` raised when the path guard trips or when
  `output_root` is empty at construction time

### Tests
- `tests/test_learning_system_e2e.py` — 26 tests covering:
  - Default `output_root` matches `DEFAULT_LEARNING_REPORT_OUTPUT_DIR`
  - Empty `output_root` rejected at construction
  - **Byte-identical reruns:** two independent pipelines sharing
    the same fixtures and `generated_at` produce byte-equal
    `report.md`, `report.json`, `recommendations.json`, and
    `manifest.json`; `stable_hash` and `report_id` independent of
    `generated_at`
  - Empty inputs produce a valid bundle; partial inputs produce a
    findings-only report; `output_root` created if missing
  - **Write-outside-root refusal:** all four files land under
    `output_root`; no stray files at the parent tmp path; a
    symlinked root resolves into the real target and stays inside it
  - Global `FeatureFlags` singleton stays disabled after a run
  - `strategy/config.py` bytes byte-identical before and after a run
  - **Forbidden-import ban across every Phase 4 module** (parametric
    over `stats_engine`, `pattern_discovery`, `feature_importance`,
    `weight_recommender`, `learning_reports`, `learning_pipeline`):
    no `alpaca`, `place_order`, `submit_order`, `TradingClient`,
    `api_key`, or `yfinance` references; no order-path modules
    pulled in at import time
  - Terminology audit across every Phase 4 module: every module
    contains the explicit "training" policy sentence and no stray
    references
  - Recommendation routing: `envelope.apply_to_entry` still routes
    into `PromotionEntry.evidence` after a pipeline run
- 919 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- **Phase 4 backlog is empty pending Hermes validation of this card**

---

## v0.22.0 — Phase 4: Learning Report Generation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_learning_reports`
**Commit:** `878c50e`, plus validation board update

### Validation
- 893 tests passing (0 failing)
- Working tree clean prior to validation commit
- `stable_hash` and `report_id` independent of `generated_at`
- Repeat render with the same `generated_at` produces byte-identical
  `to_json`, `to_markdown`, `recommendations_json`, and
  `manifest_json`
- `write` persists all four files
  (`report.md`, `report.json`, `recommendations.json`, `manifest.json`),
  is idempotent, and lands under `<output_dir>/<report_id>/`
- Manifest carries all four source hashes
  (`findings_hash`, `hypotheses_hash`, `importance_hash`,
  `recommendations_hash`)
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/learning_reports.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_learning_reports` moved to Done
- Card `t_phase4_validation` unblocked and moved to Ready

### Added
- `strategy/learning_reports.py` — aggregates Phase 4 analysis
  records into a `LearningReport` bundle mirroring the Phase 3
  `ResearchReport` layout
- `LearningReport` dataclass with Markdown body, JSON payload,
  flattened `recommendations` list, and reproducibility manifest;
  `stable_hash` excludes `generated_at`
- `LearningReportPaths` derives four artifact locations from
  `output_dir + report_id`
- `render_learning_report(findings, hypotheses, importance_scores,
  recommendations, envelopes=None)`:
  - `report_id` derived from the four source `stable_hash` values
    (`findings_hash + hypotheses_hash + importance_hash +
    recommendations_hash`)
  - Payload carries summary counts + per-section label buckets +
    every source record sorted deterministically
  - Markdown renders sections for statistical findings, pattern
    hypotheses, feature importance, weight recommendations, optional
    recommendation envelopes, and a reproducibility footer
- `write(output_dir)` persists four files under
  `<output_dir>/<report_id>/`: `report.md`, `report.json`,
  `recommendations.json`, `manifest.json`; parent dir created if
  missing and write is idempotent
- Default output tree: `reports/learning/`
  (`DEFAULT_LEARNING_REPORT_OUTPUT_DIR`)

### Tests
- `tests/test_learning_reports.py` — 26 tests covering:
  - `LearningReportPaths` derives all four artifact paths;
    `DEFAULT_LEARNING_REPORT_OUTPUT_DIR` constant
  - `render_learning_report`: `lr_` prefix; JSON schema (payload
    kind, sections, summary keys, label buckets covering
    `KNOWN_STATS_LABELS`); summary counts match inputs; Markdown
    contains required sections; empty sections flagged with
    "_No … supplied._" markers; custom title; default title
    includes report id; envelopes included in payload + Markdown
  - Determinism: `stable_hash` and `report_id` independent of
    `generated_at`; repeat render byte-identical across
    `to_json`, `to_markdown`, `recommendations_json`,
    `manifest_json`; report id changes when findings or
    recommendations change
  - `write`: all four files persisted; landed under
    `<output_dir>/<report_id>/`; idempotent; recommendations
    persisted separately; manifest carries all four source hashes
  - Empty inputs: bundle still valid; empty report id stable
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references;
    terminology check; module never mutates global feature flags;
    never imports `strategy.config`; import-time exclusion of
    `trader_cli`, `trader`, `crypto_trader`, `telegram_approvals`
- 893 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- Follow-up card remaining: `t_phase4_validation` (end-to-end
  Learning System validation)

---

## v0.21.0 — Phase 4: Strategy Weight Recommender

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_weight_recommender`
**Commit:** `a04d46d`, plus validation board update

### Validation
- 867 tests passing (0 failing)
- Working tree clean prior to validation commit
- `MAX_ALLOWED_PROMOTION_STATE == STATE_PAPER_TRADING` enforced;
  `candidate` / `approved` / `production` rejected at both
  `recommend_weights` entry and `WeightRecommendation.__post_init__`
- Recommendation math verified: 10 → 12.0 at +20% for a positive
  validated feature score
- `RecommendationEnvelope.apply_to_entry` returns a new
  `PromotionEntry` and never mutates the input (original entry
  evidence remains `{}` after routing)
- **Config immutability proven:** `strategy/config.py` bytes are
  byte-identical before and after a recommender run + envelope
  routing
- Determinism verified: repeat recommendations byte-identical;
  `recommendations_stable_hash` order-independent
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/weight_recommender.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_weight_recommender` moved to Done
- Card `t_phase4_learning_reports` unblocked and moved to Ready

### Added
- `strategy/weight_recommender.py` — read-only recommender that turns
  feature-importance scores into `WeightRecommendation` data
  artifacts
- `WeightRecommendation` frozen dataclass with target config key,
  current and proposed value, rationale, supporting feature scores,
  required promotion state, confidence level, and label
- `RecommendationEnvelope` frozen dataclass that routes a
  recommendation into `PromotionEntry.evidence` via a configurable
  `evidence_key`; `apply_to_entry` returns a new `PromotionEntry`
  and never mutates the input
- Hard promotion ceiling: `MAX_ALLOWED_PROMOTION_STATE =
  STATE_PAPER_TRADING`;
  `FORBIDDEN_PROMOTION_STATES_FOR_RECOMMENDER` covers `candidate`,
  `approved`, and `production`; refusal enforced both in
  `recommend_weights` and by `WeightRecommendation.__post_init__`
- `recommend_weights(feature_scores, current_weights, ...)` scales
  each matched weight by `max_change_pct * sign(score)`; skips
  features that are hypothesis-labeled, flagged
  `low_sample` / `zero_variance` / `ci_spans_zero`, below
  `min_feature_score`, or absent from `current_weights`
- `envelope_for`, `build_recommendation_id`, `build_envelope_id`,
  and `recommendations_stable_hash` helpers

### Tests
- `tests/test_weight_recommender.py` — 53 tests covering:
  - `WeightRecommendation` roundtrip, delta computation, and every
    validation error
  - Allowed promotion states (disabled, backtest, walk_forward,
    paper_trading) accepted; forbidden states (candidate, approved,
    production) rejected at construction; unknown state rejected
  - `MAX_ALLOWED_PROMOTION_STATE == STATE_PAPER_TRADING`; allowed +
    forbidden sets partition all seven promotion states
  - `RecommendationEnvelope`: requires ids and evidence key;
    `to_evidence_value` deterministic JSON; `apply_to_entry` returns
    new entry without mutating the input; supports custom
    `evidence_key`; preserves existing evidence; `to_dict` JSON
    serializable
  - `recommend_weights`: positive score scales weight up; negative
    score scales weight down; hypothesis scores skipped when
    `require_validated=True`; included when `require_validated=False`;
    features not in `current_weights` skipped; CI spanning zero
    skipped when `require_significant=True`; `low_sample` and
    `zero_variance` flags skip a feature; below `min_feature_score`
    skipped; refuses to target forbidden states; refuses unknown
    state; refuses non-positive or >1.0 `max_change_pct`; refuses
    negative `min_feature_score`; recommendations sorted
    deterministically; validated recommendation label set
  - Determinism: repeat recommendations byte-identical;
    `recommendations_stable_hash` order-independent;
    `build_recommendation_id` deterministic;
    `build_envelope_id` differs across `evidence_key` values
  - **Config immutability:** module source contains no
    `open(strategy/config.py`, no `config_path`, and no `"w"` /
    `'w'` write mode markers; module source does not import
    `strategy.config`; running the recommender + envelope leaves
    `strategy/config.py` bytes unchanged
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references;
    terminology check; module never mutates global feature flags;
    import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`
- 867 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- Approvals are still data artifacts — this module never constructs
  an `ApprovalRecord` autonomously; human approval remains required
  before any promotion to `candidate`, `approved`, or `production`
- Follow-up cards remain in Backlog: `t_phase4_learning_reports`,
  `t_phase4_validation`

---

## v0.20.0 — Phase 4: Feature Importance Analysis

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_feature_importance`
**Commit:** `22259ad`, plus validation board update

### Validation
- 814 tests passing (0 failing)
- Working tree clean prior to validation commit
- `pearson_r` returns 1.0 for perfect positive linear input;
  `fisher_z_ci` symmetric around zero at r=0
- Look-ahead observations rejected by default with `ValueError`;
  `filter_lookahead_observations` correctly partitions
- Deterministic ranking: perfectly correlated feature ranks above
  uncorrelated feature; passing the same list in a different order
  yields identical output
- Low-sample features labeled `hypothesis` and flagged
  `low_sample`
- `importance_stable_hash` order-independent
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/feature_importance.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_feature_importance` moved to Done
- Card `t_phase4_weight_recommender` unblocked and moved to Ready

### Added
- `strategy/feature_importance.py` — per-feature Pearson-r importance
  scoring with Fisher z-transform confidence intervals
- `FeatureObservation` frozen dataclass with feature/outcome
  timestamps and a `has_lookahead()` guard
- `FeatureImportanceScore` frozen dataclass with method,
  sample size, CI, label, `flagged` boolean, and structured flag
  reasons (`low_sample`, `zero_variance`, `ci_spans_zero`)
- `pearson_r(x, y)` and `fisher_z_ci(r, n, confidence_level)`
  helpers (clip to `(-1, 1)` to keep `atanh` finite; return
  `(r, r)` when `n < 4`)
- `filter_lookahead_observations` partitions inputs into kept and
  dropped
- `analyze_feature_importance(observations, feature_names, ...)`:
  Pearson r + Fisher z CI per feature, ranked by `|score|`
  descending with feature-name asc tiebreak, rejects look-ahead by
  default with a `ValueError`
- `importance_stable_hash` order-independent deterministic hash

### Tests
- `tests/test_feature_importance.py` — 52 tests covering:
  - `FeatureObservation` roundtrip and validation errors;
    `has_lookahead` at strictly-before, exact-equal, and after
    timestamps
  - `FeatureImportanceScore` roundtrip; `is_significant` at CI
    boundary conditions; every validation error
  - `pearson_r`: perfect positive/negative correlation, zero
    variance, empty and single-value inputs, length-mismatch error
  - `fisher_z_ci`: symmetric around zero at r=0, widens with higher
    confidence, shrinks with larger samples, bounded at r=±1,
    flat interval for n<4
  - `filter_lookahead_observations` partitions correctly on empty
    and mixed input
  - `analyze_feature_importance`: perfect positive/negative
    correlation returns r=±1 with `LABEL_VALIDATED`; low sample
    flagged `low_sample`; constant feature flagged
    `zero_variance`; uncorrelated data flagged `ci_spans_zero`;
    ranking by `|score|`; missing feature yields zero-sample record;
    look-ahead rejection by default; `reject_lookahead=False` skips
    the guard; duplicate feature names deduplicated; deterministic
    output; confidence-level propagation; unsupported confidence
    rejected; negative floor rejected
  - `importance_stable_hash` deterministic and order-independent
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references; terminology
    check; module never mutates global feature flags; never imports
    `strategy.config`; import-time exclusion of `trader_cli`,
    `trader`, `crypto_trader`, `telegram_approvals`
- 814 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- Follow-up cards remain in Backlog: `t_phase4_weight_recommender`,
  `t_phase4_learning_reports`, `t_phase4_validation`

---

## v0.19.0 — Phase 4: Historical Pattern Discovery

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_pattern_discovery`
**Commit:** `3711453`, plus validation board update

### Validation
- 762 tests passing (0 failing)
- Working tree clean prior to validation commit
- All discovered patterns default to `LABEL_HYPOTHESIS`
- OOS validation only promotes to `LABEL_VALIDATED` when the OOS
  group meets the sample-size floor, the mean-return direction
  matches, and the win-rate direction matches
- OOS insufficient sample surfaces as `LABEL_HYPOTHESIS` with an
  `insufficient OOS sample` detail note
- OOS return-direction mismatch surfaces as `LABEL_HYPOTHESIS` with a
  `return direction mismatch` detail note
- Determinism verified: shuffled observation input yields identical
  discovery output; `hypotheses_stable_hash` order-independent
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/pattern_discovery.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_pattern_discovery` moved to Done
- Card `t_phase4_feature_importance` unblocked and moved to Ready

### Added
- `strategy/pattern_discovery.py` — read-only pattern discovery over
  bucketed market-context and realized-outcome trade observations
- `PatternObservation` frozen dataclass with trade id, entry/exit
  timestamps, context dict, outcome dict, and metadata; rejects
  empty required fields and out-of-order exit timestamps
- `PatternHypothesis` frozen dataclass with sample size, win rate +
  Wilson CI, mean return + normal-approx CI, Cohen's d one-sample
  effect size, `hypothesis` / `validated` label, and sorted
  supporting evidence ids
- `discover_patterns` groups observations by sorted feature-key
  tuples, computes descriptive statistics, and emits hypotheses
  labeled `LABEL_HYPOTHESIS` for each feature combination meeting
  `PATTERN_MIN_SAMPLE_SIZE` (default 10)
- `validate_patterns_out_of_sample` promotes to `LABEL_VALIDATED`
  only when the OOS group meets the sample floor, the mean-return
  direction matches, and the win-rate direction matches; otherwise
  re-emits with `LABEL_HYPOTHESIS` and a detail note explaining the
  gap
- `hypotheses_stable_hash` deterministic order-independent hash

### Tests
- `tests/test_pattern_discovery.py` — 47 tests covering:
  - `PatternObservation`: roundtrip, context/outcome helpers, and all
    validation errors
  - `PatternHypothesis`: roundtrip, `features()` helper,
    `is_significant_return` at CI boundaries, all validation errors
    including mismatched key/value lengths
  - `discover_patterns`: every emitted hypothesis defaults to
    `LABEL_HYPOTHESIS`; per-feature-combination groups produced; win
    rate / mean return / CI math on constant-value fixtures; below-
    floor groups skipped; evidence ids sorted and reference source
    trade ids; deterministic ordering; single-key grouping; empty
    feature-key tuple skipped; observations missing context or
    outcome silently dropped; negative floor and unsupported
    confidence rejected; confidence level propagates for all
    supported levels
  - `validate_patterns_out_of_sample`: promotion to
    `LABEL_VALIDATED` on directional match; stays `LABEL_HYPOTHESIS`
    on return-direction flip; stays `LABEL_HYPOTHESIS` on OOS-sample
    shortfall; win-rate direction mismatch stays `LABEL_HYPOTHESIS`;
    pattern id preserved across validation; negative floor rejected
  - `hypotheses_stable_hash` deterministic and order-independent
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references; terminology
    check; module never mutates global feature flags; never imports
    `strategy.config`; import-time exclusion of `trader_cli`,
    `trader`, `crypto_trader`, `telegram_approvals`
- 762 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- The `validated` label indicates only that the pattern's directional
  effect held on a disjoint observation window; it is **not** a
  promotion signal and does not by itself justify advancing a feature
  past `disabled`
- Follow-up cards remain in Backlog:
  `t_phase4_feature_importance`, `t_phase4_weight_recommender`,
  `t_phase4_learning_reports`, `t_phase4_validation`

---

## v0.18.0 — Phase 4: Statistical Decision-Support Layer

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase4_stats_engine`
**Commit:** `7005254`, plus validation board update

### Validation
- 715 tests passing (0 failing)
- Working tree clean prior to validation commit
- Math verified against known-input fixtures: `normal_mean_ci` for
  values `[1..5]` (mean 3.0, CI [1.6141, 4.3859] at 95%);
  `wilson_proportion_ci` correctly handles all-failure and all-success
  boundaries; `cohens_d_one_sample` on the same fixture returns
  1.8974 as expected
- Determinism verified: repeat `analyze_comparison` produces
  byte-identical `to_dict` output; `findings_stable_hash` is
  order-independent
- Sample-size floor enforced: below-floor findings labeled
  `hypothesis`; at/above-floor findings labeled `validated`
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/stats_engine.py`
- Module never imports `strategy.config`, never reads or mutates
  `FeatureFlags`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase4_stats_engine` moved to Done
- Card `t_phase4_pattern_discovery` unblocked and moved to Ready

### Added
- `strategy/stats_engine.py` — read-only descriptive-statistics layer
  over Phase 3 comparison artifacts
- `StatisticalFinding` frozen dataclass with metric, effect size,
  confidence-interval bounds, sample size, methodology,
  `hypothesis` / `validated` label, confidence level, evidence ids,
  and detail
- Deterministic numeric helpers: `normal_mean_ci` (normal-approx mean
  CI), `wilson_proportion_ci` (Wilson score proportion CI),
  `cohens_d_one_sample`, `cohens_d_two_sample`
- Supported confidence levels: 0.90, 0.95, 0.99
  (`SUPPORTED_CONFIDENCE_LEVELS`)
- Sample-size floor `STATS_SAMPLE_SIZE_FLOOR = 30` gates the
  `validated` label; smaller samples fall through to `hypothesis`
- `analyze_comparison(comparison)` returns findings in fixed order:
  `score_delta_mean → rank_delta_mean → disagreement_rate →
  selection_agreement_rate`
- `analyze_walk_forward(wf_report)` returns aggregate findings
  (`wf_score_delta_mean` + `wf_disagreement_rate`) across all splits'
  OOS comparisons — never mixes in-sample data
- Evidence ids on every finding reference the source `run_id` values
  so downstream reports can trace back to reproducible artifacts
- `findings_stable_hash` produces an order-independent deterministic
  hash across a sequence of findings

### Tests
- `tests/test_stats_engine.py` — 59 tests covering:
  - `normal_mean_ci`: empty input, single-value, known mean/stdev,
    CI widens at higher confidence, rejects unsupported confidence
  - `wilson_proportion_ci`: zero trials, all success (CI < 1), all
    failure (CI > 0), half-success centred near 0.5, boundaries
    clipped to [0, 1], invalid inputs rejected
  - `cohens_d_one_sample` / `cohens_d_two_sample`: known effect sizes,
    zero-variance and empty-sample degenerate cases, symmetry
  - `SUPPORTED_CONFIDENCE_LEVELS` exposes 0.90 / 0.95 / 0.99
  - `StatisticalFinding`: roundtrip, `is_significant` at all CI
    boundary configurations (excludes-zero, spans-zero, touches-zero,
    negative-only), evidence-ids tuple coercion, and every
    constructor validation error
  - `analyze_comparison`: fixed metric ordering; label defaults to
    `hypothesis` for small samples; `validated` label triggered when
    sample meets floor; score delta finding surfaces a positive
    effect and a CI excluding zero; disagreement rate + selection
    agreement rate finding math on a two-symbol fixture; empty
    comparison yields zero-sample findings; evidence ids reference the
    comparison run id; confidence-level propagation; custom
    sample-size floor; negative floor rejected
  - `analyze_walk_forward`: fixed metric ordering; evidence ids
    include the walk-forward report id plus every split's comparison
    run id; score delta sample count matches OOS row count;
    disagreement rate = 1.0 for the fixture; labels default to
    hypothesis below floor; negative floor rejected
  - Determinism: repeat analysis produces identical findings;
    `findings_stable_hash` deterministic and order-independent
  - Observational-only: no `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, or `yfinance` references; terminology
    check; module never mutates global feature flags; import-time
    exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`; source-level assertion that the module does
    not import `strategy.config`
- 715 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior untouched
- No feature flags enabled; module never imports `strategy.config`
- No broker credentials, HTTP calls, or historical validation paper
  account wiring
- The `validated` label indicates only that the underlying sample
  meets `STATS_SAMPLE_SIZE_FLOOR` — it is **not** a promotion signal
  and does not by itself justify advancing a feature past `disabled`
- Follow-up cards remain in Backlog: `t_phase4_pattern_discovery`,
  `t_phase4_feature_importance`, `t_phase4_weight_recommender`,
  `t_phase4_learning_reports`, `t_phase4_validation`

---

## v0.17.0 — Phase 3: Feature Promotion Gates

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_promotion_gates`
**Commit:** `623b046`, plus validation board update

### Validation
- 656 tests passing (0 failing)
- Working tree clean prior to validation commit
- Default `PromotionEntry` reflects the disabled flag; the promotion
  machinery cannot silently claim more progress than the code state
- `report_id` and `stable_hash` independent of `generated_at`
- Standard rollback criteria trigger under worst-case metrics and pass
  under safe metrics
- Missing metrics surface as warnings rather than looking like passes
- Approved / production states without any `ApprovalRecord` emit a
  warning
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/promotion_gates.py`
- Global feature flags remain `all_disabled` after evaluation
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_promotion_gates` moved to Done
- **Phase 3 is complete — 7 / 7 cards Done**

### Added
- `strategy/promotion_gates.py` — read-only encoding of the Phase 3
  feature-flag promotion process
- Ordered promotion states:
  `disabled → backtest → walk_forward → paper_trading → candidate →
  approved → production`
- `REQUIRED_EVIDENCE_PER_STATE` maps each target state to the required
  evidence keys pulled from the ROADMAP "Feature Flag Promotion Process"
  table; `APPROVAL_EVIDENCE_KEY` is a virtual key satisfied by any
  `ApprovalRecord` on the entry
- `ApprovalRecord` frozen dataclass with mandatory approver, date,
  flag name, scope, monitoring, and rollback-plan fields
- `RollbackCriterion` frozen dataclass with `lt` / `le` / `gt` / `ge`
  / `eq` comparators; missing metrics never look like a pass
- `RollbackAlert` frozen dataclass carries `triggered`,
  `data_available`, and a human-readable detail
- `STANDARD_ROLLBACK_CRITERIA` mirrors the ROADMAP "Failure /
  Rollback Criteria" list (expectancy delta, drawdown delta, profit
  factor delta, trade frequency, concentration, data quality,
  reproducibility)
- `PromotionEntry` mutable dataclass carries the current state (default
  `disabled`), recorded evidence dict, approvals list, and free-form
  notes; validates flag_name and refuses approvals that name a
  different flag
- `evaluate_promotion(entry, metrics, rollback_criteria, generated_at)`
  returns a `PromotionReport` with current state, next state, required
  evidence, missing evidence, approval dicts, per-criterion rollback
  alerts, warnings, and `generated_at`
- `PromotionReport.stable_hash` excludes `generated_at`; `report_id`
  derived from a deterministic hash of flag, current/target state,
  evidence keys, approval count, metric keys, and criterion names
- `PromotionReport.to_markdown` renders `# Promotion Report`,
  `## Current State`, `## Required Evidence for Next Transition`
  (marked ✓/✗), `## Approvals`, `## Rollback Checks` (marked `!`/`·`),
  and an optional `## Warnings` section
- `next_state` / `state_index` / `is_terminal_state` helpers and a
  `triggered_alerts` convenience for filtering serialized alerts

### Tests
- `tests/test_promotion_gates.py` — 57 tests covering:
  - Promotion state ordering; `state_index`; `next_state` progression;
    terminal state; rejection of unknown states
  - `REQUIRED_EVIDENCE_PER_STATE` covers every state; disabled requires
    no evidence; approved and production require the approval-record
    virtual key
  - `ApprovalRecord` roundtrip and validation errors for every required
    field
  - `RollbackCriterion` for every comparator; missing metric → non-
    triggered alert with `data_available=False`; invalid comparator
    rejected at construction; empty name rejected; standard criteria
    inventory present
  - `PromotionEntry` defaults to disabled, rejects unknown states,
    rejects mismatched approval flag names, roundtrips through
    `to_dict`
  - `evaluate_promotion`:
    - Disabled entry lists backtest requirements without emitting a
      missing-evidence warning
    - Backtest entry with all evidence flags no missing keys
    - Candidate entry without approval flags approval_record missing
    - Approved state without any ApprovalRecord emits a warning
    - Terminal (Production) has no next state and no required evidence
    - Standard rollback criteria trigger correctly with worst-case
      metrics
    - Standard rollback criteria pass with safe metrics
    - Missing metrics produce warnings and never look like a pass
    - Custom criteria override the standard set
    - `report_id` is deterministic across `generated_at`
    - `report_id` changes when the evidence set changes
  - `PromotionReport`:
    - `stable_hash` excludes `generated_at`
    - `to_json` schema covers every documented key
    - Markdown contains all required sections and the flag name
    - Missing evidence marked `✗`; satisfied evidence marked `✓`
  - Observational-only:
    - No `alpaca`, `place_order`, `submit_order`, `TradingClient`,
      `api_key`, or `yfinance` references
    - Terminology check: only the one explanatory sentence containing
      `training` (inside quotes) is present
    - `evaluate_promotion` never mutates global feature flags
    - Import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
      `telegram_approvals`
    - Default `PromotionEntry` reflects the disabled flag default so
      the promotion machinery cannot silently claim more progress than
      the code state supports
- 656 passing total (0 failures)

### Notes
- Module is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior are untouched
- No feature flags enabled; `strategy/config.py` untouched
- No credentials, env plumbing, or historical validation paper account
  wiring
- Approvals are data artifacts — this module never constructs one
  autonomously; callers build them from repository documentation or
  operations records before passing them in
- Phase 3 backlog is now empty pending Hermes validation of this card

---

## v0.16.0 — Phase 3: Research Report Generation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_reports`
**Commit:** `a2f1940`, plus validation board update

### Validation
- 599 tests passing (0 failing)
- Working tree clean prior to validation commit
- `report_id` and `stable_hash` independent of `generated_at` for both
  comparison and walk-forward reports
- Repeat renders with the same `generated_at` produce byte-identical
  `to_json`, `to_markdown`, `disagreements_json`, and `manifest_json`
- Manifest carries source id + source hash for reproducibility
- `write` persists all four files (`report.md`, `report.json`,
  `disagreements.json`, `manifest.json`), is idempotent, and lands
  under `<output_dir>/<report_id>/`
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/research_reports.py`
- Global feature flags remain `all_disabled` after render + write
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_reports` moved to Done
- Card `t_phase3_promotion_gates` unblocked and moved to Ready

### Added
- `strategy/research_reports.py` — read-only research report renderers
- `ResearchReport` bundle carrying Markdown + JSON payload + flattened
  disagreements + reproducibility manifest
- `ResearchReportPaths` with deterministic file layout:
  `<output_dir>/<report_id>/report.md`,
  `<output_dir>/<report_id>/report.json`,
  `<output_dir>/<report_id>/disagreements.json`,
  `<output_dir>/<report_id>/manifest.json`
- `render_comparison_report` renders any
  `ChampionChallengerComparison`; produces daily-summary rows keyed by
  event timestamp with per-kind disagreement counts, and groups
  disagreements by kind for the Markdown body
- `render_walk_forward_report` renders any `WalkForwardReport`;
  produces per-split rows with IS/OOS windows and per-kind
  disagreement counts, and annotates disagreements with `split_id` /
  `split_index`
- `report_id` derived from the source `stable_hash`, so identical
  source objects always yield the same `report_id`
- `ResearchReport.stable_hash()` excludes `generated_at`; walk-forward
  payload strips nested `generated_at` fields for reproducibility
- `ResearchReport.write(output_dir)` persists all four files via
  `pathlib`; parent directory is created if missing and is idempotent

### Tests
- `tests/test_research_reports.py` — 32 tests covering:
  - `ResearchReport` rejects unknown kinds
  - `KNOWN_REPORT_KINDS` covers comparison and walk-forward
  - `ResearchReportPaths` derives all four artifact paths from
    `output_dir + report_id`
  - Comparison report: `rr_` prefix, custom title, JSON payload
    schema, disagreement counts match records, disagreements sorted
    deterministically, daily summary row per event, manifest carries
    reproducibility metadata, Markdown contains all required sections,
    Markdown flags the no-events case, data-quality notes include
    warnings
  - Walk-forward report: `rr_` prefix, JSON schema, disagreements
    carry split metadata, disagreements sorted by
    (split_index, timestamp, event_type, symbol, kind), Markdown key
    sections, no-splits case flagged, manifest carries source id +
    hashes, payload strips nested `generated_at` at every level
  - Determinism: `stable_hash` and `report_id` independent of
    `generated_at` for both comparison and walk-forward; repeat renders
    produce byte-identical `to_json`, `to_markdown`,
    `disagreements_json`, and `manifest_json`
  - Write: persists all four files; is idempotent; places files under
    `<output_dir>/<report_id>/`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Terminology check: only the one explanatory sentence containing
    `training` (inside quotes) is present
  - Import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`
  - Global feature flags remain `all_disabled` after render + write
- 599 passing total (0 failures)

### Notes
- Renderers are behavior-neutral: production Champion path is
  unchanged; runner, scheduler, Telegram, CLI, and plugin behavior are
  untouched
- No feature flags enabled; `strategy/config.py` untouched
- No `yfinance`, HTTP, or broker credentials touched
- Historical validation paper account remains documentation-only
- Promotion gates (`t_phase3_promotion_gates`) remain in Backlog

---

## v0.15.0 — Phase 3: Walk-Forward Evaluation Pipeline

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_walk_forward`
**Commit:** `01089d3`, plus validation board update

### Validation
- 567 tests passing (0 failing)
- Working tree clean prior to validation commit
- Leakage prevention verified: in-sample events never reach the
  harness (only OOS events populate `ComparisonHarness.run`)
- Split boundaries strictly non-overlapping (`out_of_sample_start >
  in_sample_end`) enforced at construction time
- Determinism verified: `report_id` and `stable_hash` independent of
  `generated_at`, including nested comparison metadata
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in
  `strategy/walk_forward.py`
- Global feature flags remain `all_disabled` after pipeline runs
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_walk_forward` moved to Done
- Card `t_phase3_reports` unblocked and moved to Ready

### Added
- `strategy/walk_forward.py` — read-only walk-forward orchestration for
  the Champion/Challenger comparison harness
- `WalkForwardSplit` frozen dataclass with in-sample / out-of-sample
  windows; constructor enforces `out_of_sample_start` strictly after
  `in_sample_end`
- `WalkForwardSchedule` frozen dataclass with deterministic
  `stable_hash` and enforced strictly time-ordered splits
- `generate_walk_forward_schedule` builds splits from calendar
  parameters (start/end date, in-sample days, out-of-sample days,
  step days); returns an empty tuple when the window does not fit
- `WalkForwardSplitResult` captures per-split event counts, dropped
  in-sample count, harness comparison output, and disagreement counts
  by kind
- `WalkForwardReport` aggregates per-split results, totals disagreements
  by kind across splits, and records unassigned events; `stable_hash`
  strips `generated_at` from the report and from every nested
  comparison metadata
- `WalkForwardPipeline` groups events by an OOS-date lookup and
  forwards only OOS events to a fresh `ComparisonHarness` per split;
  in-sample events are counted for reporting but never reach the
  harness
- `report_id` derived from schedule hash + champion/challenger ids +
  dataset id + seed + event count + score-delta threshold
- Uses `in-sample` / `out-of-sample` terminology; never "training"

### Tests
- `tests/test_walk_forward.py` — 35 tests covering:
  - `WalkForwardSplit` roundtrip, `contains_in_sample` /
    `contains_out_of_sample`, and every constructor validation error
    including the strictly-after-IS rule
  - Schedule generator: basic layout, no overlap within a split,
    time-ordered splits, end-date boundary, empty schedule when window
    too small, determinism, and every parameter validation error
  - `WalkForwardSchedule.stable_hash` determinism and sensitivity to
    config changes; rejection of out-of-order splits at construction
  - Pipeline leakage: in-sample events counted as `dropped_event_count`
    and never reach the harness; events outside every window recorded
    as `total_unassigned_events` with a warning
  - Per-split aggregation: `ranking_only` and `score_delta` counts
    partition correctly across splits; totals sum across splits;
    `total_out_of_sample_events` matches OOS events
  - `report_id` stable across `generated_at`; changes with dataset id;
    `stable_hash` independent of `generated_at` at every nesting level
  - Report serialization roundtrips via `to_json`
  - Pipeline construction rejects negative threshold and exposes
    champion/challenger ids
  - RS Challenger integration: disabled overlay yields zero
    disagreements; global feature flags remain `all_disabled`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Terminology check: only the one explanatory sentence containing
    `training` (inside quotes) is present; no other `training` usage
  - Import-time exclusion of `trader_cli`, `trader`, `crypto_trader`,
    `telegram_approvals`
  - Global feature flags remain `all_disabled` after pipeline runs
- 567 passing total (0 failures)

### Changed
- ROADMAP `t_phase3_walk_forward` scope phrasing updated from
  "train/evaluate splits" to "in-sample / out-of-sample splits" for
  consistency with the terminology rule.

### Notes
- Pipeline is behavior-neutral: production Champion path is unchanged;
  runner, scheduler, Telegram, CLI, and plugin behavior are untouched
- No feature flags enabled globally; RS Challenger integration uses a
  locally-scoped `FeatureFlags` instance
- No `yfinance`, HTTP, or broker credentials touched
- Historical validation paper account remains documentation-only
- Reports (`t_phase3_reports`) and promotion gates
  (`t_phase3_promotion_gates`) remain in Backlog

---

## v0.14.0 — Phase 3: Relative Strength Challenger Overlay

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_rs_challenger`
**Commit:** `4569bce`, plus validation board update

### Validation
- 532 tests passing (0 failing)
- Working tree clean prior to validation commit
- Overlay confirmed disabled by default; global `FeatureFlags`
  singleton remains `all_disabled` after harness runs
- Champion parity verified when disabled: identical scores, rankings,
  and explanations; harness records zero disagreements
- Enabled overlay produces the expected additive contribution and
  rerank (verified against
  `weight * (rs - neutral) / range = 0.20 * ±0.4/0.8`)
- Determinism verified across repeat evaluations and across
  `generated_at` differences (harness `stable_hash` stable)
- No `alpaca`, `yfinance`, `TradingClient`, `api_key`, `place_order`,
  `submit_order`, or order-path references in `strategy/rs_challenger.py`
- Runner, scheduler, Telegram, CLI, and plugin behavior unchanged
- Historical validation paper account remains documentation-only
- Card `t_phase3_rs_challenger` moved to Done
- Card `t_phase3_walk_forward` unblocked and moved to Ready

### Added
- `strategy/rs_challenger.py` — read-only, disabled-by-default RS
  overlay for Champion/Challenger comparison
- `RelativeStrengthChallenger` wraps any `ComparisonEvaluator` and
  applies an additive score contribution
  `weight * (rs - neutral) / range` when
  `FeatureFlags.enable_relative_strength` is `True`
- `RelativeStrengthProvider` protocol for caller-supplied RS lookups
  keyed by symbol and event; providers return `None` when RS data is
  unavailable
- `rs_provider_from_map` builds a deterministic provider from a
  `{timestamp: {symbol: rs}}` snapshot (defensively copied) suitable for
  the Data Catalog and unit tests
- `RS_CHALLENGER_STRATEGY_ID` / `RS_CHALLENGER_FLAG_NAME` /
  `DEFAULT_RS_OVERLAY_WEIGHT` / `DEFAULT_RS_NEUTRAL_SCORE` /
  `DEFAULT_RS_SCORE_RANGE` constants

### Behavior
- Disabled by default — global `FeatureFlags` singleton is never
  mutated; enablement is done per-instance by passing a local
  `FeatureFlags(enable_relative_strength=True)` to the constructor
- When disabled the wrapper is Champion-parity: scores, rankings, and
  explanations are byte-identical to the base evaluator (only
  `strategy_id` differs); the harness records zero disagreements
- When enabled: scores get an additive contribution, rankings are
  recomputed by new score desc with symbol-asc tie-break, per-symbol
  explanations note the base and RS contribution
- RS values outside `[neutral - range, neutral + range]` are clamped
- Missing RS values keep the base score and add a single aggregated
  warning; provider exceptions are captured per-symbol as warnings
- Symbols with a base score but no base ranking are rescored but stay
  unranked

### Tests
- `tests/test_rs_challenger.py` — 32 tests covering:
  - Constructor rejects negative weight, non-positive range, empty
    `strategy_id`; defaults match module constants; conforms to
    `ComparisonEvaluator` protocol
  - `is_enabled` reflects the flag; global singleton stays disabled
  - Champion parity when disabled: identical scores, rankings, and
    explanations; harness records zero disagreements; result is a deep
    copy of the base evaluation
  - Symmetric contribution formula for both positive and negative RS
    deltas
  - Neutral RS produces zero contribution
  - Out-of-range RS is clamped
  - Custom weight scales contribution linearly
  - Re-rank by new scores desc; symbol-asc tie-break
  - Explanations include base note, RS score, and signed contribution
  - Symbols in base scores but not base rankings stay unranked
  - Harness surfaces `ranking_only` and `score_delta` disagreements
    correctly when enabled
  - Missing RS symbol keeps base score with an aggregated warning
  - Empty snapshot marks every symbol
  - Provider exception recorded as a warning; base score preserved
  - Missing-RS symbol with preserved rank/score triggers no harness
    disagreement; `data_unavailable` remains reserved for one-sided
    symbols
  - `rs_provider_from_map` returns values for known keys, `None` for
    unknown timestamp/symbol, defensively copies its input
  - Repeat runs produce identical evaluations; harness `stable_hash` is
    independent of `generated_at`
  - Source-level ban on `alpaca`, `place_order`, `submit_order`,
    `TradingClient`, `api_key`, and `yfinance`
  - Importing `strategy.rs_challenger` does not pull in `trader_cli`,
    `trader`, `crypto_trader`, or `telegram_approvals`
  - Global feature flags remain `all_disabled` after a run
- 532 passing total (0 failures)

### Notes
- Overlay is behavior-neutral: production Champion path is unchanged;
  the runner (`strategy/runner.py`), scheduler, Telegram approval flow,
  CLI, and plugin behavior are untouched
- No feature flags are enabled globally; local `FeatureFlags` instances
  in tests do not mutate the singleton
- RS data source is caller-supplied — no `yfinance` calls, no HTTP,
  no broker credentials, no historical validation paper account wiring
- Walk-forward pipeline (`t_phase3_walk_forward`), reports
  (`t_phase3_reports`), and promotion gates (`t_phase3_promotion_gates`)
  remain in Backlog

---

## v0.13.0 — Phase 3: Champion/Challenger Comparison Harness

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_champion_challenger`
**Commit:** `1b341ef`, plus policy addendum `a0fa765` and validation board update

### Validation
- 500 tests passing (0 failing)
- Working tree clean prior to validation commit
- Harness confirmed read-only and deterministic (verified `stable_hash`
  and `run_id` independent of `generated_at`; repeated runs produce
  byte-identical `to_dict()` output)
- No references to `alpaca`, `place_order`, `submit_order`, or
  `TradingClient` in `strategy/comparison_harness.py`
- No credentials, env vars, SDK calls, or account wiring added
- Historical validation paper account remains documentation-only (no
  code path in the harness reads from it)
- Terminology audit passed: docs refer to validation, replay, or
  research — no "training" usage
- Feature flags remain `all_disabled` after harness runs; runner,
  scheduler, Telegram, CLI, and plugin behavior unchanged
- Card `t_phase3_champion_challenger` moved to Done
- Card `t_phase3_rs_challenger` unblocked and moved to Ready

### Added
- `strategy/comparison_harness.py` — read-only Champion/Challenger comparison
  harness built on top of the Backtest Lab foundation
- `ComparisonEvaluator` runtime protocol satisfied by the existing
  `NoOpStrategyAdapter`
- `ScoreRow` and `ScoreTable` per-event alignment of Champion and
  Challenger scores, ranks, selection status, score deltas, and rank
  deltas
- `DisagreementRecord` classifying divergences as `ranking_only`,
  `entry_selection`, `score_delta`, or `data_unavailable`
- `ChampionChallengerRunMetadata` with a deterministic `run_id` derived
  from champion/challenger ids, dataset id, event signature, seed, and
  threshold
- `ChampionChallengerComparison` container with `to_dict`, `to_json`,
  `stable_hash` (ignores `generated_at`), and
  `disagreements_by_kind()`
- Score-delta threshold controls score-only disagreement sensitivity
- Rows sorted by symbol; disagreements sorted by
  `(event_timestamp, event_type, symbol, kind)` for deterministic output
- `CHAMPION_ROLE` / `CHALLENGER_ROLE` aliases reuse
  `strategy.config.CHAMPION_NAME` / `CHALLENGER_NAME`

### Changed
- `strategy/data_catalog.py` — dropped unused `stable_json` import
  (non-blocking lint noted during validation of `t_phase3_data_catalog`)

### Tests
- `tests/test_comparison_harness.py` — 27 tests covering:
  - `ScoreRow`, `ScoreTable`, and `DisagreementRecord` serialization
  - Rejection of unknown disagreement kinds
  - Role aliases (`CHAMPION_ROLE`, `CHALLENGER_ROLE`)
  - `NoOpStrategyAdapter` satisfies `ComparisonEvaluator`
  - Harness rejects negative thresholds and identical strategy ids
  - No-op run produces empty tables and no disagreements
  - Identical evaluations produce no disagreements
  - Explicit classification tests for `ranking_only`, `entry_selection`,
    `score_delta`, and `data_unavailable`
  - Score-delta threshold suppresses under-threshold differences
  - Disagreements sorted deterministically across multiple events
  - Score-table rows sorted alphabetically by symbol
  - Score tables capture event type, sequence, and per-side warnings
  - Run id derived from inputs; `stable_hash` independent of
    `generated_at`; run id changes with dataset id
  - `to_json` produces byte-identical output across runs (excluding
    `generated_at`)
  - Full result is JSON-serializable
  - `disagreements_by_kind()` partitions records across known kinds
  - Module source has no `alpaca` / `place_order` / `submit_order` /
    `TradingClient` references
  - Feature flags remain `all_disabled` after harness runs
  - Importing the harness module does not pull in `trader_cli`, `trader`,
    `crypto_trader`, or `telegram_approvals`
- 500 passing total (0 failures)

### Notes
- Harness foundation only — no Relative Strength Challenger implementation
- Runner, scheduler, Telegram, CLI, plugin, and order-path behavior
  unchanged
- No feature flags enabled; `strategy/config.py` untouched
- Relative Strength Challenger (`t_phase3_rs_challenger`) and
  Walk-Forward Pipeline (`t_phase3_walk_forward`) remain in Backlog
- A separate Alpaca paper account is documented as a **future**
  validation data source (see ROADMAP "Validation Paper Account"). This
  card does not integrate it — no credentials, SDK calls, env plumbing,
  or runner wiring were added. Isolation rules: must remain separate
  from the live and normal paper accounts, must never be used by the
  live runner, must not share credentials with production, and is
  reserved for historical replay, walk-forward validation, and
  Champion/Challenger comparison only. Referred to as validation,
  replay, or research — never "training."

---

## v0.12.0 — Phase 3: Research Data Catalog

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Card:** `t_phase3_data_catalog`
**Commit:** `0410aa2`, plus validation board update

### Validation
- 473 tests passing (0 failing)
- Working tree clean prior to validation commit
- Confirmed catalog is read-only (no file/manifest mutation on validate)
- Confirmed catalog is deterministic (sorted listings, stable manifest hashes)
- Confirmed reproducibility metadata is stable across repeated calls
- Confirmed `stable_hash` is independent of `imported_at`
- Confirmed no references to `alpaca`, `place_order`, `submit_order`, or
  `TradingClient` in `strategy/data_catalog.py`
- Confirmed feature flags remain `all_disabled` after catalog operations
- No trading behavior, buy/sell logic, runner behavior, or feature flags
  changed
- Card `t_phase3_data_catalog` moved to Done
- Card `t_phase3_champion_challenger` unblocked and moved to Ready

### Added
- `strategy/data_catalog.py` — read-only Research Data Catalog
- `DatasetFile`, `DatasetManifest`, and `DatasetValidationResult` dataclasses
  with structural validation and stable JSON serialization
- `DataCatalog.from_directory()` loads immutable manifests from
  `research_data/manifests/` and rejects duplicate dataset ids
- `DataCatalog.list()` / `get()` / `has()` for read-only lookup with kind
  filtering across `historical_bars`, `benchmark`, `paper_log`, and
  `research_context`
- `DataCatalog.validate()` verifies file existence, size, SHA-256, and
  CSV / JSON schema against the manifest and never mutates the dataset
- `DataCatalog.validate_all()` produces one result per registered dataset
- `DataCatalog.checksum_file()` recomputes the on-disk SHA-256 for a
  referenced file
- `DataCatalog.reproducibility_metadata()` returns manifest hash, kind,
  source, imported timestamp, and per-file checksums for run manifests
- `build_dataset_manifest()` operator helper computes checksums offline
- `sha256_file()` streaming hash utility
- Manifest `stable_hash()` ignores `imported_at` for deterministic run ids

### Tests
- `tests/test_data_catalog.py` — 45 tests covering:
  - Dataset kind constants
  - `DatasetFile` roundtrip, tuple coercion, and validation errors
  - `DatasetManifest` roundtrip, deterministic hashing, validation errors,
    and duplicate-file-path rejection
  - `build_dataset_manifest` computes real checksums and rejects missing files
  - `sha256_file` matches `hashlib` for identical bytes
  - Catalog loading (sorted, filtered, missing dir, duplicate id rejection)
  - Validation success, missing file, checksum mismatch, size mismatch,
    CSV schema mismatch, JSON schema success, JSON schema missing key,
    unspecified-schema warning, and `validate_all` result ordering
  - `validate` and `validate_all` do not mutate manifest or data bytes
  - `checksum_file` matches on-disk digest and rejects unregistered paths
  - `reproducibility_metadata` contains manifest hash, kind, source,
    imported timestamp, symbols/benchmarks, and file checksums; is
    deterministic across calls
  - Observational-only guarantees: no `alpaca`, `place_order`, or
    `TradingClient` references, and feature flags remain all disabled
- 473 passing total (0 failures)

### Notes
- Registry is strictly read-only — no dataset files or manifests are
  written by the catalog itself
- Operator helper `build_dataset_manifest` is offline and not called by
  catalog reads
- No live brokerage calls, order placement, buy/sell logic, runner
  behavior, or feature flags changed
- `research_data/` remains untracked in this commit; manifests are
  produced offline before being checked in

---

## v0.11.0 — Phase 3: Backtest Lab Foundation

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 968b5f5, plus validation board update

### Added
- `strategy/backtest_lab.py` — Backtest Lab foundation data contracts
- Deterministic `BacktestConfig` with stable hashing and validation
- `BacktestRunMetadata`, `BacktestArtifactPaths`, and `BacktestRunManifest`
  for reproducible run identity and artifact locations
- `BacktestStrategyResult` and `BacktestReport` shells for future
  Champion/Challenger result reporting
- `BacktestEvent`, `DeterministicReplayClock`, `StrategyEvaluation`, and
  `NoOpStrategyAdapter` fixtures for deterministic replay scaffolding
- Stable JSON/hash helpers for deterministic experiment metadata
- Empty report factory for future dry-run backtest workflows

### Tests
- `tests/test_backtest_lab.py` — 27 tests covering:
  - Stable JSON and hash determinism
  - Config serialization and validation errors
  - Run metadata and timestamp-independent run ids
  - Artifact path generation
  - Manifest serialization and stable hashes
  - Deterministic replay event ordering
  - No-op strategy adapter evaluation
  - Strategy result and report serialization
  - Markdown report shell output
  - Observational-only guarantees
- 428 passing total (0 failures)

### Notes
- Foundation only — no historical replay engine yet
- No Relative Strength Challenger implementation
- No live brokerage calls, order placement, buy/sell logic, runner behavior, or feature flags changed
- Card `t_phase3_backtest_lab` validated and moved to Done
- Card `t_phase3_data_catalog` unblocked and moved to Ready

---

## v0.10.0 — Sprint 10: Market Breadth Analysis

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 5bcbf83

### Added
- `strategy/market_breadth.py` — observational market breadth tracker
- Watchlist participation analysis across 20-day and 50-day moving averages
- Advancer, decliner, unchanged, new-high, and new-low counts
- Aggregate breadth score and breadth regime classification
- `BreadthSymbolObservation` and `MarketBreadthReport` structured outputs with JSON-ready serialization
- Market breadth summaries exposed through the read-only research platform
- Disabled-by-default `enable_market_breadth` feature flag inventory

### Tests
- `tests/test_market_breadth.py` — 27 tests covering:
  - Moving average, period return, latest close, percentage, and A/D ratio helpers
  - New high and new low edge cases
  - Breadth score and regime classification
  - Report/result serialization
  - Missing data, invalid close, insufficient history, and provider failures
  - Mocked price provider behavior
  - Observational-only guarantees
- `tests/test_research_platform.py` — 3 new tests plus snapshot coverage updates covering:
  - Market breadth data contract defaults
  - Snapshot JSON compatibility
  - Mocked research platform market breadth handoff
  - Failure fallback behavior
- 401 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, runner behavior, or enabled feature flags changed
- Card `t_5ec6406e` validated and moved to Done
- Phase 2 Market Intelligence implementation completed

---

## v0.9.0 — Sprint 9: Sector Leadership Tracking

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** 465faeb

### Added
- `strategy/sector_leadership.py` — observational sector leadership tracker
- Sector ETF leadership ranking against SPY across 5-day, 20-day, and 60-day windows
- `SectorLeadershipResult` and `SectorLeadershipReport` structured outputs with JSON-ready serialization
- Strongest/weakest sector summaries exposed through the read-only research platform
- Disabled-by-default `enable_sector_leadership` feature flag inventory

### Tests
- `tests/test_sector_leadership.py` — 20 tests covering:
  - Period return calculations
  - Leadership scoring and trend classification
  - Report/result serialization
  - Missing sector data and missing benchmark handling
  - Invalid/zero price handling
  - Mocked price provider behavior
  - Observational-only guarantees
- `tests/test_research_platform.py` — 3 new tests plus snapshot coverage updates covering:
  - Sector leadership data contract defaults
  - Snapshot JSON compatibility
  - Mocked research platform sector leadership handoff
  - Failure fallback behavior
- 371 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, runner behavior, or enabled feature flags changed
- Card `t_c0ab3a10` validated and moved to Done
- Card `t_5ec6406e` unblocked and moved to Ready

---

## v0.8.0 — Sprint 8: Enhanced Daily Digest + Trade Metadata

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** c60f807

### Added
- `DigestMetadataSummary` for observational trade metadata aggregation
- Daily Digest metadata sections for symbols, exit reasons, market context,
  active flags, entry scores, custom metadata, and RS snapshots
- Optional `metadata` payloads on `TradeLogger.log_trade_entry()` and
  `TradeLogger.log_trade_exit()`
- SQLite migration for nullable `trade_metadata_entry` and
  `trade_metadata_exit` columns
- JSONL export parsing for RS snapshots and custom metadata payloads
- `rs_data` passthrough in `DailyDigestService.generate_and_deliver()`

### Changed
- Digest stats now preserve the requested report date
- Trade details render both legacy digest keys and TradeLogger row keys

### Tests
- 9 new/updated tests covering:
  - Metadata summary aggregation
  - Malformed metadata handling
  - Digest metadata and RS snapshot rendering
  - TradeLogger metadata storage and export
  - Existing database migration
  - Service RS data passthrough
- 348 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No buy/sell logic, scheduler behavior, or feature flags changed
- Card `t_194b8638` validated and moved to Done

---

## v0.7.0 — Sprint 7: Morning Intelligence Agent

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest
**Status:** Done
**Commit:** c1ed907

### Added
- `strategy/morning_intelligence.py` — Observational pre-market briefing agent
- Composes existing market regime, overnight risk, and relative strength data
- `MorningIntelligenceReport` structured output with JSON serialization
- `MorningIntelligenceRenderer` Markdown output for review/delivery workflows
- Provider injection for deterministic tests and future scheduler integration
- Feature flag recording: `enable_morning_intelligence` remains disabled by default

### Tests
- `tests/test_morning_intelligence.py` — 19 tests covering:
  - Report serialization
  - Markdown rendering
  - Mocked provider aggregation
  - Feature flag recording
  - Empty watchlist and provider failure handling
  - Observational-only guarantees
- 339 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- No scheduler, Telegram, buy/sell, ranking, or strategy behavior changes
- Card `t_909faeab` validated and moved to Done

---

## v0.6.0 — Sprint 6: Overnight Risk Engine

**Date:** 2026-07-02
**Branch:** sprint-3/daily-digest

### Added
- `strategy/overnight_risk.py` — Overnight gap risk analysis engine
- Measures overnight market risk by comparing previous close to
  pre-market/after-hours prices
- Purely observational — produces NO trading signals, places NO trades,
  and does NOT influence strategy scoring
- Per-symbol observations: previous_close, current_price, overnight_gap_pct,
  gap_direction, significant_gap, risk_score, data_quality
- Aggregate report: symbols_analyzed, significant_gaps, avg_risk_score,
  max_gap_up, max_gap_down
- Gap calculation helpers: calculate_gap_pct, determine_gap_direction,
  is_significant_gap, compute_risk_score
- PriceFetcher with yfinance integration and graceful error handling
- Feature flag: `enable_overnight_risk_engine` (disabled by default)
- Runner stubs in `_run_challenger` and `_challenger_sell` (observational only)

### Changed
- Removed dead imports (`pandas`, `OVERNIGHT_RISK_THRESHOLD`,
  `OVERNIGHT_EVAL_START_HOUR`, `OVERNIGHT_EVAL_START_MINUTE`) from
  `strategy/overnight_risk.py`
- Documented threshold design: engine uses `DEFAULT_GAP_THRESHOLD` (2.0 %)
  as the natural unit; config risk-score threshold reserved for Phase 3

### Tests
- `tests/test_overnight_risk.py` — 78 tests covering:
  - OvernightGapObservation and OvernightRiskReport dataclasses
  - Gap calculation helpers (calculate_gap_pct, determine_gap_direction,
    is_significant_gap, compute_risk_score)
  - PriceFetcher (mocked yfinance)
  - OvernightRiskEngine (assess, assess_symbol, get_summary)
  - get_engine convenience function
  - Feature flag integration
  - Observational-only guarantees (no trading methods, no signals)
  - Edge cases (missing data, zero/invalid close, extreme gaps, single price)
- 320 passing total (0 failures)

### Notes
- Observational only — zero impact on trading decisions
- Gap threshold default is wired through `strategy.config`
- Timing/risk-score constants defined for future scheduler integration
- Engine can be wired into Phase 3 Decision Engine as a sell signal source

---

## v0.5.0 — Sprint 5: Market Regime Classification

**Date:** 2026-07-02
**Commit:** b785a35
**Branch:** sprint-3/daily-digest

### Added
- `strategy/market_regime.py` — Market regime classification engine
- Classifies market as bullish/bearish/volatile/neutral
- SPY/QQQ analysis: price vs MA, ATR volatility, ADX trend strength, MACD momentum
- 8 signals across 2 benchmarks with weighted scoring
- 43 new tests for indicators, signals, and classification

### Tests
- 151 passing (0 failures)

### Notes
- Observational only — no impact on trading decisions
- Feature flag: `enable_market_regime` (disabled by default)
- Classification uses 20-day and 50-day MAs, 14-day ATR/ADX
- Foundation for Phase 3 Decision Engine regime-based scoring

---

## v0.4.0 — Sprint 4: Relative Strength Analysis

**Date:** 2026-07-02
**Commit:** 74dc06c
**Branch:** sprint-3/daily-digest

### Added
- `strategy/relative_strength.py` — Relative strength comparison against SPY/QQQ benchmark
- Relative strength analysis in daily digest
- Tests for relative strength calculations

### Tests
- 108 passing (0 failures)

### Notes
- Observational only — no impact on trading decisions
- Compares candidate performance against market benchmarks
- Foundation for Phase 3 Decision Engine

---

## v0.3.0 — Sprint 3: Daily Performance Digest

**Date:** 2026-07-01
**Commit:** 49f935f
**Branch:** sprint-3/daily-digest

### Added
- `strategy/daily_digest.py` — Full digest pipeline (Builder, Renderer, Saver, Service)
- `strategy/trade_logger.py` — `get_day_trades(date)` method
- `scripts/generate_daily_digest.py` — Standalone CLI with `--date` and `--dry-run`
- `tests/test_daily_digest.py` — 23 tests

### Changed
- Daily digest now includes relative performance metrics

### Tests
- 70 passing (0 failures)

### Notes
- Zero impact on trading decisions
- Pure observability feature
- Digests saved to `reports/` directory

---

## v0.2.0 — Sprint 2: Historical Trade Logger

**Date:** 2026-07-01
**Commit:** 70790f9
**Branch:** sprint-2/historical-statistics

### Added
- Persistent trade history logging
- Historical statistics calculation
- Performance metrics tracking (win rate, profit factor, drawdown)
- Database schema for trade records

### Tests
- 47 passing (0 failures)

### Notes
- Trades logged with entry/exit details, indicators, and timestamps
- Foundation for all future performance analysis

---

## v0.1.0 — Sprint 1: Feature Flag and Champion/Challenger

**Date:** 2026-07-01
**Commit:** 26eca28
**Branch:** sprint-1/feature-flag-framework

### Added
- Feature flag system with runtime enable/disable
- Champion/Challenger strategy architecture
- `strategy/config.py` — Feature flag configuration
- `strategy/runner.py` — Strategy runner with flag awareness
- Initial test suite

### Changed
- Strategy selection now respects feature flags
- Champion strategy remains production default

### Tests
- 4 passing (0 failures)

### Notes
- All flags disabled by default
- Champion strategy unchanged
- Foundation for all future incremental improvements

---

## v0.0.1 — Initial Release

**Date:** 2026-07-01
**Commit:** 194bf1a
**Branch:** main

### Added
- Paper trading system with Alpaca integration
- Technical indicator scanning (RSI, Bollinger Bands, MACD)
- Watchlist management
- Telegram integration for approvals and alerts
- Market screener with candidate ranking

### Notes
- Initial paper trading system
- Day-trading focused
- Six-gate buy rules, single-indicator exits
