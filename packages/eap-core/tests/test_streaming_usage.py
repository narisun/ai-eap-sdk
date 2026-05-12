"""Regression tests for streaming usage aggregation (closes v1.7-T3 dormant code + LOW-2)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from eap_core.config import RuntimeConfig
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.runtimes.local import LocalRuntimeAdapter
from eap_core.types import Chunk, Context, Message, Request

pytestmark = pytest.mark.asyncio


async def test_chunk_has_usage_field_with_empty_default() -> None:
    """Chunk.usage defaults to empty dict (strict-additive)."""
    c = Chunk(index=0, text="hi")
    assert c.usage == {}


async def test_local_adapter_emits_usage_on_final_chunk_only() -> None:
    """LocalRuntimeAdapter populates usage on the LAST chunk, empty otherwise.

    Also asserts the final chunk has finish_reason='stop' (closes LOW-2).
    """
    adapter = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo"))
    req = Request(model="echo", messages=[Message(role="user", content="hi there")])
    chunks = [c async for c in adapter.stream(req)]
    assert len(chunks) > 0
    # All non-final chunks have empty usage
    for c in chunks[:-1]:
        assert c.usage == {}, f"chunk {c.index} should have empty usage; got {c.usage}"
    # Final chunk has populated usage AND finish_reason="stop"
    final = chunks[-1]
    assert final.finish_reason == "stop", "final chunk must set finish_reason='stop'"
    assert final.usage, "final chunk must populate usage"
    assert "input_tokens" in final.usage
    assert "output_tokens" in final.usage


async def test_observability_aggregates_usage_into_ctx_metadata() -> None:
    """on_stream_chunk accumulates Chunk.usage entries into ctx.metadata['gen_ai.usage']."""
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    # Two chunks with empty usage, then one with populated usage.
    await mw.on_stream_chunk(Chunk(index=0, text="a", usage={}), ctx)
    await mw.on_stream_chunk(Chunk(index=1, text="b", usage={}), ctx)
    await mw.on_stream_chunk(
        Chunk(
            index=2,
            text="c",
            finish_reason="stop",
            usage={"input_tokens": 5, "output_tokens": 3},
        ),
        ctx,
    )
    assert ctx.metadata["gen_ai.usage"] == {"input_tokens": 5, "output_tokens": 3}


async def test_observability_aggregator_sums_per_chunk_usage() -> None:
    """If future adapters emit per-chunk usage, the accumulator sums correctly."""
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    await mw.on_stream_chunk(Chunk(index=0, text="a", usage={"output_tokens": 2}), ctx)
    await mw.on_stream_chunk(Chunk(index=1, text="b", usage={"output_tokens": 3}), ctx)
    await mw.on_stream_chunk(
        Chunk(index=2, text="c", usage={"input_tokens": 5, "output_tokens": 1}),
        ctx,
    )
    # Sums per key across all chunks
    assert ctx.metadata["gen_ai.usage"] == {"input_tokens": 5, "output_tokens": 6}


async def test_observability_skips_non_int_usage_values_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-int values in chunk.usage are skipped with a WARNING, don't crash the stream.

    Belt-and-suspenders defense (v1.8.1 L1). Pydantic normally rejects
    non-int values in ``Chunk.usage`` at the type boundary, so this
    code path is unreachable via the standard constructor — we use
    ``model_construct`` to bypass validation and verify the runtime
    aggregator behaves defensively if some future adapter slips past
    pydantic (e.g. by mutating ``chunk.usage`` after construction).
    """
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    # Bypass pydantic validation via model_construct so we can inject
    # a contract-violating dict-as-value.
    bad_chunk = Chunk.model_construct(
        index=0,
        text="hi",
        usage={"input_tokens": 5, "audio_tokens": {"channel": 1}},
    )
    with caplog.at_level(logging.WARNING, logger="eap_core.middleware.observability"):
        await mw.on_stream_chunk(bad_chunk, ctx)
    # Valid key aggregated; bad key skipped (no TypeError crash).
    assert ctx.metadata["gen_ai.usage"] == {"input_tokens": 5}
    assert any("audio_tokens" in rec.message and "non-int" in rec.message for rec in caplog.records)


async def test_observability_on_stream_end_lands_span_attrs_from_aggregated_usage() -> None:
    """v1.7-T3's dormant code now lands span attrs from the aggregated dict.

    Set up a fake span via ctx.span = MagicMock(), populate gen_ai.usage,
    fire on_stream_end, assert span.set_attribute called with the
    aggregated values.
    """
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    ctx.span = MagicMock()
    ctx.span.set_attribute = MagicMock()
    ctx.span.end = MagicMock()
    span_ref = ctx.span
    ctx.metadata["gen_ai.usage"] = {"input_tokens": 10, "output_tokens": 7}

    await mw.on_stream_end(ctx)

    # span.set_attribute called for each key
    calls = span_ref.set_attribute.call_args_list
    keys_set = {c.args[0] for c in calls}
    assert "gen_ai.usage.input_tokens" in keys_set
    assert "gen_ai.usage.output_tokens" in keys_set
    span_ref.end.assert_called_once()
    assert ctx.span is None  # cleared after end
