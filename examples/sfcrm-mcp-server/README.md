# sfcrm-mcp-server

EAP-Core validation example #2. Exposes the `sfcrm` Salesforce CRM
dataset (15 tables, ~900 rows of seed data) as an MCP stdio server
backed by an in-memory DuckDB.

This is one of two MCP servers (alongside `bankdw-mcp-server`) that the
`cross-domain-agent` example consumes end-to-end. It validates the
EAP-Core MCP scaffolding (`@mcp_tool`, `McpToolRegistry`, `run_stdio`)
on a wider, operational schema and confirms the bankdw pattern
generalizes - only env vars and per-tool description strings differ.

## What it does

On startup:

1. Loads every CSV under `$SFCRM_DATA_DIR` (default
   `../../samples/sfcrm`) into a fresh in-memory DuckDB. Table name =
   CSV stem.
2. Parses `$SFCRM_SCHEMA_CSV` (default
   `../../samples/sfcrm_schema.csv`) into typed per-column metadata
   (`ColumnInfo` / `TableSchema`).
3. Registers three tools on an `McpToolRegistry` and serves them over
   MCP stdio.

Tables loaded from the seed data (standard Salesforce object names,
PascalCase): `Account`, `Campaign`, `CampaignMember`, `Case`,
`Contact`, `Contract`, `Event`, `Lead`, `Opportunity`,
`OpportunityContactRole`, `OpportunityLineItem`, `Pricebook2`,
`PricebookEntry`, `Product2`, `Task`.

## Tool surface

- **`list_tables`** - Discovery surface: returns the domain plus a
  per-table summary (name, one-line description, row count). Call
  this first.
- **`describe_table(table)`** - Full schema for one table: every
  column with data type, size, nullability, PK/FK flags, semantic
  tags, free-text description, and an example value. Use this before
  writing a query so the LLM gets column names and types right.
- **`query_sql(sql, limit=100)`** - Run a read-only DuckDB query.
  Returns columns + rows (capped at `limit`, default 100, max 1000)
  plus a `truncated` flag. Only `SELECT` / `WITH` / `DESCRIBE` /
  `SHOW` / `EXPLAIN` / `PRAGMA` are accepted; writes and DDL are
  rejected by an allow-list guard.

## Running standalone

From the workspace root (where `eap-core` is installed editable):

```bash
uv run --with mcp --with duckdb python examples/sfcrm-mcp-server/server.py
```

Or, if your shell is already in this directory and the workspace
venv is active:

```bash
python server.py
```

The server speaks MCP over stdio - it's meant to be spawned as a
subprocess by an MCP client (Claude Desktop, the `cross-domain-agent`,
or any other MCP host).

## Pointing Claude Desktop at it

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sfcrm": {
      "command": "uv",
      "args": [
        "run",
        "--with", "mcp",
        "--with", "duckdb",
        "python",
        "/absolute/path/to/examples/sfcrm-mcp-server/server.py"
      ]
    }
  }
}
```

## Tests

```bash
cd examples/sfcrm-mcp-server
uv run --with mcp --with duckdb --with pytest --with pytest-asyncio pytest -q
```

Two test files - `tests_sfcrm/test_tools.py` covers tool logic
(list/describe/query, Account <-> Opportunity JOIN via `AccountId`,
truncation against `OpportunityLineItem`, error paths);
`tests_sfcrm/test_query_safety.py` covers the read-only guard's reject paths
for writes and DDL. Tests call each tool's underlying `.fn` directly -
no stdio session is exercised here. The stdio path is tested at the
`cross-domain-agent` layer.

## Known limitations

- **Regex-based SQL safety, not token-aware.** The `_BLOCKED_KEYWORDS`
  defense-in-depth regex matches word boundaries, so a string literal
  like `SELECT 'INSERT' AS x` is incorrectly rejected. Acceptable for
  a validation example; production deployments should use
  `sqlglot` / `sqlparse`.
- **In-memory DuckDB.** Data reloads on every server start. No
  persistence layer.
- **No auth.** `requires_auth=False` on every tool - this is local
  stdio.
- **`limit` schema bounds aren't surfaced.** The `Annotated[int,
  Field(ge=1, le=1000)] = 100` annotation on `query_sql` carries the
  bounds, but `eap_core.mcp`'s decorator builds its input schema from
  `get_type_hints(fn)` which (without `include_extras=True`) strips
  `Annotated` metadata. The bound is enforced inside the function;
  the JSON Schema shown to clients just says `{"type": "integer"}`.
  Flagged as an SDK enhancement opportunity.

## Data source

CSVs live at `samples/sfcrm/` at the workspace root and are not
shipped with the SDK package - they're committed alongside the
validation examples. The schema CSV at `samples/sfcrm_schema.csv`
is the source of truth for column metadata.

## See also

- `examples/bankdw-mcp-server/` - the first validation MCP server
  (payments data warehouse).
- `examples/cross-domain-agent/` - end-to-end agent that consumes
  both servers as remote tools.
