"""AWS Bedrock AgentCore integration helpers.

See ``docs/integrations/aws-bedrock-agentcore.md`` for the full
positioning and the phased plan.

This module is intentionally thin — it just wires our existing
OTel observability and OIDC token exchange at AgentCore's
endpoints. The middleware chain, runtime adapters, MCP tooling, and
identity primitives are unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from eap_core.identity.token_exchange import OIDCTokenExchange as _BaseOIDCTokenExchange


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


__all__ = [
    "OIDCTokenExchange",
    "configure_for_agentcore",
]
