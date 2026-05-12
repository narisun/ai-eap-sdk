"""cross-domain-agent - answer questions that span bankdw + sfcrm.

Spawns both MCP servers as subprocesses, wires their tools into an
``McpToolRegistry`` via the local ``mcp_client_adapter`` shim, and
runs a hard-coded cross-domain query to prove the bridge works.

Run locally (no cloud creds, no LLM provider):

    cd examples/cross-domain-agent
    python agent.py

The demo bypasses LLM-driven tool selection (no
``EnterpriseLLM.generate_text`` call) on purpose: the headline goal
is proving the *infrastructure* is sound end-to-end. A real
LLM-driven version would pick the tools sequentially:

  1. ``sfcrm__list_tables`` -> discover ``Account``
  2. ``sfcrm__describe_table(table="Account")`` -> find ``Name``,
     ``AnnualRevenue``
  3. ``sfcrm__query_sql("SELECT Name FROM Account ORDER BY
     AnnualRevenue DESC LIMIT 5")``
  4. ``bankdw__list_tables`` -> discover ``dim_party``
  5. ``bankdw__query_sql("SELECT PartyName FROM dim_party WHERE
     PartyName IN (...)")``

That follow-on is documented as future work in the README.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp_client_adapter import build_tool_specs, connect_servers

from eap_core import EnterpriseLLM, RuntimeConfig
from eap_core.mcp import McpToolRegistry


def _examples_root() -> Path:
    return Path(__file__).resolve().parent.parent


# An empty registry at module-load time. ``main()`` spawns the MCP
# subprocesses, populates the registry via the adapter, and runs the
# demo cross-domain query. ``build_client()`` (below) returns an
# ``EnterpriseLLM`` bound to this same registry shape - useful for
# smoke tests that need to confirm the example wires together without
# spawning real subprocesses.
REGISTRY = McpToolRegistry()


def build_client() -> EnterpriseLLM:
    """Construct an ``EnterpriseLLM`` against the local echo runtime
    with an (initially empty) ``McpToolRegistry``.

    This entry point exists so the workspace-level
    ``packages/eap-cli/tests/test_examples_smoke.py`` contract
    (every example exposes ``build_client()``) is satisfied without
    requiring a real LLM provider or live MCP subprocesses. The
    interesting orchestration lives in ``main()`` below; the smoke
    test only verifies the example constructs cleanly under the
    SDK's identity / requires_auth gates.
    """
    return EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        tool_registry=REGISTRY,
    )


async def main() -> None:
    root = _examples_root()
    server_configs = [
        {
            "name": "bankdw",
            "command": sys.executable,
            "args": ["server.py"],
            "cwd": root / "bankdw-mcp-server",
        },
        {
            "name": "sfcrm",
            "command": sys.executable,
            "args": ["server.py"],
            "cwd": root / "sfcrm-mcp-server",
        },
    ]

    async with AsyncExitStack() as stack:
        handles = await connect_servers(server_configs, stack)
        # Reuse the module-level REGISTRY that ``build_client()`` exposes
        # so the same wiring is exercised by both the demo entry point
        # and the smoke-test entry point.
        for spec in build_tool_specs(handles):
            REGISTRY.register(spec)

        print("Registered remote tools:")
        for tool in REGISTRY.list_tools():
            print(f"  - {tool.name}")

        # Demo 1: list both servers' tables to prove the bridge works.
        bankdw_tables = await REGISTRY.invoke("bankdw__list_tables", {})
        sfcrm_tables = await REGISTRY.invoke("sfcrm__list_tables", {})
        print(f"\nbankdw tables: {[t['name'] for t in bankdw_tables['tables']]}")
        print(f"sfcrm tables: {[t['name'] for t in sfcrm_tables['tables']]}")

        # Demo 2: cross-domain query. Find Salesforce Account names by
        # AnnualRevenue, then look them up in bankdw's dim_party.
        sf_top5 = await REGISTRY.invoke(
            "sfcrm__query_sql",
            {
                "sql": (
                    "SELECT Name, AnnualRevenue FROM Account ORDER BY AnnualRevenue DESC LIMIT 5"
                ),
                "limit": 5,
            },
        )
        top5_names = [r["Name"] for r in sf_top5["rows"]]
        # ``in_clause`` builds an IN list from names returned by the
        # sfcrm server. These values came out of a query whose SQL was
        # ours (not user input). A production version would still want
        # proper parameterisation; the bankdw query_sql tool doesn't
        # expose parameter binding so this demo interpolates. S608 is
        # a false positive in the validation flow.
        in_clause = ", ".join(f"'{n}'" for n in top5_names)
        bd_match = await REGISTRY.invoke(
            "bankdw__query_sql",
            {
                "sql": "SELECT PartyName FROM dim_party "  # noqa: S608
                f"WHERE PartyName IN ({in_clause})",
                "limit": 50,
            },
        )
        matched = sorted({r["PartyName"] for r in bd_match["rows"]})
        print(f"\nTop-5 SFDC Accounts by AnnualRevenue: {top5_names}")
        print(f"Of those, parties also in bankdw: {matched}")


if __name__ == "__main__":
    asyncio.run(main())
