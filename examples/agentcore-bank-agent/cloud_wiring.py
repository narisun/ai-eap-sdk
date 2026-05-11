"""AgentCore integration wiring for the bank-agent example.

Every AgentCore subsystem the bank-agent uses is constructed here.
Booting without AWS credentials is intentional — the env-flag gates
inside the SDK turn every cloud call into a stub that raises
``NotImplementedError`` until ``EAP_ENABLE_REAL_RUNTIMES=1`` is set.

To graduate to live AWS calls, set the env flag and configure boto3
credentials via the standard chain (env vars, ``~/.aws/credentials``,
or an IAM role).
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
from eap_core.identity import LocalIdPStub, NonHumanIdentity
from eap_core.integrations.agentcore import (
    AgentCoreEvalScorer,
    AgentCoreMemoryStore,
    OIDCTokenExchange,
    PaymentClient,
    RegistryClient,
    configure_for_agentcore,
    register_browser_tools,
    register_code_interpreter_tools,
)
from eap_core.mcp import McpToolRegistry
from eap_core.payments import PaymentBackend

REGION = os.environ.get("AWS_REGION", "us-east-1")


def cloud_live() -> bool:
    """True when EAP_ENABLE_REAL_RUNTIMES=1 — flip to graduate."""
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


# ---------------------------------------------------------------------------
# Observability — Cloud Trace via OTLP
# ---------------------------------------------------------------------------


def wire_observability(service_name: str = "bank-agent") -> bool:
    """Wire the OTel SDK to AgentCore Observability (CloudWatch).

    Inside AgentCore Runtime, the platform auto-injects OTLP env vars
    and this call is unnecessary. Outside Runtime (local dev, other
    deploy targets), wire it explicitly.

    Skipped here unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set so the
    local demo doesn't spam retries against a non-existent collector.
    Set the env var (e.g. point at a local Jaeger / OTel Collector)
    to exercise the wiring locally.

    Returns ``True`` if OTel was configured, ``False`` if the [otel]
    extra isn't installed or no endpoint is configured.
    `ObservabilityMiddleware` writes ``gen_ai.*`` attributes to
    ``ctx.metadata`` either way.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    return configure_for_agentcore(service_name=service_name)


# ---------------------------------------------------------------------------
# Identity — workload identity → OAuth tokens
# ---------------------------------------------------------------------------


def build_identity() -> NonHumanIdentity:
    """Construct the agent's workload identity.

    NHI's IdP signs short-lived assertions; ``get_token(audience,
    scope)`` returns one ready to attach as a Bearer header. For
    cross-IdP downstream calls, hand the assertion to
    ``OIDCTokenExchange.exchange(subject_token=..., audience=...,
    scope=...)`` which performs the RFC 8693 swap with AgentCore
    Identity.

    In stub mode: ``LocalIdPStub`` signs locally — fine for tests,
    swap for a real signer in production.
    """
    return NonHumanIdentity(
        client_id="bank-agent",
        idp=LocalIdPStub(for_testing=True),
        default_audience="https://api.bank.example",
    )


def build_token_exchange() -> OIDCTokenExchange:
    """Optional companion to ``build_identity``.

    Used by code that needs to swap an NHI assertion for a tool-
    callable Bearer token via AgentCore Identity:

        nhi = build_identity()
        exchange = build_token_exchange()
        assertion = nhi.get_token(audience="...", scope="...")
        bearer = await exchange.exchange(
            subject_token=assertion, audience="...", scope="..."
        )
    """
    return OIDCTokenExchange.from_agentcore(region=REGION)


# ---------------------------------------------------------------------------
# Memory — short-term session + long-term cross-session
# ---------------------------------------------------------------------------


def build_memory() -> MemoryStore:
    """Return AgentCore Memory in live mode, in-process dict in stub mode.

    The agent code reads from / writes to ``MemoryStore`` through the
    Protocol, so this seam is invisible upstream.
    """
    if cloud_live():
        return AgentCoreMemoryStore(memory_id="bank-agent-memory", region=REGION)
    return InMemoryStore()


# ---------------------------------------------------------------------------
# Sandboxes — Code Interpreter + Browser (registered as MCP tools)
# ---------------------------------------------------------------------------


def register_cloud_tools(registry: McpToolRegistry) -> list[str]:
    """Register AgentCore Code Interpreter + Browser MCP tools.

    The registered functions traverse the middleware chain on each
    invocation, so sanitize / PII / policy / observability fire
    *before* the code or browser action reaches the AgentCore
    sandbox. This is intentional: code execution and browser
    automation are the highest-risk agentic capabilities.
    """
    if not cloud_live():
        # In stub mode the registrars still add tool specs — but
        # invoking them raises NotImplementedError. We skip
        # registration so the local demo doesn't advertise tools it
        # can't actually run.
        return []
    register_code_interpreter_tools(registry, region=REGION)
    register_browser_tools(registry, region=REGION)
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
    """Return AgentCore Registry in live mode, in-process dict in stub mode."""
    if cloud_live():
        return RegistryClient(registry_name="bank-platform", region=REGION)
    return InMemoryAgentRegistry()


# ---------------------------------------------------------------------------
# Payments — x402 microtransactions
# ---------------------------------------------------------------------------


def build_payments() -> PaymentBackend:
    """Return AgentCore Payments in live mode, in-process budget in stub mode."""
    if cloud_live():
        return PaymentClient(
            wallet_provider_id="bank-agent-wallet",
            max_spend_cents=500,  # $5.00 budget per session
            currency="USD",
            session_ttl_seconds=3600,
            region=REGION,
        )
    return InMemoryPaymentBackend(max_spend_cents=500)


# ---------------------------------------------------------------------------
# Evaluations — Vertex-hosted scorer alongside any local scorer
# ---------------------------------------------------------------------------


def build_eval_scorer() -> Any | None:
    """Return AgentCore Eval scorer in live mode, ``None`` in stub mode.

    Callers should compose this with local scorers in ``EvalRunner``:
    ``scorers = [FaithfulnessScorer(), *([build_eval_scorer()] if s else [])]``
    """
    if not cloud_live():
        return None
    return AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness",
        region=REGION,
    )


__all__ = [
    "REGION",
    "build_eval_scorer",
    "build_identity",
    "build_memory",
    "build_payments",
    "build_registry",
    "build_token_exchange",
    "cloud_live",
    "register_cloud_tools",
    "wire_observability",
]


# ---------------------------------------------------------------------------
# Smoke-test the wiring when run directly: `python cloud_wiring.py`.
# Prints which subsystems are live vs. stub.
# ---------------------------------------------------------------------------


def _main() -> None:
    mode = "LIVE (real AWS calls enabled)" if cloud_live() else "STUB (no cloud calls)"
    print(f"AgentCore wiring — mode: {mode}")
    print(f"  region        : {REGION}")
    print(
        f"  observability : {'wired' if wire_observability() else 'no-op (install [otel] extra)'}"
    )
    print(f"  identity      : {build_identity().client_id!r}")
    print(f"  memory        : {type(build_memory()).__name__}")
    print(f"  registry      : {type(build_registry()).__name__}")
    print(f"  payments      : {type(build_payments()).__name__}")
    scorer = build_eval_scorer()
    print(f"  eval scorer   : {type(scorer).__name__ if scorer else '(none — local scorers only)'}")
    cloud_tools = register_cloud_tools(McpToolRegistry())
    print(f"  cloud tools   : {cloud_tools or '(none — would register 8 in live mode)'}")


if __name__ == "__main__":
    _main()
