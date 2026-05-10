import os

import pytest

from eap_core.config import RuntimeConfig
from eap_core.runtimes.bedrock import BedrockRuntimeAdapter
from eap_core.runtimes.vertex import VertexRuntimeAdapter
from eap_core.types import Message, Request


@pytest.fixture(autouse=True)
def clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


async def test_bedrock_raises_helpful_error_when_not_enabled():
    a = BedrockRuntimeAdapter(RuntimeConfig(provider="bedrock", model="anthropic.claude-3-5-sonnet", options={"region": "us-east-1"}))
    with pytest.raises(NotImplementedError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await a.generate(Request(model="anthropic.claude-3-5-sonnet", messages=[Message(role="user", content="hi")]))


async def test_vertex_raises_helpful_error_when_not_enabled():
    a = VertexRuntimeAdapter(RuntimeConfig(provider="vertex", model="gemini-1.5-pro", options={"project": "p", "location": "us-central1"}))
    with pytest.raises(NotImplementedError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await a.generate(Request(model="gemini-1.5-pro", messages=[Message(role="user", content="hi")]))


async def test_bedrock_lazy_imports_boto3_only_when_enabled(monkeypatch):
    """The adapter constructor must not trigger boto3 import."""
    import sys
    sys.modules.pop("boto3", None)
    a = BedrockRuntimeAdapter(RuntimeConfig(provider="bedrock", model="m", options={"region": "us-east-1"}))
    assert "boto3" not in sys.modules
    _ = a
