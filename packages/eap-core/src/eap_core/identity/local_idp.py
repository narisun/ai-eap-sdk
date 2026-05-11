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
    ) -> tuple[str, float]:
        """Issue a fresh JWT and return ``(token, expires_at_wall_time)``.

        ``expires_at`` is wall-clock seconds (``time.time()``) so callers can
        compare it directly to the JWT's ``exp`` claim. Previously the stub
        returned only the token and ``NonHumanIdentity`` had to read a
        private ``_ttl`` attribute (layering violation, plus broken for any
        IdP that didn't expose ``_ttl``). The Protocol now surfaces expiry
        explicitly — see ``IdentityProvider`` in ``nhi.py``.
        """
        now = time.time()
        exp = now + max(self._ttl, 1)
        payload: dict[str, Any] = {
            "iss": "local-idp",
            "sub": client_id,
            "aud": audience,
            "scope": scope,
            "roles": roles or [],
            "iat": int(now),
            "exp": int(exp),
            "jti": secrets.token_hex(8),  # unique per call; ensures distinct JWTs
        }
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        return token, exp

    def verify(self, token: str, *, expected_audience: str) -> dict[str, Any]:
        """Verify a JWT and return its claims.

        Args:
            token: the JWT string to verify.
            expected_audience: the audience this verifier accepts. Compared
                literally against the JWT's ``aud`` claim — PyJWT does NOT
                interpret ``'*'`` as a wildcard. There is no opt-out from
                audience validation: callers verifying inbound tokens must
                pass the audience their protected resource expects.
        """
        return jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            audience=expected_audience,
            options={"verify_aud": True, "require": ["exp", "iat", "aud"]},
        )
