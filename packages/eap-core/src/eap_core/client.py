"""EnterpriseLLM — public entry point.

Wires the middleware pipeline to a runtime adapter resolved via the
AdapterRegistry. Supports `generate_text`, `stream_text`, and a sync
proxy at `client.sync`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable
from typing import Any

from pydantic import BaseModel

from eap_core.config import RuntimeConfig
from eap_core.identity.nhi import IdentityToken
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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._client.generate_text(prompt, **kw))
        raise RuntimeError(
            "EnterpriseLLM.sync.generate_text() cannot be used inside an active "
            "event loop. Use `await client.generate_text(...)` instead. In "
            "Jupyter (IPython >=7), `await` works directly at the top level "
            "of a cell — no asyncio.run(...) wrapper needed."
        )


class EnterpriseLLM:
    def __init__(
        self,
        runtime_config: RuntimeConfig,
        middlewares: list[Middleware] | None = None,
        identity: IdentityToken | None = None,
        registry: AdapterRegistry | None = None,
        tool_registry: McpToolRegistry | None = None,
        token_exchange: Any | None = None,
        owned: list[Any] | None = None,
    ) -> None:
        self._config = runtime_config
        self._registry = registry or AdapterRegistry.from_entry_points()
        self._adapter: BaseRuntimeAdapter = self._registry.create(runtime_config)
        self._pipeline = MiddlewarePipeline(middlewares or [])
        self._identity = identity
        self._tool_registry = tool_registry
        # IdP-side components (token exchange, gateway clients, …) often
        # own their own httpx pool. ``aclose`` walks this list so users
        # don't have to remember to close each piece they wired in. Pass
        # ``owned=[...]`` for arbitrary extras; ``token_exchange`` is a
        # convenience kwarg that simply appends to ``_owned_components``.
        # We deliberately do NOT keep a ``self._token_exchange`` attribute:
        # exposing one creates a false API surface (nothing else on the
        # client reads it). Callers that need to reach the exchange after
        # construction should retain their own reference.
        self._owned_components: list[Any] = list(owned or [])
        if token_exchange is not None:
            self._owned_components.append(token_exchange)

    @property
    def sync(self) -> SyncProxy:
        return SyncProxy(self)

    @property
    def identity(self) -> IdentityToken | None:
        """The configured identity (if any) — exposes ``_identity`` for tests/observability."""
        return self._identity

    def _prepare_call_context(
        self,
        *,
        action: str,
        resource: str | None,
    ) -> Context:
        """Build a per-call Context with trusted policy inputs.

        ``policy.action`` and ``policy.resource`` MUST come from a trusted
        source (the SDK's explicit method API, not caller-controlled
        ``Request.metadata``). ``PolicyMiddleware`` reads these — and ONLY
        these — for the auth decision. Centralized here so all three call
        sites (``generate_text``, ``stream_text``, ``invoke_tool``) share
        one source of truth (Finding 5).

        ``resource`` is allowed to be ``None`` for the chat path where the
        configured model is the natural default; ``invoke_tool`` always
        passes the explicit tool name and never falls through to the
        model default.
        """
        ctx = Context(request_id=uuid.uuid4().hex, identity=self._identity)
        resolved_resource = resource if resource is not None else self._config.model
        ctx.metadata["policy.action"] = action
        ctx.metadata["policy.resource"] = resolved_resource
        return ctx

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
        # Authorization inputs are explicit keyword arguments on the public
        # ``generate_text`` API, not free-form ``Request.metadata`` keys.
        # See ``_prepare_call_context`` for the trusted-input rationale.
        ctx = self._prepare_call_context(action=action, resource=resource)
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
        # See ``_prepare_call_context`` for the trusted-input rationale.
        ctx = self._prepare_call_context(action=action, resource=resource)
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
                yield Chunk(
                    index=raw.index,
                    text=raw.text,
                    finish_reason=raw.finish_reason,
                    usage=raw.usage,
                )

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

        # ``action`` and ``resource`` are authorization inputs — they MUST come
        # from a trusted source (the tool name we just resolved), never from
        # caller-controlled ``Request.metadata``. ``_prepare_call_context``
        # stamps the initial values; the pipeline's ``run_tool`` re-stamps
        # them in Phase 3 (after ``on_tool_call``, before
        # ``on_tool_call_post_mutation``) so a Phase-2 middleware can't
        # launder the policy inputs.
        ctx = self._prepare_call_context(
            action=f"tool:{tool_name}",
            resource=tool_name,
        )

        async def terminal(name: str, current_args: dict[str, Any], c: Context) -> Any:
            # Use the POST-MUTATION ``current_args`` from the pipeline,
            # NOT a closure-captured original. Middleware that wants to
            # mutate declares so via ``on_tool_call``; PolicyMiddleware's
            # ``on_tool_call_post_mutation`` re-authorizes against the
            # SDK-controlled ``ctx.metadata`` after any mutation. The
            # v1.7 dev-guide §3.9 closure-capture rationale section
            # describes the historical concern; v1.8's hook is the
            # documented forward path.
            return await registry.invoke(name, current_args, identity=c.identity)

        return await self._pipeline.run_tool(tool_name, args, ctx, terminal)

    async def aclose(self) -> None:
        """Close the runtime adapter + every owned component.

        All aclose() calls run regardless of failures; collected exceptions
        are re-raised as an ExceptionGroup (PEP 654, Python 3.11+).

        Components are closed **concurrently** via ``asyncio.gather``. If
        multiple components share an underlying resource (e.g., an
        externally-owned ``httpx.AsyncClient``), close that resource yourself
        rather than relying on shared ownership — concurrent ``aclose()``
        calls on the same pool are undefined.
        """
        closers: list[Awaitable[None]] = [self._adapter.aclose()]
        for component in self._owned_components:
            if hasattr(component, "aclose"):
                closers.append(component.aclose())

        results = await asyncio.gather(*closers, return_exceptions=True)
        # gather(return_exceptions=True) only catches Exception subclasses;
        # BaseException (KeyboardInterrupt, SystemExit) propagates immediately
        # and is unreachable here. ExceptionGroup is also bound to Exception
        # (mypy strict).
        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            raise ExceptionGroup(
                f"{len(failures)} component(s) failed to aclose cleanly",
                failures,
            )
