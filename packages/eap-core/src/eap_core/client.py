"""EnterpriseLLM — public entry point.

Wires the middleware pipeline to a runtime adapter resolved via the
AdapterRegistry. Supports `generate_text`, `stream_text`, and a sync
proxy at `client.sync`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from eap_core.config import RuntimeConfig
from eap_core.identity.nhi import NonHumanIdentity
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import MCPError
from eap_core.middleware.base import Middleware
from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.runtimes.base import BaseRuntimeAdapter
from eap_core.runtimes.registry import AdapterRegistry
from eap_core.types import Chunk, Context, Message, Request, Response


def _to_messages(prompt: str | list[Message] | list[dict[str, Any]]) -> list[Message]:
    if isinstance(prompt, str):
        return [Message(role="user", content=prompt)]
    out: list[Message] = []
    for m in prompt:
        out.append(m if isinstance(m, Message) else Message(**m))
    return out


class SyncProxy:
    def __init__(self, client: EnterpriseLLM) -> None:
        self._client = client

    def generate_text(
        self, prompt: str | list[Message] | list[dict[str, Any]], **kw: Any
    ) -> Response:
        return asyncio.run(self._client.generate_text(prompt, **kw))


class EnterpriseLLM:
    def __init__(
        self,
        runtime_config: RuntimeConfig,
        middlewares: list[Middleware] | None = None,
        identity: NonHumanIdentity | None = None,
        registry: AdapterRegistry | None = None,
        tool_registry: McpToolRegistry | None = None,
    ) -> None:
        self._config = runtime_config
        self._registry = registry or AdapterRegistry.from_entry_points()
        self._adapter: BaseRuntimeAdapter = self._registry.create(runtime_config)
        self._pipeline = MiddlewarePipeline(middlewares or [])
        self._identity = identity
        self._tool_registry = tool_registry

    @property
    def sync(self) -> SyncProxy:
        return SyncProxy(self)

    async def generate_text(
        self,
        prompt: str | list[Message] | list[dict[str, Any]],
        *,
        schema: type[BaseModel] | None = None,
        operation_name: str = "generate_text",
        action: str = "generate_text",
        resource: str | None = None,
        **kwargs: Any,
    ) -> Response:
        ctx = Context(request_id=uuid.uuid4().hex, identity=self._identity)
        # Authorization inputs are explicit keyword arguments on the public
        # ``generate_text`` API, not free-form ``Request.metadata`` keys.
        # Stash them on ``ctx.metadata`` (the SDK-trusted, per-pipeline slot)
        # so ``PolicyMiddleware`` uses the values the SDK observed at the
        # call site, not anything a later middleware (or a caller crafting
        # a Request) might inject. ``action``/``resource`` deliberately do
        # NOT live on ``req.metadata`` so there is exactly one source of
        # truth — readers (eval/audit/observability) should consult
        # ``ctx.metadata['policy.action']`` / ``['policy.resource']``.
        resolved_resource = resource or self._config.model
        ctx.metadata["policy.action"] = action
        ctx.metadata["policy.resource"] = resolved_resource
        req = Request(
            model=self._config.model,
            messages=_to_messages(prompt),
            metadata={
                "operation_name": operation_name,
                **({"output_schema": schema} if schema else {}),
            },
            options=kwargs,
        )

        async def terminal(r: Request, c: Context) -> Response:
            raw = await self._adapter.generate(r)
            return Response(
                text=raw.text,
                usage=raw.usage,
                finish_reason=raw.finish_reason,
                raw=raw.raw,
            )

        return await self._pipeline.run(req, ctx, terminal)

    async def stream_text(
        self,
        prompt: str | list[Message] | list[dict[str, Any]],
        *,
        schema: type[BaseModel] | None = None,
        operation_name: str = "generate_text",
        action: str = "generate_text",
        resource: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Chunk]:
        ctx = Context(request_id=uuid.uuid4().hex, identity=self._identity)
        # See ``generate_text`` for the rationale: ``action``/``resource`` are
        # trusted policy inputs and live ONLY on ``ctx.metadata`` to keep
        # exactly one source of truth.
        resolved_resource = resource or self._config.model
        ctx.metadata["policy.action"] = action
        ctx.metadata["policy.resource"] = resolved_resource
        req = Request(
            model=self._config.model,
            messages=_to_messages(prompt),
            stream=True,
            metadata={
                "operation_name": operation_name,
                **({"output_schema": schema} if schema else {}),
            },
            options=kwargs,
        )

        async def terminal(r: Request, c: Context) -> AsyncIterator[Chunk]:  # type: ignore[misc,unused-ignore]
            async for raw in self._adapter.stream(r):  # type: ignore[attr-defined]
                yield Chunk(index=raw.index, text=raw.text, finish_reason=raw.finish_reason)

        async for chunk in self._pipeline.run_stream(req, ctx, terminal):
            yield chunk

    async def invoke_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        registry = self._tool_registry
        if registry is None:
            raise MCPError(
                tool_name=tool_name,
                message="no tool registry configured on EnterpriseLLM",
            )
        spec = registry.get(tool_name)
        if spec is None:
            raise MCPError(tool_name=tool_name, message="tool not found in registry")

        # Build the request with policy-relevant fields derived inside the SDK.
        # ``action`` and ``resource`` are authorization inputs — they MUST come
        # from a trusted source (the tool name we just resolved), never from
        # caller-controlled ``Request.metadata``. Allowing a caller to set
        # ``metadata['action']='tool:lookup_account'`` would let them bypass a
        # ``deny tool:transfer_funds`` rule.
        ctx = Context(request_id=uuid.uuid4().hex, identity=self._identity)
        # SDK-trusted policy inputs live on ``ctx.metadata`` (per-pipeline,
        # not part of ``Request`` and therefore not caller-mutable from the
        # public API). ``PolicyMiddleware`` reads these — and ONLY these —
        # for the auth decision. ``action``/``resource`` deliberately do not
        # appear on ``req.metadata`` so there is one source of truth.
        ctx.metadata["policy.action"] = f"tool:{tool_name}"
        ctx.metadata["policy.resource"] = tool_name
        req = Request(
            model=self._config.model,
            messages=[],
            metadata={
                "operation_name": "invoke_tool",
                "tool_name": tool_name,
            },
        )

        async def terminal(r: Request, c: Context) -> Response:
            # Use the original ``args`` captured in the closure rather than
            # ``r.metadata`` so middleware cannot swap the tool's input
            # silently. Pass ``ctx.identity`` to the registry so the
            # ``requires_auth`` gate sees the same identity the policy
            # middleware just authorized against.
            result = await registry.invoke(tool_name, args, identity=c.identity)
            return Response(text=str(result), payload=result)

        resp = await self._pipeline.run(req, ctx, terminal)
        return resp.payload

    async def aclose(self) -> None:
        await self._adapter.aclose()
