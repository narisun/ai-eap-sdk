# cross-domain-agent

An EAP-Core agent that spawns the
[`bankdw-mcp-server`](../bankdw-mcp-server/) and
[`sfcrm-mcp-server`](../sfcrm-mcp-server/) as MCP stdio subprocesses,
wraps each remote tool as a local `ToolSpec` that forwards over the
open MCP session, and runs one cross-domain query end-to-end.

It demonstrates the SDK consuming MCP servers it didn't write — the
companion to the two MCP-server examples that show the SDK *building*
servers.

## Run it

From the worktree root (`eap-core` only resolves from the workspace
editable install):

```bash
uv run --with mcp --with duckdb python examples/cross-domain-agent/agent.py
```

Expected output (the exact set of top-5 names is deterministic from
the synthetic seed data):

```
Registered remote tools:
  - bankdw__list_tables
  - bankdw__describe_table
  - bankdw__query_sql
  - sfcrm__list_tables
  - sfcrm__describe_table
  - sfcrm__query_sql

bankdw tables: ['bridge_party_account', 'dim_bank', 'dim_party', 'dim_product', 'fact_payments']
sfcrm tables: ['Account', 'Campaign', 'CampaignMember', 'Case', 'Contact', 'Contract', 'Event', 'Lead', 'Opportunity', 'OpportunityContactRole', 'OpportunityLineItem', 'Pricebook2', 'PricebookEntry', 'Product2', 'Task']

Top-5 SFDC Accounts by AnnualRevenue: ['AmerisourceBergen', 'Global Logistics Partners', 'Costco Wholesale', 'AT&T', 'PepsiCo']
Of those, parties also in bankdw: ['AT&T', 'AmerisourceBergen', 'Costco Wholesale', 'Global Logistics Partners', 'PepsiCo']
```

## Tests

From the worktree root:

```bash
uv run --with mcp --with duckdb pytest examples/cross-domain-agent/tests_cross_domain -q
```

Seven adapter unit tests + two integration tests. The integration
tests spawn real subprocesses for both MCP servers (~3–5s each
run); the adapter tests use a stubbed `ClientSession` and run in
under a second.

## How this example uses the SDK

`agent.py` uses
[`eap_core.mcp.client.McpClientPool`](../../packages/eap-core/src/eap_core/mcp/client/pool.py)
as the bridge:

```python
async with McpClientPool([cfg_bankdw, cfg_sfcrm]) as pool:
    registry = pool.build_tool_registry()
    rows = await registry.invoke("bankdw__query_sql", {"sql": "...", "limit": 50})
```

The pool is an async context manager that spawns each MCP server
subprocess, opens stdio sessions, captures advertised `outputSchema`
per tool, and produces a populated `McpToolRegistry` with namespaced
`<server-name>__<tool-name>` forwarders. Reconnect, health-check,
per-call timeout, typed errors (`McpClientError` and its subclasses),
OTel spans, and opt-in output-schema validation all live in the
`eap_core.mcp.client` subpackage.

`mcp_client_adapter.py` next to this file is a small backward-compat
shim re-exporting older `connect_servers` / `build_tool_specs` /
`ServerHandle` entry points for callers that haven't migrated yet.
New code should import from `eap_core.mcp.client` directly.

The demo bypasses LLM-driven tool selection — it calls the registry
by name to exercise the infrastructure. A real LLM-driven version
would attach an `EnterpriseLLM` and let the model pick the tools
sequentially.

## Follow-on work

- Wire an `EnterpriseLLM` with `LocalRuntimeAdapter` (or a real
  provider) so the cross-domain query is driven by tool-selection
  from a language model, not hard-coded SQL.
- Add an `eap eval` golden-set entry for the cross-domain query so
  regressions in either MCP server's contract get caught in CI.

## Data and prerequisites

The two MCP servers load their seed CSVs from `samples/bankdw/` and
`samples/sfcrm/`. Both directories are committed in the worktree
root.

Python 3.11+. Required packages: `eap-core`, `mcp>=0.9`, `duckdb`,
`pydantic>=2.7`. Run from the worktree root so `eap-core` resolves
from the workspace's editable install:

```bash
uv run --with mcp --with duckdb python examples/cross-domain-agent/agent.py
uv run --with mcp --with duckdb pytest examples/cross-domain-agent/tests_cross_domain -q
```
