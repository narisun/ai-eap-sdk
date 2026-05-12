# cross-domain-agent

EAP-Core validation example #3. An agent that spawns the
[`bankdw-mcp-server`](../bankdw-mcp-server/) and
[`sfcrm-mcp-server`](../sfcrm-mcp-server/) as MCP stdio subprocesses,
wraps each remote tool as a local `ToolSpec` that forwards over the
open MCP session, and runs one cross-domain query end-to-end.

This is the headline validation. T1 and T2 prove the SDK can build
MCP servers; T3 proves an EAP-Core agent can *consume* MCP servers
the SDK didn't write. The exercise surfaced a real gap in the SDK
(no first-class MCP client) — see **What this validation surfaced**
below.

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
uv run --with mcp --with duckdb pytest examples/cross-domain-agent/tests -q
```

Six adapter unit tests + two integration tests. The integration
tests spawn real subprocesses for both MCP servers (~3–5s each
run); the adapter tests use a stubbed `ClientSession` and run in
under a second.

## How the bridge works

As of **v1.1.0** the bridge is the SDK itself. `agent.py` uses
[`eap_core.mcp.client.McpClientPool`](../../packages/eap-core/src/eap_core/mcp/client/pool.py)
directly:

```python
async with McpClientPool([cfg_bankdw, cfg_sfcrm]) as pool:
    registry = pool.build_tool_registry()
    rows = await registry.invoke("bankdw__query_sql", {"sql": "...", "limit": 50})
```

The pool spawns each MCP server subprocess, opens stdio sessions,
captures advertised `outputSchema` per tool, and produces a populated
`McpToolRegistry` with namespaced `<server-name>__<tool-name>`
forwarders. Reconnect / health-check / per-call timeout / typed
errors / OTel spans / opt-in output-schema validation all live in
the SDK now; see the `eap_core.mcp.client` subpackage.

`mcp_client_adapter.py` next to this file is preserved as a ~25-line
**v1.0 → v1.1 compat shim**: `connect_servers`, `build_tool_specs`,
and `ServerHandle` keep their v1.0 signatures and delegate to the SDK.
External callers that pinned to the v1.0 example surface can upgrade
without touching their own code.

`agent.py` wires it together: enter the pool, register every remote
tool, then call the registry by name. No LLM is involved in the demo
— the goal is to prove the *infrastructure*, not the language-model
loop. Follow-on work would attach a configured `EnterpriseLLM` and
let it drive the same flow via natural-language tool selection.

## What this validation surfaced

The headline finding: **`eap_core.mcp` shipped server primitives but
no MCP client** through v1.0. This validation drove the v1.1 plan and
implementation. Each gap below is now **CLOSED in v1.1.0**, with a
one-line pointer to the SDK module that closes it.

1. **No structured server-config primitive.** — **CLOSED in v1.1.**
   [`eap_core.mcp.client.McpServerConfig`](../../packages/eap-core/src/eap_core/mcp/client/config.py)
   is a pydantic v2 model with name/command/args/cwd/env/timeout
   fields and a `transport` discriminator (only `stdio` in v1.1; v1.2
   may add `http`). Replaces the v1.0 `list[dict[str, Any]]` shape.

2. **No session lifecycle (pool / retry / timeout).** — **CLOSED in v1.1.**
   [`eap_core.mcp.client.McpClientPool`](../../packages/eap-core/src/eap_core/mcp/client/pool.py)
   is an async context manager with `reconnect(name)` /
   `health_check()` / per-server `request_timeout_s` enforced by
   [`McpClientSession`](../../packages/eap-core/src/eap_core/mcp/client/session.py).
   The per-call timeout raises `McpToolTimeoutError`; pool teardown
   is bound to a single `AsyncExitStack` owned by the pool.

3. **No output-schema validation against the remote tool's
   advertised shape.** — **CLOSED in v1.1** (opt-in, shallow).
   [`eap_core.mcp.client.adapter._maybe_validate`](../../packages/eap-core/src/eap_core/mcp/client/adapter.py)
   threads each tool's advertised `outputSchema` (captured on
   `McpServerHandle.tool_output_schemas` at spawn time) into the
   forwarder. Enable via `McpServerConfig(validate_output_schemas=True)`;
   shape mismatches raise `McpOutputSchemaError`. Deeper JSON-Schema
   validation is flagged for v1.2 (would require adding `jsonschema`
   as a dep — over-engineered for v1.1 given most servers don't yet
   publish an `outputSchema`).

4. **No observability spans around remote calls.** — **CLOSED in v1.1.**
   [`eap_core.mcp.client.session.McpClientSession`](../../packages/eap-core/src/eap_core/mcp/client/session.py)
   wraps every `call_tool` in an OTel span (`mcp.server.name`,
   `mcp.tool.name`, `mcp.duration_s`, `mcp.error.kind` on failure) —
   symmetric with the server-side observability middleware. Zero-cost
   no-op when the `[otel]` extra isn't installed.

5. **No reconnect-on-stale logic / typed errors.** — **CLOSED in v1.1.**
   [`eap_core.mcp.client.errors`](../../packages/eap-core/src/eap_core/mcp/client/errors.py)
   defines `McpClientError` + 5 subclasses (`McpServerSpawnError`,
   `McpServerDisconnectedError`, `McpToolTimeoutError`,
   `McpToolInvocationError`, `McpOutputSchemaError`). The adapter
   catches `McpServerDisconnectedError`, calls `pool.reconnect`, and
   re-raises so the caller decides retry semantics. (Known v1.2
   follow-up: per-handle nested `AsyncExitStack` so reconnect can
   unwind the old subprocess immediately instead of waiting for pool
   exit — the current implementation can leak fds across many
   reconnects in a long-lived pool.)

### Already closed — v0.7.1

- **Server-side `str(result)` serialisation.** This validation
  exercise originally surfaced a bug: `eap_core.mcp.server.
  build_mcp_server` emitted `TextContent(text=str(result))` for tool
  results, which produced a Python repr (`field='x'
  nested=Model(...)`) for pydantic v2 BaseModel returns — unparseable
  by non-Python MCP clients. Fixed in v0.7.1 via
  `_serialize_for_text_content` which routes BaseModel through
  `model_dump_json()` and dict/list through `json.dumps`. The
  adapter's response decoder is now a single `json.loads` call;
  it does not need an AST-based fallback parser.

All five gaps shipped in **v1.1.0** under the `eap_core.mcp.client`
subpackage. The per-agent shim that used to live in
`mcp_client_adapter.py` is now a ~25-line backward-compat layer that
delegates to the SDK so callers pinned to the v1.0 surface keep
working.

## Follow-on work (not in this validation)

- Wire an `EnterpriseLLM` with `LocalRuntimeAdapter` (or a real
  provider) so the cross-domain query is driven by tool-selection
  from a language model, not hard-coded SQL.
- Add an `eap eval` golden-set entry for the cross-domain query so
  regressions in either MCP server's contract get caught in CI.
- **v1.2 candidates from this validation:** deeper JSON-Schema
  output validation (currently shallow required-keys check),
  per-handle nested `AsyncExitStack` so reconnect unwinds the old
  subprocess immediately, and HTTP/SSE transport (the
  `McpServerConfig.transport` discriminator is already in place).

## Data and prerequisites

The two MCP servers load their seed CSVs from `samples/bankdw/` and
`samples/sfcrm/`. Both directories are committed in the worktree
root.

Python 3.11+. Required packages: `eap-core`, `mcp>=0.9`, `duckdb`,
`pydantic>=2.7`. Run from the worktree root so `eap-core` resolves
from the workspace's editable install:

```bash
uv run --with mcp --with duckdb python examples/cross-domain-agent/agent.py
uv run --with mcp --with duckdb pytest examples/cross-domain-agent/tests -q
```
