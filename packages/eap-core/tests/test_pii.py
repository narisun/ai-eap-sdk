from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.types import Context, Message, Request, Response


async def test_masks_email_and_ssn_in_request():
    mw = PiiMaskingMiddleware()
    req = Request(
        model="m",
        messages=[
            Message(role="user", content="Email me at jane.doe@example.com or call 555-12-3456")
        ],
    )
    ctx = Context()
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "jane.doe@example.com" not in text
    assert "555-12-3456" not in text
    assert "<EMAIL_" in text and "<SSN_" in text
    assert len(ctx.vault) == 2


async def test_unmasks_response_via_vault():
    mw = PiiMaskingMiddleware()
    req = Request(
        model="m",
        messages=[Message(role="user", content="contact jane.doe@example.com")],
    )
    ctx = Context()
    await mw.on_request(req, ctx)
    token = next(iter(ctx.vault))
    resp = Response(text=f"I will email {token} now.")
    out = await mw.on_response(resp, ctx)
    assert "jane.doe@example.com" in out.text


async def test_response_without_tokens_is_unchanged():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    resp = Response(text="nothing to see here")
    out = await mw.on_response(resp, ctx)
    assert out.text == "nothing to see here"


async def test_vault_is_per_context_not_shared():
    mw = PiiMaskingMiddleware()
    ctx_a = Context()
    ctx_b = Context()
    await mw.on_request(
        Request(model="m", messages=[Message(role="user", content="a@x.com")]), ctx_a
    )
    await mw.on_request(
        Request(model="m", messages=[Message(role="user", content="b@y.com")]), ctx_b
    )
    assert "a@x.com" in ctx_a.vault.values()
    assert "a@x.com" not in ctx_b.vault.values()


async def test_on_stream_chunk_replaces_tokens():
    from eap_core.types import Chunk

    mw = PiiMaskingMiddleware()
    ctx = Context()
    # Pre-populate vault with a token
    ctx.vault["<EMAIL_abc12345>"] = "secret@example.com"
    chunk = Chunk(index=0, text="reply to <EMAIL_abc12345> please")
    out = await mw.on_stream_chunk(chunk, ctx)
    assert "secret@example.com" in out.text
    assert "<EMAIL_abc12345>" not in out.text


async def test_on_stream_chunk_no_vault_passthrough():
    from eap_core.types import Chunk

    mw = PiiMaskingMiddleware()
    ctx = Context()
    chunk = Chunk(index=0, text="no tokens here")
    out = await mw.on_stream_chunk(chunk, ctx)
    assert out.text == "no tokens here"


async def test_mask_message_with_multipart_content():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    msg = Message(
        role="user", content=[{"type": "text", "text": "call 555-12-3456"}, {"type": "image"}]
    )
    req = Request(model="m", messages=[msg])
    masked = await mw.on_request(req, ctx)
    # The text part should be masked
    parts = masked.messages[0].content
    assert isinstance(parts, list)
    assert "555-12-3456" not in str(parts)


# ---------------------------------------------------------------------- H10
# New PII categories: IPv4, Amex 15-digit, international phone, bare
# US phone (no country code). IBAN deferred to Presidio (poor regex
# precision).


async def test_masks_ipv4():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="server 10.0.0.1 is down")])
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "10.0.0.1" not in text
    assert "<IPV4_" in text


async def test_masks_amex_15_digit():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m", messages=[Message(role="user", content="charge 378282246310005 please")]
    )
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "378282246310005" not in text
    assert "<AMEX_" in text


async def test_masks_international_phone():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m", messages=[Message(role="user", content="call me on +44 20 7946 0958 today")]
    )
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "+44 20 7946 0958" not in text
    assert "<PHONE_INTL_" in text


# Bare US phone numbers (no leading country code) must also be masked.


async def test_masks_us_phone_with_parens():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="call (415) 555-1234 today")])
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "(415) 555-1234" not in text
    assert "<PHONE_US_" in text


async def test_masks_us_phone_dashed():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="call 415-555-1234 today")])
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "415-555-1234" not in text
    assert "<PHONE_US_" in text


async def test_masks_us_phone_spaced():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="call 415 555 1234 today")])
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "415 555 1234" not in text
    assert "<PHONE_US_" in text


async def test_masks_us_phone_with_country_code():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="call +1 415 555 1234 today")])
    masked = await mw.on_request(req, ctx)
    text = masked.messages[0].content
    assert "415 555 1234" not in text
    # Either PHONE_INTL (anchored by +) or PHONE_US may claim it depending
    # on regex iteration order; both labels are acceptable here.
    assert "<PHONE_INTL_" in text or "<PHONE_US_" in text


# ---------------------------------------------------------------------- H11
# Unmask robustness, masked_count metadata, wider tokens.


async def test_unmask_handles_overlapping_tokens():
    """A token that is a prefix of another must not be matched before
    the longer one — single-regex alternation with longest-first ordering
    guarantees this regardless of dict insertion order.
    """
    mw = PiiMaskingMiddleware()
    vault = {"<EMAIL_aa>": "short@x.com", "<EMAIL_aabb>": "longer@x.com"}
    result = mw._unmask("<EMAIL_aabb> and <EMAIL_aa>", vault=vault)
    assert "longer@x.com" in result
    assert "short@x.com" in result
    # And no leftover token fragments.
    assert "<EMAIL_aabb>" not in result
    assert "<EMAIL_aa>" not in result


async def test_unmask_response_with_overlapping_tokens_via_middleware():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    ctx.vault["<EMAIL_aa>"] = "short@x.com"
    ctx.vault["<EMAIL_aabb>"] = "longer@x.com"
    resp = Response(text="primary <EMAIL_aabb>, fallback <EMAIL_aa>")
    out = await mw.on_response(resp, ctx)
    assert "longer@x.com" in out.text
    assert "short@x.com" in out.text


async def test_metadata_masked_count_is_populated():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[
            Message(role="user", content="a@x.com b@y.com 555-12-3456"),
        ],
    )
    await mw.on_request(req, ctx)
    assert ctx.metadata["pii.masked_count"] == 3


async def test_metadata_masked_count_zero_when_no_pii():
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="hello world")])
    await mw.on_request(req, ctx)
    assert ctx.metadata["pii.masked_count"] == 0


async def test_token_width_is_16_hex():
    """Wider token (16 hex chars) reduces collision probability (H11)."""
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="contact a@x.com")])
    await mw.on_request(req, ctx)
    token = next(iter(ctx.vault))
    # token shape: <EMAIL_<16-hex>>
    assert token.startswith("<EMAIL_")
    assert token.endswith(">")
    hex_part = token[len("<EMAIL_") : -1]
    assert len(hex_part) == 16
    assert all(c in "0123456789abcdef" for c in hex_part)


# ---------------------------------------------------------------------- H12
# Streaming unmask buffering across chunk boundaries.


async def test_on_stream_chunk_buffers_partial_token_across_chunks():
    from eap_core.types import Chunk

    mw = PiiMaskingMiddleware()
    ctx = Context()
    # 16-hex token to match the new width.
    token = "<EMAIL_abcdef0123456789>"
    ctx.vault[token] = "secret@example.com"

    # Split the token mid-way across two chunks.
    head = "reply to <EMAIL_abcdef01"
    tail = "23456789> please"

    out1 = await mw.on_stream_chunk(Chunk(index=0, text=head), ctx)
    out2 = await mw.on_stream_chunk(Chunk(index=1, text=tail), ctx)

    combined = out1.text + out2.text
    # The original chunk N must not have leaked the partial token-prefix
    # downstream as "<EMAIL_abcdef01" — it should be held in the buffer.
    assert "<EMAIL_abcdef01" not in out1.text
    assert "secret@example.com" in combined
    assert token not in combined


async def test_on_stream_chunk_flushes_buffer_on_finish():
    """If a stray '<' never closes, the final chunk must still emit it."""
    from eap_core.types import Chunk

    mw = PiiMaskingMiddleware()
    ctx = Context()
    out1 = await mw.on_stream_chunk(Chunk(index=0, text="here is a stray <bracket"), ctx)
    out2 = await mw.on_stream_chunk(Chunk(index=1, text=" rest of line", finish_reason="stop"), ctx)
    combined = out1.text + out2.text
    assert "<bracket" in combined
    assert "rest of line" in combined


async def test_on_stream_chunk_does_not_buffer_unboundedly():
    """A chunk with stray '<' and lots of trailing text must not grow the
    buffer past the max-token-width lookback (Issue #2). Without the
    bounded lookback a stray '<' followed by arbitrary text would hold
    the entire tail in ``ctx.metadata['pii._stream_buffer']`` until a
    closing '>' or finish_reason arrived.
    """
    from eap_core.types import Chunk

    mw = PiiMaskingMiddleware()
    ctx = Context()
    out = await mw.on_stream_chunk(Chunk(index=0, text="x" * 1000 + "< " + "y" * 1000), ctx)
    buf = ctx.metadata.get("pii._stream_buffer", "")
    # Buffer must be bounded by max token width (~32 chars) plus a little
    # slack; without the fix it would hold ~1000 chars.
    assert len(buf) < 64, f"buffer grew to {len(buf)}"
    # And the emitted text covers the bulk of the input (modulo the held
    # tail near the stray '<').
    assert len(out.text) > 1000


def test_pii_unmask_cache_does_not_grow_unboundedly():
    """If a request grows its vault during on_response (e.g. middleware
    that masks new PII mid-stream), the unmask cache must not retain
    a Pattern per intermediate vault size."""
    from eap_core.middleware.pii import PiiMaskingMiddleware
    from eap_core.types import Context

    mw = PiiMaskingMiddleware()
    ctx = Context()
    # Simulate vault growth by calling _unmask repeatedly with growing vault.
    for i in range(50):
        ctx.vault[f"<EMAIL_{i:016x}>"] = f"user{i}@x.com"
        mw._unmask("foo", vault=ctx.vault, ctx=ctx)
    # Cache should hold at most ONE compiled pattern (the latest size), not 50.
    cache_keys = [k for k in ctx.metadata if k.startswith("pii._unmask_cache_")]
    assert len(cache_keys) <= 1
