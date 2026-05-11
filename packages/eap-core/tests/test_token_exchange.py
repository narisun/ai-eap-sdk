from typing import Any

import httpx
import pytest

from eap_core.identity.token_exchange import OIDCTokenExchange


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


async def test_token_exchange_posts_rfc8693_grant_and_returns_access_token():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        body = httpx.QueryParams(req.content.decode())
        captured["body"] = dict(body)
        return httpx.Response(
            200, json={"access_token": "exchanged-token", "expires_in": 60, "token_type": "Bearer"}
        )

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    token = await ex.exchange(
        subject_token="initial-jwt", audience="api.bank", scope="read:accounts"
    )
    assert token == "exchanged-token"
    assert captured["url"] == "https://idp.example/token"
    assert captured["body"]["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert captured["body"]["subject_token"] == "initial-jwt"
    assert captured["body"]["audience"] == "api.bank"
    assert captured["body"]["scope"] == "read:accounts"


async def test_token_exchange_raises_on_idp_error():
    def handler(req):
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    with pytest.raises(Exception, match="invalid_grant"):
        await ex.exchange(subject_token="x", audience="y", scope="z")


async def test_token_exchange_aclose():
    def handler(req):
        return httpx.Response(200, json={"access_token": "tok"})

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    await ex.aclose()  # should complete without error


async def test_token_exchange_non_json_error_body():
    def handler(req):
        return httpx.Response(503, text="Service Unavailable")

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    # Non-JSON body: the error field comes from resp.text ("Service Unavailable")
    with pytest.raises(Exception):
        await ex.exchange(subject_token="x", audience="y", scope="z")


@pytest.mark.parametrize(
    "body, expected_msg",
    [
        ({}, "access_token missing"),
        ({"access_token": None}, "access_token must be a non-empty string"),
        ({"access_token": ""}, "access_token must be a non-empty string"),
        ({"access_token": 42}, "access_token must be a non-empty string"),
    ],
)
async def test_token_exchange_rejects_malformed_response(body, expected_msg):
    """H14: a malformed token-exchange response (missing access_token,
    null access_token, non-string access_token) must surface as
    ``IdentityError`` — not propagate as ``KeyError`` or ``TypeError``
    from raw dict access. Callers catching ``IdentityError`` for token
    exchange failures no longer need a parallel
    ``except (KeyError, TypeError)`` block.
    """
    from eap_core.exceptions import IdentityError

    def handler(req):
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    with pytest.raises(IdentityError, match=expected_msg):
        await ex.exchange(subject_token="x", audience="y", scope="z")


async def test_token_exchange_rejects_non_object_response():
    """H14: a 200-OK response whose JSON body is not an object (e.g. a
    JSON array or scalar) must raise ``IdentityError`` rather than crash
    inside ``data["access_token"]`` with a ``TypeError``."""
    from eap_core.exceptions import IdentityError

    def handler(req):
        return httpx.Response(200, json=["not", "an", "object"])

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    with pytest.raises(IdentityError, match="not a JSON object"):
        await ex.exchange(subject_token="x", audience="y", scope="z")
