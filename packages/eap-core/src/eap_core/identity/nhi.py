"""NonHumanIdentity — workload identity for agents."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


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
    # ``_cache`` and ``_lock`` are runtime state, not configuration — keep
    # them out of ``__repr__`` (noisy) and ``__eq__`` (identity-tainted:
    # two NHIs with identical config would otherwise compare unequal
    # because their per-instance locks differ).
    _cache: dict[tuple[str, str], TokenCacheEntry] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    # H2: serialize the cache-miss path so N concurrent ``get_token`` calls
    # for the same (audience, scope) issue exactly ONE IdP request instead
    # of N. Without this lock, two simultaneous misses both call
    # ``idp.issue(...)`` and the second write overwrites the first —
    # doubling cost against a paid / rate-limited IdP and giving callers
    # different tokens for what should have been a coalesced request.
    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
        compare=False,
    )

    async def get_token(self, audience: str | None = None, scope: str = "") -> str:
        aud = audience or self.default_audience
        if aud is None:
            raise ValueError("audience required (no default_audience set)")
        key = (aud, scope)
        # The lock spans both the cache lookup AND the IdP call. Holding
        # it across the (potentially slow) IdP round-trip is the whole
        # point — anything narrower lets a second caller observe the
        # miss before the first one's write lands.
        async with self._lock:
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


async def resolve_token(
    identity: Any,
    *,
    audience: str | None = None,
    scope: str = "",
) -> str:
    """Await an identity's ``get_token`` whether it's sync or async.

    Supports both ``NonHumanIdentity`` (async since v0.5.0, with per-instance
    asyncio.Lock against duplicate IdP issuance) and sync identities like
    ``VertexAgentIdentityToken`` (wraps Google's sync google-auth). Gateway
    clients use this so both work polymorphically.
    """
    token = identity.get_token(audience=audience, scope=scope)
    if asyncio.iscoroutine(token):
        token = await token
    return str(token)
