import pytest

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import BaseRuntimeAdapter, ModelInfo, RawChunk, RawResponse
from eap_core.runtimes.registry import AdapterRegistry
from eap_core.types import Request


class FakeAdapter(BaseRuntimeAdapter):
    name = "fake"

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    async def generate(self, req: Request) -> RawResponse:
        return RawResponse(text=f"echo:{req.model}", usage={"input_tokens": 1})

    async def stream(self, req: Request):
        yield RawChunk(index=0, text="echo")

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name="echo-1")]


async def test_registry_can_register_and_resolve():
    reg = AdapterRegistry()
    reg.register("fake", FakeAdapter)
    cfg = RuntimeConfig(provider="fake", model="m")
    adapter = reg.create(cfg)
    assert isinstance(adapter, FakeAdapter)
    resp = await adapter.generate(Request(model="m"))
    assert resp.text == "echo:m"


def test_registry_raises_on_unknown_provider():
    reg = AdapterRegistry()
    with pytest.raises(KeyError, match="bogus"):
        reg.create(RuntimeConfig(provider="bogus", model="m"))


@pytest.mark.skip(reason="enabled after Task 12 lands cloud adapters")
async def test_registry_loads_default_entry_points():
    reg = AdapterRegistry.from_entry_points()
    assert "local" in reg.providers()
