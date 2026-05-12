import pytest

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.exceptions import PolicyDeniedError
from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import MCPError
from eap_core.middleware.policy import PolicyMiddleware, SimpleJsonPolicyEvaluator

PERMIT_ALL = {
    "version": "1",
    "rules": [
        {"id": "permit-all", "effect": "permit", "principal": "*", "action": "*", "resource": "*"}
    ],
}


async def test_invoke_tool_dispatches_via_registry():
    reg = McpToolRegistry()

    @mcp_tool()
    async def double(n: int) -> int:
        return n * 2

    reg.register(double.spec)

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))],
        tool_registry=reg,
    )
    result = await client.invoke_tool("double", {"n": 21})
    assert result == 42


async def test_invoke_tool_unknown_raises_mcp_error():
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))],
        tool_registry=McpToolRegistry(),
    )
    with pytest.raises(MCPError):
        await client.invoke_tool("nonexistent", {})


async def test_invoke_tool_runs_through_policy_middleware():
    """Policy denies tool actions when no rule permits the tool name."""
    deny_writes = {
        "version": "1",
        "rules": [
            {
                "id": "permit-reads",
                "effect": "permit",
                "principal": "*",
                "action": ["tool:read_account"],
                "resource": "*",
            },
            {
                "id": "deny-default",
                "effect": "forbid",
                "principal": "*",
                "action": ["tool:transfer"],
                "resource": "*",
            },
        ],
    }
    reg = McpToolRegistry()

    @mcp_tool()
    async def transfer(amount: int) -> str:
        return "ok"

    reg.register(transfer.spec)

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(SimpleJsonPolicyEvaluator(deny_writes))],
        tool_registry=reg,
    )
    with pytest.raises(PolicyDeniedError):
        await client.invoke_tool("transfer", {"amount": 100})
