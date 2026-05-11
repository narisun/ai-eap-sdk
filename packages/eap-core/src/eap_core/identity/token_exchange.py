"""RFC 8693 OAuth 2.0 token exchange client."""

from __future__ import annotations

from typing import Self

import httpx

from eap_core.exceptions import IdentityError

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"  # noqa: S105


class OIDCTokenExchange:
    def __init__(self, token_endpoint: str, http: httpx.AsyncClient | None = None) -> None:
        self._endpoint = token_endpoint
        # Track http-client ownership: if the caller passed in their own
        # ``http``, we treat that pool as borrowed and refuse to close it
        # on their behalf. ``aclose`` only closes pools we created.
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None

    async def exchange(self, *, subject_token: str, audience: str, scope: str) -> str:
        body = {
            "grant_type": GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": SUBJECT_TOKEN_TYPE,
            "audience": audience,
            "scope": scope,
        }
        resp = await self._http.post(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"error": resp.text}
            raise IdentityError(
                payload.get("error", f"token exchange failed: HTTP {resp.status_code}")
            )
        # H14: validate response shape — malformed responses surface as
        # ``IdentityError`` instead of propagating as ``KeyError`` /
        # ``TypeError`` from raw dict access. Callers catching ``IdentityError``
        # for token-exchange failures no longer need a parallel
        # ``except (KeyError, TypeError)`` block for the malformed-response
        # case (which is functionally the same failure mode: "the IdP
        # didn't give us a usable token").
        data = resp.json()
        if not isinstance(data, dict):
            raise IdentityError(
                f"token exchange response is not a JSON object: {type(data).__name__}"
            )
        if "access_token" not in data:
            raise IdentityError("token exchange response: access_token missing")
        access_token = data["access_token"]
        if not isinstance(access_token, str) or not access_token:
            raise IdentityError("token exchange response: access_token must be a non-empty string")
        return access_token

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
