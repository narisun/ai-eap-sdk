"""query_sql - run a read-only SQL query against the bankdw warehouse.

Returns columns + rows + a truncated flag. Hard caps:
- Row limit defaulted to 100, capped at 1000 to keep MCP responses
  reasonable. Callers asking for more should paginate via OFFSET in SQL.
- Only SELECT / WITH / DESCRIBE / SHOW / EXPLAIN / PRAGMA statements
  pass the read-only gate. Anything else returns an error result.

The read-only check is a simple first-keyword allow-list. DuckDB
in-memory connections can't be made strictly read-only after data
load; this check is the actual safety boundary. It's deliberately
conservative - better to reject a valid query than execute a write.

Known false-positive: the defense-in-depth `_BLOCKED_KEYWORDS` regex
uses `\\b` word boundaries that match string-literal content as well
as actual SQL tokens. A `SELECT 'INSERT' AS x` query is rejected.
Documented as acceptable for a validation example; production
deployments should swap in sqlparse / sqlglot for token-aware parsing.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, Field

from eap_core.mcp import mcp_tool

_con = None

# Allow-list: case-insensitive first keyword after stripping comments
# and whitespace. PRAGMA is included so callers can run
# `PRAGMA table_info(...)` style introspection.
_READ_ONLY_FIRST_TOKENS = {
    "select",
    "with",
    "describe",
    "show",
    "explain",
    "pragma",
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
def query_sql(
    sql: str,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> QueryResult | QueryError:
    assert _con is not None, "query_sql not bound - server.py:_bind() must run first"

    if not _is_read_only(sql):
        return QueryError(
            error="only SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/PRAGMA statements are accepted",
            sql=sql,
        )

    # Wrap user SQL with an outer LIMIT so we don't materialize a
    # 100M-row result. limit+1 lets us detect truncation cheaply.
    # The user-supplied ``sql`` is interpolated here, but it has
    # already passed the ``_is_read_only`` allow-list guard above —
    # any value that would make this a real injection vector was
    # rejected before reaching this line. DuckDB's ``execute`` also
    # only runs the first statement, so trailing-semicolon attacks
    # are blocked at the engine layer. S608 is a false positive here.
    wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {limit + 1}"  # noqa: S608
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
