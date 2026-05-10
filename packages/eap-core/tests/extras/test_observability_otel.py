import pytest

pytest.importorskip("opentelemetry.sdk")
pytestmark = pytest.mark.extras

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.types import Context, Message, Request, Response


@pytest.fixture
def memory_exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


async def test_emits_genai_span_with_attributes(memory_exporter):
    mw = ObservabilityMiddleware()
    ctx = Context()
    await mw.on_request(
        Request(
            model="claude-3-5-sonnet",
            messages=[Message(role="user", content="hi")],
            metadata={"operation_name": "generate_text"},
        ),
        ctx,
    )
    await mw.on_response(
        Response(text="ok", usage={"input_tokens": 5, "output_tokens": 9}, finish_reason="stop"),
        ctx,
    )
    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "gen_ai.generate_text"
    assert s.attributes["gen_ai.request.model"] == "claude-3-5-sonnet"
    assert s.attributes["gen_ai.usage.input_tokens"] == 5
    assert s.attributes["gen_ai.usage.output_tokens"] == 9
    assert s.attributes["gen_ai.response.finish_reason"] == "stop"
