"""Parse sfcrm_schema.csv into typed per-table metadata.

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
    """Return a dict mapping table_name -> TableSchema for every table in the
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
