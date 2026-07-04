"""Phase 5.6 Historical Data Warehouse implementation.

Submodules land incrementally with each Phase 5.6 card:

* ``parquet_io``   — canonical bar writer / reader (this card).
* ``duckdb_query`` — analytical query layer.
* ``validation``   — integrity + gap detection.
* ``versioning``   — lineage tracking.
* ``import_pipeline`` — bulk provider / ZIP / CSV imports.

Read-only guarantees inherited from every other Phase 5.6 module.
"""

from __future__ import annotations
