# EAP-Core examples

Eight example projects, organised by what they teach. All are
runnable from a clone of this repo without cloud credentials unless
explicitly noted.

## Reference agent templates

The starting points for new agents. Each is the output of an
`eap create-agent --template <name>` invocation, plus a README that
maps the wiring back to the user guide.

| Project | Template | Demonstrates |
|---|---|---|
| [`transactional-agent/`](transactional-agent/) | `transactional` | Action-style agent — writes via tools, policy gates, `requires_auth=True` tool, idempotency-key dedup. Good base for any agent that mutates state. |
| [`research-agent/`](research-agent/) | `research` | Retrieval-style agent — `search_docs` tool, RAG-backed reasoning, eval golden-set. Good base for QA / research / RAG assistants. |
| [`mcp-server-example/`](mcp-server-example/) | `mcp_server` | Standalone MCP stdio server — exposes EAP-Core tools to any MCP-aware client (Claude Code, Claude Desktop, IDE extensions, other agents). |

## Cloud reference implementations

End-to-end references that wire all the cloud-provider integrations
(identity, observability, memory, registry, payments, eval) into the
same business logic. Read side-by-side with the user guides.

| Project | Mirror of | Cloud |
|---|---|---|
| [`agentcore-bank-agent/`](agentcore-bank-agent/) | [`docs/user-guide-aws-agentcore.md`](../docs/user-guide-aws-agentcore.md) | AWS Bedrock AgentCore (11 services) |
| [`vertex-bank-agent/`](vertex-bank-agent/) | [`docs/user-guide-gcp-vertex.md`](../docs/user-guide-gcp-vertex.md) | GCP Vertex Agent Engine |

Both share the same `agent.py` business logic — only the integration
wiring differs.

## Validation examples (added in 2026-05)

Three projects that together validate the SDK against a realistic
data use case: loading CSV sample data into in-memory DuckDB,
exposing it as MCP servers, and consuming both servers from a
cross-domain EAP-Core agent. The two MCP servers stand on their own;
the cross-domain agent is the end-to-end test.

| Project | Demonstrates |
|---|---|
| [`bankdw-mcp-server/`](bankdw-mcp-server/) | Payments data warehouse (5-table star schema, ~3000 rows) exposed as an MCP stdio server. Three tools: `list_tables`, `describe_table`, `query_sql`. Read-only SQL guard with first-keyword allow-list + word-boundary mid-statement block. |
| [`sfcrm-mcp-server/`](sfcrm-mcp-server/) | Salesforce CRM (15-table operational schema, ~900 rows) exposed via the same three-tool surface as bankdw. Confirms the pattern generalises from a star schema to a many-table operational schema without code changes. |
| [`cross-domain-agent/`](cross-domain-agent/) | EAP-Core agent that spawns BOTH MCP servers as stdio subprocesses, wraps each remote tool as a local `@mcp_tool`-style forwarder (namespaced `server__tool`), and runs a cross-domain query (top-5 SFDC Accounts → matching bankdw parties). The README documents five open SDK gaps the exercise surfaced — backlog for a v0.8.0 `eap_core.mcp.client` module. |

The validation surfaced a serialization bug in `eap_core.mcp.server`
fixed in v0.7.1 (BaseModel returns were being emitted as Python repr
instead of JSON). See `CHANGELOG.md` for that fix.

## Running an example

Each project has its own `pyproject.toml` and `README.md`. Most can
run from the repo root via:

```bash
cd examples/<project>
python <entry>.py
```

The validation examples (`bankdw-mcp-server`, `sfcrm-mcp-server`,
`cross-domain-agent`) need the `mcp` and `duckdb` Python packages.
Their READMEs document the exact invocations.

For tests, run from the repo root with the relevant extras layered
on:

```bash
uv run --with mcp --with duckdb pytest examples/<project>/tests -q
```

(Bare `tests/` package names collide if you try to run two example
projects' tests in a single pytest invocation — run them
sequentially.)
