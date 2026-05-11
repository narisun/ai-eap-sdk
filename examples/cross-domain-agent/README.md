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

`mcp_client_adapter.py` is a ~200-line shim sitting between the
upstream `mcp.client.stdio` API and EAP-Core's `McpToolRegistry`.
Four pieces:

1. **`ServerHandle`** — a small dataclass carrying the server name,
   the open MCP `ClientSession`, and the list of tool names the
   server advertised.

2. **`connect_servers(configs, stack)`** — spawns each MCP server
   subprocess and opens an MCP session via `stdio_client`. Sessions
   are entered into a caller-provided `AsyncExitStack` so subprocess
   teardown is deterministic on exit (no zombies).

3. **`build_tool_specs(handles)`** — for every remote tool on every
   handle, builds a local `ToolSpec`. The local name is namespaced
   as `<server-name>__<tool-name>` to avoid collisions (both
   validation servers expose `query_sql`). The input schema is left
   as the permissive `{"type": "object"}` because the remote server
   re-validates on call.

4. **`_build_one(handle, remote_name, local_name)`** — a factory
   that captures `handle` + `remote_name` as **function parameters**
   (not loop variables) so each forwarder closure binds the correct
   values. Inlining the forwarder inside `build_tool_specs`'s loop
   would be a classic closure-capture bug — every forwarder would
   call the *last* tool name. The unit test
   `test_forwarder_invokes_correct_remote_tool_with_kwargs` pins
   this.

`agent.py` wires it together: spawn both servers, register every
remote tool, then call the registry by name. No LLM is involved in
the demo — the goal is to prove the *infrastructure*, not the
language-model loop. Follow-on work would attach a configured
`EnterpriseLLM` and let it drive the same flow via natural-language
tool selection.

## What this validation surfaced

The headline finding: **`eap_core.mcp` ships server primitives but
no MCP client**. `mcp_client_adapter.py` is a per-agent shim that
papers over the gap. Each item below is a concrete missing piece
of the SDK surface, with a one-sentence sketch of what an
`eap_core.mcp.client` module would provide.

1. **No structured server-config primitive.** The adapter accepts
   `list[dict[str, Any]]` for server configs (name + command + args
   + cwd + env). A `McpServerConfig` pydantic model with validation
   (command exists, cwd resolves, env is `dict[str, str]`) would
   eliminate the schema-by-comment pattern at the top of
   `connect_servers`.

2. **No session lifecycle (pool / retry / timeout).** Today the
   adapter opens one session per server per process and relies on
   the `AsyncExitStack` for teardown. An `McpClientPool` with
   per-server `connect()` / `reconnect()` / `health_check()` and a
   configurable `request_timeout` would let an agent recover from
   a crashed subprocess without rebuilding its registry. Long-lived
   agents that run for hours need this.

3. **No output-schema validation against the remote tool's
   advertised shape.** The adapter sets `output_schema=None` on
   every spec because we don't re-fetch the remote schema (and most
   MCP tools don't advertise output schemas anyway). An
   `McpRemoteToolSpec` that captures `tools/list`'s `inputSchema`
   per tool *and* runs response shape checks against an optional
   `outputSchema` would catch server-side contract drift in
   integration tests.

4. **No observability spans around remote calls.** Each
   `session.call_tool(...)` is a network-ish operation (subprocess
   I/O) but there is no `with otel_span("mcp.client.call_tool", ...)`
   wrapping it. The SDK already integrates OTEL spans on the server
   side via `eap_core.mcp.server`'s middleware chain; a symmetric
   client-side `McpClientMiddleware` would close the loop.

5. **No reconnect-on-stale logic.** If a server subprocess dies
   mid-session (OOM, segfault, SIGPIPE) every subsequent
   `call_tool` raises raw `anyio` errors out of the adapter. An
   SDK-managed pool would notice, mark the handle dead, optionally
   respawn, and surface a typed `McpClientError` to the caller —
   the same shape `eap_core.mcp.registry.McpToolRegistry.invoke`
   uses today for local errors.

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

The five open items together are roughly the shape of a v0.8.0
`eap_core.mcp.client` module. They are flagged here as feedback
from this validation exercise, not implemented — the validation
plan explicitly defers the SDK change.

## Follow-on work (not in this validation)

- Wire an `EnterpriseLLM` with `LocalRuntimeAdapter` (or a real
  provider) so the cross-domain query is driven by tool-selection
  from a language model, not hard-coded SQL.
- Push the adapter (or its successor `eap_core.mcp.client` module)
  into the SDK, with the lifecycle / observability /
  schema-validation items above.
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
uv run --with mcp --with duckdb pytest examples/cross-domain-agent/tests -q
```
