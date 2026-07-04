"""Operator-facing entrypoints for the Phase 5.6 warehouse.

Modules here are launched by shell scripts under ``scripts/`` after
sourcing ``.env.research``.  They never source production
credentials, never place orders, never touch live trading paths.

Every command uses only ``RESEARCH_ALPACA_*`` and ``WAREHOUSE_*``
env namespaces.
"""

from __future__ import annotations
