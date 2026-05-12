"""list_tables - return a summary of every table in the sfcrm dataset.

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
        "List every table in the sfcrm Salesforce CRM data with a "
        "one-line description and row count. Call this first to discover "
        "what's available, then call describe_table for column-level detail."
    )
)
def list_tables() -> ListTablesResult:
    from duck import row_count

    assert _con is not None and _schema is not None, (
        "tools not bound - server.py:_bind() must run first"
    )
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
