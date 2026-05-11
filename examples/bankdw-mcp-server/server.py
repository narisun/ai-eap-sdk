"""bankdw-mcp-server - standalone MCP-stdio server.

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
