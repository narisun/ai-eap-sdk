"""Per-request tool-call tracing for the playground UI.

The SDK's ``Middleware`` Protocol exposes only ``on_request``,
``on_response``, ``on_stream_chunk`` and ``on_error`` — there is no
``on_tool_call`` hook. We therefore capture tool invocations via a
**registry wrapper**: ``install_trace`` monkey-patches the loaded
client's ``McpToolRegistry.invoke`` with a traced version that appends
entries to a per-request list stored on a ``ContextVar``.

``PlaygroundTraceMiddleware`` sits at the front of the middleware
pipeline and resets the ``ContextVar`` on every ``on_request`` so each
``generate_text`` / ``invoke_tool`` call starts with a fresh trace.
After the call returns the playground server reads the trace via
``_current_trace.get()`` and ships it to the frontend.

This module is **playground-local** — it is NOT in
``eap_core.middleware``. SDK users who want production tracing should
use ``ObservabilityMiddleware`` (OTel spans). This is a debugging /
visualisation aid scoped to the playground.
"""

from __future__ import annotations

import contextvars
import json
import time
from typing import Any

from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Request, Response

# Per-async-task trace buffer. ``ContextVar`` is async-safe — concurrent
# ``generate_text`` calls (each a separate task) get independent
# buffers. Default ``None`` means "no active trace" — registry wrappers
# silently no-op when there is no buffer to write to.
_current_trace: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "playground_trace", default=None
)
_trace_start: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "playground_trace_start", default=None
)


def get_current_trace() -> list[dict[str, Any]]:
    """Return the active trace list, or an empty list if none."""
    trace = _current_trace.get()
    return list(trace) if trace is not None else []


def _ts_ms() -> float:
    start = _trace_start.get()
    return ((time.perf_counter() - start) * 1000.0) if start is not None else 0.0


def _make_json_safe(value: Any) -> Any:
    """Best-effort JSON normalisation. Non-serialisable values become
    their ``str()`` repr so the frontend never chokes.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _make_json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_make_json_safe(v) for v in value]
        return str(value)


class PlaygroundTraceMiddleware(PassthroughMiddleware):
    """Initialise a fresh per-request trace buffer + start timestamp.

    Each entry recorded into the buffer (by the registry wrapper below)
    has shape::

        {kind, name?, args?, result?, error?, duration_ms?, ts_ms}

    The frontend renders these as a timeline in the trace panel.
    """

    name = "playground_trace"

    async def on_request(self, req: Request, ctx: Context) -> Request:
        trace: list[dict[str, Any]] = []
        _current_trace.set(trace)
        _trace_start.set(time.perf_counter())
        # Also expose on ctx.metadata for any middleware that wants to
        # peek at the running trace mid-request.
        ctx.metadata["playground.trace"] = trace
        trace.append({"kind": "request_start", "ts_ms": 0.0})
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        trace = _current_trace.get()
        if trace is not None:
            trace.append({"kind": "response", "ts_ms": _ts_ms()})
        return resp

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        trace = _current_trace.get()
        if trace is not None:
            trace.append(
                {
                    "kind": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "ts_ms": _ts_ms(),
                }
            )


def install_trace(client: Any) -> None:
    """Wire trace capture into ``client``.

    Two side effects:

    1. Insert ``PlaygroundTraceMiddleware`` at the front of the
       client's pipeline so every request starts with a fresh trace
       buffer.
    2. Monkey-patch the client's ``tool_registry.invoke`` (if any) with
       a traced wrapper that appends one entry per tool call.

    Best-effort: if the client's internals don't match what we expect
    we silently skip — the playground still works, the trace panel is
    just empty for that agent.

    Idempotency caveat:
        Assumes each client owns its own ``_tool_registry`` instance.
        If a future agent shares a registry across clients, this
        function will only attach the registry-level trace wrapper
        once — the second client's tool calls will still be recorded
        via the shared wrapper, but its per-client middleware
        installation runs independently (the
        ``PlaygroundTraceMiddleware`` is inserted into each client's
        own pipeline regardless). Today every example agent
        constructs its own ``McpToolRegistry()``, so this caveat is
        latent rather than active — but the contract is fragile and
        documented here so a future refactor that introduces shared
        registries notices.
    """
    pipeline = getattr(client, "_pipeline", None)
    mws = getattr(pipeline, "_mws", None) if pipeline is not None else None
    if isinstance(mws, list):
        # Idempotent: don't double-install if called twice.
        if not any(isinstance(mw, PlaygroundTraceMiddleware) for mw in mws):
            mws.insert(0, PlaygroundTraceMiddleware())

    registry = getattr(client, "_tool_registry", None)
    if registry is not None and not getattr(registry, "_playground_traced", False):
        _wrap_registry_invoke(registry)


def _wrap_registry_invoke(registry: Any) -> None:
    """Replace ``registry.invoke`` with a traced wrapper.

    The wrapper preserves the original keyword-only ``identity``
    parameter so the SDK's ``requires_auth`` dispatcher gate keeps
    working. Trace entries are only appended when a per-request buffer
    is active (i.e. inside a ``generate_text`` / ``invoke_tool`` call
    on a client that has ``PlaygroundTraceMiddleware`` installed).
    """
    original = registry.invoke

    async def traced_invoke(
        name: str,
        args: dict[str, Any],
        *,
        identity: Any | None = None,
    ) -> Any:
        trace = _current_trace.get()
        start = time.perf_counter()
        try:
            result = await original(name, args, identity=identity)
        except Exception as exc:
            if trace is not None:
                trace.append(
                    {
                        "kind": "tool_error",
                        "name": name,
                        "args": _make_json_safe(args),
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": (time.perf_counter() - start) * 1000.0,
                        "ts_ms": _ts_ms(),
                    }
                )
            raise
        if trace is not None:
            trace.append(
                {
                    "kind": "tool_call",
                    "name": name,
                    "args": _make_json_safe(args),
                    "result": _make_json_safe(result),
                    "duration_ms": (time.perf_counter() - start) * 1000.0,
                    "ts_ms": _ts_ms(),
                }
            )
        return result

    registry.invoke = traced_invoke
    registry._playground_traced = True
