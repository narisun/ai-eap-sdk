"""cross-domain-agent — answer questions that span bankdw + sfcrm.

Migrated in v1.1.0 to use :class:`eap_core.mcp.client.McpClientPool`
directly. The previous version went through ``mcp_client_adapter.py``
(now a thin v1.0 compat shim sitting next to this file); the headline
example now uses the SDK API end-to-end so newcomers see the canonical
pattern.

Spawns both MCP servers as subprocesses through the SDK pool, builds a
namespaced ``McpToolRegistry`` via :meth:`McpClientPool.build_tool_registry`,
and runs the same cross-domain query the v1.0 demo printed. The output
shape and the top-5 names are unchanged — this is a refactor of HOW the
example wires things, not WHAT it produces.

Run locally (no cloud creds, no LLM provider):

    cd examples/cross-domain-agent
    python agent.py

The demo bypasses LLM-driven tool selection (no
``EnterpriseLLM.generate_text`` call) on purpose: the headline goal is
proving the *infrastructure* is sound end-to-end. A real LLM-driven
version would let the model pick the tools sequentially — that's
documented as follow-on work in the README.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from eap_core import EnterpriseLLM, RuntimeConfig
from eap_core.mcp import McpToolRegistry
from eap_core.mcp.client import McpClientPool, McpServerConfig


def _examples_root() -> Path:
    return Path(__file__).resolve().parent.parent


# An empty registry at module-load time. ``main()`` spawns the MCP
# subprocesses, populates the registry via the SDK pool, and runs the
# demo. ``build_client()`` (below) returns an ``EnterpriseLLM`` bound to
# this same registry — useful for smoke tests that need to confirm the
# example wires together without spawning real subprocesses.
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
    configs = [
        McpServerConfig(
            name="bankdw",
            command=sys.executable,
            args=["server.py"],
            cwd=root / "bankdw-mcp-server",
        ),
        McpServerConfig(
            name="sfcrm",
            command=sys.executable,
            args=["server.py"],
            cwd=root / "sfcrm-mcp-server",
        ),
    ]

    async with McpClientPool(configs) as pool:
        # The pool owns its own AsyncExitStack; entering it spawns both
        # servers and opens stdio sessions. ``build_tool_registry``
        # produces a populated ``McpToolRegistry`` with namespaced
        # forwarders (``<server-name>__<tool-name>``). For the demo we
        # copy those specs into the module-level REGISTRY so the same
        # wiring is exposed via ``build_client()`` for smoke tests.
        pool_registry = pool.build_tool_registry()
        for spec in pool_registry.list_tools():
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
