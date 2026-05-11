"""End-to-end integration test - spawns both real MCP servers,
queries one tool on each, asserts shape. Validates the full bridge.

This test is the validation deliverable. If it passes, the SDK +
both MCP servers + the adapter all hang together.

Subprocess startup is ~3-8s per server. Two servers spawn per test,
two tests in this file - allow a generous run budget. Cleanup is
handled by the ``AsyncExitStack``; if the stack exits cleanly,
both subprocesses get SIGTERM via the upstream ``stdio_client``
context manager.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from eap_core.mcp import McpToolRegistry  # noqa: E402

from mcp_client_adapter import build_tool_specs, connect_servers  # noqa: E402

EXAMPLES_ROOT = AGENT_DIR.parent


def _server_configs() -> list[dict]:
    """Use ``sys.executable`` to pin the subprocess interpreter to the
    same Python (and therefore the same .venv with eap-core editable)
    that pytest is running under."""
    return [
        {
            "name": "bankdw",
            "command": sys.executable,
            "args": ["server.py"],
            "cwd": EXAMPLES_ROOT / "bankdw-mcp-server",
        },
        {
            "name": "sfcrm",
            "command": sys.executable,
            "args": ["server.py"],
            "cwd": EXAMPLES_ROOT / "sfcrm-mcp-server",
        },
    ]


@pytest.mark.asyncio
async def test_agent_can_invoke_tools_on_both_remote_servers():
    """Spawn bankdw + sfcrm, list tools on each, invoke list_tables,
    assert the expected tables come back. Covers: subprocess spawn,
    MCP initialize handshake, tools/list, tools/call round-trip, and
    response decoding for the pydantic-repr payload format the
    SDK's ``run_stdio`` currently emits."""
    async with AsyncExitStack() as stack:
        handles = await connect_servers(_server_configs(), stack)
        registry = McpToolRegistry()
        for spec in build_tool_specs(handles):
            registry.register(spec)

        registered_names = {t.name for t in registry.list_tools()}
        for name in [
            "bankdw__list_tables",
            "bankdw__describe_table",
            "bankdw__query_sql",
            "sfcrm__list_tables",
            "sfcrm__describe_table",
            "sfcrm__query_sql",
        ]:
            assert name in registered_names, f"missing remote tool: {name}"

        bankdw_tables = await registry.invoke("bankdw__list_tables", {})
        bankdw_names = {t["name"] for t in bankdw_tables["tables"]}
        assert "dim_party" in bankdw_names
        assert "fact_payments" in bankdw_names

        sfcrm_tables = await registry.invoke("sfcrm__list_tables", {})
        sfcrm_names = {t["name"] for t in sfcrm_tables["tables"]}
        assert "Account" in sfcrm_names
        assert "Opportunity" in sfcrm_names


@pytest.mark.asyncio
async def test_agent_runs_cross_domain_query_round_trip():
    """The headline validation: top-5 Salesforce Accounts by revenue,
    then look those names up in bankdw's ``dim_party``. Proves the
    framework orchestrates a real cross-server flow.

    The synthetic seed data is shared between the two warehouses by
    design - top revenue-generating SFDC accounts also appear as
    bankdw parties - so a non-trivial number of cross-domain matches
    is expected.
    """
    async with AsyncExitStack() as stack:
        handles = await connect_servers(_server_configs(), stack)
        registry = McpToolRegistry()
        for spec in build_tool_specs(handles):
            registry.register(spec)

        sf_result = await registry.invoke(
            "sfcrm__query_sql",
            {
                "sql": (
                    "SELECT Name FROM Account "
                    "ORDER BY AnnualRevenue DESC LIMIT 5"
                ),
                "limit": 5,
            },
        )
        assert sf_result["row_count"] == 5
        top5 = [r["Name"] for r in sf_result["rows"]]
        # The synthetic data ranks well-known enterprise names at the
        # top - all five overlap by design with bankdw.dim_party.
        assert len(top5) == 5, top5

        in_clause = ", ".join(f"'{n}'" for n in top5)
        bd_result = await registry.invoke(
            "bankdw__query_sql",
            {
                "sql": (
                    "SELECT PartyName FROM dim_party "
                    f"WHERE PartyName IN ({in_clause})"
                ),
                "limit": 50,
            },
        )
        # At least one match - the seed data has overlapping company
        # names between Account.Name and dim_party.PartyName.
        assert bd_result["row_count"] >= 1, (
            f"expected at least one cross-domain match; sf top5={top5}, "
            f"bd rows={bd_result['rows']}"
        )
