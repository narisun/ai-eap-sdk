"""bank-agent — reference implementation on GCP Vertex Agent Engine.

Mirrors `docs/user-guide-gcp-vertex.md` end-to-end. Boots locally
without GCP credentials (stubs swap in for cloud calls). Flip
`EAP_ENABLE_REAL_RUNTIMES=1` and configure ADC (`gcloud auth
application-default login`) to graduate to real Vertex Memory Bank
/ Sandbox / Registry / AP2 Payments / Gen AI Eval calls.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from cloud_wiring import (
    build_memory,
    build_payments,
    build_registry,
    cloud_live,
    register_cloud_tools,
    wire_observability,
)
from tools import lookup_account, transfer_funds  # noqa: F401  # registers tools

from eap_core import EnterpriseLLM, RuntimeConfig
from eap_core.mcp import default_registry
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware
from eap_core.middleware.sanitize import PromptInjectionMiddleware
from eap_core.middleware.validate import OutputValidationMiddleware
from eap_core.payments import PaymentRequired

# Single memory instance per process — `MemoryStore` doesn't promise
# any cross-instance coherence, and rebuilding per call would defeat
# the cache.
MEMORY = build_memory()


def _load_policy() -> dict:
    return json.loads((Path(__file__).parent / "configs" / "policy.json").read_text())


def build_client() -> EnterpriseLLM:
    """Construct the EnterpriseLLM with the default middleware chain.

    The runtime is `local` so the agent runs without cloud creds.
    Swap `provider="local"` → `provider="vertex"` when ready and set
    `EAP_ENABLE_REAL_RUNTIMES=1`.

    Note: Vertex's auth flow uses Application Default Credentials
    rather than RFC 8693, so we don't pass an `identity=` here —
    `VertexAgentIdentityToken` is used directly by callers that need
    a Bearer token (e.g. `VertexGatewayClient`).
    """
    return EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            PromptInjectionMiddleware(),
            PiiMaskingMiddleware(),
            ObservabilityMiddleware(),
            PolicyMiddleware(JsonPolicyEvaluator(_load_policy())),
            OutputValidationMiddleware(),
        ],
        tool_registry=default_registry(),
    )


async def lookup_balance(account_id: str) -> dict:
    """Read-only balance lookup with memory caching."""
    client = build_client()
    cached = await MEMORY.recall(session_id=account_id, key="last_balance")
    if cached is not None:
        return {"id": account_id, "balance_cents": int(cached), "source": "cache"}
    result = await client.invoke_tool("lookup_account", {"account_id": account_id})
    await MEMORY.remember(
        session_id=account_id,
        key="last_balance",
        value=str(result["balance_cents"]),
    )
    return {**result, "source": "fresh"}


async def execute_transfer(from_id: str, to_id: str, amount_cents: int) -> dict:
    """Auth-required transfer with idempotency + payment retry.

    Demonstrates two cross-cutting concerns the SDK handles for you:
    1. The auth-required tool requires an OAuth token (NHI flow).
    2. If the downstream rate-quote service responds with HTTP 402,
       the AP2 PaymentBackend signs and the caller retries.
    """
    client = build_client()
    pay = build_payments()
    await pay.start_session()

    src = await client.invoke_tool("lookup_account", {"account_id": from_id})
    if src["balance_cents"] < amount_cents:
        return {"status": "rejected", "reason": "insufficient_funds"}

    try:
        return await client.invoke_tool(
            "transfer_funds",
            {
                "from_id": from_id,
                "to_id": to_id,
                "amount_cents": amount_cents,
                "idempotency_key": uuid.uuid4().hex,
            },
        )
    except PaymentRequired as pr:
        # Real flow: pay.authorize(pr) then re-issue.
        # In this example we just surface what would happen.
        return {
            "status": "payment_required",
            "amount_cents": pr.amount_cents,
            "currency": pr.currency,
            "can_afford": pay.can_afford(pr.amount_cents),
        }


async def publish_agent_card() -> str:
    """Publish this agent's A2A card to the Vertex Agent Registry."""
    from eap_core import build_card

    card = build_card(
        name="bank-agent",
        description="Bank account assistant — balance lookups and transfers.",
        skills_from=default_registry(),
    )
    registry = build_registry()
    return await registry.publish(
        {
            "name": card.name,
            "record_type": "AGENT",
            "description": card.description,
            "metadata": card.model_dump(),
        }
    )


async def run() -> None:
    """Smoke-test the agent end-to-end."""
    wire_observability()
    cloud_tools = register_cloud_tools(default_registry())

    mode = "LIVE" if cloud_live() else "STUB"
    print(f"=== bank-agent (mode: {mode}) ===")

    # 1. Balance lookup (read-only, cached in memory)
    balance = await lookup_balance("acct-1")
    print(f"balance: {balance}")

    # 2. Transfer (auth-required tool)
    transfer = await execute_transfer("acct-1", "acct-2", 1000)
    print(f"transfer: {transfer}")

    # 3. Cached re-read demonstrates the MemoryStore round-trip
    cached = await lookup_balance("acct-1")
    print(f"balance (re-read): {cached}")

    # 4. Registry publish
    record_id = await publish_agent_card()
    print(f"published to registry: {record_id}")

    # 5. Cloud-only summary
    if cloud_tools:
        print(f"cloud tools registered: {cloud_tools}")
    else:
        print("cloud tools: none (set EAP_ENABLE_REAL_RUNTIMES=1 to wire them)")


if __name__ == "__main__":
    asyncio.run(run())
