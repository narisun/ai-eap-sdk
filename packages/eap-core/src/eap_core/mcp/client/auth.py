"""``httpx.Auth`` adapter that fetches Bearer tokens from an EAP-Core
identity layer.

EAP-Core ships a small ``IdentityToken`` Protocol (see
``eap_core.identity``) that every workload-identity implementation
satisfies — ``NonHumanIdentity`` (async ``get_token``),
``VertexAgentIdentityToken`` (sync ``get_token``),
``OIDCTokenExchange``, etc. The Protocol is structural and minimal;
``resolve_token`` in ``eap_core.identity`` is the canonical
polymorphic dispatcher (it inspects the return value with
``asyncio.iscoroutine`` and awaits if needed).

``BearerTokenAuth`` wraps any such object as an ``httpx.Auth`` flow
that attaches ``Authorization: Bearer <token>`` to every outgoing
request. Token refresh and caching are the identity layer's
responsibility — this class is intentionally trivial; it calls
``get_token`` once per request and assumes the implementation has
sensible caching semantics.

Async vs sync identities (v1.3): ``NonHumanIdentity.get_token`` is an
``async def``, so calling it returns a coroutine. Under
``httpx.AsyncClient`` (which is what ``streamable_http_client`` and
``sse_client`` use internally), ``async_auth_flow`` detects this with
``inspect.iscoroutine(token)`` and ``await``s the coroutine before
attaching the header. Under ``httpx.Client``, ``sync_auth_flow``
cannot ``await`` and instead raises a clear ``RuntimeError`` directing
the caller to either use an async client or pre-resolve the token —
fail-loud beats writing ``"Bearer <coroutine object>"`` to the wire.
Sync identities (e.g. ``VertexAgentIdentityToken``) work unchanged in
both flows.

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

import inspect
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

        Sync identities (e.g. ``VertexAgentIdentityToken``) return a
        ``str`` directly and are attached as-is. Async identities
        (e.g. ``NonHumanIdentity``) return a coroutine — there is no
        way to ``await`` that here, so the flow raises a clear
        ``RuntimeError`` rather than silently formatting
        ``"Bearer <coroutine object>"`` into the header.
        """
        token = self._identity.get_token(audience=self._audience, scope=self._scope)
        # ``iscoroutinefunction`` catches the ``async def get_token``
        # case (NHI); ``iscoroutine(token)`` also catches a plain
        # function that happens to return a coroutine (rare but real,
        # e.g. a wrapper that returns ``some_async_fn()``). Cover both.
        if inspect.iscoroutine(token):
            # Close the coroutine to avoid the "never awaited" warning
            # that would otherwise fire when the unawaited coroutine
            # is garbage-collected.
            token.close()
            raise RuntimeError(
                "BearerTokenAuth.sync_auth_flow received an async identity "
                "(get_token returned a coroutine). Use this auth under an "
                "httpx.AsyncClient, or wrap the identity to pre-resolve the "
                "token before constructing BearerTokenAuth."
            )
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """``httpx.Auth`` async flow.

        Handles both identity shapes: sync ``get_token`` returns a
        ``str`` and is attached directly; async ``get_token`` returns
        a coroutine which is awaited here before formatting the
        header. This is the load-bearing path because both
        ``streamable_http_client`` and ``sse_client`` use
        ``httpx.AsyncClient`` internally, so async identities like
        ``NonHumanIdentity`` flow through here.
        """
        token = self._identity.get_token(audience=self._audience, scope=self._scope)
        if inspect.iscoroutine(token):
            token = await token
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
