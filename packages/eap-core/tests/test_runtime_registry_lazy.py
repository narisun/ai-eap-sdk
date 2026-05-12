"""Regression test for lazy entry-point loading in AdapterRegistry (P1-7)."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from eap_core.config import RuntimeConfig
from eap_core.runtimes.registry import AdapterRegistry


class _FailingEP(EntryPoint):
    """An EntryPoint subclass whose .load() simulates a broken optional dep."""

    def load(self) -> Any:
        raise ImportError("simulated broken optional dependency")


class _GoodEP(EntryPoint):
    """An EntryPoint subclass that lazy-loads cleanly and tracks load count."""

    # Use a class-level tracker dict keyed on the EntryPoint's name so
    # EntryPoint's immutability (it forbids __setattr__) doesn't get in
    # the way. Each test creates its own instance with a fresh name.
    _counts: ClassVar[dict[str, int]] = {}

    @classmethod
    def reset(cls, name: str) -> None:
        cls._counts[name] = 0

    @classmethod
    def count(cls, name: str) -> int:
        return cls._counts.get(name, 0)

    def load(self) -> Any:
        _GoodEP._counts[self.name] = _GoodEP._counts.get(self.name, 0) + 1

        # Return a no-op adapter factory.
        def _factory(config: RuntimeConfig) -> Any:
            return MagicMock(spec=["generate", "stream"])

        return _factory


def _broken() -> EntryPoint:
    return _FailingEP(
        name="broken", value="eap_core.does_not_exist:Broken", group="eap_core.runtimes"
    )


def _good(name: str = "local") -> EntryPoint:
    _GoodEP.reset(name)
    return _GoodEP(name=name, value="eap_core.runtimes.stub:factory", group="eap_core.runtimes")


def test_broken_provider_does_not_break_registry_construction() -> None:
    """A failing ep.load() must not raise during from_entry_points()."""
    broken = _broken()
    good = _good("local")

    with patch("eap_core.runtimes.registry.entry_points") as mock_ep:
        mock_ep.return_value = [broken, good]
        registry = AdapterRegistry.from_entry_points()

    # Both providers should be registered (lazily); neither should have loaded yet.
    assert set(registry.providers()) == {"broken", "local"}
    assert _GoodEP.count("local") == 0


def test_broken_provider_only_fails_on_its_own_create() -> None:
    """The broken provider raises ImportError only when actually used."""
    broken = _broken()
    good = _good("local")

    with patch("eap_core.runtimes.registry.entry_points") as mock_ep:
        mock_ep.return_value = [broken, good]
        registry = AdapterRegistry.from_entry_points()

    # Using `local` works.
    config = RuntimeConfig(provider="local", model="m")
    registry.create(config)
    assert _GoodEP.count("local") == 1

    # Using `broken` raises the original ImportError.
    bad_config = RuntimeConfig(provider="broken", model="m")
    with pytest.raises(ImportError, match="simulated broken optional dependency"):
        registry.create(bad_config)


def test_lazy_load_caches_factory_across_creates() -> None:
    """A successful load is cached — subsequent create() calls don't re-load."""
    good = _good("local")

    with patch("eap_core.runtimes.registry.entry_points") as mock_ep:
        mock_ep.return_value = [good]
        registry = AdapterRegistry.from_entry_points()

    config = RuntimeConfig(provider="local", model="m")
    registry.create(config)
    registry.create(config)
    registry.create(config)

    assert _GoodEP.count("local") == 1  # not 3
