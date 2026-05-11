# SDK Validation — bankdw + sfcrm MCP servers + cross-domain agent

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End-to-end validation of EAP-Core's MCP scaffolding via two real data-backed MCP servers (`bankdw`, `sfcrm`) plus a cross-domain EAP-Core agent that consumes both as remote tools to answer questions that span the two domains.

**Architecture:** Three example projects under `examples/`. Both MCP servers load CSV sample data into in-memory DuckDB and expose a discovery + query tool triplet (`list_tables`, `describe_table`, `query_sql`). The cross-domain agent spawns both servers as MCP subprocesses via `mcp.client.stdio`, wraps each remote tool as a local `@mcp_tool` that forwards over the MCP stdio session, then wires everything into `EnterpriseLLM` with the default middleware chain. One end-to-end demo question proves the pipeline.

**Tech Stack:** Python 3.11+ for the example projects; **DuckDB** (in-memory) as the SQL engine; `mcp>=0.9` for stdio server + client; pydantic v2 for tool input/output contracts; `eap-core` for the `@mcp_tool` decorator + registry + middleware. No new dependencies on the SDK itself.

**Validation goals (success criteria):**
1. Both MCP servers boot, load their CSV data, register three tools, and respond to `tools/list` + `tools/call` over stdio.
2. Tool tests (no stdio, direct invocation) prove correct shape on representative SQL queries — single-table SELECT, JOIN, COUNT, schema introspection.
3. Cross-domain agent answers a natural-language question that requires querying both domains in one session — proves the SDK orchestrates remote MCP servers cleanly.
4. **A gap-finding artifact**: identify SDK limitations the exercise surfaces (e.g. no first-class MCP client adapter). Captured in the cross-domain agent's README.

**Out of scope:**
- Eval golden-set / `eap eval` integration (was option 3; user picked option 2).
- Cloud deployment (Bedrock AgentCore or Vertex) of either server or the agent.
- Persistent DuckDB; data reloads every server start.
- Production hardening (auth, rate limits, query plan caching, etc.). These are validation examples.
- A first-class MCP-client adapter in `eap_core.mcp` — flagged as gap, deferred.

**Project layout:**
```
examples/
  bankdw-mcp-server/
    server.py              # entry: build registry, run_stdio
    duck.py                # in-memory DuckDB factory + CSV loader
    schema.py              # parse bankdw_schema.csv into typed metadata
    tools/
      __init__.py
      list_tables.py
      describe_table.py
      query_sql.py
    tests/
      test_tools.py
      test_query_safety.py
    pyproject.toml
    README.md
  sfcrm-mcp-server/        # mirror shape; sfcrm-specific data + schema
    ...
  cross-domain-agent/
    agent.py               # spawns both MCP servers, wires remote tools
    mcp_client_adapter.py  # bridge: remote MCP tool → local @mcp_tool wrapper
    tests/
      test_agent.py
    pyproject.toml
    README.md              # documents the SDK gaps the exercise surfaced
```

CSVs live at `samples/` (already in the repo). Each example project references them via env var (`BANKDW_DATA_DIR`, `SFCRM_DATA_DIR`) with sensible relative-path defaults (`../../samples/bankdw`, `../../samples/sfcrm`).

---

## Task 1: bankdw MCP server (`examples/bankdw-mcp-server/`)

**Why:** First of the two data-MCP servers. Establishes the pattern T2 will mirror. Pattern includes: schema-CSV → typed metadata parsing, CSV → DuckDB loader, three-tool surface, read-only-SQL guard, tool tests.

**Files:**
- Create: `examples/bankdw-mcp-server/pyproject.toml`
- Create: `examples/bankdw-mcp-server/README.md`
- Create: `examples/bankdw-mcp-server/server.py`
- Create: `examples/bankdw-mcp-server/duck.py`
- Create: `examples/bankdw-mcp-server/schema.py`
- Create: `examples/bankdw-mcp-server/tools/__init__.py`
- Create: `examples/bankdw-mcp-server/tools/list_tables.py`
- Create: `examples/bankdw-mcp-server/tools/describe_table.py`
- Create: `examples/bankdw-mcp-server/tools/query_sql.py`
- Create: `examples/bankdw-mcp-server/tests/test_tools.py`
- Create: `examples/bankdw-mcp-server/tests/test_query_safety.py`
- Create: `examples/bankdw-mcp-server/tests/__init__.py`

### Subtasks

- [ ] **Step 1.1: `pyproject.toml`** — minimal hatch-built project

```toml
[project]
name = "bankdw-mcp-server"
version = "0.1.0"
description = "EAP-Core validation example: payments data warehouse exposed as an MCP server backed by in-memory DuckDB."
requires-python = ">=3.11"
dependencies = [
    "eap-core>=0.7",
    "mcp>=0.9",
    "duckdb>=1.0",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.4", "mypy>=1.10"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["."]
include = ["server.py", "duck.py", "schema.py", "tools/**"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 1.2: `schema.py`** — parse `bankdw_schema.csv` into typed metadata

```python
"""Parse bankdw_schema.csv into typed per-table metadata.

The schema CSV is the source of truth for column-level metadata
(types, nullability, FK relationships, semantic_tags, descriptions,
example_value). The describe_table tool serves this metadata directly
to MCP clients so an LLM has everything it needs to write a correct
query without hallucination.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import BaseModel


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    data_size: int | None
    nullable: bool
    description: str
    is_primary_key: bool
    is_foreign_key: bool
    foreign_key_table: str | None
    foreign_key_column: str | None
    semantic_tags: list[str]
    example_value: str


class TableSchema(BaseModel):
    domain: str
    name: str
    description: str
    columns: list[ColumnInfo]


def _parse_bool(s: str) -> bool:
    return s.strip().upper() == "Y"


def _parse_tags(s: str) -> list[str]:
    s = s.strip()
    return [t for t in s.split("|") if t] if s else []


def _parse_size(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_schema(schema_csv: Path) -> dict[str, TableSchema]:
    """Return a dict mapping table_name → TableSchema for every table in the
    schema CSV. Tables are accumulated in encounter order; columns preserve
    file order within each table."""
    by_table: dict[str, TableSchema] = {}
    with schema_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            tname = row["table_name"]
            if tname not in by_table:
                by_table[tname] = TableSchema(
                    domain=row["domain_name"],
                    name=tname,
                    description=row["table_description"],
                    columns=[],
                )
            by_table[tname].columns.append(
                ColumnInfo(
                    name=row["column_name"],
                    data_type=row["data_type"],
                    data_size=_parse_size(row["data_size"]),
                    nullable=not _parse_bool(row["nullable"]),
                    description=row["description"],
                    is_primary_key=_parse_bool(row["is_primary_key"]),
                    is_foreign_key=_parse_bool(row["is_foreign_key"]),
                    foreign_key_table=row["foreign_key_table"] or None,
                    foreign_key_column=row["foreign_key_column"] or None,
                    semantic_tags=_parse_tags(row["semantic_tags"]),
                    example_value=row["example_value"],
                )
            )
    return by_table
```

- [ ] **Step 1.3: `duck.py`** — in-memory DuckDB factory + CSV loader

```python
"""In-memory DuckDB factory.

Loads every <table>.csv from a data directory into a fresh in-memory
DuckDB connection using DuckDB's auto-typing CSV reader. The connection
is the unit of state — server.py owns one; tests own their own. There
is no shared global connection (per developer-guide §6.7 on per-process
state).

Read-only enforcement is layered on at the tool level (see
tools/query_sql.py's _is_read_only check). DuckDB in-memory connections
cannot be made strictly read-only after data load — the SQL allow-list
in the query tool is the actual safety boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


def open_in_memory(data_dir: Path) -> "duckdb.DuckDBPyConnection":
    """Open a fresh in-memory DuckDB connection and load every CSV
    under ``data_dir`` as a table. Table name = CSV stem.

    Raises ``FileNotFoundError`` if ``data_dir`` does not exist.
    """
    import duckdb

    if not data_dir.is_dir():
        raise FileNotFoundError(f"data_dir does not exist: {data_dir}")

    con = duckdb.connect(":memory:")
    for csv_path in sorted(data_dir.glob("*.csv")):
        table_name = csv_path.stem
        # `read_csv_auto` infers types; `header=true` is the default.
        # We use parameter binding for the path because table names
        # can't be parametrized.
        con.execute(
            f'CREATE TABLE "{table_name}" AS SELECT * FROM read_csv_auto(?)',
            [str(csv_path)],
        )
    return con


def row_count(con: "duckdb.DuckDBPyConnection", table: str) -> int:
    """Return the row count for a loaded table. Used by list_tables."""
    return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
```

- [ ] **Step 1.4: `tools/list_tables.py`**

```python
"""list_tables — return a summary of every table in the bankdw warehouse.

The output is the LLM's discovery surface: name, one-line description,
row count. The LLM then picks one or more tables and calls
``describe_table`` for full schema.
"""

from __future__ import annotations

from pydantic import BaseModel

from eap_core.mcp import mcp_tool

# Module-level handles set by server.py at startup. Tests construct
# their own and pass them via the indirection function below.
_con = None
_schema = None


class TableSummary(BaseModel):
    name: str
    description: str
    row_count: int


class ListTablesResult(BaseModel):
    domain: str
    tables: list[TableSummary]


def _bind(con, schema) -> None:
    """Server.py calls this after loading data. Tests use it to inject
    fixtures."""
    global _con, _schema
    _con = con
    _schema = schema


@mcp_tool(
    description=(
        "List every table in the bankdw payments data warehouse with a "
        "one-line description and row count. Call this first to discover "
        "what's available, then call describe_table for column-level detail."
    )
)
def list_tables() -> ListTablesResult:
    from duck import row_count

    assert _con is not None and _schema is not None, "tools not bound — server.py:_bind() must run first"
    tables = [
        TableSummary(
            name=t.name,
            description=t.description,
            row_count=row_count(_con, t.name),
        )
        for t in _schema.values()
    ]
    domain = next(iter(_schema.values())).domain if _schema else ""
    return ListTablesResult(domain=domain, tables=tables)
```

- [ ] **Step 1.5: `tools/describe_table.py`**

```python
"""describe_table — return full schema for one table.

Emits every column with type / size / nullability / PK/FK flags /
semantic_tags / free-text description / example value. This is the
LLM's brief for writing a correct SELECT.
"""

from __future__ import annotations

from pydantic import BaseModel

from eap_core.mcp import mcp_tool

from schema import TableSchema

_schema: dict[str, TableSchema] | None = None


def _bind(schema: dict[str, TableSchema]) -> None:
    global _schema
    _schema = schema


class DescribeTableError(BaseModel):
    error: str
    available_tables: list[str]


@mcp_tool(
    description=(
        "Return full schema for one bankdw table: every column with its "
        "data type, nullability, primary/foreign key flags, semantic "
        "tags, description, and an example value. Use this before "
        "writing a query against the table to ensure column names and "
        "types are correct."
    )
)
def describe_table(table: str) -> TableSchema | DescribeTableError:
    assert _schema is not None, "describe_table not bound — server.py:_bind() must run first"
    if table not in _schema:
        return DescribeTableError(
            error=f"unknown table: {table!r}",
            available_tables=sorted(_schema.keys()),
        )
    return _schema[table]
```

- [ ] **Step 1.6: `tools/query_sql.py`** — the load-bearing tool

```python
"""query_sql — run a read-only SQL query against the bankdw warehouse.

Returns columns + rows + a truncated flag. Hard caps:
- Row limit defaulted to 100, capped at 1000 to keep MCP responses
  reasonable. Callers asking for more should paginate via OFFSET in SQL.
- Only SELECT / WITH / DESCRIBE / SHOW / EXPLAIN / PRAGMA statements
  pass the read-only gate. Anything else returns an error result.

The read-only check is a simple first-keyword allow-list. DuckDB
in-memory connections can't be made strictly read-only after data
load; this check is the actual safety boundary. It's deliberately
conservative — better to reject a valid query than execute a write.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from eap_core.mcp import mcp_tool

_con = None

# Allow-list: case-insensitive first keyword after stripping comments
# and whitespace. PRAGMA is included so callers can run
# `PRAGMA table_info(...)` style introspection.
_READ_ONLY_FIRST_TOKENS = {
    "select", "with", "describe", "show", "explain", "pragma",
}

# Block these mid-statement even if first token is allowed, to defend
# against `SELECT * FROM t; DROP TABLE t` style attacks. (Note: DuckDB
# does NOT execute multiple statements via `execute()` so this is
# defense-in-depth, not the primary control.)
_BLOCKED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|copy|export|truncate)\b",
    re.IGNORECASE,
)


def _bind(con) -> None:
    global _con
    _con = con


def _strip_sql_comments(sql: str) -> str:
    # Remove /* ... */ blocks, then -- ... line comments.
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def _is_read_only(sql: str) -> bool:
    stripped = _strip_sql_comments(sql)
    if not stripped:
        return False
    first = stripped.split(None, 1)[0].lower()
    if first not in _READ_ONLY_FIRST_TOKENS:
        return False
    if _BLOCKED_KEYWORDS.search(stripped):
        return False
    return True


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


class QueryError(BaseModel):
    error: str
    sql: str


@mcp_tool(
    description=(
        "Run a read-only SQL query against the bankdw payments warehouse "
        "(DuckDB dialect). Returns up to `limit` rows (default 100, max "
        "1000). Only SELECT / WITH / DESCRIBE / SHOW / EXPLAIN / PRAGMA "
        "statements are accepted; writes and DDL are rejected. Use "
        "list_tables and describe_table first to discover the schema."
    )
)
def query_sql(sql: str, limit: int = Field(default=100, ge=1, le=1000)) -> QueryResult | QueryError:
    assert _con is not None, "query_sql not bound — server.py:_bind() must run first"

    if not _is_read_only(sql):
        return QueryError(
            error="only SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/PRAGMA statements are accepted",
            sql=sql,
        )

    # Wrap user SQL with an outer LIMIT so we don't materialize a
    # 100M-row result. limit+1 lets us detect truncation cheaply.
    wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {limit + 1}"
    try:
        result = _con.execute(wrapped)
        columns = [d[0] for d in result.description]
        all_rows = result.fetchall()
    except Exception as e:
        return QueryError(error=str(e), sql=sql)

    truncated = len(all_rows) > limit
    rows = [dict(zip(columns, r, strict=True)) for r in all_rows[:limit]]
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )
```

**Important:** `Field(default=100, ge=1, le=1000)` in a function signature isn't how pydantic v2 + plain functions work — Field defaults are for BaseModel attributes, not function args. The `@mcp_tool` decorator generates JSON Schema from type hints via TypeAdapter; for limit bounds we need `Annotated[int, Field(ge=1, le=1000)] = 100` instead. Verify behavior in Step 1.10's test write-up; if the schema generation chokes on Field-in-signature, switch to `Annotated[int, ...]` form.

- [ ] **Step 1.7: `tools/__init__.py`**

```python
"""bankdw MCP tools — three-tool surface for LLM-driven query."""

from tools.describe_table import describe_table
from tools.list_tables import list_tables
from tools.query_sql import query_sql

__all__ = ["describe_table", "list_tables", "query_sql"]
```

- [ ] **Step 1.8: `server.py`**

```python
"""bankdw-mcp-server — standalone MCP-stdio server.

Run from this directory:

    python server.py

Loads every CSV from `$BANKDW_DATA_DIR` (default `../../samples/bankdw`)
into a fresh in-memory DuckDB, parses `$BANKDW_SCHEMA_CSV` (default
`../../samples/bankdw_schema.csv`), and registers three tools:
list_tables, describe_table, query_sql.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from eap_core.mcp import McpToolRegistry
from eap_core.mcp.server import run_stdio

from duck import open_in_memory
from schema import parse_schema
from tools import describe_table, list_tables, query_sql
from tools.describe_table import _bind as _bind_describe
from tools.list_tables import _bind as _bind_list
from tools.query_sql import _bind as _bind_query

REGISTRY = McpToolRegistry()


def _resolve_paths() -> tuple[Path, Path]:
    here = Path(__file__).resolve().parent
    data_dir = Path(os.environ.get(
        "BANKDW_DATA_DIR",
        here.parent.parent / "samples" / "bankdw",
    )).resolve()
    schema_csv = Path(os.environ.get(
        "BANKDW_SCHEMA_CSV",
        here.parent.parent / "samples" / "bankdw_schema.csv",
    )).resolve()
    return data_dir, schema_csv


def _init() -> None:
    data_dir, schema_csv = _resolve_paths()
    con = open_in_memory(data_dir)
    schema = parse_schema(schema_csv)
    _bind_list(con, schema)
    _bind_describe(schema)
    _bind_query(con)
    REGISTRY.register(list_tables.spec)
    REGISTRY.register(describe_table.spec)
    REGISTRY.register(query_sql.spec)


async def main() -> None:
    _init()
    await run_stdio(REGISTRY, server_name="bankdw-mcp-server")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 1.9: Smoke-test the server boots**

```bash
cd /Users/admin-h26/EAAP/ai-eap-sdk/examples/bankdw-mcp-server
uv run --with mcp --with duckdb --with eap-core python -c "
from server import _init, REGISTRY
_init()
print('tools registered:', [t.name for t in REGISTRY.list_tools()])
"
```

Expected output: `tools registered: ['list_tables', 'describe_table', 'query_sql']`.

If `_init()` raises, fix before proceeding. Common issues: data dir path resolution wrong; pydantic v2 Field-in-signature rejection (switch to `Annotated[int, Field(...)] = 100`).

- [ ] **Step 1.10: `tests/test_tools.py`** — direct-call tool tests, no stdio

```python
"""Tool tests — call each @mcp_tool function directly via .fn, no stdio.

The MCP stdio path is exercised once at the cross-domain-agent layer
(T3) — there, the agent spawns this server as a subprocess and lists
its tools. Here we test tool logic in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Server lives one dir up; add to sys.path so we can import its modules.
import sys
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from duck import open_in_memory  # noqa: E402
from schema import parse_schema  # noqa: E402
from tools.describe_table import _bind as _bind_describe, describe_table  # noqa: E402
from tools.list_tables import _bind as _bind_list, list_tables  # noqa: E402
from tools.query_sql import _bind as _bind_query, query_sql  # noqa: E402


@pytest.fixture(scope="module")
def loaded():
    """Load the real bankdw data once per test module."""
    data_dir = SERVER_DIR.parent.parent / "samples" / "bankdw"
    schema_csv = SERVER_DIR.parent.parent / "samples" / "bankdw_schema.csv"
    con = open_in_memory(data_dir)
    schema = parse_schema(schema_csv)
    _bind_list(con, schema)
    _bind_describe(schema)
    _bind_query(con)
    return con, schema


def test_list_tables_returns_all_five_bankdw_tables(loaded):
    result = list_tables.fn()  # call the underlying function directly
    names = {t.name for t in result.tables}
    assert names == {
        "bridge_party_account", "dim_bank", "dim_party",
        "dim_product", "fact_payments",
    }
    # Row counts are real (fact_payments is ~1000 rows).
    fact = next(t for t in result.tables if t.name == "fact_payments")
    assert fact.row_count == 1000
    assert fact.description.startswith("Fact table")


def test_describe_table_returns_typed_columns(loaded):
    result = describe_table.fn(table="dim_party")
    assert result.name == "dim_party"
    party_id_col = next(c for c in result.columns if c.name == "PartyID")
    assert party_id_col.data_type == "varchar"
    assert "identifier" in party_id_col.semantic_tags
    assert not party_id_col.is_primary_key  # PartyKey is the PK
    party_key_col = next(c for c in result.columns if c.name == "PartyKey")
    assert party_key_col.is_primary_key is True


def test_describe_table_returns_error_with_available_tables_on_unknown(loaded):
    result = describe_table.fn(table="not_a_table")
    assert hasattr(result, "error")
    assert "not_a_table" in result.error
    assert "dim_party" in result.available_tables


def test_query_sql_simple_select(loaded):
    result = query_sql.fn(sql="SELECT BankID, BankName FROM dim_bank LIMIT 5")
    assert result.row_count <= 5
    assert result.columns == ["BankID", "BankName"]
    assert all("BankID" in row and "BankName" in row for row in result.rows)


def test_query_sql_join_across_tables(loaded):
    # Cross-table query: bank names linked via the bridge table.
    sql = """
        SELECT b.BankName, COUNT(*) AS n_accounts
        FROM bridge_party_account bpa
        JOIN dim_bank b ON bpa.BankID = b.BankID
        GROUP BY b.BankName
        ORDER BY n_accounts DESC
    """
    result = query_sql.fn(sql=sql)
    assert result.row_count > 0
    assert "BankName" in result.columns
    assert "n_accounts" in result.columns


def test_query_sql_truncates_at_limit(loaded):
    # fact_payments has 1000 rows; ask for 10.
    result = query_sql.fn(sql="SELECT * FROM fact_payments", limit=10)
    assert result.row_count == 10
    assert result.truncated is True


def test_query_sql_returns_error_on_invalid_sql(loaded):
    result = query_sql.fn(sql="SELECT * FROM nonexistent_table")
    assert hasattr(result, "error")
    assert "nonexistent_table" in result.error.lower() or "not found" in result.error.lower()
```

- [ ] **Step 1.11: `tests/test_query_safety.py`** — read-only guard tests

```python
"""query_sql read-only guard tests — must reject writes/DDL.

Cover the SQL allow-list: any non-SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/PRAGMA
first keyword is rejected, AND any mid-statement write keyword (INSERT,
UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, COPY, EXPORT, TRUNCATE) is
rejected as defense-in-depth.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sys
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from duck import open_in_memory  # noqa: E402
from schema import parse_schema  # noqa: E402
from tools.query_sql import _bind as _bind_query, query_sql  # noqa: E402


@pytest.fixture(scope="module")
def bound():
    data_dir = SERVER_DIR.parent.parent / "samples" / "bankdw"
    con = open_in_memory(data_dir)
    _bind_query(con)
    return con


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dim_bank",
        "INSERT INTO dim_bank VALUES (...)",
        "UPDATE dim_bank SET BankName = 'X'",
        "DELETE FROM dim_bank",
        "ALTER TABLE dim_bank ADD COLUMN c INTEGER",
        "CREATE TABLE x AS SELECT 1",
        "TRUNCATE dim_bank",
        "ATTACH 'other.db'",
        "COPY dim_bank TO '/tmp/x.csv'",
    ],
)
def test_write_statements_are_rejected(sql, bound):
    result = query_sql.fn(sql=sql)
    assert hasattr(result, "error")
    assert "read-only" in result.error.lower() or "SELECT" in result.error


def test_select_with_embedded_write_keyword_in_string_is_allowed(bound):
    """The defense-in-depth guard uses a word-boundary regex, but a
    SELECT against a column literally named INSERT/UPDATE/etc would
    trip false-positive. This test pins that the guard is conservative
    — a string containing the substring is fine, the keyword as an
    actual SQL token is not."""
    # We use a literal string in SELECT — no SQL keyword fires.
    result = query_sql.fn(sql="SELECT 'INSERT' AS my_string")
    # If this fails, the safety regex is too aggressive and needs
    # narrowing.
    assert not hasattr(result, "error"), result.error if hasattr(result, "error") else ""


def test_empty_or_whitespace_only_sql_is_rejected(bound):
    for sql in ["", "   ", "  -- only a comment\n", "/* block */ \n  "]:
        result = query_sql.fn(sql=sql)
        assert hasattr(result, "error")


def test_select_with_block_comment_is_allowed(bound):
    result = query_sql.fn(sql="/* explain */ SELECT 1 AS n")
    assert not hasattr(result, "error")
    assert result.columns == ["n"]
```

Note about the embedded-write-keyword test: the `_BLOCKED_KEYWORDS` regex above uses `\b` word boundaries which match string literal content as well as keyword tokens. So `SELECT 'INSERT' AS x` will trip `\bINSERT\b` and be rejected. Two options:

1. **Accept the false-positive** — document it: callers can't have keyword-name strings. Simplest.
2. **Use a real SQL tokenizer** (sqlparse, sqlglot) — proper but adds a dep.

For a validation example, option 1 is fine. **Update the test to expect rejection in that case**, AND add a comment in `query_sql.py` documenting the false-positive class. So the test becomes:

```python
def test_select_with_string_literal_containing_keyword_is_rejected_as_false_positive(bound):
    """Known false-positive: a string literal containing a blocked
    keyword trips the regex. Documented in query_sql.py; acceptable
    for a validation example. Production deployments should swap in
    sqlglot or sqlparse for token-aware parsing."""
    result = query_sql.fn(sql="SELECT 'INSERT' AS my_string")
    assert hasattr(result, "error")
```

- [ ] **Step 1.12: `README.md`**

Write a 60-100 line README covering:
- What this is (validation MCP server for bankdw payments warehouse)
- How to run it standalone (`python server.py` after env setup)
- The three-tool surface with one-line each
- How to point an MCP client at it (claude desktop config snippet)
- Where the data comes from (`samples/bankdw/`)
- Known limitations (regex-based SQL safety, in-memory only, etc.)
- Pointer to cross-domain agent example for end-to-end use

- [ ] **Step 1.13: Gauntlet**

```bash
cd examples/bankdw-mcp-server
uv run --with mcp --with duckdb --with eap-core --with pytest --with pytest-asyncio pytest -q
```

Expected: all tool tests + safety tests green. Test count likely 13-15.

Outside of this directory, run the SDK's gauntlet too — the example shouldn't regress anything:

```bash
cd /Users/admin-h26/EAAP/ai-eap-sdk
uv run pytest -m "not extras and not cloud" -q
```

Expected: same 576 passing as before (no changes inside `packages/`).

- [ ] **Step 1.14: Commit**

```bash
git add examples/bankdw-mcp-server
git commit -m "$(cat <<'EOF'
feat(examples): bankdw payments MCP server

EAP-Core validation example #1: payments data warehouse (5 tables,
~3000 rows) exposed as an MCP stdio server backed by in-memory DuckDB.

Three-tool surface:
- list_tables: discovery (name + description + row count)
- describe_table: per-column types, PK/FK, semantic tags, examples
- query_sql: read-only SQL with allow-list guard (SELECT/WITH/etc),
  row cap (default 100, max 1000), truncation flag

Data loaded from samples/bankdw/*.csv on server start. Schema
metadata parsed from samples/bankdw_schema.csv. Direct-call tool
tests cover SELECT, JOIN, COUNT, schema introspection, and the
read-only guard's reject paths.
EOF
)"
```

---

## Task 2: sfcrm MCP server (`examples/sfcrm-mcp-server/`)

**Why:** Second of the two MCP servers. Mirrors T1's structure with sfcrm-specific data (Salesforce schema, 15 tables). Confirms the pattern is generalizable, not a one-off.

**Files:**
- Create: parallel directory structure under `examples/sfcrm-mcp-server/`

### Subtasks

- [ ] **Step 2.1: Copy `bankdw-mcp-server/` to `sfcrm-mcp-server/` and adapt**

Per-file edits:
- `pyproject.toml` — change `name` to `sfcrm-mcp-server` and the description.
- `server.py` — change all env vars from `BANKDW_*` to `SFCRM_*`, default paths to `samples/sfcrm/` and `samples/sfcrm_schema.csv`, `server_name="sfcrm-mcp-server"`.
- Each tool's `@mcp_tool(description=...)` — replace "bankdw payments warehouse" wording with "sfcrm Salesforce CRM data".

`duck.py`, `schema.py`, the tool *logic*, the read-only guard, the test fixtures' import shape — **all unchanged**. The data is generic CSVs into DuckDB; the schema is generic per-column metadata.

- [ ] **Step 2.2: Update tests with sfcrm-specific assertions**

`tests/test_tools.py` — replace bankdw-specific assertions with sfcrm equivalents:
- `test_list_tables_returns_all_fifteen_sfcrm_tables` — expected set: `{Account, Campaign, CampaignMember, Case, Contact, Contract, Event, Lead, Opportunity, OpportunityContactRole, OpportunityLineItem, Pricebook2, PricebookEntry, Product2, Task}`.
- `test_describe_table_returns_typed_columns` — pick `Account`, assert `Id` is the primary key, `Name` is `varchar`, `AnnualRevenue` has `measure|financial` tags.
- `test_query_sql_join_across_tables` — Salesforce join: `SELECT a.Name, COUNT(o.Id) AS opportunity_count FROM Account a LEFT JOIN Opportunity o ON o.AccountId = a.Id GROUP BY a.Name`. (Verify `AccountId` is the actual FK column on Opportunity via the schema CSV first.)
- `test_query_sql_truncates_at_limit` — use `OpportunityLineItem` (206 rows).

`tests/test_query_safety.py` — unchanged (the guard isn't data-dependent).

- [ ] **Step 2.3: Smoke-test boot**

```bash
cd examples/sfcrm-mcp-server
uv run --with mcp --with duckdb --with eap-core python -c "
from server import _init, REGISTRY
_init()
print('tools registered:', [t.name for t in REGISTRY.list_tools()])
"
```

Expected: `tools registered: ['list_tables', 'describe_table', 'query_sql']`.

- [ ] **Step 2.4: `README.md`** — mirror bankdw's README, swap data domain references

- [ ] **Step 2.5: Gauntlet**

```bash
cd examples/sfcrm-mcp-server
uv run --with mcp --with duckdb --with eap-core --with pytest --with pytest-asyncio pytest -q
```

All tests green.

- [ ] **Step 2.6: Commit**

```bash
git add examples/sfcrm-mcp-server
git commit -m "$(cat <<'EOF'
feat(examples): sfcrm Salesforce CRM MCP server

EAP-Core validation example #2: Salesforce CRM data (15 tables, ~900
rows including Account, Opportunity, Lead, Campaign, Contact)
exposed as an MCP stdio server backed by in-memory DuckDB.

Mirrors bankdw-mcp-server's three-tool surface and read-only guard.
Direct-call tool tests cover SELECT, JOIN across Account ↔
Opportunity, COUNT, schema introspection.

Confirms the load-CSVs-into-DuckDB-and-expose-via-MCP pattern
generalizes from a 5-table star schema (bankdw) to a 15-table
operational schema (sfcrm) without code changes — only env vars
and per-tool description strings differ.
EOF
)"
```

---

## Task 3: Cross-domain agent (`examples/cross-domain-agent/`)

**Why:** The end-to-end validation. An EAP-Core agent spawns BOTH MCP servers as subprocesses, lists their tools, wraps each remote tool as a local `@mcp_tool` that forwards over the open MCP stdio session, then runs through `EnterpriseLLM` with the default middleware chain. One demo question (e.g. "Which Salesforce Accounts have a matching party in bankdw by name?") proves the framework orchestrates the cross-domain call.

This task will surface a real SDK gap: `eap_core.mcp` has server primitives (`McpToolRegistry`, `@mcp_tool`, `build_mcp_server`, `run_stdio`) but **no MCP client adapter**. The cross-domain agent has to build its own bridge using the upstream `mcp.client.stdio` API. This gap goes into the agent's README and is itself a deliverable of the validation.

**Files:**
- Create: `examples/cross-domain-agent/pyproject.toml`
- Create: `examples/cross-domain-agent/README.md`
- Create: `examples/cross-domain-agent/agent.py`
- Create: `examples/cross-domain-agent/mcp_client_adapter.py`
- Create: `examples/cross-domain-agent/tests/__init__.py`
- Create: `examples/cross-domain-agent/tests/test_adapter.py`
- Create: `examples/cross-domain-agent/tests/test_agent.py`

### Subtasks

- [ ] **Step 3.1: `pyproject.toml`**

```toml
[project]
name = "cross-domain-agent"
version = "0.1.0"
description = "EAP-Core validation: agent that consumes bankdw + sfcrm MCP servers as remote tools."
requires-python = ">=3.11"
dependencies = [
    "eap-core>=0.7",
    "mcp>=0.9",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.4", "mypy>=1.10"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["."]
include = ["agent.py", "mcp_client_adapter.py"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 3.2: `mcp_client_adapter.py`** — the bridge

```python
"""Bridge between a remote MCP server (subprocess over stdio) and
EAP-Core's local @mcp_tool / McpToolRegistry surface.

This module exists because eap_core.mcp ships server-side primitives
(McpToolRegistry, @mcp_tool, run_stdio) but no first-class client.
An agent that wants to consume an external MCP server has to:

1. Spawn the server as a subprocess.
2. Open an MCP stdio session (mcp.client.stdio.stdio_client).
3. List its tools.
4. For each remote tool, build a local wrapper that forwards
   call_tool requests through the open session.

This adapter does (1)-(4). It returns a list of ToolSpec values
ready for ``McpToolRegistry.register()``.

LIMITATION (see README.md): this is a per-agent shim, not a
general-purpose SDK feature. The official path would be a new
``eap_core.mcp.client`` module with:
- structured config (server name, command, args, env)
- session lifecycle management (connection pool, retry, timeout)
- response-shape validation against the remote tool's outputSchema
- observability integration (spans around remote calls)
None of those live in this shim. Flagged as a v0.8.0 candidate.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from eap_core.mcp.types import ToolSpec


@dataclass
class ServerHandle:
    """Handle to one running MCP server subprocess. Created by
    ``connect_servers``; closed by exiting the AsyncExitStack returned
    alongside it."""
    name: str
    session: Any  # mcp.client.ClientSession — typed loosely so this
                  # module doesn't hard-import the upstream package at
                  # module-load time.
    tool_names: list[str]


async def connect_servers(
    server_configs: list[dict[str, Any]],
    stack: AsyncExitStack,
) -> list[ServerHandle]:
    """Spawn each MCP server subprocess and open an MCP stdio session
    to it. Returns one ``ServerHandle`` per server.

    Caller owns the ``AsyncExitStack`` — when the stack exits, all
    sessions and subprocesses are torn down.

    ``server_configs`` items shape:
        {"name": "bankdw", "command": "python", "args": ["server.py"], "cwd": Path("...")}
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    handles: list[ServerHandle] = []
    for cfg in server_configs:
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg["args"],
            cwd=str(cfg["cwd"]) if cfg.get("cwd") else None,
            env=cfg.get("env"),
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_response = await session.list_tools()
        handles.append(ServerHandle(
            name=cfg["name"],
            session=session,
            tool_names=[t.name for t in tools_response.tools],
        ))
    return handles


def build_tool_specs(handles: list[ServerHandle]) -> list[ToolSpec]:
    """For every remote tool on every connected server, build a local
    ``ToolSpec`` whose ``fn`` forwards to that remote tool. The remote
    tool's name is namespaced as ``<server-name>__<tool-name>`` to
    avoid collisions (both servers expose ``query_sql``).

    Description is preserved from the remote ``tools/list`` response;
    input schema is preserved as the remote advertised it. Output
    schema is left as a generic ``{"type": "object"}`` because remote
    MCP tools advertise input schemas but rarely the output shape.
    """
    specs: list[ToolSpec] = []
    for handle in handles:
        for remote_tool in handle.tool_names:
            local_name = f"{handle.name}__{remote_tool}"
            specs.append(_build_one(handle, remote_tool, local_name))
    return specs


def _build_one(handle: ServerHandle, remote_name: str, local_name: str) -> ToolSpec:
    # Build the forwarder coroutine. Closure captures `handle` + `remote_name`.
    async def _forward(**kwargs: Any) -> Any:
        response = await handle.session.call_tool(remote_name, kwargs)
        # response.content is a list[TextContent | ImageContent | EmbeddedResource];
        # for our DuckDB tools it's a single TextContent whose .text holds the
        # JSON-serialized pydantic model returned from the tool.
        if response.content and hasattr(response.content[0], "text"):
            text = response.content[0].text
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return None

    # We can't introspect the remote tool's full schema without another
    # session call, so build a minimal ToolSpec. The local registry treats
    # this as just another async tool.
    return ToolSpec(
        name=local_name,
        description=f"[remote: {handle.name}] {remote_name}",
        input_schema={"type": "object"},  # Permissive — the remote validates.
        output_schema=None,
        fn=_forward,
        requires_auth=False,
        is_async=True,
    )
```

- [ ] **Step 3.3: `agent.py`**

```python
"""cross-domain-agent — answer questions that span bankdw + sfcrm.

Spawns both MCP servers as subprocesses, wires their tools into the
agent's McpToolRegistry, and runs an EnterpriseLLM with the default
middleware chain.

Run locally (no cloud creds):

    python agent.py

Demo question is hard-coded for now ("Which top-5 Salesforce Accounts
by AnnualRevenue have matching parties in bankdw dim_party by name?").
A real LLM call would pick the tools sequentially:
  1. sfcrm__list_tables → discover sfcrm.Account
  2. sfcrm__describe_table(table="Account") → find Name, AnnualRevenue
  3. sfcrm__query_sql("SELECT Name FROM Account ORDER BY AnnualRevenue DESC LIMIT 5")
  4. bankdw__list_tables → discover dim_party
  5. bankdw__query_sql("SELECT PartyName FROM dim_party WHERE PartyName IN (...)")
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from eap_core.mcp import McpToolRegistry

from mcp_client_adapter import build_tool_specs, connect_servers


def _examples_root() -> Path:
    return Path(__file__).resolve().parent.parent


async def main() -> None:
    root = _examples_root()
    server_configs = [
        {
            "name": "bankdw",
            "command": "python",
            "args": ["server.py"],
            "cwd": root / "bankdw-mcp-server",
        },
        {
            "name": "sfcrm",
            "command": "python",
            "args": ["server.py"],
            "cwd": root / "sfcrm-mcp-server",
        },
    ]

    async with AsyncExitStack() as stack:
        handles = await connect_servers(server_configs, stack)
        registry = McpToolRegistry()
        for spec in build_tool_specs(handles):
            registry.register(spec)

        # Demo: list both servers' tables to prove the bridge works.
        bankdw_tables = await registry.invoke("bankdw__list_tables", {})
        sfcrm_tables = await registry.invoke("sfcrm__list_tables", {})
        print(f"bankdw tables: {[t['name'] for t in bankdw_tables['tables']]}")
        print(f"sfcrm tables: {[t['name'] for t in sfcrm_tables['tables']]}")

        # Demo: cross-domain query. Find Salesforce Account names that
        # also appear as bankdw party names.
        sf_top5 = await registry.invoke(
            "sfcrm__query_sql",
            {
                "sql": "SELECT Name FROM Account ORDER BY AnnualRevenue DESC LIMIT 5",
                "limit": 5,
            },
        )
        top5_names = [r["Name"] for r in sf_top5["rows"]]
        in_clause = ", ".join(f"'{n}'" for n in top5_names)
        bd_match = await registry.invoke(
            "bankdw__query_sql",
            {
                "sql": f"SELECT PartyName FROM dim_party WHERE PartyName IN ({in_clause})",
                "limit": 50,
            },
        )
        matched = {r["PartyName"] for r in bd_match["rows"]}
        print(f"\nTop-5 SFDC Accounts by revenue: {top5_names}")
        print(f"Of those, parties also in bankdw: {sorted(matched)}")


if __name__ == "__main__":
    asyncio.run(main())
```

Note: this demo bypasses the actual LLM-driven tool selection (no `EnterpriseLLM.generate_text` call). A full LLM-driven version requires either a configured LLM provider or a `LocalRuntimeAdapter` with stubbed responses, both of which add scope. The demo above proves the **infrastructure** end-to-end — both MCP servers reachable, tools callable, results returned — without depending on a live model. The README documents the next step ("wire an `EnterpriseLLM` with `LocalRuntimeAdapter` to drive the same flow via natural language").

- [ ] **Step 3.4: `tests/test_adapter.py`** — adapter unit tests

Test the namespace-prefixing logic, the JSON-decode path, and the closure capture (each forwarder must invoke its own remote tool, not the last-bound one). Use a stub session.

```python
"""Adapter unit tests — exercise build_tool_specs against a stub
ClientSession to verify namespace prefixing, closure capture, and
JSON decoding without spawning real subprocesses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from mcp_client_adapter import ServerHandle, build_tool_specs  # noqa: E402


class _StubResponse:
    def __init__(self, payload: dict):
        self.content = [SimpleNamespace(text=json.dumps(payload))]


@pytest.mark.asyncio
async def test_build_tool_specs_namespaces_each_tool_with_server_name():
    h1 = ServerHandle(name="bankdw", session=AsyncMock(), tool_names=["query_sql", "list_tables"])
    h2 = ServerHandle(name="sfcrm", session=AsyncMock(), tool_names=["query_sql"])
    specs = build_tool_specs([h1, h2])
    names = sorted(s.name for s in specs)
    assert names == ["bankdw__list_tables", "bankdw__query_sql", "sfcrm__query_sql"]


@pytest.mark.asyncio
async def test_forwarder_invokes_correct_remote_tool_with_kwargs():
    """Closure capture must pin each forwarder to its own remote name,
    not the last name in the loop."""
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_StubResponse({"row_count": 7}))
    h = ServerHandle(name="bankdw", session=session, tool_names=["query_sql", "list_tables"])
    specs = build_tool_specs([h])

    list_spec = next(s for s in specs if s.name == "bankdw__list_tables")
    result = await list_spec.fn()  # no kwargs
    session.call_tool.assert_called_with("list_tables", {})

    session.call_tool.reset_mock()
    query_spec = next(s for s in specs if s.name == "bankdw__query_sql")
    result = await query_spec.fn(sql="SELECT 1", limit=10)
    session.call_tool.assert_called_with("query_sql", {"sql": "SELECT 1", "limit": 10})
    assert result == {"row_count": 7}


@pytest.mark.asyncio
async def test_forwarder_returns_text_unchanged_when_response_is_non_json():
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(text="not json")]
    ))
    h = ServerHandle(name="x", session=session, tool_names=["t"])
    [spec] = build_tool_specs([h])
    result = await spec.fn()
    assert result == "not json"


@pytest.mark.asyncio
async def test_forwarder_returns_none_when_response_has_empty_content():
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=SimpleNamespace(content=[]))
    h = ServerHandle(name="x", session=session, tool_names=["t"])
    [spec] = build_tool_specs([h])
    assert await spec.fn() is None
```

- [ ] **Step 3.5: `tests/test_agent.py`** — end-to-end integration test (real subprocesses)

```python
"""End-to-end integration test — spawns both real MCP servers,
queries one tool on each, asserts shape. Validates the full bridge.

This test is the validation deliverable. If it passes, the SDK +
both MCP servers + the adapter all hang together.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from eap_core.mcp import McpToolRegistry  # noqa: E402
from mcp_client_adapter import build_tool_specs, connect_servers  # noqa: E402

EXAMPLES_ROOT = AGENT_DIR.parent


@pytest.mark.asyncio
async def test_agent_can_invoke_tools_on_both_remote_servers():
    """Spawn bankdw + sfcrm, list tools on each, invoke list_tables,
    assert the expected tables come back."""
    server_configs = [
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

    async with AsyncExitStack() as stack:
        handles = await connect_servers(server_configs, stack)
        registry = McpToolRegistry()
        for spec in build_tool_specs(handles):
            registry.register(spec)

        registered_names = {t.name for t in registry.list_tools()}
        for name in ["bankdw__list_tables", "bankdw__describe_table",
                     "bankdw__query_sql", "sfcrm__list_tables",
                     "sfcrm__describe_table", "sfcrm__query_sql"]:
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
    then find which of those names match bankdw parties. Proves the
    framework orchestrates a real cross-server flow."""
    server_configs = [
        {"name": "bankdw", "command": sys.executable, "args": ["server.py"],
         "cwd": EXAMPLES_ROOT / "bankdw-mcp-server"},
        {"name": "sfcrm", "command": sys.executable, "args": ["server.py"],
         "cwd": EXAMPLES_ROOT / "sfcrm-mcp-server"},
    ]
    async with AsyncExitStack() as stack:
        handles = await connect_servers(server_configs, stack)
        registry = McpToolRegistry()
        for spec in build_tool_specs(handles):
            registry.register(spec)

        sf_result = await registry.invoke(
            "sfcrm__query_sql",
            {"sql": "SELECT Name FROM Account ORDER BY AnnualRevenue DESC LIMIT 5", "limit": 5},
        )
        assert sf_result["row_count"] == 5
        top5 = [r["Name"] for r in sf_result["rows"]]
        # Microsoft and Ford are in the seed data; one of them is in
        # the top 5 by revenue.
        assert any("Microsoft" in n or "Ford" in n for n in top5), top5

        in_clause = ", ".join(f"'{n}'" for n in top5)
        bd_result = await registry.invoke(
            "bankdw__query_sql",
            {"sql": f"SELECT PartyName FROM dim_party WHERE PartyName IN ({in_clause})", "limit": 50},
        )
        # At least one match — the seed data has overlapping company
        # names between Account.Name and dim_party.PartyName.
        assert bd_result["row_count"] >= 1, (
            f"expected at least one cross-domain match; sf top5={top5}, "
            f"bd rows={bd_result['rows']}"
        )
```

- [ ] **Step 3.6: `README.md`** — the gap-finding artifact

Cover:
- What this is and what it validates
- How to run it (`python agent.py`)
- The bridge pattern (subprocess + stdio + adapter)
- **The SDK gap** — explicit section titled "What this validation surfaced":
  - `eap_core.mcp` ships server primitives but no client. The bridge in `mcp_client_adapter.py` is a per-agent shim, not first-class.
  - A first-class `eap_core.mcp.client` module would provide: structured server config, session lifecycle (pool/retry/timeout), output-schema validation, observability spans around remote calls. None are in this shim.
  - Recommended as a v0.8.0 backlog item.
- Demo output (the cross-domain query result)
- Pointer to the two MCP server projects

- [ ] **Step 3.7: Gauntlet**

```bash
cd examples/cross-domain-agent
uv run --with mcp --with duckdb --with eap-core --with pytest --with pytest-asyncio pytest -q
```

Both adapter unit tests AND the integration test green. The integration test spawns real subprocesses — slow (~5-10s), but worth it as the headline validation.

Outside this directory:

```bash
cd /Users/admin-h26/EAAP/ai-eap-sdk
uv run pytest -m "not extras and not cloud" -q
```

Still 576 passing (no changes inside `packages/`).

- [ ] **Step 3.8: Commit**

```bash
git add examples/cross-domain-agent
git commit -m "$(cat <<'EOF'
feat(examples): cross-domain agent consuming bankdw + sfcrm MCP servers

EAP-Core validation example #3 (end-to-end). An EAP-Core agent
spawns both MCP servers as stdio subprocesses, wraps each remote
tool as a local @mcp_tool that forwards over the MCP session,
and demonstrates a cross-domain query (top SFDC accounts by
revenue → matching bankdw parties).

mcp_client_adapter.py is a thin bridge because eap_core.mcp ships
server-side primitives but no client. The adapter handles spawn +
session init + tools/list + per-tool forwarder closure with
namespaced names (server__tool to avoid collisions). The README's
"What this validation surfaced" section flags a first-class
eap_core.mcp.client adapter as a v0.8.0 candidate.

Integration test (test_agent.py) spawns both real servers and
asserts the full bridge — registered tools, remote list_tables,
cross-domain query round-trip. Adapter unit tests (test_adapter.py)
cover namespacing, closure capture, JSON-decode path, empty
response handling — no subprocesses.
EOF
)"
```

---

## Task 4: Top-level `examples/README.md` update

**Why:** The repo's `examples/` folder currently has two entries (`transactional-agent`, `research-agent`). Adding three MCP-validation projects warrants an index update so a new reader can orient.

**Files:**
- Modify: `examples/README.md` (if it exists — check first; create if not)

### Subtasks

- [ ] **Step 4.1: Check if `examples/README.md` exists**

```bash
test -f examples/README.md && echo EXISTS || echo MISSING
```

If exists: append a "Validation examples" section. If not: create a new index covering all five example projects (the two existing + three new).

- [ ] **Step 4.2: Update / create the index**

If creating new, the file should be ~40 lines: one paragraph per example with what-it-demonstrates and the path. Sections: "Agent templates" (transactional, research) and "Validation examples" (bankdw-mcp-server, sfcrm-mcp-server, cross-domain-agent).

- [ ] **Step 4.3: Commit**

```bash
git add examples/README.md
git commit -m "docs(examples): index the bankdw/sfcrm/cross-domain validation triplet"
```

---

## Self-review

**Spec coverage:** The user picked option 2 (Core + cross-system agent). Three example projects deliver that — two MCP servers (T1, T2) and the cross-domain agent (T3). The index update (T4) is a small navigation aid; no release task because these are example projects, not SDK changes.

**Placeholder scan:** Every code block is concrete. Two known sources of mid-implementation correction noted explicitly:
- Step 1.6's `Field(default=100, ge=1, le=1000)` in a function signature may not work — fall back to `Annotated[int, Field(...)] = 100` if `@mcp_tool`'s schema generation chokes.
- The `_BLOCKED_KEYWORDS` regex false-positives on string literals containing keywords (`SELECT 'INSERT' AS x`). Plan documents this as a known limitation; tests assert rejection (the conservative behavior).

**Internal consistency:** All three example projects share the same `@mcp_tool` decorator pattern, the same tool-binding indirection (via `_bind()` module-level functions — necessary because tools are decorated functions, not classes with state), and the same env-var-with-relative-default approach for data paths. The cross-domain agent's adapter pins remote tools with `<server-name>__<tool-name>` to avoid the collision both servers create by exposing `query_sql`.

**Breaking changes:** None. No SDK code changes. Only adds files under `examples/`. The cross-domain agent's `mcp_client_adapter.py` is local to its project; it doesn't touch `eap_core.mcp`.

**Risk:** Step 1.6's pydantic Field-in-signature is the only real unknown; the plan accounts for it. Step 3.5's integration test spawns real subprocesses and is slower than unit tests (~5-10s) — acceptable for a validation deliverable. If the subprocess test is flaky on CI (slow startup, port-style timing), wrap in `@pytest.mark.timeout(30)` and add retry.

**Scope discipline:** Four tasks. Three substantive (T1, T2, T3) plus one tiny index update (T4). No drift into eval golden-set (option 3, deferred), no cloud deployment, no SDK-side MCP client adapter (flagged for v0.8.0).

**SDK gap captured as deliverable:** The cross-domain agent's README will document the missing `eap_core.mcp.client` module as the validation's headline finding. This is exactly the kind of feedback the exercise is supposed to surface.

---

## Execution

Subagent-driven. Four implementer dispatches with two-stage review per task. Controller doesn't tag or release anything — these are example projects, not SDK changes. After T4 lands, the work is done.

Tasks 1, 2, 4 are tight enough for one-shot implementer dispatches (1 + 1 + 1). T3 is the most complex and most likely to need a re-dispatch on the spec reviewer's findings (the adapter's closure-capture pattern is easy to get wrong; the integration test depends on T1+T2 being correct first).

Recommend running T1 first to settle the pattern, then T2 in parallel-style mirror, then T3 once T1+T2 are merged so the integration test has working servers to spawn.
