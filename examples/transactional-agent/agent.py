"""transactional-agent — transactional agent (action-style).

Demonstrates: an EAP-Core agent that performs writes via tools, with
explicit policy gates and idempotency-key handling on `transfer_funds`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from tools import get_account, transfer_funds  # noqa: F401  # registers tools

from eap_core import EnterpriseLLM, RuntimeConfig
from eap_core.mcp import default_registry
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware
from eap_core.middleware.sanitize import PromptInjectionMiddleware
from eap_core.middleware.validate import OutputValidationMiddleware


def _load_policy() -> dict:
    return json.loads((Path(__file__).parent / "configs" / "policy.json").read_text())


def build_client() -> EnterpriseLLM:
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
