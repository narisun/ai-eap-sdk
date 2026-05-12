"""Middleware Protocol and shared base types."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from eap_core.types import Chunk, Context, Request, Response


@runtime_checkable
class Middleware(Protocol):
    """Contract every middleware implements.

    Implementations may be classes or any object satisfying this Protocol.
    """

    name: str

    async def on_request(self, req: Request, ctx: Context) -> Request: ...
    async def on_response(self, resp: Response, ctx: Context) -> Response: ...
    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk: ...
    async def on_stream_end(self, ctx: Context) -> None: ...
    async def on_tool_call(
        self, tool_name: str, args: dict[str, Any], ctx: Context
    ) -> dict[str, Any]: ...
    async def on_tool_call_post_mutation(
        self, tool_name: str, args: dict[str, Any], ctx: Context
    ) -> None: ...
    async def on_call_end(self, ctx: Context) -> None: ...
    async def on_error(self, exc: Exception, ctx: Context) -> None: ...


class PassthroughMiddleware:
    """Convenience base class — overrides only what you need."""

    name: str = "passthrough"

    async def on_request(self, req: Request, ctx: Context) -> Request:
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        return resp

    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
        return chunk

    async def on_stream_end(self, ctx: Context) -> None:
        return None

    async def on_tool_call(
        self, tool_name: str, args: dict[str, Any], ctx: Context
    ) -> dict[str, Any]:
        """Default tool-call mutation hook — identity.

        Middlewares MAY override to transform ``args`` before the
        terminal tool invocation. The SDK re-stamps the trusted policy
        inputs (``ctx.metadata['policy.action']`` / ``['policy.resource']``)
        from the current ``tool_name`` AFTER this hook runs and BEFORE
        ``on_tool_call_post_mutation`` fires, so any mutation of those
        keys here is overwritten — see ``MiddlewarePipeline.run_tool``.
        """
        return args

    async def on_tool_call_post_mutation(
        self, tool_name: str, args: dict[str, Any], ctx: Context
    ) -> None:
        """Default post-mutation hook — no-op.

        ``PolicyMiddleware`` overrides this to re-authorize against the
        SDK-controlled ``ctx.metadata['policy.*']`` keys after any
        Phase-2 mutation completes.
        """
        return None

    async def on_call_end(self, ctx: Context) -> None:
        return None

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        return None
