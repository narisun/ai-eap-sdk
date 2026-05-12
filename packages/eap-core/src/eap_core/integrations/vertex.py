"""GCP Vertex AI Agent Engine integration helpers.

See ``docs/integrations/gcp-vertex-agent-engine.md`` for the full
positioning and the phased plan.

This module mirrors the shape of ``eap_core.integrations.agentcore``:
thin wrappers that wire EAP-Core abstractions at Google's endpoints.
Live network calls lazy-import ``google-cloud-aiplatform`` and are
gated behind ``EAP_ENABLE_REAL_RUNTIMES=1``.
"""

from __future__ import annotations

import os
from typing import Any

from eap_core.exceptions import RealRuntimeDisabledError

# Re-export inbound JWT helpers under the Vertex submodule so handler.py
# code generated for the Vertex runtime imports from the matching module
# name. The implementation lives in :mod:`eap_core.integrations.agentcore`
# (OIDC verifiers are cloud-agnostic) — the re-export here is purely
# cosmetic for Vertex-deployed images (N-N2).
from eap_core.integrations.agentcore import (
    InboundJwtVerifier as InboundJwtVerifier,
)
from eap_core.integrations.agentcore import (
    jwt_dependency as jwt_dependency,
)

_VERTEX_GUIDE = (
    "Vertex adapter requires the [gcp] extra and Google Cloud credentials. "
    "Set EAP_ENABLE_REAL_RUNTIMES=1 once configured."
)


def _real_runtimes_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


# ---------------------------------------------------------------------------
# Phase A — Observability + Identity wiring
# ---------------------------------------------------------------------------


def configure_for_vertex_observability(
    *,
    project_id: str | None = None,
    service_name: str | None = None,
    endpoint: str | None = None,
) -> bool:
    """Configure OpenTelemetry to emit traces to Google Cloud Trace / Cloud Observability.

    Vertex Agent Observability ingests OTLP-compatible traces into
    Cloud Trace and visualizes them in the Agent Platform dashboards.
    When your agent runs *inside* Vertex Agent Runtime, the service
    typically auto-injects OTLP env vars and this helper is unnecessary.
    Outside Vertex (local dev, other clouds), configure explicitly.

    Returns ``True`` if the OTel SDK was configured. Returns ``False``
    if the ``[otel]`` extra is not installed (``ObservabilityMiddleware``
    still writes ``gen_ai.*`` attributes to ``ctx.metadata`` regardless).

    Args:
        project_id: GCP project id. Sets the ``gcp.project_id`` resource
            attribute. Defaults to env var ``GOOGLE_CLOUD_PROJECT``.
        service_name: Logical agent name. Defaults to env var
            ``AGENT_NAME`` or ``"eap-core-agent"``.
        endpoint: OTLP endpoint URL. Defaults to env var
            ``OTEL_EXPORTER_OTLP_ENDPOINT``. For Cloud Trace's
            OTLP-compatible endpoint, point at
            ``https://telemetry.googleapis.com``.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False

    resource_attrs: dict[str, Any] = {
        "service.name": service_name or os.environ.get("AGENT_NAME", "eap-core-agent"),
    }
    gcp_project = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if gcp_project:
        resource_attrs["gcp.project_id"] = gcp_project

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)

    exporter_kwargs: dict[str, Any] = {}
    if endpoint is not None:
        exporter_kwargs["endpoint"] = endpoint
    exporter = OTLPSpanExporter(**exporter_kwargs)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return True


class VertexAgentIdentityToken:
    """Acquire a Google Cloud access token for a workload identity.

    Wraps the standard Google auth chain (Application Default
    Credentials → workload identity federation → IAM service account).
    Lazy-imports ``google.auth``. Tokens are fetched on demand and
    auto-refreshed by the underlying library.

    Usage::

        identity = VertexAgentIdentityToken(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        token = identity.get_token()  # blocks once; subsequent calls cached

    For use with ``GatewayClient`` and similar, this matches the
    `get_token(audience=..., scope=...)` shape that ``NonHumanIdentity``
    exposes — the audience argument is ignored (Google tokens are
    audience-implicit via service account).
    """

    name: str = "vertex"

    def __init__(
        self,
        *,
        scopes: list[str] | None = None,
    ) -> None:
        self._scopes = scopes or ["https://www.googleapis.com/auth/cloud-platform"]
        self._cached_creds: Any = None

    def get_token(self, *, audience: str | None = None, scope: str = "") -> str:
        """Return a valid Google access token.

        Both ``audience`` and ``scope`` are accepted for API compatibility
        with ``NonHumanIdentity.get_token`` but are not used — Google
        tokens are scoped at credential-creation time via ``scopes``.
        """
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        try:  # pragma: no cover
            import google.auth
            import google.auth.transport.requests
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "VertexAgentIdentityToken requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e

        if self._cached_creds is None:  # pragma: no cover
            self._cached_creds, _ = google.auth.default(scopes=self._scopes)

        # Refresh if needed (google.auth handles cache + auto-refresh)
        if not self._cached_creds.valid:  # pragma: no cover
            self._cached_creds.refresh(google.auth.transport.requests.Request())
        return str(self._cached_creds.token)  # pragma: no cover


# ---------------------------------------------------------------------------
# Phase B — Memory Bank + Code Execution + Browser Sandbox
# ---------------------------------------------------------------------------


class VertexMemoryBankStore:
    """Vertex AI Memory Bank backend for the ``MemoryStore`` Protocol.

    Vertex Memory Bank persists short-term session memory and long-term
    cross-session facts in a managed store. Construction is cheap (no
    I/O); methods lazy-import ``google-cloud-aiplatform`` and call the
    Memory Bank REST surface.

    Live calls are gated behind ``EAP_ENABLE_REAL_RUNTIMES=1``. Without
    the flag, every method raises ``RealRuntimeDisabledError`` with a
    clear "wire credentials" message.
    """

    name: str = "vertex_memory_bank"

    def __init__(
        self,
        *,
        project_id: str,
        location: str = "us-central1",
        memory_bank_id: str,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._memory_bank_id = memory_bank_id

    def _client(self) -> Any:
        try:
            from google.cloud import aiplatform_v1beta1
        except ImportError as e:
            raise ImportError(
                "VertexMemoryBankStore requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e
        return aiplatform_v1beta1.MemoryBankServiceClient()

    def _parent(self) -> str:
        return (
            f"projects/{self._project_id}/locations/{self._location}"
            f"/memoryBanks/{self._memory_bank_id}"
        )

    async def remember(self, session_id: str, key: str, value: str) -> None:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        client.upsert_memory(
            parent=self._parent(),
            session_id=session_id,
            key=key,
            value=value,
        )

    async def recall(self, session_id: str, key: str) -> str | None:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        # Narrow the swallowed exception to Google's NotFound so credential
        # errors, throttling, and transient API failures propagate to the
        # caller instead of being silently reported as a cache miss (H16).
        # pragma: no cover comments stay — google.api_core is gcp-extra only.
        from google.api_core import exceptions as gax_exceptions  # pragma: no cover

        client = self._client()  # pragma: no cover
        try:  # pragma: no cover
            resp = client.get_memory(parent=self._parent(), session_id=session_id, key=key)
            return str(resp.value) if resp.value else None
        except gax_exceptions.NotFound:  # pragma: no cover
            # Absent key matches the AgentCore "missing => None" contract.
            return None
        # Any other gax exception (auth, throttle, transient) — propagate.

    async def list_keys(self, session_id: str) -> list[str]:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        resp = client.list_memories(parent=self._parent(), session_id=session_id)
        return [m.key for m in resp.memories]

    async def forget(self, session_id: str, key: str) -> None:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        client.delete_memory(parent=self._parent(), session_id=session_id, key=key)

    async def clear(self, session_id: str) -> None:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        client.delete_session(parent=self._parent(), session_id=session_id)


# ---- Code Sandbox ----------------------------------------------------------


class VertexCodeSandbox:
    """Vertex Agent Sandbox (code path) — implements ``CodeSandbox`` Protocol.

    Vertex Agent Sandbox is Google's managed code-execution environment
    for agents. It accepts Python (and a handful of other languages
    depending on the sandbox image) and returns stdout/stderr/exit_code
    plus any GCS artifact URIs produced.

    Live calls are gated behind ``EAP_ENABLE_REAL_RUNTIMES=1`` and
    lazy-import ``google-cloud-aiplatform``.
    """

    name: str = "vertex_code_sandbox"

    def __init__(
        self,
        *,
        project_id: str,
        location: str = "us-central1",
        sandbox_id: str | None = None,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._sandbox_id = sandbox_id

    def _client(self) -> Any:
        try:
            from google.cloud import aiplatform_v1beta1
        except ImportError as e:
            raise ImportError(
                "VertexCodeSandbox requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e
        # SandboxServiceClient exists at runtime but isn't in the
        # type stubs google-cloud-aiplatform ships with.
        return aiplatform_v1beta1.SandboxServiceClient()  # type: ignore[attr-defined]

    async def execute(self, language: str, code: str) -> Any:
        from eap_core.sandbox import SandboxResult

        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        parent = f"projects/{self._project_id}/locations/{self._location}"
        resp = client.execute_code(
            parent=parent,
            language=language,
            code=code,
            sandbox_id=self._sandbox_id,
        )
        return SandboxResult(
            stdout=resp.stdout or "",
            stderr=resp.stderr or "",
            exit_code=int(resp.exit_code or 0),
            artifacts={a.name: a.uri for a in (resp.artifacts or [])},
        )


def register_code_sandbox_tools(
    registry: Any,
    *,
    project_id: str,
    location: str = "us-central1",
    sandbox_id: str | None = None,
) -> None:
    """Register Vertex Agent Sandbox code-execution MCP tools on a registry.

    Adds three ``@mcp_tool``-decorated functions:

    - ``execute_python(code: str) -> dict``
    - ``execute_javascript(code: str) -> dict``
    - ``execute_typescript(code: str) -> dict``

    Like the AgentCore equivalent, tools traverse the user's middleware
    chain on each invoke — sanitize / PII / policy / observability all
    apply to the agent-generated code that flows through them.
    """
    from eap_core.mcp.decorator import mcp_tool

    sandbox = VertexCodeSandbox(project_id=project_id, location=location, sandbox_id=sandbox_id)

    @mcp_tool(description="Execute Python code in a Vertex Agent Sandbox.")
    async def execute_python(code: str) -> dict[str, Any]:
        r = await sandbox.execute("python", code)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.exit_code}

    @mcp_tool(description="Execute JavaScript code in a Vertex Agent Sandbox.")
    async def execute_javascript(code: str) -> dict[str, Any]:
        r = await sandbox.execute("javascript", code)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.exit_code}

    @mcp_tool(description="Execute TypeScript code in a Vertex Agent Sandbox.")
    async def execute_typescript(code: str) -> dict[str, Any]:
        r = await sandbox.execute("typescript", code)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.exit_code}

    registry.register(execute_python.spec)
    registry.register(execute_javascript.spec)
    registry.register(execute_typescript.spec)


# ---- Browser Sandbox -------------------------------------------------------


class VertexBrowserSandbox:
    """Vertex Agent Sandbox (browser path) — implements ``BrowserSandbox``.

    The browser path of Vertex Agent Sandbox runs a managed headless
    browser per session. The shape of this class matches the
    ``BrowserSandbox`` Protocol so agents can swap between
    AgentCore Browser and Vertex Browser by config alone.
    """

    name: str = "vertex_browser_sandbox"

    def __init__(
        self,
        *,
        project_id: str,
        location: str = "us-central1",
        session_id: str | None = None,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._session_id = session_id

    def _client(self) -> Any:
        try:
            from google.cloud import aiplatform_v1beta1
        except ImportError as e:
            raise ImportError(
                "VertexBrowserSandbox requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e
        return aiplatform_v1beta1.SandboxServiceClient()  # type: ignore[attr-defined]

    def _parent(self) -> str:
        return f"projects/{self._project_id}/locations/{self._location}"

    async def _action(self, action: str, **params: Any) -> dict[str, Any]:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        resp = client.invoke_browser_action(
            parent=self._parent(),
            session_id=self._session_id,
            action=action,
            params=params,
        )
        return dict(resp.result) if resp.result else {}

    async def navigate(self, url: str) -> dict[str, Any]:
        return await self._action("navigate", url=url)

    async def click(self, selector: str) -> dict[str, Any]:
        return await self._action("click", selector=selector)

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        return await self._action("fill", selector=selector, value=value)

    async def extract_text(self, selector: str = "body") -> str:
        r = await self._action("extract_text", selector=selector)
        return str(r.get("text", ""))

    async def screenshot(self) -> bytes:
        r = await self._action("screenshot")
        # API returns base64-encoded PNG; decode for the Protocol.
        import base64

        data = r.get("png_base64", "")
        return base64.b64decode(data) if data else b""


def register_browser_sandbox_tools(
    registry: Any,
    *,
    project_id: str,
    location: str = "us-central1",
    session_id: str | None = None,
) -> None:
    """Register Vertex Browser Sandbox MCP tools on a registry.

    Symmetric to ``agentcore.register_browser_tools``. Registers five
    tools: ``browser_navigate``, ``browser_click``, ``browser_fill``,
    ``browser_extract_text``, ``browser_screenshot``.
    """
    from eap_core.mcp.decorator import mcp_tool

    browser = VertexBrowserSandbox(project_id=project_id, location=location, session_id=session_id)

    @mcp_tool(description="Navigate the Vertex Browser Sandbox to a URL.")
    async def browser_navigate(url: str) -> dict[str, Any]:
        return await browser.navigate(url)

    @mcp_tool(description="Click an element by CSS selector.")
    async def browser_click(selector: str) -> dict[str, Any]:
        return await browser.click(selector)

    @mcp_tool(description="Fill an input field by CSS selector.")
    async def browser_fill(selector: str, value: str) -> dict[str, Any]:
        return await browser.fill(selector, value)

    @mcp_tool(description="Extract text from the current page (default: body).")
    async def browser_extract_text(selector: str = "body") -> str:
        return await browser.extract_text(selector)

    @mcp_tool(description="Capture a screenshot of the current page as PNG bytes.")
    async def browser_screenshot() -> dict[str, Any]:
        png = await browser.screenshot()
        import base64

        return {"png_base64": base64.b64encode(png).decode("ascii") if png else ""}

    registry.register(browser_navigate.spec)
    registry.register(browser_click.spec)
    registry.register(browser_fill.spec)
    registry.register(browser_extract_text.spec)
    registry.register(browser_screenshot.spec)


# ---------------------------------------------------------------------------
# Phase C — Gateway (outbound MCP-over-HTTP)
# ---------------------------------------------------------------------------


class VertexGatewayClient:
    """Outbound MCP-over-HTTP client for a Vertex Agent Gateway endpoint.

    Speaks plain MCP (JSON-RPC 2.0). Identical wire protocol to
    ``agentcore.GatewayClient`` — the same client works against any
    MCP-HTTP endpoint, and Vertex's Agent Gateway is the supported
    Google configuration.

    Auth is pluggable. Pass a ``VertexAgentIdentityToken`` for Google
    Bearer auth, or an arbitrary httpx auth callable for other schemes.
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
        ``eap_core.identity.resolve_token`` so this client and the
        AgentCore sibling share one awaitable-aware shim — no drift if
        the identity Protocol evolves.
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
        from eap_core.mcp.types import MCPError

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        headers = {"Content-Type": "application/json", **(await self._bearer_header())}
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
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        result = await self._rpc("tools/list", {})
        return list(result.get("tools", []))

    async def invoke(self, name: str, args: dict[str, Any]) -> Any:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        result = await self._rpc("tools/call", {"name": name, "arguments": args})
        content = result.get("content", [])
        if (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
        ):
            return content[0].get("text", "")
        return content

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> VertexGatewayClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


# ---------------------------------------------------------------------------
# Phase D — Registry, Payments (AP2), Evaluations
# ---------------------------------------------------------------------------


class VertexAgentRegistry:
    """Publish and discover agents/tools/MCP servers in Vertex Agent Registry.

    Implements the ``AgentRegistry`` Protocol against Google's
    Agent Registry (an extension of Vertex Model Registry for agentic
    artifacts). Live calls are gated by ``EAP_ENABLE_REAL_RUNTIMES=1``.
    """

    name: str = "vertex_agent_registry"

    def __init__(
        self,
        *,
        project_id: str,
        location: str = "us-central1",
        registry_id: str = "default",
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._registry_id = registry_id

    def _client(self) -> Any:
        try:
            from google.cloud import aiplatform_v1beta1
        except ImportError as e:
            raise ImportError(
                "VertexAgentRegistry requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e
        return aiplatform_v1beta1.AgentRegistryServiceClient()  # type: ignore[attr-defined]

    def _parent(self) -> str:
        return (
            f"projects/{self._project_id}/locations/{self._location}"
            f"/agentRegistries/{self._registry_id}"
        )

    async def publish(self, record: dict[str, Any]) -> str:
        if "name" not in record:
            raise ValueError("record must have a 'name' field")
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        resp = client.create_registry_record(
            parent=self._parent(),
            record_type=record.get("record_type", "AGENT"),
            name=record["name"],
            description=record.get("description", ""),
            metadata=record,
        )
        return str(resp.record_id)

    async def get(self, name: str) -> dict[str, Any] | None:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        try:
            resp = client.get_registry_record(parent=self._parent(), name=name)
            return dict(resp.record) if resp.record else None
        except Exception:
            return None

    async def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        resp = client.search_registry_records(
            parent=self._parent(), query=query, max_results=max_results
        )
        return [dict(r) for r in resp.records]

    async def list_records(
        self,
        *,
        record_type: str | None = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        kwargs: dict[str, Any] = {
            "parent": self._parent(),
            "max_results": max_results,
        }
        if record_type is not None:
            kwargs["record_type"] = record_type
        resp = client.list_registry_records(**kwargs)
        return [dict(r) for r in resp.records]


# ---- Payments (AP2) --------------------------------------------------------


class AP2PaymentClient:
    """Pay for microtransactions via Google's Agent Payment Protocol (AP2).

    Implements the ``PaymentBackend`` Protocol. AP2 is Google's
    standardized agent-payments scheme — conceptually parallel to AWS's
    x402 — wrapping wallet-provider signing, budget enforcement, and
    receipt issuance behind a managed service.

    The class is shaped to be drop-in compatible with
    ``agentcore.PaymentClient``: same ``start_session`` / ``authorize``
    methods, same ``remaining_cents`` / ``spent_cents`` properties.
    """

    name: str = "ap2_payment"

    def __init__(
        self,
        *,
        wallet_provider_id: str,
        project_id: str,
        max_spend_cents: int,
        location: str = "us-central1",
        currency: str = "USD",
        session_ttl_seconds: int = 3600,
    ) -> None:
        self._wallet_provider_id = wallet_provider_id
        self._project_id = project_id
        self._location = location
        self._max_spend_cents = max_spend_cents
        self._currency = currency
        self._ttl = session_ttl_seconds
        self._session_id: str | None = None
        self._spent_cents = 0

    def _client(self) -> Any:
        try:
            from google.cloud import aiplatform_v1beta1
        except ImportError as e:
            raise ImportError(
                "AP2PaymentClient requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e
        return aiplatform_v1beta1.PaymentServiceClient()  # type: ignore[attr-defined]

    def _parent(self) -> str:
        return f"projects/{self._project_id}/locations/{self._location}"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def spent_cents(self) -> int:
        return self._spent_cents

    @property
    def remaining_cents(self) -> int:
        return max(self._max_spend_cents - self._spent_cents, 0)

    def can_afford(self, amount_cents: int) -> bool:
        return amount_cents <= self.remaining_cents

    async def start_session(self) -> str:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        resp = client.create_payment_session(
            parent=self._parent(),
            wallet_provider_id=self._wallet_provider_id,
            max_spend_amount_cents=self._max_spend_cents,
            currency=self._currency,
            ttl_seconds=self._ttl,
        )
        self._session_id = str(resp.session_id)
        return self._session_id

    async def authorize(self, req: Any) -> dict[str, Any]:
        """Sign an AP2 payment request and return a receipt.

        ``req`` is a ``PaymentRequired`` (from ``eap_core.payments``)
        carrying amount, currency, merchant, and original URL.
        """
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        if self._session_id is None:
            raise RuntimeError("call start_session() before authorize()")
        if not self.can_afford(req.amount_cents):
            raise RuntimeError(
                f"payment of {req.amount_cents} {req.currency} would exceed "
                f"remaining budget {self.remaining_cents}"
            )
        client = self._client()
        resp = client.authorize_payment(
            session_id=self._session_id,
            amount_cents=req.amount_cents,
            currency=req.currency,
            merchant=req.merchant,
            original_url=req.original_url,
        )
        self._spent_cents += req.amount_cents
        return dict(resp.receipt) if resp.receipt else {}


# ---- Evaluations adapters --------------------------------------------------


def to_vertex_eval_dataset(trajectories: list[Any]) -> list[dict[str, Any]]:
    """Convert ``Trajectory`` records to Vertex Gen AI Evaluation Service shape.

    Vertex's Gen AI Eval service ingests prompt/response/contexts/reference.
    We map ``Trajectory`` fields onto that shape:

    - ``prompt`` ← ``trajectory.extra["input_text"]`` if present
    - ``response`` ← ``trajectory.final_answer``
    - ``context`` ← ``trajectory.retrieved_contexts``
    - ``trace_id`` ← ``trajectory.request_id``
    """
    rows: list[dict[str, Any]] = []
    for t in trajectories:
        extra = t.extra or {}
        rows.append(
            {
                "trace_id": t.request_id,
                "prompt": extra.get("input_text", ""),
                "response": t.final_answer,
                "context": list(t.retrieved_contexts),
                "steps": [s.model_dump() for s in t.steps],
            }
        )
    return rows


class VertexEvalScorer:
    """Score a ``Trajectory`` via Vertex Gen AI Evaluation Service.

    Implements the same ``score(traj) -> FaithfulnessResult`` shape as
    ``AgentCoreEvalScorer`` so both can sit in ``EvalRunner.scorers``
    interchangeably.

    Built-in metric names include ``faithfulness``, ``groundedness``,
    ``coherence``, ``helpfulness``, etc. See the Vertex Gen AI Eval
    docs for the full catalog.
    """

    name: str = "vertex_eval"

    def __init__(
        self,
        *,
        project_id: str,
        location: str = "us-central1",
        metric: str = "faithfulness",
        scorer_name: str | None = None,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._metric = metric
        if scorer_name is not None:
            self.name = scorer_name

    def _client(self) -> Any:
        try:
            from google.cloud import aiplatform_v1beta1
        except ImportError as e:
            raise ImportError("VertexEvalScorer requires the [gcp] extra") from e
        return aiplatform_v1beta1.EvaluationServiceClient()

    async def score(self, traj: Any) -> Any:
        from eap_core.eval.faithfulness import FaithfulnessResult

        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_VERTEX_GUIDE)
        client = self._client()
        row = to_vertex_eval_dataset([traj])[0]
        parent = f"projects/{self._project_id}/locations/{self._location}"
        resp = client.evaluate_instance(
            parent=parent,
            metric=self._metric,
            instance={
                "prompt": row["prompt"],
                "response": row["response"],
                "context": row["context"],
            },
        )
        score_value = float(resp.score)
        return FaithfulnessResult(
            request_id=traj.request_id,
            score=score_value,
            notes=str(resp.explanation or ""),
        )


__all__ = [  # noqa: RUF022 — grouped by phase, not alphabetically
    # Phase A
    "VertexAgentIdentityToken",
    "configure_for_vertex_observability",
    # Phase B — Memory
    "VertexMemoryBankStore",
    # Phase B — Code Sandbox
    "VertexCodeSandbox",
    "register_code_sandbox_tools",
    # Phase B — Browser Sandbox
    "VertexBrowserSandbox",
    "register_browser_sandbox_tools",
    # Phase B — Inbound JWT (re-exported from agentcore)
    "InboundJwtVerifier",
    "jwt_dependency",
    # Phase C — Gateway
    "VertexGatewayClient",
    # Phase D — Registry
    "VertexAgentRegistry",
    # Phase D — Payments (AP2)
    "AP2PaymentClient",
    # Phase D — Evaluations
    "to_vertex_eval_dataset",
    "VertexEvalScorer",
]
