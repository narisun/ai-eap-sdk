"""Regression tests for _prepare_call_context (Finding 5).

The three public call sites (``generate_text``, ``stream_text``,
``invoke_tool``) must all set the trusted ``policy.action`` /
``policy.resource`` slots on ``ctx.metadata`` via the shared
``_prepare_call_context`` helper. The unary path defaults ``resource``
to the configured model; the tool path uses ``f"tool:{tool_name}"`` and
``tool_name`` and never falls through to ``model``.
"""

from __future__ import annotations

from typing import Any

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import ToolSpec
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.types import Context, Request


class _Capturer(PassthroughMiddleware):
    """Records every ``ctx`` it sees on ``on_request`` for assertion."""

    name = "capturer"

    def __init__(self) -> None:
        self.seen: list[Context] = []

    async def on_request(self, req: Request, ctx: Context) -> Request:
        self.seen.append(ctx)
        return req


async def test_generate_text_uses_prepared_context() -> None:
    cap = _Capturer()
    client = EnterpriseLLM(RuntimeConfig(provider="local", model="echo-1"))
    client._pipeline = MiddlewarePipeline([cap])
    await client.generate_text("hello")
    ctx = cap.seen[-1]
    assert ctx.metadata["policy.action"] == "generate_text"
    assert ctx.metadata["policy.resource"] == "echo-1"


async def test_generate_text_honors_explicit_action_and_resource() -> None:
    cap = _Capturer()
    client = EnterpriseLLM(RuntimeConfig(provider="local", model="echo-1"))
    client._pipeline = MiddlewarePipeline([cap])
    await client.generate_text("hi", action="custom_action", resource="custom_resource")
    ctx = cap.seen[-1]
    assert ctx.metadata["policy.action"] == "custom_action"
    assert ctx.metadata["policy.resource"] == "custom_resource"


async def test_stream_text_uses_prepared_context() -> None:
    cap = _Capturer()
    client = EnterpriseLLM(RuntimeConfig(provider="local", model="echo-1"))
    client._pipeline = MiddlewarePipeline([cap])
    async for _ in client.stream_text("hi"):
        pass
    ctx = cap.seen[-1]
    assert ctx.metadata["policy.action"] == "generate_text"
    assert ctx.metadata["policy.resource"] == "echo-1"


async def test_invoke_tool_uses_prepared_context_with_tool_shape() -> None:
    cap = _Capturer()
    registry = McpToolRegistry()

    async def _ping(**kwargs: Any) -> str:
        return "pong"

    registry.register(ToolSpec(name="ping", description="d", input_schema={}, fn=_ping))
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        tool_registry=registry,
    )
    client._pipeline = MiddlewarePipeline([cap])
    await client.invoke_tool("ping", {})
    ctx = cap.seen[-1]
    # Tool path always uses ``tool:<name>`` / ``<name>`` — NOT the model.
    assert ctx.metadata["policy.action"] == "tool:ping"
    assert ctx.metadata["policy.resource"] == "ping"
