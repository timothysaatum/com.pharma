"""The atlasdb fence must actually refuse atlasdb.

Guards the guard: `tests/conftest.py` drops the public schema on every
PostgreSQL target, so a mis-pointed TEST_DATABASE_URL destroys live data. This
test proves the refusal fires for the real database name and stays out of the way
for a disposable one.

Refuses to import the real conftest with a live URL, so it exercises the check
function by re-executing the source rather than trusting a hand-copied version.
"""
import os
import re
import pytest

CONFTEST = os.path.join(os.path.dirname(__file__), "..", "conftest.py")


def _load_check():
    """Extract and exec just the guard function from conftest."""
    src = open(CONFTEST, encoding="utf-8").read()
    m = re.search(
        r"^def _assert_not_production_database\(url: str\) -> None:.*?(?=^_assert_not_production_database\(DATABASE_URL_TEST\))",
        src,
        re.M | re.S,
    )
    assert m, "could not find the guard function in conftest.py"
    ns: dict = {}
    exec(compile(m.group(0), CONFTEST, "exec"), ns)
    return ns["_assert_not_production_database"]


def test_refuses_atlasdb():
    check = _load_check()
    with pytest.raises(RuntimeError, match="REFUSING TO RUN"):
        check("postgresql+asyncpg://user:pw@db.example.org:5432/atlasdb")


def test_refuses_query_string_form_of_atlasdb():
    check = _load_check()
    with pytest.raises(RuntimeError, match="REFUSING TO RUN"):
        check("postgresql+asyncpg://user:pw@localhost:5432/atlasdb?sslmode=require")


def test_allows_a_disposable_cluster():
    check = _load_check()
    check("postgresql+asyncpg://postgres@/rx_impl?host=/tmp/pgsock&port=55999")


def test_allows_in_memory_sqlite():
    check = _load_check()
    check("sqlite+aiosqlite:///:memory:")


def test_guard_runs_before_any_fixture():
    """The check must be at import time, not inside a fixture.

    If it only ran when `db` was first requested, a collection-time error or an
    early fixture could touch the database first.
    """
    src = open(CONFTEST, encoding="utf-8").read()
    call = "_assert_not_production_database(DATABASE_URL_TEST)"
    assert call in src
    fixture_at = src.index("async def db(")
    assert src.index(call) < fixture_at, "guard must be called before the db fixture"
