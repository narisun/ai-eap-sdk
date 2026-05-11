"""NonHumanIdentity — workload identity for agents."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class IdentityProvider(Protocol):
    """Mint tokens and report their wall-clock expiry.

    The return is ``(token, expires_at)`` — ``expires_at`` MUST be
    wall-clock seconds (``time.time()``-comparable) so callers can match
    it against the JWT's ``exp`` claim. Returning only the token forced
    ``NonHumanIdentity`` to probe a private ``_ttl`` attribute on the
    provider — a layering violation that broke for any IdP without it.
    """

    def issue(
        self,
        *,
        client_id: str,
        audience: str,
        scope: str,
        roles: list[str] | None = None,
    ) -> tuple[str, float]: ...


@dataclass
class TokenCacheEntry:
    token: str
    expires_at: float


@dataclass
class NonHumanIdentity:
    client_id: str
    idp: IdentityProvider
    roles: list[str] = field(default_factory=list)
    default_audience: str | None = None
    cache_buffer_seconds: int = 5
    _cache: dict[tuple[str, str], TokenCacheEntry] = field(default_factory=dict)

    def get_token(self, audience: str | None = None, scope: str = "") -> str:
        aud = audience or self.default_audience
        if aud is None:
            raise ValueError("audience required (no default_audience set)")
        key = (aud, scope)
        entry = self._cache.get(key)
        # ``time.time()`` (wall clock) is comparable to the IdP-issued
        # ``expires_at``; the previous ``time.monotonic()`` mixed clock
        # domains and made the cache TTL incoherent with the JWT exp claim.
        if entry and entry.expires_at - self.cache_buffer_seconds > time.time():
            return entry.token
        token, expires_at = self.idp.issue(
            client_id=self.client_id,
            audience=aud,
            scope=scope,
            roles=self.roles,
        )
        self._cache[key] = TokenCacheEntry(token=token, expires_at=expires_at)
        return token
