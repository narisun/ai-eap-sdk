"""NonHumanIdentity — workload identity for agents."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class IdentityProvider(Protocol):
    def issue(
        self, *, client_id: str, audience: str, scope: str, roles: list[str] | None = None
    ) -> str: ...


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
        if entry and entry.expires_at - self.cache_buffer_seconds > time.monotonic():
            return entry.token
        token = self.idp.issue(
            client_id=self.client_id, audience=aud, scope=scope, roles=self.roles
        )
        ttl = getattr(self.idp, "_ttl", 300)
        self._cache[key] = TokenCacheEntry(token=token, expires_at=time.monotonic() + ttl)
        return token
