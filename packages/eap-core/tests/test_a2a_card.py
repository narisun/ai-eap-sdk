import pytest

from eap_core.a2a.card import AgentCard, Skill, build_card
from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry


def test_agent_card_serializes_to_dict():
    card = AgentCard(
        name="research-agent",
        description="answers research questions",
        skills=[Skill(name="search", description="search docs", input_schema={}, output_schema=None)],
        endpoints={"http": "https://agent.example/v1"},
        authentication={"type": "oauth2.1"},
    )
    d = card.model_dump()
    assert d["name"] == "research-agent"
    assert d["skills"][0]["name"] == "search"
    assert d["authentication"]["type"] == "oauth2.1"


def test_build_card_reads_skills_from_registry():
    reg = McpToolRegistry()

    @mcp_tool(description="Look up an account.")
    async def lookup_account(id: str) -> dict:
        return {}

    @mcp_tool(description="Transfer funds.", requires_auth=True)
    async def transfer(amount: int) -> str:
        return "ok"

    reg.register(lookup_account.spec)
    reg.register(transfer.spec)

    card = build_card(
        name="bank-agent",
        description="helps with banking ops",
        skills_from=reg,
        auth="oauth2.1",
        endpoints={"http": "https://bank.example/v1"},
    )
    skill_names = {s.name for s in card.skills}
    assert {"lookup_account", "transfer"}.issubset(skill_names)
    assert card.authentication == {"type": "oauth2.1"}


def test_build_card_with_no_auth():
    reg = McpToolRegistry()
    card = build_card(name="x", description="y", skills_from=reg)
    assert card.authentication is None
