"""``httpx.Auth`` adapter that fetches Bearer tokens from an EAP-Core
identity layer.

EAP-Core ships a small ``IdentityToken`` Protocol (see
``eap_core.identity``) that every workload-identity implementation
satisfies — ``NonHumanIdentity``, ``LocalIdPStub``,
``VertexAgentIdentityToken``, ``OIDCTokenExchange``, etc. The Protocol
is one method: ``get_token(audience, scope) -> str``.

``BearerTokenAuth`` wraps any such object as an ``httpx.Auth`` flow
that attaches ``Authorization: Bearer <token>`` to every outgoing
request. Token refresh and caching are the identity layer's
responsibility — this class is intentionally trivial; it calls
``get_token`` once per request and assumes the implementation has
sensible caching semantics.

Design note (Step 3.2 verification): ``httpx.AsyncClient`` calls
``_build_auth`` on the value passed via the ``auth=`` argument, and
that helper uses ``isinstance(auth, httpx.Auth)`` to recognise a
custom auth instance (anything else either matches the
tuple/callable shortcuts or raises ``TypeError``). A duck-typed class
with ``sync_auth_flow``/``async_auth_flow`` methods but no
``httpx.Auth`` ancestry is rejected. ``BearerTokenAuth`` therefore
subclasses ``httpx.Auth`` directly. ``httpx`` is already a core
dependency, so the module-level import is fine.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from typing import Any

import httpx


class BearerTokenAuth(httpx.Auth):
    """Plug an EAP-Core identity into ``httpx`` authentication.

    Usage::

        from eap_core.identity import NonHumanIdentity
        from eap_core.mcp.client import (
            BearerTokenAuth, McpClientPool, McpServerConfig,
        )

        identity = NonHumanIdentity(...)
        cfg = McpServerConfig(
            name="bankdw",
            transport="http",
            url="https://bankdw.example.com/mcp",
            auth=BearerTokenAuth(
                identity,
                audience="mcp.bankdw.example.com",
                scope="read",
            ),
        )

    The ``IdentityToken`` Protocol is duck-typed — any object with a
    callable ``get_token(audience, scope) -> str`` works. The
    constructor validates only that ``.get_token`` exists and is
    callable; this keeps ``auth.py`` decoupled from any specific
    identity implementation so a future JWT-only or SPIFFE-issued
    identity slots in without changes here.
    """

    name = "bearer_token"

    def __init__(
        self,
        identity: Any,
        *,
        audience: str | None = None,
        scope: str = "",
    ) -> None:
        # ``identity`` is typed ``Any`` rather than ``IdentityToken``
        # so this module doesn't hard-import the identity package and
        # so tests can pass stub identities without subclassing the
        # Protocol. Runtime requirement: object with
        # ``get_token(audience, scope) -> str``.
        if not callable(getattr(identity, "get_token", None)):
            raise TypeError(
                "BearerTokenAuth: identity must have a callable .get_token(audience, scope) method"
            )
        self._identity = identity
        self._audience = audience
        self._scope = scope

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """``httpx.Auth`` sync flow: attach token then forward.

        Overrides the base ``sync_auth_flow``/``auth_flow`` pair to
        keep the implementation single-step and free of the base
        class's ``flow.send(response)`` re-entry machinery (which is
        only needed for challenge-response schemes like Digest).
        """
        token = self._identity.get_token(audience=self._audience, scope=self._scope)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """``httpx.Auth`` async flow.

        The existing identity implementations return either sync
        strings (``VertexAgentIdentityToken``) or coroutines
        (``NonHumanIdentity``); the runtime expectation for
        ``BearerTokenAuth`` is that ``get_token`` returns a string
        directly. Async identities should be wrapped by the caller
        (e.g. eagerly resolved before constructing
        ``BearerTokenAuth``) — token caching/refresh is the identity
        layer's responsibility, not this adapter's.
        """
        token = self._identity.get_token(audience=self._audience, scope=self._scope)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
