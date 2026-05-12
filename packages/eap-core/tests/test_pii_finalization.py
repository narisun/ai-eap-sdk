"""Regression tests for PII lifecycle finalization (v1.6.2 -> v1.7 follow-up).

Two real bugs flagged in v1.6.2 CHANGELOG's "Follow-up (v1.7 backlog)":

1. PII stream buffer leak on mid-stream exception -- buffer survived in
   ctx.metadata after a failed stream, polluting downstream state.

2. PII vault leak symmetry -- vault relied on ctx GC for cleanup;
   patterns that retain ctx leaked the masking table.
"""

from __future__ import annotations

import logging

import pytest

from eap_core.middleware.pii import _STREAM_BUFFER_KEY, PiiMaskingMiddleware
from eap_core.types import Context, Message, Request


async def test_pii_clears_vault_on_call_end() -> None:
    """After on_call_end fires, the vault should be cleared."""
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content="my email is alice@example.com")],
    )
    await mw.on_request(req, ctx)
    assert ctx.vault, "vault should hold the email mapping after on_request"

    await mw.on_call_end(ctx)
    assert not ctx.vault, "vault must be cleared after on_call_end"


async def test_pii_clears_stream_buffer_on_stream_end() -> None:
    """After on_stream_end fires, the streaming buffer should be cleared."""
    mw = PiiMaskingMiddleware()
    ctx = Context()
    # Simulate a partial vault token straddling chunk boundaries.
    ctx.metadata[_STREAM_BUFFER_KEY] = ""  # empty buffer (normal end-of-stream)

    await mw.on_stream_end(ctx)
    assert _STREAM_BUFFER_KEY not in ctx.metadata, (
        "stream buffer must be cleared at on_stream_end to prevent cross-request leak"
    )


async def test_pii_stream_buffer_with_held_text_logs_warning_then_clears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If on_stream_end finds a non-empty buffer, log a WARNING and clear it.

    The buffer holding text at on_stream_end indicates the upstream
    stopped abruptly with a partial vault token. We drop it to prevent
    cross-request state pollution, but log so operators can diagnose.
    """
    mw = PiiMaskingMiddleware()
    ctx = Context()
    ctx.metadata[_STREAM_BUFFER_KEY] = "<EMAIL_abcd"  # 11 chars of held text

    with caplog.at_level(logging.WARNING, logger="eap_core.middleware.pii"):
        await mw.on_stream_end(ctx)

    assert any(
        "PII stream buffer non-empty" in rec.message and "11 chars" in rec.message
        for rec in caplog.records
    ), "operators must be warned about dropped buffer content"
    assert _STREAM_BUFFER_KEY not in ctx.metadata


async def test_pii_call_end_clears_both_vault_and_buffer() -> None:
    """on_call_end is defense-in-depth: clears the buffer too even though
    on_stream_end normally handles it."""
    mw = PiiMaskingMiddleware()
    ctx = Context()
    # Populate vault via on_request.
    req = Request(
        model="m",
        messages=[Message(role="user", content="ping bob@example.com")],
    )
    await mw.on_request(req, ctx)
    # And inject a stream buffer directly (simulating a path that
    # bypasses on_stream_end somehow).
    ctx.metadata[_STREAM_BUFFER_KEY] = "<EMAIL_xyz"
    assert ctx.vault, "precondition: vault populated"

    await mw.on_call_end(ctx)
    assert _STREAM_BUFFER_KEY not in ctx.metadata, (
        "on_call_end must defensively clear the stream buffer"
    )
    assert not ctx.vault, "on_call_end must clear the vault"
