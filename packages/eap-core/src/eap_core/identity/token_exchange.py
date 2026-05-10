"""RFC 8693 OAuth 2.0 token exchange client."""

from __future__ import annotations

import httpx

from eap_core.exceptions import IdentityError

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"  # noqa: S105


class OIDCTokenExchange:
    def __init__(self, token_endpoint: str, http: httpx.AsyncClient | None = None) -> None:
        self._endpoint = token_endpoint
        self._http = http or httpx.AsyncClient()

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
        data: dict[str, str] = resp.json()
        return data["access_token"]

    async def aclose(self) -> None:
        await self._http.aclose()
