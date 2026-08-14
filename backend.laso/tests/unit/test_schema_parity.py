"""
test_schema_parity.py

Asserts that the ORM model column set matches what Base.metadata.create_all
would generate for each table. The test builds the schema in an in-memory
SQLite database, reflects it back with SQLAlchemy's Inspector, and diffs the
two sets. Any column that exists in the ORM but not in the DB (or vice versa)
fails fast.

This catches the class of bug where a migration adds a column (or drops one)
but the ORM model is not updated to match — the kind of drift that lets
autogenerate emit a silent DROP COLUMN the next time someone runs
``alembic revision --autogenerate``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

# Importing all models registers them on Base.metadata.
import app.models  # noqa: F401  — side-effect import
from app.db.base import Base

# Tables that are intentionally excluded from parity checks (e.g. views,
# legacy shadow tables, or tables managed outside alembic).
_EXCLUDED_TABLES: set[str] = set()


def _orm_columns(table_name: str) -> set[str]:
    """Column names as declared on the ORM Table object."""
    return {col.name for col in Base.metadata.tables[table_name].columns}


def _db_columns(engine, table_name: str) -> set[str]:
    """Column names as reflected from the live database."""
    insp = inspect(engine)
    return {col["name"] for col in insp.get_columns(table_name)}


@pytest.fixture(scope="module")
def parity_engine():
    """In-memory SQLite engine with the full ORM schema created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_orm_and_db_column_sets_match(parity_engine):
    """
    Every table created by Base.metadata must have the same column names
    in the ORM definition as in the reflected database schema.
    """
    mismatches: list[str] = []

    for table_name in Base.metadata.tables:
        if table_name in _EXCLUDED_TABLES:
            continue

        orm_cols = _orm_columns(table_name)
        db_cols = _db_columns(parity_engine, table_name)

        only_in_orm = orm_cols - db_cols
        only_in_db = db_cols - orm_cols

        if only_in_orm or only_in_db:
            mismatches.append(
                f"Table '{table_name}':\n"
                f"  Only in ORM model : {sorted(only_in_orm)}\n"
                f"  Only in database  : {sorted(only_in_db)}"
            )

    assert not mismatches, (
        "Schema parity failures detected — ORM models and DB schema are out of sync.\n\n"
        + "\n".join(mismatches)
    )
