"""Phase 5.6 market-data provider plugins.

Each plugin implements
:class:`strategy.market_data_provider.MarketDataProvider` — a
protocol every warehouse fetch consults.  Plugins are loaded by
explicit import (no side-effect autoloading).  Callers who want
runtime discovery should use :func:`get_plugin` after importing
the plugins they need.

Read-only guarantees inherited from the interface layer.  No
plugin places orders, mutates feature flags, or imports live-runner
modules.
"""

from __future__ import annotations

from typing import Callable, Dict


_REGISTRY: Dict[str, Callable[..., "MarketDataProvider"]] = {}


def register(name: str, factory: Callable[..., "MarketDataProvider"]) -> None:
    """Register a plugin factory under ``name``.

    Idempotent — registering the same factory under the same name
    twice is fine.  Registering a different factory under an
    already-taken name raises ``KeyError``.
    """
    existing = _REGISTRY.get(name)
    if existing is None:
        _REGISTRY[name] = factory
        return
    if existing is not factory:
        raise KeyError(f"provider {name!r} is already registered")


def get_plugin(name: str) -> Callable[..., "MarketDataProvider"]:
    """Return the factory registered under ``name``.  Raises
    ``KeyError`` if no plugin is registered.
    """
    if name not in _REGISTRY:
        raise KeyError(f"provider plugin {name!r} not registered")
    return _REGISTRY[name]


def registered() -> Dict[str, Callable[..., "MarketDataProvider"]]:
    """Snapshot of every registered plugin factory.  Callers may
    inspect but must not mutate the returned dict.
    """
    return dict(_REGISTRY)


__all__ = ["get_plugin", "register", "registered"]
