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
    exc = RuntimeError("oops")
    await mw.on_error(exc, ctx)
    assert ctx.span.attrs.get("gen_ai.error.type") == "RuntimeError"
    assert ctx.span.exc is exc
    assert ctx.span.ended
