"""Local IdP stub for development and testing.

Issues HS256 JWTs signed with a randomly generated per-instance secret
(``secrets.token_hex(32)``). Intended for local development and tests
only — pass ``for_testing=True`` to silence the production-warning;
replace with a real IdP integration in production.
"""

from __future__ import annotations

import secrets
import time
import warnings
from typing import Any

import jwt


class LocalIdPStub:
    def __init__(
        self,
        secret: str | None = None,
        token_ttl: int = 300,
        *,
        for_testing: bool = False,
    ) -> None:
        if not for_testing:
            warnings.warn(
                "LocalIdPStub is not for production. Pass for_testing=True to silence "
                "this warning, or replace with a real IdP integration.",
                category=RuntimeWarning,
                stacklevel=2,
            )
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

    def verify(self, token: str, *, expected_audience: str) -> dict[str, Any]:
        """Verify a JWT and return its claims.

        Args:
            token: the JWT string to verify.
            expected_audience: required — the audience this verifier accepts.
                Pass the audience your protected resource expects. There is
                no opt-out from audience validation; if you intentionally
                don't care about the audience, pass an explicit '*' and let
                your downstream policy layer decide.
        """
        return jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            audience=expected_audience,
            options={"verify_aud": True, "require": ["exp", "iat", "aud"]},
        )
