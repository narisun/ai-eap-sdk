import pytest

pytest.importorskip("presidio_analyzer")
pytestmark = pytest.mark.extras

from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.types import Context, Message, Request, Response


async def test_presidio_masks_and_unmasks_round_trip():
    mw = PiiMaskingMiddleware(engine="presidio")
    req = Request(
        model="m",
        messages=[Message(role="user", content="My SSN is 456-78-9012 and email john@acme.com")],
    )
    ctx = Context()
    masked = await mw.on_request(req, ctx)
    assert "456-78-9012" not in masked.messages[0].content
    assert "john@acme.com" not in masked.messages[0].content
    assert len(ctx.vault) >= 2
    token = next(iter(ctx.vault))
    resp = await mw.on_response(Response(text=f"Confirmed {token}"), ctx)
    assert any(orig in resp.text for orig in ctx.vault.values())
