"""describe_table - return full schema for one table.

Emits every column with type / size / nullability / PK/FK flags /
semantic_tags / free-text description / example value. This is the
LLM's brief for writing a correct SELECT.
"""

from __future__ import annotations

from pydantic import BaseModel
from schema import TableSchema

from eap_core.mcp import mcp_tool

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
    assert _schema is not None, "describe_table not bound - server.py:_bind() must run first"
    if table not in _schema:
        return DescribeTableError(
            error=f"unknown table: {table!r}",
            available_tables=sorted(_schema.keys()),
        )
    return _schema[table]
