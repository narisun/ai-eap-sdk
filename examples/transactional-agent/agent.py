"""transactional-agent — transactional agent (action-style).

Demonstrates: an EAP-Core agent that performs writes via tools, with
explicit policy gates and idempotency-key handling on `transfer_funds`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from tools.get_account import get_account
from tools.transfer_funds import transfer_funds

from eap_core import EnterpriseLLM, RuntimeConfig
from eap_core.identity import LocalIdPStub, NonHumanIdentity
from eap_core.mcp import McpToolRegistry
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.middleware.policy import PolicyMiddleware, SimpleJsonPolicyEvaluator
from eap_core.middleware.sanitize import ThreatDetectionMiddleware
from eap_core.middleware.validate import OutputValidationMiddleware

# Explicit per-process tool registry — replaces the deprecated
# ``default_registry()`` singleton.
REGISTRY = McpToolRegistry()
REGISTRY.register(get_account.spec)
REGISTRY.register(transfer_funds.spec)

# Workload identity for the agent. ``transfer_funds`` declares
# ``requires_auth=True``; the v0.5.0 C5 dispatcher enforcement in
# ``McpToolRegistry.invoke`` refuses such tools without an identity.
# ``LocalIdPStub`` signs locally — fine for tests and the in-tree
# example. In production, swap for a real IdP (e.g. AgentCore
# ``OIDCTokenExchange.from_agentcore(...)``).
IDENTITY = NonHumanIdentity(
    client_id="transactional-agent",
    idp=LocalIdPStub(for_testing=True),
    default_audience="https://api.bank.example",
)


def _load_policy() -> dict:
    return json.loads((Path(__file__).parent / "configs" / "policy.json").read_text())


def build_client() -> EnterpriseLLM:
    return EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            ThreatDetectionMiddleware(),
            PiiMaskingMiddleware(),
            ObservabilityMiddleware(),
            PolicyMiddleware(SimpleJsonPolicyEvaluator(_load_policy())),
            OutputValidationMiddleware(),
        ],
        tool_registry=REGISTRY,
        identity=IDENTITY,
    )


async def execute_transfer(from_id: str, to_id: str, amount_cents: int) -> dict:
    client = build_client()
    src = await client.invoke_tool("get_account", {"account_id": from_id})
    if src["balance_cents"] < amount_cents:
        return {"status": "rejected", "reason": "insufficient_funds"}
    return await client.invoke_tool(
        "transfer_funds",
        {
            "from_id": from_id,
            "to_id": to_id,
            "amount_cents": amount_cents,
            "idempotency_key": uuid.uuid4().hex,
        },
    )


async def run() -> None:
    result = await execute_transfer("acct-1", "acct-2", 1000)
    print(result)


if __name__ == "__main__":
    asyncio.run(run())
