"""Middleware Protocol and shared base types."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        return None
