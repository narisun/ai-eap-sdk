"""Runtime adapter registry with entry-point discovery."""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Callable

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import BaseRuntimeAdapter

AdapterFactory = Callable[[RuntimeConfig], BaseRuntimeAdapter]


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AdapterFactory] = {}

    def register(self, provider: str, adapter_cls: type[BaseRuntimeAdapter]) -> None:
        self._adapters[provider] = adapter_cls

    def providers(self) -> list[str]:
        return sorted(self._adapters)

    def create(self, config: RuntimeConfig) -> BaseRuntimeAdapter:
        try:
            cls = self._adapters[config.provider]
        except KeyError as e:
            raise KeyError(
                f"unknown runtime provider {config.provider!r}; "
                f"registered: {self.providers()}"
            ) from e
        return cls(config)

    @classmethod
    def from_entry_points(cls, group: str = "eap_core.runtimes") -> "AdapterRegistry":
        reg = cls()
        for ep in entry_points(group=group):
            reg.register(ep.name, ep.load())
        return reg
