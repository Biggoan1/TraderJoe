"""Strategy registry — Card 3.

Auto-discovers every :class:`~strategy.lab.strategy.Strategy`
subclass under ``strategy/lab/*.py`` and makes them addressable
by ``name``.  Registration is idempotent — importing the module
twice does not double-register.

Discovery rules:

* Every ``.py`` file under ``strategy/lab/`` (excluding
  ``__init__``, ``registry``, ``strategy``, ``single_factor``,
  and ``experiment_runner``) is imported.
* Every attribute in each module that inherits from
  :class:`~strategy.lab.strategy.StrategyBase` and has a non-empty
  ``name`` class attribute is registered.  Registering the same
  class again is a no-op.  Registering a *different* class under
  an already-taken name raises :class:`RegistryConflictError`.

Read-only: no live-runner imports, no order-path references, no
credential env reads.  Discovery never enables feature flags or
mutates promotion state.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Type

from strategy.lab.strategy import Strategy, StrategyBase


# Discovery skips these — they are framework, not strategies.
_SKIP_MODULES = frozenset({
    "__init__",
    "registry",
    "strategy",
    "single_factor",
    "experiment_runner",
})


class RegistryError(RuntimeError):
    """Base class for registry errors."""


class RegistryConflictError(RegistryError):
    """A different class was registered under an existing name."""


class StrategyNotFoundError(RegistryError, KeyError):
    """Requested strategy name is not registered."""

    def __init__(self, name: str, available: Sequence[str]) -> None:
        super().__init__(
            f"strategy {name!r} is not registered "
            f"(available: {list(available)})"
        )
        self.name = name
        self.available = tuple(available)


@dataclass(frozen=True)
class StrategyRegistration:
    """One entry in the registry."""

    name: str
    version: str
    factory: Callable[..., Strategy]
    source_module: str

    def build(self, **kwargs: Any) -> Strategy:
        return self.factory(**kwargs)


class StrategyRegistry:
    """Thread-safe registry keyed by strategy ``name``."""

    def __init__(self) -> None:
        self._entries: Dict[str, StrategyRegistration] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        cls: Type[StrategyBase],
        *,
        source_module: str = "",
    ) -> StrategyRegistration:
        """Register a strategy class.  Idempotent for the same class
        under the same name; raises for a conflicting registration.
        """
        name = getattr(cls, "name", "")
        version = getattr(cls, "version", "")
        if not name:
            raise RegistryError(
                f"cannot register {cls!r}: missing non-empty 'name' attribute"
            )
        entry = StrategyRegistration(
            name=name,
            version=version,
            factory=cls,
            source_module=source_module or getattr(cls, "__module__", ""),
        )
        with self._lock:
            existing = self._entries.get(name)
            if existing is not None:
                if existing.factory is cls:
                    return existing
                raise RegistryConflictError(
                    f"name {name!r} already registered to "
                    f"{existing.factory!r}; cannot re-register to {cls!r}"
                )
            self._entries[name] = entry
        return entry

    def unregister(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._entries)

    def entries(self) -> List[StrategyRegistration]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.name)

    def get(self, name: str) -> StrategyRegistration:
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            raise StrategyNotFoundError(name, sorted(self._entries))
        return entry

    def build(self, name: str, **kwargs: Any) -> Strategy:
        return self.get(name).build(**kwargs)

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._entries

    # ------------------------------------------------------------------
    # Auto-discovery
    # ------------------------------------------------------------------

    def discover(
        self,
        package: str = "strategy.lab",
        *,
        skip_modules: Optional[Sequence[str]] = None,
    ) -> List[StrategyRegistration]:
        """Walk ``package`` and register every StrategyBase subclass
        found in each module.  Returns the list of registrations
        touched by this call (existing entries are re-returned as
        idempotent).
        """
        skip = set(_SKIP_MODULES)
        if skip_modules:
            skip.update(skip_modules)
        pkg = importlib.import_module(package)
        pkg_path = Path(pkg.__file__).parent
        touched: List[StrategyRegistration] = []
        for info in pkgutil.iter_modules([str(pkg_path)]):
            if info.name in skip or info.ispkg:
                continue
            module_name = f"{package}.{info.name}"
            module = importlib.import_module(module_name)
            for attr_name, obj in vars(module).items():
                if not inspect.isclass(obj):
                    continue
                if obj is StrategyBase or not issubclass(obj, StrategyBase):
                    continue
                # Skip classes without a proper name attribute
                obj_name = getattr(obj, "name", "")
                if not obj_name or obj_name == "":
                    continue
                # Skip classes defined outside their own module
                # (avoid re-registering via re-exports).
                if obj.__module__ != module_name:
                    continue
                touched.append(
                    self.register(obj, source_module=module_name)
                )
        return touched


# Module-level default registry --------------------------------------------


_default_registry = StrategyRegistry()


def get_default_registry() -> StrategyRegistry:
    return _default_registry


def register(cls: Type[StrategyBase]) -> Type[StrategyBase]:
    """Decorator form for direct registration."""
    _default_registry.register(cls)
    return cls


def discover_strategies(
    package: str = "strategy.lab",
    registry: Optional[StrategyRegistry] = None,
) -> List[StrategyRegistration]:
    reg = registry or _default_registry
    return reg.discover(package)


__all__ = [
    "RegistryConflictError",
    "RegistryError",
    "StrategyNotFoundError",
    "StrategyRegistration",
    "StrategyRegistry",
    "discover_strategies",
    "get_default_registry",
    "register",
]
