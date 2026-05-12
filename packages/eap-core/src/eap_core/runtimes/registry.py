"""Runtime adapter registry with entry-point discovery."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import EntryPoint, entry_points
from typing import Any, TypeGuard, cast

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import BaseRuntimeAdapter

AdapterFactory = Callable[[RuntimeConfig], BaseRuntimeAdapter]
# A factory may be the loaded class/callable OR a deferred EntryPoint that
# resolves to one on first use. Deferred entries are loaded lazily inside
# ``create()`` so a broken optional provider (e.g. a missing transitive
# dep in [gcp]) does not break the registry for users of other providers.
_RegisteredFactory = AdapterFactory | EntryPoint

# Tuple of EntryPoint classes we accept as "deferred loader" markers.
# Some environments (notably pytest with certain plugin sets) deliver
# entry points as instances of the ``importlib_metadata`` backport rather
# than the stdlib ``importlib.metadata`` class — the two are unrelated by
# inheritance, so a bare ``isinstance(x, EntryPoint)`` would miss the
# backport variant. Build the tuple at import time so the check is a
# single ``isinstance`` call in the hot path.
_ENTRY_POINT_TYPES: tuple[type, ...]
try:
    import importlib_metadata as _importlib_metadata_backport

    _ENTRY_POINT_TYPES = (EntryPoint, _importlib_metadata_backport.EntryPoint)
except ImportError:  # pragma: no cover — backport not installed
    _ENTRY_POINT_TYPES = (EntryPoint,)


def _is_entry_point(obj: Any) -> TypeGuard[EntryPoint]:
    """True if ``obj`` is an EntryPoint (stdlib OR importlib_metadata backport).

    Typed as a TypeGuard narrowing to stdlib ``EntryPoint`` so callers can use
    ``.load()`` afterwards under strict mypy. The backport's EntryPoint has
    the same ``.load()`` interface; mypy doesn't know about the backport class
    but the runtime behavior is identical.
    """
    return isinstance(obj, _ENTRY_POINT_TYPES)


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, _RegisteredFactory] = {}

    def register(self, provider: str, adapter_cls: type[BaseRuntimeAdapter]) -> None:
        self._adapters[provider] = adapter_cls

    def register_entry_point(self, provider: str, ep: EntryPoint) -> None:
        """Register a provider via a deferred EntryPoint.

        The entry point is loaded on first ``create()`` for that provider —
        not at registration time. A broken optional dependency thus only
        affects users who actually request that provider.
        """
        self._adapters[provider] = ep

    def providers(self) -> list[str]:
        return sorted(self._adapters)

    def create(self, config: RuntimeConfig) -> BaseRuntimeAdapter:
        try:
            entry = self._adapters[config.provider]
        except KeyError as e:
            raise KeyError(
                f"unknown runtime provider {config.provider!r}; registered: {self.providers()}"
            ) from e
        # Lazy-resolve deferred EntryPoint entries on first use; cache the
        # loaded factory in place so subsequent calls don't re-load.
        if _is_entry_point(entry):
            factory: AdapterFactory = entry.load()
            self._adapters[config.provider] = factory
            return factory(config)
        # ``entry`` is not an EntryPoint here at runtime, but TypeGuard does
        # not narrow the negative branch, so mypy still sees the union. The
        # union member that isn't EntryPoint is ``AdapterFactory`` — assert
        # that explicitly with ``cast`` rather than silencing mypy.
        return cast(AdapterFactory, entry)(config)

    @classmethod
    def from_entry_points(cls, group: str = "eap_core.runtimes") -> AdapterRegistry:
        reg = cls()
        for ep in entry_points(group=group):
            reg.register_entry_point(ep.name, ep)
        return reg
