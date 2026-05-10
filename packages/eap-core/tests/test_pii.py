import pytest

from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.types import Context, Message, Request, Response


async def test_masks_email_and_ssn_in_request():
    mw = PiiMaskingMiddleware()
    req = Request(
        model="m",
        messages=[Message(role="user", content="Email me at jane.doe@example.com or call 555-12-3456")],
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
