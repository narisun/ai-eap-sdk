"""OTel GenAI observability middleware.

Records OpenTelemetry GenAI semantic-convention attributes. Uses the
opentelemetry-api package if available; falls back to a no-op tracer
otherwise. Either way, the same attributes are written to ``ctx.metadata``
so downstream consumers (eval, audit) get the data without depending on OTel.
"""

from __future__ import annotations

from typing import Any

from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Request, Response

# `_otel_trace` is annotated as Any so the optional opentelemetry-api
# integration works regardless of whether the SDK ships py.typed markers
# or whether the package is installed at all. Use absolute submodule
# import to avoid mypy attr-defined warnings on the namespace package.
_otel_trace: Any
try:
    import opentelemetry.trace as _otel_trace_module  # type: ignore[import-not-found,unused-ignore]

    _otel_trace = _otel_trace_module
    _HAS_OTEL = True
except ImportError:  # pragma: no cover
    _otel_trace = None
    _HAS_OTEL = False


class ObservabilityMiddleware(PassthroughMiddleware):
    name = "observability"

    def __init__(self, tracer_name: str = "eap_core") -> None:
        self._tracer_name = tracer_name
        self._tracer: Any = _otel_trace.get_tracer(tracer_name) if _HAS_OTEL else None

    async def on_request(self, req: Request, ctx: Context) -> Request:
        op = req.metadata.get("operation_name", "generate_text")
        ctx.metadata["gen_ai.request.model"] = req.model
        ctx.metadata["gen_ai.operation.name"] = op
        if self._tracer is not None:
            span = self._tracer.start_span(f"gen_ai.{op}")
            span.set_attribute("gen_ai.request.model", req.model)
            span.set_attribute("gen_ai.operation.name", op)
            ctx.span = span
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        usage = resp.usage or {}
        for k in ("input_tokens", "output_tokens"):
            if k in usage:
                ctx.metadata[f"gen_ai.usage.{k}"] = usage[k]
        if ctx.span is not None:
            for k, v in usage.items():
                ctx.span.set_attribute(f"gen_ai.usage.{k}", v)
            if resp.finish_reason:
                ctx.span.set_attribute("gen_ai.response.finish_reason", resp.finish_reason)
            ctx.span.end()
            ctx.span = None
        return resp

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        """End any started span, recording the exception and ERROR status.

        Symmetric with on_request's span start and on_response's span end:
        a span attached to ctx must always be ended and cleared, regardless
        of whether it's recording. Otherwise downstream consumers that
        inspect ``ctx.span`` to detect "did observability run?" see
        different state on success vs. error. The attribute writes
        (set_attribute, record_exception, set_status) are gated on
        ``is_recording()`` because they're meaningful only on recording
        spans.

        The outer try/finally guarantees ``end()`` + ``ctx.span = None``
        always run, even if the inner attribute writes raise something
        exotic.
        """
        span = getattr(ctx, "span", None)
        if span is None:
            return
        # Treat missing ``is_recording`` as "recording" — caller-supplied fake
        # spans in tests may not implement the full OTel Span protocol.
        is_recording_fn = getattr(span, "is_recording", None)
        recording = not callable(is_recording_fn) or is_recording_fn()
        try:
            if recording:
                try:
                    span.set_attribute("gen_ai.error.type", type(exc).__name__)
                except Exception:  # noqa: S110 - secondary failures must not mask end()
                    pass
                try:
                    span.record_exception(exc)
                except Exception:  # noqa: S110 - secondary failures must not mask end()
                    pass
                try:
                    from opentelemetry.trace import (  # type: ignore[import-not-found,unused-ignore]
                        Status,
                        StatusCode,
                    )

                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                except ImportError:
                    pass  # [otel] extra not installed; status is a no-op
                except Exception:  # noqa: S110 - secondary failures must not mask end()
                    pass
        finally:
            try:
                span.end()
            except Exception:  # noqa: S110 - misbehaving fake spans must not propagate
                pass
            ctx.span = None
