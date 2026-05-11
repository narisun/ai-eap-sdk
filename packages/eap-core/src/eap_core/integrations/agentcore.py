"""AWS Bedrock AgentCore integration helpers.

See ``docs/integrations/aws-bedrock-agentcore.md`` for the full
positioning and the phased plan.

This module is intentionally thin — it just wires our existing
OTel observability and OIDC token exchange at AgentCore's
endpoints. The middleware chain, runtime adapters, MCP tooling, and
identity primitives are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlparse

from eap_core.identity.token_exchange import OIDCTokenExchange as _BaseOIDCTokenExchange

_LOG = logging.getLogger(__name__)


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return a normalized ``(scheme, host, port)`` origin tuple for ``url``.

    Used for RFC 3986 §3.2.2-correct origin comparison: hosts are
    case-insensitive, and an implicit default port (``443`` for https)
    must compare equal to an explicit ``:443``. Byte-equality on
    ``urlparse(...).netloc`` would falsely reject ``IDP.example`` vs
    ``idp.example`` and ``idp.example`` vs ``idp.example:443``.
    """
    p = urlparse(url)
    host = (p.hostname or "").lower()
    port = p.port if p.port is not None else (443 if p.scheme == "https" else None)
    return (p.scheme, host, port)


def _agentcore_identity_token_endpoint(region: str) -> str:
    """Default AgentCore Identity token-exchange endpoint for a region.

    The exact path is documented at:
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html

    Override via the ``token_endpoint`` argument when calling
    ``OIDCTokenExchange.from_agentcore`` if AWS publishes a different
    URL pattern in your region.
    """
    return f"https://bedrock-agentcore.{region}.amazonaws.com/identity/token"


class OIDCTokenExchange(_BaseOIDCTokenExchange):
    """OIDCTokenExchange with an AgentCore Identity factory.

    Use the ``from_agentcore`` classmethod when your IdP is AgentCore
    Identity. The factory just fills in the endpoint URL — everything
    else (RFC 8693 grant, TTL caching, NHI integration) works unchanged.
    """

    @classmethod
    def from_agentcore(
        cls,
        *,
        region: str = "us-east-1",
        workload_identity_id: str | None = None,
        token_endpoint: str | None = None,
        http: Any | None = None,
    ) -> OIDCTokenExchange:
        """Build an OIDCTokenExchange pointed at AgentCore Identity.

        Args:
            region: AWS region the AgentCore tenancy lives in.
            workload_identity_id: Optional, recorded for downstream
                consumers; can also be set via env var
                ``AGENTCORE_WORKLOAD_IDENTITY_ID``.
            token_endpoint: Override the computed endpoint URL.
            http: Optional ``httpx.AsyncClient`` to reuse a connection
                pool across calls.
        """
        endpoint = token_endpoint or _agentcore_identity_token_endpoint(region)
        instance = cls(token_endpoint=endpoint, http=http)
        instance._agentcore_region = region  # type: ignore[attr-defined]
        instance._workload_identity_id = (  # type: ignore[attr-defined]
            workload_identity_id or os.environ.get("AGENTCORE_WORKLOAD_IDENTITY_ID")
        )
        return instance


def configure_for_agentcore(
    *,
    service_name: str | None = None,
    endpoint: str | None = None,
    headers: dict[str, str] | None = None,
) -> bool:
    """Configure the OpenTelemetry SDK to emit traces to AgentCore Observability.

    AgentCore Observability ingests OTLP-compatible traces into
    CloudWatch. When your agent runs *inside* AgentCore Runtime, the
    service typically auto-injects the right OTLP env vars and you do
    not need to call this. When you run elsewhere (local dev, other
    clouds, custom shells), this helper sets up the SDK explicitly.

    Returns ``True`` if the OTel SDK was configured. Returns ``False``
    if the ``[otel]`` extra is not installed (the
    ``ObservabilityMiddleware`` still writes ``gen_ai.*`` attributes
    to ``ctx.metadata`` regardless, so audit and trajectory recording
    work without OTel).

    Args:
        service_name: Logical agent name (sets ``service.name`` resource
            attribute). Defaults to env var ``AGENT_NAME`` or
            ``"eap-core-agent"``.
        endpoint: OTLP endpoint URL. Defaults to env var
            ``OTEL_EXPORTER_OTLP_ENDPOINT``. Inside AgentCore Runtime
            this is injected automatically.
        headers: Extra OTLP headers (e.g. auth). Defaults to env var
            ``OTEL_EXPORTER_OTLP_HEADERS`` (parsed as comma-separated
            ``k=v`` pairs by the SDK).
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False

    name = service_name or os.environ.get("AGENT_NAME", "eap-core-agent")
    resource = Resource.create({"service.name": name})
    provider = TracerProvider(resource=resource)

    exporter_kwargs: dict[str, Any] = {}
    if endpoint is not None:
        exporter_kwargs["endpoint"] = endpoint
    if headers is not None:
        exporter_kwargs["headers"] = headers
    exporter = OTLPSpanExporter(**exporter_kwargs)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return True


# ---------------------------------------------------------------------------
# Phase B — In-process AgentCore service adapters
# ---------------------------------------------------------------------------
#
# All adapters in this section lazy-import ``boto3`` inside their methods.
# Construction must not pull boto3. Real network calls are gated behind
# ``EAP_ENABLE_REAL_RUNTIMES=1`` so tests stay deterministic and CI doesn't
# need AWS credentials.

_AGENTCORE_GUIDE = (
    "AgentCore adapter requires the [aws] extra and AWS credentials. "
    "Set EAP_ENABLE_REAL_RUNTIMES=1 once configured."
)


def _real_runtimes_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


# ---- Memory ----------------------------------------------------------------


class AgentCoreMemoryStore:
    """AWS Bedrock AgentCore Memory backend for the ``MemoryStore`` Protocol.

    Persists per-session short-term memory and long-term cross-session
    facts to AgentCore Memory. Construction is cheap (no I/O); methods
    lazy-import boto3 and call the AgentCore Memory API.

    Live calls are gated behind ``EAP_ENABLE_REAL_RUNTIMES=1``. Without
    the flag, every method raises ``NotImplementedError`` with a clear
    "wire credentials" message — same pattern as the Bedrock / Vertex
    runtime adapters.
    """

    name: str = "agentcore"

    def __init__(
        self,
        *,
        memory_id: str,
        region: str = "us-east-1",
    ) -> None:
        self._memory_id = memory_id
        self._region = region

    def _client(self) -> Any:
        try:
            import boto3
        except ImportError as e:
            raise ImportError(
                "AgentCoreMemoryStore requires the [aws] extra: pip install eap-core[aws]"
            ) from e
        return boto3.client("bedrock-agentcore", region_name=self._region)

    async def remember(self, session_id: str, key: str, value: str) -> None:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover  — exercised in cloud workflow
        client.put_memory_record(  # pragma: no cover
            memoryId=self._memory_id,
            sessionId=session_id,
            recordKey=key,
            recordValue=value,
        )

    async def recall(self, session_id: str, key: str) -> str | None:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        try:  # pragma: no cover
            resp = client.get_memory_record(
                memoryId=self._memory_id,
                sessionId=session_id,
                recordKey=key,
            )
            value = resp.get("recordValue")
            return str(value) if value is not None else None
        except client.exceptions.ResourceNotFoundException:  # pragma: no cover
            return None

    async def list_keys(self, session_id: str) -> list[str]:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        resp = client.list_memory_records(  # pragma: no cover
            memoryId=self._memory_id, sessionId=session_id
        )
        return [r["recordKey"] for r in resp.get("records", [])]  # pragma: no cover

    async def forget(self, session_id: str, key: str) -> None:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        client.delete_memory_record(  # pragma: no cover
            memoryId=self._memory_id, sessionId=session_id, recordKey=key
        )

    async def clear(self, session_id: str) -> None:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        client.delete_memory_session(  # pragma: no cover
            memoryId=self._memory_id, sessionId=session_id
        )


# ---- Code Interpreter ------------------------------------------------------


def register_code_interpreter_tools(
    registry: Any,
    *,
    region: str = "us-east-1",
    session_id: str | None = None,
) -> None:
    """Register AgentCore Code Interpreter MCP tools on a registry.

    Adds three ``@mcp_tool``-decorated functions:

    - ``execute_python(code: str) -> dict`` — Python in an AgentCore sandbox.
    - ``execute_javascript(code: str) -> dict`` — JavaScript runtime.
    - ``execute_typescript(code: str) -> dict`` — TypeScript runtime.

    The dict returns ``{"stdout": str, "stderr": str, "exit_code": int}``.
    Tools call ``bedrock-agentcore`` via boto3 when
    ``EAP_ENABLE_REAL_RUNTIMES=1``; otherwise they raise
    ``NotImplementedError``.

    Tools run through the user's middleware chain when invoked via
    ``client.invoke_tool(...)`` — sanitize / PII / policy / observability
    all apply to the agent-generated code that flows through them. This
    is intentional: code interpretation is one of the highest-risk
    agentic capabilities and must traverse the safety chain.
    """
    from eap_core.mcp.decorator import mcp_tool

    def _execute(language: str, code: str) -> dict[str, Any]:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        try:  # pragma: no cover
            import boto3
        except ImportError as e:  # pragma: no cover
            raise ImportError("Code Interpreter tools require the [aws] extra") from e
        client = boto3.client("bedrock-agentcore", region_name=region)  # pragma: no cover
        resp = client.invoke_code_interpreter(  # pragma: no cover
            language=language, code=code, sessionId=session_id
        )
        return {  # pragma: no cover
            "stdout": resp.get("stdout", ""),
            "stderr": resp.get("stderr", ""),
            "exit_code": resp.get("exitCode", 0),
        }

    @mcp_tool(description="Execute Python code in an AgentCore Code Interpreter sandbox.")
    async def execute_python(code: str) -> dict[str, Any]:
        return _execute("python", code)

    @mcp_tool(description="Execute JavaScript code in an AgentCore Code Interpreter sandbox.")
    async def execute_javascript(code: str) -> dict[str, Any]:
        return _execute("javascript", code)

    @mcp_tool(description="Execute TypeScript code in an AgentCore Code Interpreter sandbox.")
    async def execute_typescript(code: str) -> dict[str, Any]:
        return _execute("typescript", code)

    registry.register(execute_python.spec)  # type: ignore[attr-defined]
    registry.register(execute_javascript.spec)  # type: ignore[attr-defined]
    registry.register(execute_typescript.spec)  # type: ignore[attr-defined]


# ---- Browser ---------------------------------------------------------------


def register_browser_tools(
    registry: Any,
    *,
    region: str = "us-east-1",
    session_id: str | None = None,
) -> None:
    """Register AgentCore Browser MCP tools on a registry.

    Adds five ``@mcp_tool``-decorated functions for web interaction:

    - ``browser_navigate(url: str) -> dict`` — navigate to a URL.
    - ``browser_click(selector: str) -> dict`` — click a CSS selector.
    - ``browser_fill(selector: str, value: str) -> dict`` — fill an input.
    - ``browser_extract_text(selector: str = "body") -> str`` — read text.
    - ``browser_screenshot() -> dict`` — capture base64-encoded PNG.

    Live calls go through boto3 to ``bedrock-agentcore`` and are gated
    by ``EAP_ENABLE_REAL_RUNTIMES=1``.

    Like Code Interpreter tools, browser operations flow through the
    user's middleware chain on each ``invoke_tool`` call. Policy can
    deny ``browser_navigate`` to specific hostnames; observability
    records every browser action as a span.
    """
    from eap_core.mcp.decorator import mcp_tool

    def _browser_call(action: str, **kwargs: Any) -> dict[str, Any]:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        try:  # pragma: no cover
            import boto3
        except ImportError as e:  # pragma: no cover
            raise ImportError("Browser tools require the [aws] extra") from e
        client = boto3.client("bedrock-agentcore", region_name=region)  # pragma: no cover
        resp = client.invoke_browser_action(  # pragma: no cover
            action=action, sessionId=session_id, **kwargs
        )
        return dict(resp)  # pragma: no cover

    @mcp_tool(description="Navigate the AgentCore Browser to a URL.")
    async def browser_navigate(url: str) -> dict[str, Any]:
        return _browser_call("navigate", url=url)

    @mcp_tool(description="Click an element by CSS selector in the AgentCore Browser.")
    async def browser_click(selector: str) -> dict[str, Any]:
        return _browser_call("click", selector=selector)

    @mcp_tool(description="Fill an input field by CSS selector.")
    async def browser_fill(selector: str, value: str) -> dict[str, Any]:
        return _browser_call("fill", selector=selector, value=value)

    @mcp_tool(description="Extract text from the current page (default: body).")
    async def browser_extract_text(selector: str = "body") -> str:
        result = _browser_call("extract_text", selector=selector)
        return str(result.get("text", ""))

    @mcp_tool(description="Capture a screenshot of the current page as base64 PNG.")
    async def browser_screenshot() -> dict[str, Any]:
        return _browser_call("screenshot")

    registry.register(browser_navigate.spec)  # type: ignore[attr-defined]
    registry.register(browser_click.spec)  # type: ignore[attr-defined]
    registry.register(browser_fill.spec)  # type: ignore[attr-defined]
    registry.register(browser_extract_text.spec)  # type: ignore[attr-defined]
    registry.register(browser_screenshot.spec)  # type: ignore[attr-defined]


# ---- Inbound JWT verification ---------------------------------------------


class InboundJwtVerifier:
    """Verify JWTs issued by AgentCore Identity (or any OIDC IdP).

    Used at the HTTP boundary of an agent — before the request reaches
    the LLM middleware chain. Inside AgentCore Runtime, this is already
    done by the configured inbound authorizer (see
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html);
    you only need this when:

    - Running the agent outside AgentCore Runtime (own infra, Lambda,
      Cloud Run) and you want the same auth model.
    - Doing defense-in-depth: re-verify inside the agent even though
      AgentCore already did at the edge.

    Standard usage with our generated AgentCore handler.py is via the
    FastAPI dependency factory (``jwt_dependency``).
    """

    def __init__(
        self,
        *,
        discovery_url: str,
        issuer: str,
        allowed_audiences: list[str],
        allowed_scopes: list[str] | None = None,
        allowed_clients: list[str] | None = None,
        jwks_cache_ttl_seconds: int = 600,
        clock_skew_seconds: int = 30,
    ) -> None:
        # Audience validation is MANDATORY. The previous default of
        # ``allowed_audiences=None`` silently disabled ``aud`` checking,
        # which means a default-constructed verifier would accept a token
        # minted for ANY agent — a critical authorization gap (C4).
        #
        # ``discovery_url`` MUST be https — plaintext OIDC discovery is a
        # MITM hole. An attacker who can rewrite the discovery doc can
        # point ``jwks_uri`` at their own JWKS and forge tokens (C1).
        # ``issuer`` is required so we can pin the ``iss`` claim during
        # ``jwt.decode`` (C2) and cross-check the discovery doc's
        # advertised issuer.
        parsed = urlparse(discovery_url)
        if parsed.scheme != "https":
            raise ValueError(
                f"discovery_url must be https (got {parsed.scheme!r}); "
                "plaintext OIDC discovery is insecure"
            )
        if not allowed_audiences:
            raise ValueError(
                "InboundJwtVerifier requires at least one allowed_audience — "
                "audience validation is mandatory. Pass the audience(s) your "
                "agent accepts."
            )
        # ``issuer`` may legally be a non-URL identifier (some OIDC providers
        # use opaque strings), but if it parses as a URL the scheme MUST NOT
        # be ``http://`` — pinning ``iss`` to a plaintext URL claims https-only
        # discovery while accepting an http-flavored issuer.
        issuer_parsed = urlparse(issuer)
        if issuer_parsed.scheme == "http":
            raise ValueError(
                f"issuer must not use http:// scheme (got {issuer!r}); "
                "use https:// or a non-URL issuer identifier"
            )
        self._discovery_url = discovery_url
        self._discovery_origin = _origin(discovery_url)
        self._issuer = issuer
        self._allowed_audiences = set(allowed_audiences)
        self._allowed_scopes = set(allowed_scopes or [])
        self._allowed_clients = set(allowed_clients or [])
        self._cache_ttl = jwks_cache_ttl_seconds
        self._clock_skew = clock_skew_seconds
        self._jwks: list[dict[str, Any]] = []
        self._jwks_fetched_at: float = 0.0
        # Single-flight guard for the async refresh path. Concurrent
        # ``averify`` callers on a cold cache all queue on this lock and
        # only one performs the network round-trip; the rest re-check
        # the cache under the lock and skip the fetch.
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    def _validate_discovery_meta(self, meta: dict[str, Any]) -> str:
        """Validate a parsed OIDC discovery doc and return its ``jwks_uri``.

        Shared by sync ``_refresh_jwks`` and async ``_arefresh_jwks`` —
        keeps https / same-origin / advertised-issuer checks in one place
        so a future tightening can't land in one path and miss the other.

        Enforces (C1):

        - The advertised ``jwks_uri`` is https.
        - The advertised ``jwks_uri`` is on the SAME origin as
          ``discovery_url`` (RFC 3986 §3.2.2 case-insensitive host, plus
          implicit-default-port normalization) — we refuse to fetch keys
          from a third-party origin even if the discovery doc tells us to.
        - The discovery doc's ``issuer`` field is REQUIRED (OIDC
          Discovery 1.0 §3) and must match the configured ``issuer``
          (C2 cross-check). A missing field is a §6.6 silent-degrade
          attack vector — reject explicitly.
        """
        jwks_uri = meta.get("jwks_uri")
        if not jwks_uri:
            raise ValueError(f"discovery doc at {self._discovery_url} has no jwks_uri")
        parsed = urlparse(jwks_uri)
        if parsed.scheme != "https":
            raise ValueError(
                f"jwks_uri must be https (got {parsed.scheme!r}); "
                "plaintext JWKS retrieval is insecure"
            )
        jwks_origin = _origin(jwks_uri)
        if jwks_origin != self._discovery_origin:
            raise ValueError(
                f"jwks_uri origin {jwks_origin!r} does not match discovery origin "
                f"{self._discovery_origin!r} — refusing to fetch keys from a "
                "third-party origin (must be on the same host as discovery_url)"
            )
        # Cross-check: the discovery doc MUST advertise its issuer (OIDC
        # Discovery 1.0 §3 makes ``issuer`` REQUIRED) and it must agree
        # with the configured one. A missing field would silently bypass
        # the cross-check; a mismatch means the discovery doc is for a
        # different IdP (or has been tampered with).
        advertised_iss = meta.get("issuer")
        if not advertised_iss:
            raise ValueError(
                f"discovery doc at {self._discovery_url} has no 'issuer' field "
                "(required by OIDC Discovery 1.0)"
            )
        if advertised_iss != self._issuer:
            raise ValueError(
                f"discovery doc issuer {advertised_iss!r} does not match "
                f"configured issuer {self._issuer!r}"
            )
        return str(jwks_uri)

    def _refresh_jwks(self, http_get: Any) -> None:
        """Fetch the JWKS from the discovery URL.

        ``http_get`` is a callable returning a response-like object with
        ``.json()``; injected for testability. In production code call
        ``verify(token, http_get=httpx.get)``.

        Validation (https / same-origin / advertised-issuer cross-check)
        is delegated to :meth:`_validate_discovery_meta` so the sync and
        async refresh paths share one source of truth.
        """
        meta_resp = http_get(self._discovery_url)
        meta = meta_resp.json()
        jwks_uri = self._validate_discovery_meta(meta)
        jwks_resp = http_get(jwks_uri)
        keys = jwks_resp.json().get("keys", [])
        self._jwks = keys
        import time as _time

        self._jwks_fetched_at = _time.time()

    def _maybe_refresh_jwks(self, http_get: Any) -> None:
        """Refresh JWKS if cache is cold or stale.

        Uses the timestamp (not list-truthiness) as the cache-populated
        signal — an IdP that legitimately returns ``{"keys": []}`` would
        cause infinite re-fetch otherwise. Matches the async path's
        invariant; see ``_amaybe_refresh_jwks``.
        """
        import time as _time

        if self._jwks_fetched_at > 0 and (_time.time() - self._jwks_fetched_at) <= self._cache_ttl:
            return
        self._refresh_jwks(http_get)

    def verify(self, token: str, *, http_get: Any | None = None) -> dict[str, Any]:
        """Verify a JWT and return its claims.

        Raises a JWT-flavored exception (from ``PyJWT``) if the token is
        invalid, expired, has the wrong audience/scope/client, or is
        signed by an unknown key.
        """
        import jwt

        if http_get is None:
            import httpx

            http_get = httpx.get
        self._maybe_refresh_jwks(http_get)

        # PyJWT picks the right key by kid header automatically given a JWKS set.
        from jwt.algorithms import RSAAlgorithm

        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        signing_key: Any = None
        for k in self._jwks:
            if k.get("kid") == kid:
                signing_key = RSAAlgorithm.from_jwk(k)
                break
        if signing_key is None:
            raise jwt.InvalidTokenError(f"no JWKS key matches kid={kid!r}")

        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512"],
            audience=list(self._allowed_audiences),
            issuer=self._issuer,
            options={
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "require": ["exp", "iat", "aud", "iss"],
            },
            leeway=self._clock_skew,
        )

        if self._allowed_clients:
            client_id = claims.get("client_id") or claims.get("azp")
            if client_id not in self._allowed_clients:
                raise jwt.InvalidTokenError(f"client_id {client_id!r} not allowed")

        if self._allowed_scopes:
            token_scopes = set((claims.get("scope") or "").split())
            if not (token_scopes & self._allowed_scopes):
                raise jwt.InvalidTokenError("no allowed scope present in token")

        return claims

    async def _arefresh_jwks(self, http: Any) -> None:
        """Async sibling of :meth:`_refresh_jwks`.

        ``http`` is an ``httpx.AsyncClient`` (or a compatible stub exposing
        an awaitable ``get(url)`` returning an object with ``.json()``).
        Validation is delegated to :meth:`_validate_discovery_meta` so
        both refresh paths share one source of truth for the C1/C2
        trust-pinning invariants.
        """
        meta_resp = await http.get(self._discovery_url)
        meta = meta_resp.json()
        jwks_uri = self._validate_discovery_meta(meta)
        jwks_resp = await http.get(jwks_uri)
        self._jwks = jwks_resp.json().get("keys", [])
        import time as _time

        self._jwks_fetched_at = _time.time()

    async def _amaybe_refresh_jwks(self, http: Any) -> None:
        """Refresh JWKS if the cache is cold/stale, single-flighting concurrent callers.

        Double-checked locking: the fast path checks the cache without
        the lock; on a miss we acquire the lock and re-check before
        fetching, so only the first waiter performs the network round-trip.

        The "cache populated" signal is ``_jwks_fetched_at > 0`` (set at
        the end of every successful refresh) rather than the truthiness
        of ``_jwks`` itself — an empty keys list is a valid response and
        must not cause every concurrent caller to re-fetch.
        """
        import time as _time

        if self._jwks_fetched_at > 0 and (_time.time() - self._jwks_fetched_at) <= self._cache_ttl:
            return
        async with self._refresh_lock:
            # Re-check under the lock — another coroutine may have refreshed
            # while we were waiting.
            if (
                self._jwks_fetched_at > 0
                and (_time.time() - self._jwks_fetched_at) <= self._cache_ttl
            ):
                return
            await self._arefresh_jwks(http)

    async def averify(self, token: str, *, http: Any | None = None) -> dict[str, Any]:
        """Async sibling of :meth:`verify`.

        Use this from async FastAPI dependencies (or any code running in an
        event loop) so the JWKS fetch does not block the loop. Pass an
        ``httpx.AsyncClient`` via ``http`` to share a connection pool, or
        accept the default (a temporary ``AsyncClient`` per call, closed
        on return).
        """
        import jwt
        from jwt.algorithms import RSAAlgorithm

        owns_http = http is None
        if http is None:
            import httpx

            http = httpx.AsyncClient()
        try:
            await self._amaybe_refresh_jwks(http)
        finally:
            if owns_http:
                await http.aclose()

        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        signing_key: Any = None
        for k in self._jwks:
            if k.get("kid") == kid:
                signing_key = RSAAlgorithm.from_jwk(k)
                break
        if signing_key is None:
            raise jwt.InvalidTokenError(f"no JWKS key matches kid={kid!r}")

        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512"],
            audience=list(self._allowed_audiences),
            issuer=self._issuer,
            options={
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "require": ["exp", "iat", "aud", "iss"],
            },
            leeway=self._clock_skew,
        )

        if self._allowed_clients:
            client_id = claims.get("client_id") or claims.get("azp")
            if client_id not in self._allowed_clients:
                raise jwt.InvalidTokenError(f"client_id {client_id!r} not allowed")

        if self._allowed_scopes:
            token_scopes = set((claims.get("scope") or "").split())
            if not (token_scopes & self._allowed_scopes):
                raise jwt.InvalidTokenError("no allowed scope present in token")

        return claims


def jwt_dependency(verifier: InboundJwtVerifier) -> Any:
    """Build a FastAPI dependency that verifies the inbound bearer token.

    Usage in your generated AgentCore ``handler.py``::

        from fastapi import Depends
        from eap_core.integrations.agentcore import (
            InboundJwtVerifier,
            jwt_dependency,
        )

        verifier = InboundJwtVerifier(
            discovery_url="https://your-idp/.well-known/openid-configuration",
            issuer="https://your-idp",
            allowed_audiences=["my-agent-audience"],
        )

        @app.post("/invocations", dependencies=[Depends(jwt_dependency(verifier))])
        async def invocations(req: InvocationRequest): ...
    """
    try:
        from fastapi import Depends, HTTPException
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    except ImportError as e:
        raise ImportError(
            "jwt_dependency requires the [a2a] extra (FastAPI). "
            "Install with: pip install eap-core[a2a]"
        ) from e

    bearer = HTTPBearer(auto_error=False)

    async def _dep(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008  — FastAPI dependency-injection pattern requires the call in the default
    ) -> dict[str, Any]:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="missing bearer token")
        try:
            return await verifier.averify(credentials.credentials)
        except Exception as e:
            # M-N5: never reflect internal verifier state (PyJWT error
            # text, attacker-controlled kid values, configuration hints)
            # back to the client. Log at INFO for operators; respond
            # with a fixed string. ``from None`` chains None so the
            # verifier's exception context can't leak via middleware
            # that prints ``__cause__`` or ``__context__``.
            _LOG.info("inbound JWT verification failed: %s", e)
            raise HTTPException(status_code=401, detail="invalid token") from None

    return _dep


# ---------------------------------------------------------------------------
# Phase C — Gateway integration (outbound)
# ---------------------------------------------------------------------------
#
# AgentCore Gateway exposes targets (Lambda / OpenAPI / Smithy / other MCP
# servers) as a single MCP-over-HTTP endpoint. Clients speak standard MCP
# (JSON-RPC 2.0 over HTTPS) — same as any MCP server, just over HTTP instead
# of stdio. See:
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using.html


class GatewayClient:
    """Outbound MCP-over-HTTP client for AWS Bedrock AgentCore Gateway.

    Speaks plain MCP (JSON-RPC 2.0) — the Gateway URL accepts the standard
    ``tools/list`` and ``tools/call`` methods. Any MCP-HTTP server works
    with this client; AgentCore Gateway is the supported configuration.

    Auth is intentionally pluggable. Pass an ``httpx`` auth object for
    SigV4 (AWS-native) or set ``identity`` to a ``NonHumanIdentity`` for
    OAuth Bearer tokens (the client reads an audience-scoped token from
    the NHI's cache on each call).
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        identity: Any | None = None,
        audience: str | None = None,
        scope: str = "",
        http: Any | None = None,
        auth: Any | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        import httpx

        self._url = gateway_url.rstrip("/")
        self._identity = identity
        self._audience = audience or gateway_url
        self._scope = scope
        # Track http-client ownership: callers that supply their own pool
        # keep ownership; we only close pools we created in ``aclose``.
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http = http is None
        self._auth = auth
        self._next_request_id = 0

    async def _bearer_header(self) -> dict[str, str]:
        """Return an Authorization header from the identity when configured.

        Delegates the sync-vs-async ``get_token`` dispatch to
        ``eap_core.identity.resolve_token`` so this client and the Vertex
        sibling share one awaitable-aware shim — no drift if the identity
        Protocol evolves.
        """
        if self._identity is None:
            return {}
        from eap_core.identity import resolve_token

        token = await resolve_token(self._identity, audience=self._audience, scope=self._scope)
        return {"Authorization": f"Bearer {token}"}

    def _next_id(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Send one JSON-RPC 2.0 request to the gateway and return ``result``.

        Raises ``MCPError`` if the gateway returns a JSON-RPC error.
        """
        from eap_core.mcp.types import MCPError

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        headers = {"Content-Type": "application/json", **(await self._bearer_header())}
        # ``auth`` is intentionally pluggable (httpx Auth, callable, tuple, etc.)
        # — narrow its type at the call site with a cast.
        post_kwargs: dict[str, Any] = {"json": payload, "headers": headers}
        if self._auth is not None:
            post_kwargs["auth"] = self._auth
        resp = await self._http.post(self._url, **post_kwargs)
        if resp.status_code >= 400:
            raise MCPError(
                tool_name=str(params.get("name", "<gateway>")),
                message=f"gateway returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise MCPError(
                tool_name=str(params.get("name", "<gateway>")),
                message=f"gateway error {err.get('code')}: {err.get('message')}",
            )
        return body.get("result")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the tools the gateway advertises via ``tools/list``."""
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        result = await self._rpc("tools/list", {})  # pragma: no cover
        return list(result.get("tools", []))  # pragma: no cover

    async def invoke(self, name: str, args: dict[str, Any]) -> Any:
        """Call a tool via ``tools/call`` and return its result."""
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        result = await self._rpc(  # pragma: no cover
            "tools/call", {"name": name, "arguments": args}
        )
        # MCP returns ``content`` (list of TextContent / etc.). For SDK
        # ergonomics, surface text content directly when there's exactly
        # one TextContent; otherwise return the full content list.
        content = result.get("content", [])  # pragma: no cover
        if (  # pragma: no cover
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
        ):
            return content[0].get("text", "")
        return content  # pragma: no cover

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> GatewayClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def add_gateway_to_registry(
    registry: Any,
    gateway: GatewayClient,
    tool_specs: list[dict[str, Any]],
) -> int:
    """Register remote Gateway tools as proxy specs on a local registry.

    Takes ``tool_specs`` (typically the output of ``gateway.list_tools()``)
    and creates an ``McpToolRegistry`` ``ToolSpec`` for each, with the
    spec's ``fn`` closure-bound to ``gateway.invoke(name, args)``.

    After this call, ``client.invoke_tool("<remote_tool>", {...})``
    dispatches through the middleware chain locally (sanitize / PII /
    policy / observability / validate) and then forwards to the gateway.

    Returns the count of tools registered.

    Note: ``tool_specs`` is passed in (rather than fetched here) so this
    helper stays sync-friendly. Typical usage::

        registry = McpToolRegistry()
        gw = GatewayClient(gateway_url=..., identity=nhi)
        specs = await gw.list_tools()
        add_gateway_to_registry(registry, gw, specs)
    """
    from eap_core.mcp.types import ToolSpec

    count = 0
    for spec_dict in tool_specs:
        name = spec_dict.get("name")
        if not name:
            continue

        # Bind name into the closure to avoid late-binding issues.
        def _make_proxy(tool_name: str) -> Any:
            async def _proxy(**kwargs: Any) -> Any:
                return await gateway.invoke(tool_name, kwargs)

            return _proxy

        proxy_fn = _make_proxy(name)
        spec = ToolSpec(
            name=name,
            description=spec_dict.get("description", f"Remote tool via gateway: {name}"),
            input_schema=spec_dict.get("inputSchema") or spec_dict.get("input_schema") or {},
            output_schema=None,
            fn=proxy_fn,
            requires_auth=True,  # remote calls are always auth-required
            is_async=True,
        )
        registry.register(spec)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Phase C — Gateway publishing (inbound)
# ---------------------------------------------------------------------------


def export_tools_as_openapi(
    registry: Any,
    *,
    title: str = "EAP-Core tools",
    version: str = "0.1.0",
    server_url: str = "https://example.com",
) -> dict[str, Any]:
    """Generate an OpenAPI 3.1 spec from all tools registered on ``registry``.

    Each ``@mcp_tool`` becomes a ``POST /tools/<name>`` operation whose
    request body schema is the tool's ``input_schema``. AgentCore Gateway
    accepts OpenAPI specs as a "HTTP target" type; ``eap publish-to-gateway``
    writes this to disk so users can upload it through the AWS console or
    API.

    Returns a Python dict (caller serializes to JSON).
    """
    paths: dict[str, Any] = {}
    for spec in registry.list_tools():
        op_id = spec.name
        request_schema = spec.input_schema or {"type": "object"}
        paths[f"/tools/{spec.name}"] = {
            "post": {
                "operationId": op_id,
                "summary": spec.description,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": request_schema}},
                },
                "responses": {
                    "200": {
                        "description": "Tool result",
                        "content": {
                            "application/json": {"schema": spec.output_schema or {"type": "object"}}
                        },
                    }
                },
                "x-mcp-tool": {"requires_auth": spec.requires_auth},
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "servers": [{"url": server_url}],
        "paths": paths,
    }


# ---------------------------------------------------------------------------
# Phase D — Registry, Payments, Evaluations
# ---------------------------------------------------------------------------
#
# Polish phase: AWS Agent Registry for org-wide tool/agent discovery, AgentCore
# Payments for x402 microtransactions, and bidirectional adapters between our
# eval framework and AgentCore Evaluations. All three lazy-import boto3 and
# gate live calls behind EAP_ENABLE_REAL_RUNTIMES=1.


# ---- Registry --------------------------------------------------------------


class RegistryClient:
    """Publish and discover agents/tools/MCP servers in AWS Agent Registry.

    AWS Agent Registry is a centralized catalog with semantic + keyword
    search across the org. This client wraps the boto3 calls; live
    operations are gated by ``EAP_ENABLE_REAL_RUNTIMES=1``.

    Auth is whatever boto3's default chain resolves (IAM role, AWS_PROFILE,
    static creds). For JWT-based authorization, pass an ``identity`` —
    the bearer token is attached to MCP-mode calls; SigV4 still flows
    through boto3 for the control-plane APIs.
    """

    def __init__(
        self,
        *,
        registry_name: str,
        region: str = "us-east-1",
        identity: Any | None = None,
    ) -> None:
        self._registry_name = registry_name
        self._region = region
        self._identity = identity

    def _client(self) -> Any:
        try:
            import boto3
        except ImportError as e:
            raise ImportError(
                "RegistryClient requires the [aws] extra: pip install eap-core[aws]"
            ) from e
        return boto3.client("bedrock-agentcore-control", region_name=self._region)

    async def publish_agent_card(self, card: Any) -> str:
        """Publish an AgentCard as a registry record.

        Returns the record id. Card is a Pydantic ``AgentCard`` from
        ``eap_core.a2a``; we serialize via ``model_dump()`` for the API.
        """
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        body = card.model_dump()  # pragma: no cover
        resp = client.create_registry_record(  # pragma: no cover
            registryName=self._registry_name,
            recordType="AGENT",
            name=body.get("name", "unnamed"),
            description=body.get("description", ""),
            metadata=body,
        )
        return str(resp.get("recordId", ""))  # pragma: no cover

    async def publish_mcp_server(self, name: str, *, description: str, mcp_endpoint: str) -> str:
        """Publish an MCP server record (e.g. one generated by `eap create-mcp-server`)."""
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        resp = client.create_registry_record(  # pragma: no cover
            registryName=self._registry_name,
            recordType="MCP_SERVER",
            name=name,
            description=description,
            metadata={"mcpEndpoint": mcp_endpoint},
        )
        return str(resp.get("recordId", ""))  # pragma: no cover

    async def get_record(self, name: str) -> dict[str, Any] | None:
        """Fetch a record by name; returns ``None`` if absent."""
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        try:  # pragma: no cover
            resp = client.get_registry_record(registryName=self._registry_name, name=name)
            return dict(resp.get("record", {}))
        except client.exceptions.ResourceNotFoundException:  # pragma: no cover
            return None

    async def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Hybrid semantic + keyword search over the registry.

        Returns a list of record dicts. Each dict has at minimum
        ``name``, ``description``, ``recordType``, plus the metadata
        the publisher attached.
        """
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        resp = client.search_registry_records(  # pragma: no cover
            registryName=self._registry_name, query=query, maxResults=max_results
        )
        return list(resp.get("records", []))  # pragma: no cover

    async def list_records(
        self, *, record_type: str | None = None, max_results: int = 100
    ) -> list[dict[str, Any]]:
        """List all records in the registry; optionally filter by type."""
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        kwargs: dict[str, Any] = {  # pragma: no cover
            "registryName": self._registry_name,
            "maxResults": max_results,
        }
        if record_type is not None:  # pragma: no cover
            kwargs["recordType"] = record_type
        resp = client.list_registry_records(**kwargs)  # pragma: no cover
        return list(resp.get("records", []))  # pragma: no cover


# ---- Payments (x402) -------------------------------------------------------


class PaymentRequired(Exception):  # noqa: N818  — matches HTTP 402 "Payment Required" status name
    """Raised by a tool when an upstream service responds with HTTP 402.

    Carries the payment-required metadata (amount, currency, address,
    callback URL) so a ``PaymentClient`` can sign and retry.

    The x402 protocol returns these fields in the response body when
    the upstream service requires payment.
    """

    def __init__(
        self,
        *,
        amount_cents: int,
        currency: str,
        merchant: str,
        original_url: str,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"payment required: {amount_cents} {currency} to {merchant}")
        self.amount_cents = amount_cents
        self.currency = currency
        self.merchant = merchant
        self.original_url = original_url
        self.raw = raw or {}


class PaymentClient:
    """Pay for x402 microtransactions via AgentCore Payments.

    Wraps the AgentCore Payments API (boto3-backed). A ``PaymentSession``
    holds a budget (``maxSpendAmountCents``) and an expiry; the session
    refuses payments past the budget or expiry.

    Typical use:

        client = PaymentClient(
            wallet_provider_id="my-cdp-wallet",
            max_spend_cents=100,           # $1.00 budget
            session_ttl_seconds=3600,
        )
        await client.start_session()

        try:
            result = await some_tool_that_might_require_payment()
        except PaymentRequired as pr:
            await client.authorize_and_retry(pr)
    """

    def __init__(
        self,
        *,
        wallet_provider_id: str,
        max_spend_cents: int = 100,
        currency: str = "USD",
        session_ttl_seconds: int = 3600,
        region: str = "us-east-1",
    ) -> None:
        self._wallet_provider_id = wallet_provider_id
        self._max_spend_cents = max_spend_cents
        self._currency = currency
        self._ttl = session_ttl_seconds
        self._region = region
        self._session_id: str | None = None
        self._spent_cents = 0

    def _client(self) -> Any:
        try:
            import boto3
        except ImportError as e:
            raise ImportError(
                "PaymentClient requires the [aws] extra: pip install eap-core[aws]"
            ) from e
        return boto3.client("bedrock-agentcore", region_name=self._region)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def spent_cents(self) -> int:
        return self._spent_cents

    @property
    def remaining_cents(self) -> int:
        return max(self._max_spend_cents - self._spent_cents, 0)

    async def start_session(self) -> str:
        """Open a PaymentSession with the configured budget + TTL.

        Returns the session id, which is also kept on the client for
        subsequent ``authorize_and_retry`` calls.
        """
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        resp = client.create_payment_session(  # pragma: no cover
            walletProviderId=self._wallet_provider_id,
            maxSpendAmountCents=self._max_spend_cents,
            currency=self._currency,
            ttlSeconds=self._ttl,
        )
        self._session_id = str(resp.get("sessionId", ""))  # pragma: no cover
        return self._session_id  # pragma: no cover  # type: ignore[return-value]

    def can_afford(self, amount_cents: int) -> bool:
        """Pre-check: would this payment fit within the remaining budget?"""
        return amount_cents <= self.remaining_cents

    async def authorize_and_retry(self, req: PaymentRequired) -> dict[str, Any]:
        """Sign the x402 payment and return a cryptographic receipt.

        Caller is responsible for using the receipt to retry the
        original call (typically by re-issuing the HTTP request with
        the receipt in an ``X-Payment-Receipt`` header). Live behavior:
        calls AgentCore Payments to sign via the configured wallet,
        deducts ``req.amount_cents`` from the session budget, and
        returns the signed proof.

        Raises:
            RuntimeError if the session would exceed budget or has expired.
            PaymentRequired (re-raise) if AgentCore Payments declines.
        """
        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        if self._session_id is None:  # pragma: no cover
            raise RuntimeError("call start_session() before authorize_and_retry()")
        if not self.can_afford(req.amount_cents):  # pragma: no cover
            raise RuntimeError(
                f"payment of {req.amount_cents} {req.currency} would exceed "
                f"remaining budget {self.remaining_cents}"
            )
        client = self._client()  # pragma: no cover
        resp = client.authorize_payment(  # pragma: no cover
            sessionId=self._session_id,
            amountCents=req.amount_cents,
            currency=req.currency,
            merchant=req.merchant,
            originalUrl=req.original_url,
        )
        self._spent_cents += req.amount_cents  # pragma: no cover
        return dict(resp.get("receipt", {}))  # pragma: no cover


# ---- Evaluations adapters --------------------------------------------------


def to_agentcore_eval_dataset(trajectories: list[Any]) -> list[dict[str, Any]]:
    """Convert ``Trajectory`` records to the AgentCore Eval dataset shape.

    AgentCore Evaluations ingests question/answer/contexts/traces. We
    map our ``Trajectory`` fields onto that shape:

    - ``question`` ← ``trajectory.extra["input_text"]`` if present
    - ``answer`` ← ``trajectory.final_answer``
    - ``contexts`` ← ``trajectory.retrieved_contexts``
    - ``trace_id`` ← ``trajectory.request_id``
    - ``steps`` ← serialized via Pydantic

    The output is a list of plain dicts ready for boto3 calls or for
    upload to S3.
    """
    rows: list[dict[str, Any]] = []
    for t in trajectories:
        extra = t.extra or {}
        rows.append(
            {
                "trace_id": t.request_id,
                "question": extra.get("input_text", ""),
                "answer": t.final_answer,
                "contexts": list(t.retrieved_contexts),
                "steps": [s.model_dump() for s in t.steps],
            }
        )
    return rows


class AgentCoreEvalScorer:
    """Score a ``Trajectory`` by calling AgentCore Evaluations.

    Implements the same shape as our other ``Scorer`` types (a ``name``
    attr and an ``async score(traj) -> FaithfulnessResult``), so it
    plugs into ``EvalRunner.scorers`` directly. AgentCore runs its
    LLM-as-a-Judge evaluator with the configured ``evaluator_arn`` and
    we surface the result into our ``FaithfulnessResult`` envelope.

    Built-in evaluator ARNs look like::

        arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness
        arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness

    Custom evaluator ARNs include account + region.
    """

    name: str = "agentcore_eval"

    def __init__(
        self,
        *,
        evaluator_arn: str,
        region: str = "us-east-1",
        scorer_name: str | None = None,
    ) -> None:
        self._evaluator_arn = evaluator_arn
        self._region = region
        if scorer_name is not None:
            self.name = scorer_name

    def _client(self) -> Any:
        try:
            import boto3
        except ImportError as e:
            raise ImportError("AgentCoreEvalScorer requires the [aws] extra") from e
        return boto3.client("bedrock-agentcore", region_name=self._region)

    async def score(self, traj: Any) -> Any:
        """Score a single trajectory via AgentCore Evaluations."""
        from eap_core.eval.faithfulness import FaithfulnessResult

        if not _real_runtimes_enabled():
            raise NotImplementedError(_AGENTCORE_GUIDE)
        client = self._client()  # pragma: no cover
        row = to_agentcore_eval_dataset([traj])[0]  # pragma: no cover
        resp = client.evaluate_trace(  # pragma: no cover
            evaluatorArn=self._evaluator_arn,
            input={
                "question": row["question"],
                "answer": row["answer"],
                "contexts": row["contexts"],
            },
        )
        score_value = float(resp.get("score", 0.0))  # pragma: no cover
        return FaithfulnessResult(  # pragma: no cover
            request_id=traj.request_id,
            score=score_value,
            notes=str(resp.get("explanation", "")),
        )


__all__ = [  # noqa: RUF022 — grouped by phase, not alphabetically
    # Phase A
    "OIDCTokenExchange",
    "configure_for_agentcore",
    # Phase B — Memory
    "AgentCoreMemoryStore",
    # Phase B — Code Interpreter
    "register_code_interpreter_tools",
    # Phase B — Browser
    "register_browser_tools",
    # Phase B — Inbound JWT
    "InboundJwtVerifier",
    "jwt_dependency",
    # Phase C — Gateway
    "GatewayClient",
    "add_gateway_to_registry",
    "export_tools_as_openapi",
    # Phase D — Registry
    "RegistryClient",
    # Phase D — Payments
    "PaymentRequired",
    "PaymentClient",
    # Phase D — Evaluations
    "to_agentcore_eval_dataset",
    "AgentCoreEvalScorer",
]
