from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.types import Context, Message, Request, Response


async def test_middleware_runs_without_otel_installed_as_passthrough():
    mw = ObservabilityMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    out_req = await mw.on_request(req, ctx)
    assert out_req is not None
    out_resp = await mw.on_response(Response(text="ok", usage={"input_tokens": 3}), ctx)
    assert out_resp.text == "ok"


async def test_middleware_records_genai_attributes_in_context():
    mw = ObservabilityMiddleware()
    ctx = Context()
    req = Request(
        model="anthropic.claude-3-5-sonnet",
        messages=[Message(role="user", content="hi")],
        metadata={"operation_name": "generate_text"},
    )
    await mw.on_request(req, ctx)
    assert ctx.metadata["gen_ai.request.model"] == "anthropic.claude-3-5-sonnet"
    assert ctx.metadata["gen_ai.operation.name"] == "generate_text"


async def test_response_records_token_usage():
    mw = ObservabilityMiddleware()
    ctx = Context()
    await mw.on_request(Request(model="m", messages=[Message(role="user", content="hi")]), ctx)
    await mw.on_response(Response(text="ok", usage={"input_tokens": 7, "output_tokens": 12}), ctx)
    assert ctx.metadata["gen_ai.usage.input_tokens"] == 7
    assert ctx.metadata["gen_ai.usage.output_tokens"] == 12


async def test_on_error_no_span_is_noop():
    mw = ObservabilityMiddleware()
    ctx = Context()
    # ctx.span is None; on_error should be a no-op
    await mw.on_error(ValueError("boom"), ctx)


async def test_on_error_with_mock_span():
    mw = ObservabilityMiddleware()
    ctx = Context()

    class FakeSpan:
        def __init__(self):
            self.attrs = {}
            self.ended = False
            self.exc = None

        def set_attribute(self, k, v):
            self.attrs[k] = v

        def record_exception(self, exc):
            self.exc = exc

        def end(self):
            self.ended = True

    ctx.span = FakeSpan()
    span = ctx.span
    exc = RuntimeError("oops")
    await mw.on_error(exc, ctx)
    assert span.attrs.get("gen_ai.error.type") == "RuntimeError"
    assert span.exc is exc
    assert span.ended
    # on_error must clear ctx.span so a re-entrant pipeline doesn't re-use a
    # finished span on the next call.
    assert ctx.span is None


async def test_observability_ends_span_when_subsequent_middleware_raises():
    """If observability starts a span in on_request and a later middleware
    raises, observability's on_error must end the span — otherwise the
    BatchSpanProcessor drops it and the trace is invisible.

    Uses a fake recording span preseeded onto ctx so the test runs in any
    env (no OTel SDK install needed); the H6 invariant is exercised
    regardless. Without an SDK provider the OTel API returns a global
    NonRecordingSpan from ``start_span``, so we can't rely on the real
    middleware-driven span here.
    """

    class FakeSpan:
        def __init__(self) -> None:
            self.ended = False
            self.attributes: dict[str, object] = {}
            self.exception_recorded: BaseException | None = None
            self.status_set: object | None = None

        def is_recording(self) -> bool:
            return not self.ended

        def set_attribute(self, k: str, v: object) -> None:
            self.attributes[k] = v

        def record_exception(self, exc: BaseException) -> None:
            self.exception_recorded = exc

        def set_status(self, status: object) -> None:
            self.status_set = status

        def end(self) -> None:
            self.ended = True

    fake = FakeSpan()
    mw = ObservabilityMiddleware()
    ctx = Context()
    ctx.span = fake  # pre-seed the span as if on_request had created it
    assert fake.is_recording()

    await mw.on_error(RuntimeError("downstream boom"), ctx)
    assert fake.ended, "on_error must end the span"
    assert ctx.span is None, "on_error must clear ctx.span"
    # Recording-path attributes should have been written before end()
    assert fake.exception_recorded is not None
    assert fake.attributes.get("gen_ai.error.type") == "RuntimeError"


async def test_on_error_ends_span_even_if_record_exception_raises():
    """try/finally guarantee: end() must run even if record_exception or
    set_status raises secondarily."""
    mw = ObservabilityMiddleware()
    ctx = Context()

    class ExplodingSpan:
        def __init__(self) -> None:
            self.ended = False

        def is_recording(self) -> bool:
            return not self.ended

        def set_attribute(self, k: str, v: object) -> None:
            pass

        def record_exception(self, exc: BaseException) -> None:
            raise RuntimeError("OTel exporter blew up")

        def set_status(self, status: object) -> None:  # pragma: no cover
            raise RuntimeError("status also blew up")

        def end(self) -> None:
            self.ended = True

    ctx.span = ExplodingSpan()
    span = ctx.span
    # on_error must swallow the secondary failure and still end the span.
    await mw.on_error(RuntimeError("downstream"), ctx)
    assert span.ended
    assert ctx.span is None
