"""Tests for BearerTokenAuth — identity-aware httpx auth flow.

The class is the v1.3 seam between EAP-Core's existing
``IdentityToken`` Protocol (``get_token(audience, scope) -> str``) and
the upstream Streamable-HTTP / legacy-SSE MCP transports, both of
which accept an ``httpx.Auth`` instance via the ``auth`` keyword.

These unit tests use a minimal stub identity (no real IdP round-trip)
to validate:

- Construction with a valid identity succeeds.
- Construction with an invalid identity (no ``get_token``) raises
  ``TypeError`` at the boundary — the failure surfaces at
  ``BearerTokenAuth(...)`` rather than at first HTTP request.
- Both the sync and async ``httpx.Auth`` flows attach the Bearer
  header.
- Each request triggers a fresh ``get_token`` call (the identity
  layer owns caching; this adapter does not add its own).
- ``BearerTokenAuth`` subclasses ``httpx.Auth`` so
  ``httpx.AsyncClient(auth=...)`` accepts it (Step 3.2 design
  decision).
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import httpx
import pytest

from eap_core.mcp.client import BearerTokenAuth


class _StubIdentity:
    """Minimal IdentityToken Protocol stand-in.

    Sync ``get_token`` matches the ``VertexAgentIdentityToken`` shape
    and the documented BearerTokenAuth runtime expectation. The
    ``calls`` list lets tests assert audience/scope pass-through.
    """

    def __init__(self, token: str = "fake-token") -> None:
        self.token = token
        self.calls: list[tuple[str | None, str]] = []

    def get_token(self, *, audience: str | None = None, scope: str = "") -> str:
        self.calls.append((audience, scope))
        return self.token


def test_construction_with_valid_identity_succeeds() -> None:
    identity = _StubIdentity()
    auth = BearerTokenAuth(identity, audience="mcp.x.com", scope="read")
    assert auth.name == "bearer_token"


def test_construction_with_invalid_identity_raises_typeerror() -> None:
    """Caller passed something that doesn't have ``.get_token``.

    Validating at construction time means the failure surfaces where
    the user wrote the bad code, not deep inside the httpx flow on
    the first request.
    """
    with pytest.raises(TypeError, match="get_token"):
        BearerTokenAuth(object())  # no get_token method


def test_construction_with_non_callable_get_token_raises_typeerror() -> None:
    """``getattr(identity, 'get_token', None)`` returning a non-callable
    (e.g. a plain string attribute) is rejected — the check is
    ``callable(...)``, not just attribute presence.
    """

    class _Bad:
        get_token = "not a method"

    with pytest.raises(TypeError, match="get_token"):
        BearerTokenAuth(_Bad())


def test_sync_auth_flow_attaches_bearer_header() -> None:
    """Sync flow yields the request with Authorization header set."""
    identity = _StubIdentity(token="xyz")
    auth = BearerTokenAuth(identity, audience="x", scope="y")
    request = Mock(headers={})
    flow = auth.sync_auth_flow(request)
    next(flow)
    assert request.headers["Authorization"] == "Bearer xyz"
    # Verify the audience/scope passed through.
    assert identity.calls == [("x", "y")]


def test_async_auth_flow_attaches_bearer_header() -> None:
    """Async flow attaches the same header.

    The flow is an async generator; we step it via ``anext`` (or
    ``asend(None)``) inside a running event loop.
    """
    identity = _StubIdentity(token="async-token")
    auth = BearerTokenAuth(identity, audience=None, scope="")
    request = Mock(headers={})

    async def _drive() -> None:
        flow = auth.async_auth_flow(request)
        await flow.__anext__()

    asyncio.run(_drive())
    assert request.headers["Authorization"] == "Bearer async-token"
    assert identity.calls == [(None, "")]


def test_token_fetched_per_request() -> None:
    """Each request triggers a fresh ``get_token`` call.

    Token caching is the identity layer's responsibility —
    ``BearerTokenAuth`` doesn't add its own cache. Two requests
    therefore produce two ``get_token`` calls.
    """
    identity = _StubIdentity()
    auth = BearerTokenAuth(identity, audience="x")
    for _ in range(2):
        request = Mock(headers={})
        next(auth.sync_auth_flow(request))
    assert len(identity.calls) == 2


def test_bearer_token_auth_is_httpx_auth_subclass() -> None:
    """Step 3.2 design decision: subclass ``httpx.Auth``.

    ``httpx.AsyncClient._build_auth`` uses ``isinstance(auth, Auth)``
    to recognise a custom auth instance, so a duck-typed class would
    be rejected. Asserting the subclass relation here pins the design
    decision against accidental regression — if someone refactors
    ``BearerTokenAuth`` to a plain class, this test fails loudly.
    """
    identity = _StubIdentity()
    auth = BearerTokenAuth(identity, audience="x")
    assert isinstance(auth, httpx.Auth)


def test_default_audience_and_scope() -> None:
    """No audience/scope kwargs use ``audience=None`` and ``scope=""``.

    These are the documented defaults — useful when the identity
    implementation has a ``default_audience`` set (NHI's
    ``default_audience`` field, for example) and the scope is empty.
    """
    identity = _StubIdentity(token="default-tok")
    auth = BearerTokenAuth(identity)
    request = Mock(headers={})
    next(auth.sync_auth_flow(request))
    assert request.headers["Authorization"] == "Bearer default-tok"
    assert identity.calls == [(None, "")]
