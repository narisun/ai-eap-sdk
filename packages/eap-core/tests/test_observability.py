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


async def test_on_request_starts_span_when_tracer_is_present():
    """Covers observability.py:43-46 — when a tracer is wired the
    middleware starts a span via ``tracer.start_span`` and stamps GenAI
    request attributes on it before stashing it on the context.

    The baseline gauntlet runs without ``opentelemetry-api`` installed,
    so the module's import-time tracer probe leaves ``self._tracer`` at
    None. We inject a fake tracer here to exercise the branch that runs
    under a real OTel SDK install.
    """

    class FakeSpan:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}
            self.ended = False

        def set_attribute(self, k: str, v: object) -> None:
            self.attrs[k] = v

        def end(self) -> None:
            self.ended = True

    started_with: list[str] = []
    fake_span = FakeSpan()

    class FakeTracer:
        def start_span(self, name: str) -> FakeSpan:
            started_with.append(name)
            return fake_span

    mw = ObservabilityMiddleware()
    mw._tracer = FakeTracer()
    ctx = Context()
    req = Request(
        model="claude-3-5",
        messages=[Message(role="user", content="hi")],
        metadata={"operation_name": "generate_text"},
    )
    await mw.on_request(req, ctx)
    assert started_with == ["gen_ai.generate_text"]
    assert ctx.span is fake_span
    # Request-time attributes set on the span before downstream
    # middleware runs.
    assert fake_span.attrs["gen_ai.request.model"] == "claude-3-5"
    assert fake_span.attrs["gen_ai.operation.name"] == "generate_text"


async def test_on_response_writes_usage_and_finish_reason_to_span():
    """Covers observability.py:55-60 — when a span is attached, on_response
    stamps the usage metrics + finish_reason and then ends + clears the
    span. Pre-seeding ctx.span lets us exercise the span path without
    requiring the OTel SDK.
    """

    class FakeSpan:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}
            self.ended = False

        def set_attribute(self, k: str, v: object) -> None:
            self.attrs[k] = v

        def end(self) -> None:
            self.ended = True

    mw = ObservabilityMiddleware()
    ctx = Context()
    span = FakeSpan()
    ctx.span = span
    resp = Response(
        text="ok",
        usage={"input_tokens": 5, "output_tokens": 8},
        finish_reason="stop",
    )
    out = await mw.on_response(resp, ctx)
    assert out is resp
    assert span.attrs["gen_ai.usage.input_tokens"] == 5
    assert span.attrs["gen_ai.usage.output_tokens"] == 8
    assert span.attrs["gen_ai.response.finish_reason"] == "stop"
    assert span.ended
    assert ctx.span is None


async def test_on_error_set_attribute_secondary_failure_is_swallowed():
    """Covers observability.py:90-91 — a span whose ``set_attribute``
    raises must not mask the eventual ``end()``. The set_attribute
    failure is swallowed; record_exception still runs; the span is
    still ended and cleared.
    """

    class HostileSpan:
        def __init__(self) -> None:
            self.ended = False
            self.exc_recorded: BaseException | None = None

        def is_recording(self) -> bool:
            return not self.ended

        def set_attribute(self, k: str, v: object) -> None:
            raise RuntimeError("set_attribute is broken in this exporter")

        def record_exception(self, exc: BaseException) -> None:
            self.exc_recorded = exc

        def end(self) -> None:
            self.ended = True

    mw = ObservabilityMiddleware()
    ctx = Context()
    span = HostileSpan()
    ctx.span = span
    err = ValueError("downstream")
    await mw.on_error(err, ctx)
    assert span.ended
    # Despite set_attribute exploding, record_exception still ran.
    assert span.exc_recorded is err
    assert ctx.span is None


async def test_on_error_skips_recording_paths_when_span_is_not_recording():
    """Covers the branch 87->108 — a non-recording span goes straight
    from the ``if recording:`` guard into the ``finally`` block. The
    span must still be ended and cleared, but ``set_attribute`` /
    ``record_exception`` / ``set_status`` must NOT be called.
    """

    class NonRecordingSpan:
        def __init__(self) -> None:
            self.ended = False
            self.attr_calls = 0
            self.exc_calls = 0
            self.status_calls = 0

        def is_recording(self) -> bool:
            return False

        def set_attribute(self, k: str, v: object) -> None:  # pragma: no cover
            self.attr_calls += 1

        def record_exception(self, exc: BaseException) -> None:  # pragma: no cover
            self.exc_calls += 1

        def set_status(self, status: object) -> None:  # pragma: no cover
            self.status_calls += 1

        def end(self) -> None:
            self.ended = True

    mw = ObservabilityMiddleware()
    ctx = Context()
    span = NonRecordingSpan()
    ctx.span = span
    await mw.on_error(ValueError("ignored"), ctx)
    assert span.ended
    assert ctx.span is None
    assert span.attr_calls == 0
    assert span.exc_calls == 0
    assert span.status_calls == 0


async def test_on_error_end_secondary_failure_is_swallowed():
    """Covers observability.py:110-111 — a span whose ``end()`` raises
    must not bubble the exception up to the pipeline. The exception is
    swallowed and ctx.span is still cleared so a re-entrant pipeline
    doesn't see a finished span.
    """

    class EndExplodingSpan:
        def __init__(self) -> None:
            self.attempted_end = False

        def is_recording(self) -> bool:
            return False  # short-circuit the recording branch

        def set_attribute(self, k: str, v: object) -> None:  # pragma: no cover
            pass

        def record_exception(self, exc: BaseException) -> None:  # pragma: no cover
            pass

        def end(self) -> None:
            self.attempted_end = True
            raise RuntimeError("exporter crashed during end()")

    mw = ObservabilityMiddleware()
    ctx = Context()
    span = EndExplodingSpan()
    ctx.span = span
    # The middleware must swallow the secondary failure.
    await mw.on_error(RuntimeError("primary error"), ctx)
    assert span.attempted_end
    assert ctx.span is None


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
