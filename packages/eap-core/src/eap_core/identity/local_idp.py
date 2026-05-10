"""LocalIdPStub — in-memory IdP for the walking skeleton.

Issues HS256 JWTs with a fixed secret. Used as the default
``token_endpoint_handler`` for ``OIDCTokenExchange`` when no real IdP
is configured.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt


class LocalIdPStub:
    def __init__(self, secret: str | None = None, token_ttl: int = 300) -> None:
        self._secret = secret or secrets.token_hex(32)
        self._ttl = token_ttl

    def issue(
        self,
        *,
        client_id: str,
        audience: str,
        scope: str,
        roles: list[str] | None = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": "local-idp",
            "sub": client_id,
            "aud": audience,
            "scope": scope,
            "roles": roles or [],
            "iat": now,
            "exp": now + max(self._ttl, 1),
            "jti": secrets.token_hex(8),  # unique per call; ensures distinct JWTs
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, self._secret, algorithms=["HS256"], options={"verify_aud": False})
