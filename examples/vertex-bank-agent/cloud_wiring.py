"""Vertex Agent Engine integration wiring for the bank-agent example.

Every Vertex subsystem the bank-agent uses is constructed here.
Booting without GCP credentials is intentional — the env-flag gates
inside the SDK turn every cloud call into a stub that raises
``NotImplementedError`` until ``EAP_ENABLE_REAL_RUNTIMES=1`` is set.

To graduate to live GCP calls, set the env flag and configure
Application Default Credentials (``gcloud auth
application-default login`` for local dev, or attach a service
account to the workload in production).
"""

from __future__ import annotations

import os
from typing import Any

from eap_core import (
    InMemoryAgentRegistry,
    InMemoryPaymentBackend,
    InMemoryStore,
    MemoryStore,
)
from eap_core.discovery import AgentRegistry
from eap_core.integrations.vertex import (
    AP2PaymentClient,
    VertexAgentIdentityToken,
    VertexAgentRegistry,
    VertexEvalScorer,
    VertexMemoryBankStore,
    configure_for_vertex_observability,
    register_browser_sandbox_tools,
    register_code_sandbox_tools,
)
from eap_core.mcp import McpToolRegistry, default_registry
from eap_core.payments import PaymentBackend

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")


def cloud_live() -> bool:
    """True when EAP_ENABLE_REAL_RUNTIMES=1 — flip to graduate."""
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


# ---------------------------------------------------------------------------
# Observability — Cloud Trace via OTLP
# ---------------------------------------------------------------------------


def wire_observability(service_name: str = "bank-agent") -> bool:
    """Wire the OTel SDK to Vertex Agent Observability (Cloud Trace).

    Inside Vertex Agent Runtime, the platform auto-injects OTLP env
    vars and this call is unnecessary. Outside Runtime (local dev,
    other deploy targets), wire it explicitly.

    Skipped here unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set so
    the local demo doesn't spam retries against a non-existent
    collector.

    Returns ``True`` if OTel was configured, ``False`` if the [otel]
    extra isn't installed or no endpoint is configured.
    `ObservabilityMiddleware` writes ``gen_ai.*`` attributes to
    ``ctx.metadata`` either way.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    return configure_for_vertex_observability(
        project_id=PROJECT_ID,
        service_name=service_name,
    )


# ---------------------------------------------------------------------------
# Identity — workload identity → Google access tokens
# ---------------------------------------------------------------------------


def build_identity() -> VertexAgentIdentityToken:
    """Construct the agent's workload identity.

    On GCP, ``VertexAgentIdentityToken`` wraps the standard Google
    auth chain (Application Default Credentials → workload identity
    federation → IAM service account). The ``get_token(audience=,
    scope=)`` shape mirrors ``NonHumanIdentity`` from the AgentCore
    side, so anywhere the SDK accepts a NHI you can pass this
    instead.
    """
    return VertexAgentIdentityToken(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


# ---------------------------------------------------------------------------
# Memory — short-term session + long-term cross-session
# ---------------------------------------------------------------------------


def build_memory() -> MemoryStore:
    """Return Vertex Memory Bank in live mode, in-process dict in stub mode.

    The agent code reads from / writes to ``MemoryStore`` through the
    Protocol, so this seam is invisible upstream.
    """
    if cloud_live():
        return VertexMemoryBankStore(
            project_id=PROJECT_ID,
            location=LOCATION,
            memory_bank_id="bank-agent-memory",
        )
    return InMemoryStore()


# ---------------------------------------------------------------------------
# Sandboxes — Code + Browser (registered as MCP tools)
# ---------------------------------------------------------------------------


def register_cloud_tools(registry: McpToolRegistry) -> list[str]:
    """Register Vertex Agent Sandbox MCP tools (code + browser).

    The registered functions traverse the middleware chain on each
    invocation, so sanitize / PII / policy / observability fire
    *before* the code or browser action reaches the Vertex sandbox.
    This is intentional: code execution and browser automation are
    the highest-risk agentic capabilities.
    """
    if not cloud_live():
        # In stub mode the registrars still add tool specs — but
        # invoking them raises NotImplementedError. We skip
        # registration so the local demo doesn't advertise tools it
        # can't actually run.
        return []
    register_code_sandbox_tools(registry, project_id=PROJECT_ID, location=LOCATION)
    register_browser_sandbox_tools(registry, project_id=PROJECT_ID, location=LOCATION)
    return [
        "execute_python",
        "execute_javascript",
        "execute_typescript",
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_extract_text",
        "browser_screenshot",
    ]


# ---------------------------------------------------------------------------
# Registry — agent + tool discovery
# ---------------------------------------------------------------------------


def build_registry() -> AgentRegistry:
    """Return Vertex Agent Registry in live mode, in-process dict in stub mode."""
    if cloud_live():
        return VertexAgentRegistry(
            project_id=PROJECT_ID,
            location=LOCATION,
            registry_id="bank-platform",
        )
    return InMemoryAgentRegistry()


# ---------------------------------------------------------------------------
# Payments — AP2 (Google's Agent Payment Protocol)
# ---------------------------------------------------------------------------


def build_payments() -> PaymentBackend:
    """Return AP2 Payments in live mode, in-process budget in stub mode."""
    if cloud_live():
        return AP2PaymentClient(
            wallet_provider_id="bank-agent-wallet",
            project_id=PROJECT_ID,
            location=LOCATION,
            max_spend_cents=500,  # $5.00 budget per session
            currency="USD",
            session_ttl_seconds=3600,
        )
    return InMemoryPaymentBackend(max_spend_cents=500)


# ---------------------------------------------------------------------------
# Evaluations — Vertex Gen AI Eval scorer
# ---------------------------------------------------------------------------


def build_eval_scorer() -> Any | None:
    """Return Vertex Gen AI Eval scorer in live mode, ``None`` in stub mode.

    Callers should compose this with local scorers in ``EvalRunner``:
    ``scorers = [FaithfulnessScorer(), *([build_eval_scorer()] if s else [])]``
    """
    if not cloud_live():
        return None
    return VertexEvalScorer(
        project_id=PROJECT_ID,
        location=LOCATION,
        metric="faithfulness",
    )


__all__ = [
    "LOCATION",
    "PROJECT_ID",
    "build_eval_scorer",
    "build_identity",
    "build_memory",
    "build_payments",
    "build_registry",
    "cloud_live",
    "register_cloud_tools",
    "wire_observability",
]


# ---------------------------------------------------------------------------
# Smoke-test the wiring when run directly: `python cloud_wiring.py`.
# Prints which subsystems are live vs. stub.
# ---------------------------------------------------------------------------


def _main() -> None:
    mode = "LIVE (real GCP calls enabled)" if cloud_live() else "STUB (no cloud calls)"
    print(f"Vertex wiring — mode: {mode}")
    print(f"  project       : {PROJECT_ID}")
    print(f"  location      : {LOCATION}")
    obs_status = "wired" if wire_observability() else "no-op (set OTEL_EXPORTER_OTLP_ENDPOINT)"
    print(f"  observability : {obs_status}")
    print(f"  identity      : {build_identity().name!r}")
    print(f"  memory        : {type(build_memory()).__name__}")
    print(f"  registry      : {type(build_registry()).__name__}")
    print(f"  payments      : {type(build_payments()).__name__}")
    scorer = build_eval_scorer()
    print(f"  eval scorer   : {type(scorer).__name__ if scorer else '(none — local scorers only)'}")
    cloud_tools = register_cloud_tools(default_registry())
    print(f"  cloud tools   : {cloud_tools or '(none — would register 8 in live mode)'}")


if __name__ == "__main__":
    _main()
