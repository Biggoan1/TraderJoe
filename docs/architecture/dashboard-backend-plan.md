# Dashboard Backend API Plan

**Status:** PLANNED (docs only — no implementation)

**Card:** `t_dashboard_backend_plan`

**Scope:** Design of the read-only HTTP API the dashboard front end
will consume.  This document does NOT propose implementing the
front end.  It does NOT propose implementing the backend either —
that lands in follow-up cards under Phase 5.5 (Research & Learning
Dashboard) once these endpoints have been reviewed.

---

## Design constraints (inherited from Phase 5.5)

The dashboard backend is a **read-only** window onto the on-disk
artifact tree produced by:

- `research_data/` — Phase 3 catalog + Phase 5.6 warehouse manifests
- `market_data/` — Phase 5.6 warehouse (Parquet + metadata)
- `reports/validation/` — Phase 5 outputs (comparison, walk-forward,
  learning, analyst)
- `reports/warehouse/` — Phase 5.6 warehouse reports (validation,
  gap detection)
- `reports/run_manifests/` — Phase 5.6+ operator run manifests
- Kanban SQLite DB (`~/.hermes/kanban.db`) — read-only

Hard rules — no exceptions:

1. **No write endpoints.**  Every route is `GET`.  The dashboard
   never mutates the warehouse, catalog, manifests, or Kanban DB.
2. **No credential surface.**  Endpoints never return an API key,
   secret, or unredacted provider URL.  `llm_endpoint_redacted`
   from the run manifest is the only endpoint identity ever
   surfaced.
3. **No order surface.**  No route references `submit_order`,
   `place_order`, `TradingClient`, or any live-runner module.
4. **No feature-flag mutation.**  `FeatureFlags` is exposed
   read-only; toggling requires the operator to edit config and
   restart the process.
5. **No `PromotionEntry` mutation.**  Promotion routes return the
   `disabled` state and evidence blob; advancing state is out of
   scope for the dashboard.
6. **No `ApprovalRecord` construction anywhere in the backend
   code.**  Source-level tests enforce this on the backend module
   the same way they enforce it on every other Phase 5 / 5.6
   module.
7. **Terminology:** validation / replay / research / acquisition.
   Never "training."
8. **Deterministic output.**  Every JSON response is a stable
   projection of on-disk artifacts.  Two dashboards pointed at the
   same tree produce the same responses.
9. **CORS + auth are out of scope for this plan** — those are
   deployment concerns of Phase 5.5's follow-up implementation
   card.  The plan below focuses on shape, not middleware.

## Shape conventions

- All routes: `GET /api/v1/...`
- Response envelope:
  ```json
  {
    "data": { ... },
    "warnings": [ ... ],
    "generated_at": "2026-07-04T00:00:00+00:00"
  }
  ```
- Errors: HTTP 404 for missing artifact; 400 for malformed query
  string; body:
  ```json
  { "error": "not_found", "message": "..." }
  ```
- Pagination: `?limit=N&offset=M`; default limit 50, hard cap 500.
- Time-stamped resources include `generated_at` from the underlying
  artifact so front-end caches can freshness-check.

---

## Endpoints

### 1. Warehouse coverage

#### `GET /api/v1/warehouse/coverage`

Returns per-symbol × interval coverage summaries from the catalog.

Query params:
- `symbols=AAPL,MSFT,...` (default: all catalog symbols)
- `interval=1Day|1Hour|1Min|...` (default: `1Day`)
- `asset_class=equity|etf|crypto|option|future` (default: `equity`)

Response `data`:
```json
{
  "coverage": [
    {
      "symbol": "AAPL",
      "asset_class": "equity",
      "interval": "1Day",
      "datasets": [
        { "dataset_id": "watchlist-2020-2026",
          "version": "1",
          "start_date": "2020-01-02",
          "end_date": "2026-07-04",
          "validation_status": "validated",
          "row_count": 1638,
          "provider": "alpaca" }
      ],
      "gaps": []
    }
  ]
}
```

Backed by: `strategy.data_catalog.DataCatalog.find_coverage` +
`gaps`.

#### `GET /api/v1/warehouse/coverage/grid`

Compact heatmap of which (symbol, month) cells the warehouse
covers.  Frontend-friendly shape for a coverage grid widget.

---

### 2. Import jobs

#### `GET /api/v1/import-jobs`

Lists recent `import_bars` runs by inspecting
`<warehouse_root>/queue/*.db` and any persisted `ImportReport`
JSON files under `reports/warehouse/imports/`.

Response `data`:
```json
{
  "jobs": [
    {
      "dataset_id": "watchlist-2020-2026",
      "provider_name": "alpaca",
      "symbols_ok": ["AAPL","MSFT","NVDA","SPY","QQQ"],
      "symbols_empty": [],
      "total_bars": 8190,
      "started_at": "...",
      "completed_at": "...",
      "manifest_path": "market_data/manifests/watchlist-2020-2026.json"
    }
  ]
}
```

#### `GET /api/v1/import-jobs/{dataset_id}`

Full `ImportReport.to_dict()` for one job.  Truncates warnings to
the first N (default 100).

---

### 3. Validation runs

#### `GET /api/v1/validation-runs`

Lists recent runs by scanning `reports/run_manifests/**/*.json`.

Response `data`:
```json
{
  "runs": [
    {
      "run_id": "matrix-watchlist-60d-2026-07-04-...",
      "dataset_id": "watchlist-2020-2026",
      "window_start": "2026-05-05",
      "window_end": "2026-07-04",
      "provenance_source": "warehouse",
      "completed_at": "...",
      "promotion_state": "disabled",
      "artifact_paths": {
        "comparison": "reports/validation/.../rr_x",
        "walk_forward": "reports/validation/.../rr_y",
        "learning": "reports/validation/.../lr_z",
        "analyst": ["reports/validation/.../ra_1"]
      }
    }
  ]
}
```

#### `GET /api/v1/validation-runs/{run_id}`

Full `ResearchRunManifest.to_dict()`.

---

### 4. Latest reports

#### `GET /api/v1/reports/latest`

Returns pointers to the most recent artifact of each type across
the whole `reports/` tree.  Uses `reports/run_manifests/latest.json`
as the authoritative pointer produced by
`strategy.research_run_manifest.write`.

---

### 5. Run manifests

Already covered by `/api/v1/validation-runs/{run_id}`; this section
is intentionally left as a redirect to keep the plan concise.

---

### 6. Analyst narratives

#### `GET /api/v1/analyst/narratives`

Lists analyst narratives under `reports/validation/analyst/`.

Response `data`:
```json
{
  "narratives": [
    {
      "artifact_id": "ra_ee8e378294a9",
      "model": "qwen3.6-27b",
      "source_kind": "champion_challenger_comparison",
      "source_id": "cc_97400b6c2f9e",
      "generated_at": "...",
      "warnings_count": 0
    }
  ]
}
```

#### `GET /api/v1/analyst/narratives/{artifact_id}`

Returns the full narrative body + provenance.  Front end renders
the Markdown.

---

### 7. Promotion evidence

#### `GET /api/v1/promotion/evidence`

Read-only view of the most recent bundle's `PromotionEntry`:

```json
{
  "flag_name": "enable_relative_strength",
  "current_state": "disabled",
  "approvals": [],
  "evidence": {
    "dataset_provenance_id": "warehouse:watchlist-2020-2026@1",
    "dataset_id": "matrix-60d-2026-07-04",
    "backtest_report_id": "cc_x",
    "walk_forward_report_id": "wf_x",
    "learning_report_id": "lr_x",
    "analyst_report_0": "ra_x",
    "analyst_report_1": "ra_y",
    "analyst_report_2": "ra_z"
  }
}
```

Dashboard NEVER offers a "promote" button.  The DB layer refuses
writes at the module level.

---

### 8. Gap reports

#### `GET /api/v1/warehouse/gap-reports`

Lists recent gap reports under
`reports/warehouse/gaps/<run_id>/`.

#### `GET /api/v1/warehouse/gap-reports/{run_id}`

Full `GapReport.to_dict()`.

---

### 9. Data version lineage

#### `GET /api/v1/warehouse/lineage/{dataset_id}`

Returns the version chain via `DataCatalog.versions`.  Includes
`corporate_action_version`, `adjustment_version`, and revision
metadata.

#### `GET /api/v1/warehouse/version-diff/{parent}/{child}`

Returns `strategy.warehouse.versioning.VersionDiff.to_dict()`.
Diff computation runs on-demand — no cache.  Response includes
warnings when computation was clamped by size limits.

---

### 10. Model status

#### `GET /api/v1/model/status`

Read-only summary of the Research Analyst LLM configuration.  NEVER
returns the endpoint URL unredacted or any API key.

```json
{
  "endpoint_redacted": "http://10.100.0.13:8080",
  "model_id": "qwen3.6-27b",
  "api_style": "openai",
  "allow_remote": true,
  "allowed_hosts": ["10.100.0.13"],
  "reachable": true,
  "available_models": ["gemma-3-1b", "..."]
}
```

Sources: `strategy.research_analyst.LocalLLMClient.list_models()`,
`RESEARCH_LLM_API_STYLE`, `RESEARCH_LLM_ALLOW_REMOTE`,
`RESEARCH_LLM_ALLOWED_HOSTS`.  Credentials are never fetched or
returned.

---

## Implementation notes (for a future card, not this one)

1. **Framework:** likely FastAPI (async, typed, small footprint).
   No mandate here — the Phase 5.5 implementation card will pick.
2. **Auth:** local-only (bind to `127.0.0.1`) unless the operator
   deploys behind a reverse proxy.  Follow-up decision.
3. **Testing:** every endpoint gets a snapshot test that seeds a
   temp warehouse + reports tree and asserts response shape.  No
   route may be tested against a live provider.
4. **Never call this "training."**

## Read-only guarantees (mandatory for the implementation card)

- No live-runner imports (`trader`, `crypto_trader`, etc.).
- No order-path token references.
- No provider credential env-var reads (`ALPACA_*`, `APCA_*`,
  `RESEARCH_ALPACA_*`, `CRYPTO_ALPACA_*`).
- No `ApprovalRecord` construction.
- No `PromotionEntry` state advancement.
- `FeatureFlags.all_disabled` must be true throughout every unit
  test.
- No write file handles opened against `market_data/`,
  `research_data/`, or `reports/` (read-only IO only).

## Out of scope

- Websocket / server-sent-events streaming — a follow-up card.
- Multi-tenant auth — the dashboard targets a single operator.
- Alerting / notifications — Phase 6 territory.
- Charts / visualisations — the frontend's concern; this plan is
  API-only.

## Deliverable summary

This document is the sole deliverable of `t_dashboard_backend_plan`.
No source files change under this card.  When the Phase 5.5
implementation card is planned, it should reference this document
as `docs/architecture/dashboard-backend-plan.md`.
