"""NonHumanIdentity — workload identity for agents."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IdentityToken(Protocol):
    """Anything that resolves identity tokens for tool/gateway calls.

    Concrete impls: ``NonHumanIdentity`` (async ``get_token``) and
    ``VertexAgentIdentityToken`` (sync ``get_token``). Use
    ``eap_core.identity.resolve_token`` to dispatch polymorphically.

    The Protocol is intentionally structural and minimal — only the
    ``name`` attribute is asserted (used in logs/traces). The
    ``get_token`` shape differs between async (NHI) and sync (Vertex)
    impls; ``resolve_token`` handles that polymorphism via
    ``asyncio.iscoroutine``.
    """

    name: str


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
    # Match ``InboundJwtVerifier.clock_skew_seconds`` (30) so an agent
    # whose JWT exp is 30s ahead of the server's clock does not observe a
    # cached-then-rejected token. v0.5.0's default of 5s was too tight
    # relative to the verifier's 30s skew tolerance — bumped in v0.6.0
    # for consistency (N-N1).
    cache_buffer_seconds: int = 30
    # Used in logs/traces and to satisfy the ``IdentityToken`` Protocol —
    # both ``NonHumanIdentity`` and ``VertexAgentIdentityToken`` carry a
    # ``name`` attribute so ``EnterpriseLLM(identity=...)`` accepts either
    # under a single structural type.
    name: str = "nhi"
    # ``_cache`` and the lock dicts are runtime state, not configuration —
    # keep them out of ``__repr__`` (noisy) and ``__eq__`` (identity-
    # tainted: two NHIs with identical config would otherwise compare
    # unequal because their per-instance locks differ).
    _cache: dict[tuple[str, str], TokenCacheEntry] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    # M-N2: per-(audience, scope) locking so two concurrent ``get_token``
    # calls for DIFFERENT keys do NOT serialize behind a single
    # instance-wide lock. The single ``asyncio.Lock`` in v0.5.0 still
    # single-flighted same-key duplicate issuance, but it also
    # needlessly blocked unrelated audience/scope pairs.
    #
    # Two-level locking:
    #   - ``_locks_mutex`` guards the ``_locks`` dict itself (held only
    #     during dict mutation — never spans an IdP round-trip).
    #   - ``_locks[key]`` is the per-key serializer that single-flights
    #     the cache-miss → ``idp.issue`` path for the same key.
    #
    # Per-(audience, scope) lock dict. Shares lifecycle with ``_cache``
    # above: instance-level, no eviction, sized by the number of distinct
    # (audience, scope) keys the NHI sees over its lifetime. For
    # long-lived agents with a stable audience set this is bounded; for
    # processes where audiences are derived from request data (e.g.
    # per-customer multi-tenant gateways), size for the expected key
    # cardinality.
    _locks: dict[tuple[str, str], asyncio.Lock] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _locks_mutex: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
        compare=False,
    )

    async def _get_key_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        async with self._locks_mutex:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_token(self, audience: str | None = None, scope: str = "") -> str:
        aud = audience or self.default_audience
        if aud is None:
            raise ValueError("audience required (no default_audience set)")
        key = (aud, scope)
        # The per-key lock spans both the cache lookup AND the IdP call.
        # Holding it across the (potentially slow) IdP round-trip is the
        # whole point — anything narrower lets a second caller observe
        # the miss before the first one's write lands. Distinct keys take
        # distinct locks, so unrelated audience/scope pairs run in
        # parallel (M-N2).
        lock = await self._get_key_lock(key)
        async with lock:
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

    Supports both ``NonHumanIdentity`` (async since v0.5.0, with per-key
    asyncio.Lock against duplicate IdP issuance) and sync identities like
    ``VertexAgentIdentityToken`` (wraps Google's sync google-auth). Gateway
    clients use this so both work polymorphically.
    """
    token = identity.get_token(audience=audience, scope=scope)
    if asyncio.iscoroutine(token):
        token = await token
    return str(token)
