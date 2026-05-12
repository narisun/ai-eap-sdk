"""End-to-end integration test — spawns both real MCP servers,
queries one tool on each, asserts shape. Validates the full bridge.

Migrated to use :class:`eap_core.mcp.client.McpClientPool` directly
in v1.1.0. Previously the test went through the per-agent shim
(``mcp_client_adapter.py``); now it uses the canonical SDK API the
example itself uses. The shim is exercised by ``test_adapter.py``'s
compat-suite at the unit level.

If this test passes, the SDK + both MCP servers + the SDK adapter all
hang together end-to-end.

Subprocess startup is ~3-8s per server. Two servers spawn per test,
two tests in this file — allow a generous run budget. Cleanup is
handled by the pool's :class:`AsyncExitStack`; on context exit both
subprocesses get SIGTERM via the upstream ``stdio_client`` context.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from eap_core.mcp import McpToolRegistry
from eap_core.mcp.client import McpClientPool, McpServerConfig

AGENT_DIR = Path(__file__).resolve().parent.parent
EXAMPLES_ROOT = AGENT_DIR.parent


def _server_configs() -> list[McpServerConfig]:
    """Use ``sys.executable`` to pin the subprocess interpreter to the
    same Python (and therefore the same .venv with eap-core editable)
    that pytest is running under.
    """
    return [
        McpServerConfig(
            name="bankdw",
            command=sys.executable,
            args=["server.py"],
            cwd=EXAMPLES_ROOT / "bankdw-mcp-server",
        ),
        McpServerConfig(
            name="sfcrm",
            command=sys.executable,
            args=["server.py"],
            cwd=EXAMPLES_ROOT / "sfcrm-mcp-server",
        ),
    ]


@pytest.mark.asyncio
async def test_agent_can_invoke_tools_on_both_remote_servers() -> None:
    """Spawn bankdw + sfcrm via :class:`McpClientPool`, list tools on
    each, invoke list_tables, assert the expected tables come back.

    Covers: subprocess spawn (pool.__aenter__ → _spawn), MCP initialize
    handshake, tools/list capture (including the new
    ``tool_output_schemas`` mapping), tools/call round-trip, and the
    adapter's response decoding for the pydantic-repr payload format
    the SDK's ``run_stdio`` currently emits.
    """
    async with McpClientPool(_server_configs()) as pool:
        # ``build_tool_registry`` populates a fresh ``McpToolRegistry``
        # with namespaced forwarders for every remote tool. Mirror the
        # registry into a top-level instance so the assertions read the
        # same way they did pre-migration.
        pool_registry = pool.build_tool_registry()
        registry = McpToolRegistry()
        for spec in pool_registry.list_tools():
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
async def test_agent_runs_cross_domain_query_round_trip() -> None:
    """The headline validation: top-5 Salesforce Accounts by revenue,
    then look those names up in bankdw's ``dim_party``. Proves the
    framework orchestrates a real cross-server flow through the SDK
    pool — the printed-output contract the README documents.

    The synthetic seed data is shared between the two warehouses by
    design — top revenue-generating SFDC accounts also appear as
    bankdw parties — so a non-trivial number of cross-domain matches
    is expected.
    """
    async with McpClientPool(_server_configs()) as pool:
        registry = pool.build_tool_registry()

        sf_result = await registry.invoke(
            "sfcrm__query_sql",
            {
                "sql": "SELECT Name FROM Account ORDER BY AnnualRevenue DESC LIMIT 5",
                "limit": 5,
            },
        )
        assert sf_result["row_count"] == 5
        top5 = [r["Name"] for r in sf_result["rows"]]
        # The synthetic data ranks well-known enterprise names at the
        # top — all five overlap by design with bankdw.dim_party.
        assert len(top5) == 5, top5

        in_clause = ", ".join(f"'{n}'" for n in top5)
        bd_result = await registry.invoke(
            "bankdw__query_sql",
            {
                "sql": f"SELECT PartyName FROM dim_party WHERE PartyName IN ({in_clause})",
                "limit": 50,
            },
        )
        # At least one match — the seed data has overlapping company
        # names between Account.Name and dim_party.PartyName.
        assert bd_result["row_count"] >= 1, (
            f"expected at least one cross-domain match; sf top5={top5}, bd rows={bd_result['rows']}"
        )
