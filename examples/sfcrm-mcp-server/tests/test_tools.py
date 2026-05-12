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

from duck import open_in_memory
from schema import parse_schema
from tools.describe_table import _bind as _bind_describe
from tools.describe_table import describe_table
from tools.list_tables import _bind as _bind_list
from tools.list_tables import list_tables
from tools.query_sql import _bind as _bind_query
from tools.query_sql import query_sql


@pytest.fixture(scope="module")
def loaded():
    """Load the real sfcrm data once per test module."""
    data_dir = SERVER_DIR.parent.parent / "samples" / "sfcrm"
    schema_csv = SERVER_DIR.parent.parent / "samples" / "sfcrm_schema.csv"
    con = open_in_memory(data_dir)
    schema = parse_schema(schema_csv)
    _bind_list(con, schema)
    _bind_describe(schema)
    _bind_query(con)
    return con, schema


def test_list_tables_returns_all_fifteen_sfcrm_tables(loaded):
    # The @mcp_tool decorator returns the original function unmodified
    # (with .spec attached as metadata), so calling it directly is the
    # underlying-function call. Equivalent to list_tables.spec.fn().
    result = list_tables()
    names = {t.name for t in result.tables}
    assert names == {
        "Account",
        "Campaign",
        "CampaignMember",
        "Case",
        "Contact",
        "Contract",
        "Event",
        "Lead",
        "Opportunity",
        "OpportunityContactRole",
        "OpportunityLineItem",
        "Pricebook2",
        "PricebookEntry",
        "Product2",
        "Task",
    }
    # Row counts are real (Account has 45 rows: 46-line CSV minus header).
    account = next(t for t in result.tables if t.name == "Account")
    assert account.row_count == 45
    assert "account" in account.description.lower()


def test_describe_table_returns_typed_columns(loaded):
    result = describe_table(table="Account")
    assert result.name == "Account"
    id_col = next(c for c in result.columns if c.name == "Id")
    assert id_col.data_type == "varchar"
    assert id_col.is_primary_key is True
    assert "identifier" in id_col.semantic_tags
    name_col = next(c for c in result.columns if c.name == "Name")
    assert name_col.data_type == "varchar"
    revenue_col = next(c for c in result.columns if c.name == "AnnualRevenue")
    assert "measure" in revenue_col.semantic_tags
    assert "financial" in revenue_col.semantic_tags


def test_describe_table_returns_error_with_available_tables_on_unknown(loaded):
    result = describe_table(table="not_a_table")
    assert hasattr(result, "error")
    assert "not_a_table" in result.error
    assert "Account" in result.available_tables


def test_query_sql_simple_select(loaded):
    result = query_sql(sql="SELECT Id, Name FROM Account LIMIT 5")
    assert result.row_count <= 5
    assert result.columns == ["Id", "Name"]
    assert all("Id" in row and "Name" in row for row in result.rows)


def test_query_sql_join_across_tables(loaded):
    # Cross-table query: Salesforce Account <-> Opportunity via AccountId FK.
    sql = """
        SELECT a.Name, COUNT(o.Id) AS opportunity_count
        FROM Account a
        LEFT JOIN Opportunity o ON o.AccountId = a.Id
        GROUP BY a.Name
        ORDER BY opportunity_count DESC
    """
    result = query_sql(sql=sql)
    assert result.row_count > 0
    assert "Name" in result.columns
    assert "opportunity_count" in result.columns


def test_query_sql_truncates_at_limit(loaded):
    # OpportunityLineItem has 206 rows; ask for 10.
    result = query_sql(sql="SELECT * FROM OpportunityLineItem", limit=10)
    assert result.row_count == 10
    assert result.truncated is True


def test_query_sql_returns_error_on_invalid_sql(loaded):
    result = query_sql(sql="SELECT * FROM nonexistent_table")
    assert hasattr(result, "error")
    assert "nonexistent_table" in result.error.lower() or "not found" in result.error.lower()
