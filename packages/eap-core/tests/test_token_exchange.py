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
        return httpx.Response(200, json={"access_token": "exchanged-token", "expires_in": 60, "token_type": "Bearer"})

    client = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange(token_endpoint="https://idp.example/token", http=client)
    token = await ex.exchange(subject_token="initial-jwt", audience="api.bank", scope="read:accounts")
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
