"""Onion-model executor for the middleware chain."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from eap_core.types import Chunk, Context, Request, Response

if TYPE_CHECKING:
    from eap_core.middleware.base import Middleware

Terminal = Callable[[Request, Context], Awaitable[Response]]
StreamTerminal = Callable[[Request, Context], AsyncIterator[Chunk]]
ToolTerminal = Callable[[str, dict[str, Any], Context], Awaitable[Any]]

_LOG = logging.getLogger(__name__)


class MiddlewarePipeline:
    """Chain-of-responsibility executor.

    Runs `on_request` left-to-right, invokes the terminal callable, then
    `on_response` right-to-left. On exception, runs `on_error` in reverse
    order on every middleware whose `on_request` already executed.
    """

    def __init__(self, middlewares: list[Middleware]) -> None:
        self._mws = list(middlewares)

    async def run(self, req: Request, ctx: Context, terminal: Terminal) -> Response:
        ran: list[Middleware] = []
        try:
            try:
                for mw in self._mws:
                    ran.append(mw)
                    req = await mw.on_request(req, ctx)
                resp = await terminal(req, ctx)
                for mw in reversed(ran):
                    resp = await mw.on_response(resp, ctx)
                return resp
            finally:
                # ``on_call_end`` runs right-to-left, best-effort, in a
                # ``finally`` block so it fires on BOTH normal completion AND
                # failure — mirrors ``on_stream_end`` semantics on the
                # streaming path. A secondary failure here must not mask the
                # primary terminal exception (dev-guide §3.2).
                for mw in reversed(ran):
                    try:
                        await mw.on_call_end(ctx)
                    except Exception as secondary:
                        mw_name = getattr(mw, "name", type(mw).__name__)
                        _LOG.warning(
                            "secondary failure in %s.on_call_end during call finalization",
                            mw_name,
                            exc_info=secondary,
                        )
        except Exception as exc:
            await self._on_error(ran, exc, ctx)
            raise

    async def run_stream(
        self, req: Request, ctx: Context, terminal: StreamTerminal
    ) -> AsyncIterator[Chunk]:
        ran: list[Middleware] = []
        try:
            for mw in self._mws:
                # Append BEFORE awaiting so a middleware that raises in
                # ``on_request`` still receives ``on_error`` — mirrors the
                # symmetry of ``run`` above (dev-guide §3.2). Without this,
                # streaming requests silently skipped the on_error hook for
                # the very middleware that failed.
                ran.append(mw)
                req = await mw.on_request(req, ctx)
            try:
                async for chunk in terminal(req, ctx):
                    for mw in self._mws:
                        chunk = await mw.on_stream_chunk(chunk, ctx)
                    yield chunk
            finally:
                # ``on_stream_end`` runs right-to-left to mirror ``on_response``
                # semantics. It fires whether the stream completes normally OR
                # the terminal/chunk pipeline raises — audit close, span close,
                # PII vault flush, trajectory write all need a final hook.
                # Errors from ``on_stream_end`` are best-effort: a secondary
                # failure here should not mask a primary terminal exception.
                for mw in reversed(ran):
                    try:
                        await mw.on_stream_end(ctx)
                    except Exception as secondary:
                        mw_name = getattr(mw, "name", type(mw).__name__)
                        _LOG.warning(
                            "secondary failure in %s.on_stream_end during streaming finalization",
                            mw_name,
                            exc_info=secondary,
                        )
        except Exception as exc:
            await self._on_error(ran, exc, ctx)
            raise

    async def run_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: Context,
        terminal: ToolTerminal,
    ) -> Any:
        """Tool-invocation pipeline orchestrator.

        Six phases:

        1. ``on_request`` L→R — existing audit / threat-detection / policy hooks.
        2. ``on_tool_call`` L→R — NEW mutation phase. Middlewares may
           transform ``args``; the result of each call becomes the input
           to the next middleware.
        3. SDK re-stamps ``ctx.metadata['policy.action']`` /
           ``['policy.resource']`` from the current ``tool_name``.
           Pipeline-level, not middleware-level: a Phase-2 middleware
           that tried to launder the policy inputs by writing to
           ``ctx.metadata`` finds those writes overwritten BEFORE
           Phase-4 runs, so re-authorization sees the SDK-controlled
           values.
        4. ``on_tool_call_post_mutation`` L→R — NEW. ``PolicyMiddleware``
           overrides this hook to re-authorize against the trusted
           ``ctx.metadata`` keys re-stamped in Phase 3.
        5. terminal(tool_name, args, ctx) — invoked with the FINAL
           post-mutation ``args``.
        6. ``on_response`` R→L — mirrors ``run()``.

        Plus ``on_call_end`` R→L in ``finally`` (mirrors v1.7 ``run()``)
        and ``on_error`` R→L on exception.

        A ``Request`` is constructed in Phase 1 so existing audit /
        threat-detection middleware works unchanged. The ``Request.metadata``
        is observability-only; the canonical ``args`` travel as a
        separate parameter through Phases 2-5.
        """
        ran: list[Middleware] = []
        try:
            try:
                # Phase 1: standard request hooks.
                req = self._build_tool_request(tool_name, args)
                for mw in self._mws:
                    ran.append(mw)
                    req = await mw.on_request(req, ctx)

                # Phase 2: mutation phase, L→R.
                for mw in self._mws:
                    args = await mw.on_tool_call(tool_name, args, ctx)

                # Phase 3: SDK re-stamps trusted policy inputs. The
                # ``tool_name`` doesn't change here (it's the registered
                # tool we're invoking) — but a future enhancement could
                # derive ``policy.resource`` from a named args field if a
                # documented contract emerges. For v1.8 we just re-stamp
                # the same values that ``_prepare_call_context`` set
                # initially. The point: any middleware that mutated
                # ``ctx.metadata['policy.*']`` during Phase 2 sees its
                # mutation OVERWRITTEN here, so the re-authorization in
                # Phase 4 evaluates against the SDK-controlled inputs.
                #
                # Tool-name aliasing is NOT supported in v1.8 — the
                # ``tool_name`` flowing through Phases 2-5 is the
                # ORIGINAL value passed to ``invoke_tool()``. Letting
                # middleware rewrite the tool name would re-open the
                # laundering attack this re-stamp closes (an attacker
                # could rewrite ``"transfer_funds"`` → ``"lookup_account"``
                # via the alias to bypass policy). If alias resolution
                # becomes a real need, add it as an explicit pre-pipeline
                # step in ``EnterpriseLLM.invoke_tool``, BEFORE
                # ``_prepare_call_context`` — not as a middleware mutation.
                ctx.metadata["policy.action"] = f"tool:{tool_name}"
                ctx.metadata["policy.resource"] = tool_name

                # Phase 4: post-mutation hooks — PolicyMiddleware re-authorizes.
                for mw in self._mws:
                    await mw.on_tool_call_post_mutation(tool_name, args, ctx)

                # Phase 5: terminal.
                result = await terminal(tool_name, args, ctx)

                # Phase 6: response hooks, R→L.
                resp = Response(text=str(result), payload=result)
                for mw in reversed(ran):
                    resp = await mw.on_response(resp, ctx)
                return resp.payload
            finally:
                for mw in reversed(ran):
                    try:
                        await mw.on_call_end(ctx)
                    except Exception as secondary:
                        mw_name = getattr(mw, "name", type(mw).__name__)
                        _LOG.warning(
                            "secondary failure in %s.on_call_end during tool call finalization",
                            mw_name,
                            exc_info=secondary,
                        )
        except Exception as exc:
            await self._on_error(ran, exc, ctx)
            raise

    def _build_tool_request(self, tool_name: str, args: dict[str, Any]) -> Request:
        """Construct an audit-observable Request for the tool invocation.

        The ``args`` carried here are for observability / audit only;
        the canonical ``args`` flow through Phases 2-5 as a separate
        parameter so middleware mutation of ``Request.metadata`` cannot
        bypass authorization (see dev-guide §3.9).
        """
        return Request(
            model="(tool)",
            messages=[],
            metadata={
                "operation_name": "invoke_tool",
                "tool_name": tool_name,
                "tool_args": args,
            },
        )

    async def _on_error(self, ran: list[Middleware], exc: Exception, ctx: Context) -> None:
        """Run on_error in reverse order, surfacing any secondary failures.

        A middleware's own ``on_error`` may itself raise — historically we
        swallowed those with ``except Exception: pass``, which hid genuine
        bugs (timeouts, broken transports) from operators.

        We log every secondary at WARNING with ``exc_info``. We do NOT set
        ``exc.__context__ = secondary``: that is semantically inverted
        (``Y.__context__ = X`` reads as "Y was raised while handling X",
        but here ``secondary`` is the one raised while handling ``exc``),
        and assigning in a loop would clobber every prior secondary,
        leaving only the last one visible.

        Instead, we attach a PEP 678 note on the primary for each
        secondary. Notes survive re-raise and render at the bottom of the
        traceback, so operators see every middleware whose ``on_error``
        failed — without inverting Python's exception-chaining semantics.
        """
        for mw in reversed(ran):
            try:
                await mw.on_error(exc, ctx)
            except Exception as secondary:
                mw_name = getattr(mw, "name", type(mw).__name__)
                _LOG.warning(
                    "middleware %s.on_error raised: %s",
                    mw_name,
                    secondary,
                    exc_info=True,
                )
                # PEP 678: attach a note so the secondary is visible in
                # the traceback even though only the primary is re-raised.
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"on_error secondary in {mw_name}: {type(secondary).__name__}: {secondary}"
                    )
