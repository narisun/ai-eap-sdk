"""Tool tests - call each @mcp_tool function directly via .fn, no stdio.

The MCP stdio path is exercised once at the cross-domain-agent layer
(T3) - there, the agent spawns this server as a subprocess and lists
its tools. Here we test tool logic in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Server lives one dir up; add to sys.path so we can import its modules.
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from duck import open_in_memory  # noqa: E402
from schema import parse_schema  # noqa: E402
from tools.describe_table import _bind as _bind_describe  # noqa: E402
from tools.describe_table import describe_table  # noqa: E402
from tools.list_tables import _bind as _bind_list  # noqa: E402
from tools.list_tables import list_tables  # noqa: E402
from tools.query_sql import _bind as _bind_query  # noqa: E402
from tools.query_sql import query_sql  # noqa: E402


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
    # The @mcp_tool decorator returns the original function unmodified
    # (with .spec attached as metadata), so calling it directly is the
    # underlying-function call. Equivalent to list_tables.spec.fn().
    result = list_tables()
    names = {t.name for t in result.tables}
    assert names == {
        "bridge_party_account", "dim_bank", "dim_party",
        "dim_product", "fact_payments",
    }
    # Row counts are real (fact_payments is ~1000 rows).
    fact = next(t for t in result.tables if t.name == "fact_payments")
    assert fact.row_count == 1000
    assert "fact table" in fact.description.lower()


def test_describe_table_returns_typed_columns(loaded):
    result = describe_table(table="dim_party")
    assert result.name == "dim_party"
    party_id_col = next(c for c in result.columns if c.name == "PartyID")
    assert party_id_col.data_type == "varchar"
    assert "identifier" in party_id_col.semantic_tags
    assert not party_id_col.is_primary_key  # PartyKey is the PK
    party_key_col = next(c for c in result.columns if c.name == "PartyKey")
    assert party_key_col.is_primary_key is True


def test_describe_table_returns_error_with_available_tables_on_unknown(loaded):
    result = describe_table(table="not_a_table")
    assert hasattr(result, "error")
    assert "not_a_table" in result.error
    assert "dim_party" in result.available_tables


def test_query_sql_simple_select(loaded):
    result = query_sql(sql="SELECT BankID, BankName FROM dim_bank LIMIT 5")
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
    result = query_sql(sql=sql)
    assert result.row_count > 0
    assert "BankName" in result.columns
    assert "n_accounts" in result.columns


def test_query_sql_truncates_at_limit(loaded):
    # fact_payments has 1000 rows; ask for 10.
    result = query_sql(sql="SELECT * FROM fact_payments", limit=10)
    assert result.row_count == 10
    assert result.truncated is True


def test_query_sql_returns_error_on_invalid_sql(loaded):
    result = query_sql(sql="SELECT * FROM nonexistent_table")
    assert hasattr(result, "error")
    assert "nonexistent_table" in result.error.lower() or "not found" in result.error.lower()
