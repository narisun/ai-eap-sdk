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
            raise NotImplementedError(_VERTEX_GUIDE)
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


__all__ = [
    "VertexAgentIdentityToken",
    "configure_for_vertex_observability",
]
