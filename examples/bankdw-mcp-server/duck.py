"""In-memory DuckDB factory.

Loads every <table>.csv from a data directory into a fresh in-memory
DuckDB connection using DuckDB's auto-typing CSV reader. The connection
is the unit of state - server.py owns one; tests own their own. There
is no shared global connection (per developer-guide section 6.7 on
per-process state).

Read-only enforcement is layered on at the tool level (see
tools/query_sql.py's _is_read_only check). DuckDB in-memory connections
cannot be made strictly read-only after data load - the SQL allow-list
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
