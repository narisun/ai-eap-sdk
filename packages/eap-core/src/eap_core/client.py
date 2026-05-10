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
    ) -> None:
        self._config = runtime_config
        self._registry = registry or AdapterRegistry.from_entry_points()
        self._adapter: BaseRuntimeAdapter = self._registry.create(runtime_config)
        self._pipeline = MiddlewarePipeline(middlewares or [])
        self._identity = identity

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
        req = Request(
            model=self._config.model,
            messages=_to_messages(prompt),
            metadata={
                "operation_name": operation_name,
                "action": action,
                "resource": resource or self._config.model,
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
        req = Request(
            model=self._config.model,
            messages=_to_messages(prompt),
            stream=True,
            metadata={
                "operation_name": operation_name,
                "action": action,
                "resource": resource or self._config.model,
                **({"output_schema": schema} if schema else {}),
            },
            options=kwargs,
        )

        async def terminal(r: Request, c: Context) -> AsyncIterator[Chunk]:  # type: ignore[misc,unused-ignore]
            async for raw in self._adapter.stream(r):  # type: ignore[attr-defined]
                yield Chunk(index=raw.index, text=raw.text, finish_reason=raw.finish_reason)

        async for chunk in self._pipeline.run_stream(req, ctx, terminal):
            yield chunk

    async def aclose(self) -> None:
        await self._adapter.aclose()
