"""query_sql read-only guard tests - must reject writes/DDL.

Cover the SQL allow-list: any non-SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/PRAGMA
first keyword is rejected, AND any mid-statement write keyword (INSERT,
UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, COPY, EXPORT, TRUNCATE) is
rejected as defense-in-depth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from duck import open_in_memory
from tools.query_sql import _bind as _bind_query
from tools.query_sql import query_sql


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
    result = query_sql(sql=sql)
    assert hasattr(result, "error")
    assert "read-only" in result.error.lower() or "SELECT" in result.error


def test_select_with_string_literal_containing_keyword_is_rejected_as_false_positive(bound):
    """Known false-positive: a string literal containing a blocked
    keyword trips the regex. Documented in query_sql.py; acceptable
    for a validation example. Production deployments should swap in
    sqlglot or sqlparse for token-aware parsing."""
    result = query_sql(sql="SELECT 'INSERT' AS my_string")
    assert hasattr(result, "error")


def test_empty_or_whitespace_only_sql_is_rejected(bound):
    for sql in ["", "   ", "  -- only a comment\n", "/* block */ \n  "]:
        result = query_sql(sql=sql)
        assert hasattr(result, "error")


def test_select_with_block_comment_is_allowed(bound):
    result = query_sql(sql="/* explain */ SELECT 1 AS n")
    assert not hasattr(result, "error")
    assert result.columns == ["n"]
