"""The standalone-script fence (scripts/refuse_real_database.py).

Neither existing fence covers a script run by hand: conftest (e8dd90c) only runs
on pytest import, and the Playwright helper (213b6fc) only guards
`BackendDatabase`. A script reading `settings.DATABASE_URL` from .env writes to
atlasdb, which is how sentinel-UUID fixture rows reached the live database.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from refuse_real_database import (  # noqa: E402
    DOWNGRADE_FLAG,
    check_disposable_database,
    require_disposable_database,
)

ATLAS = "postgresql://cassie1:secret@localhost:5432/atlasdb"
DISPOSABLE = "postgresql+asyncpg://postgres@/custfix?host=/tmp/x&port=55998"
DISPOSABLE_TCP = "postgresql://postgres@localhost:5432/my_throwaway"


def test_refuses_atlasdb(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    ok, reason = check_disposable_database(ATLAS)
    assert ok is False
    assert "atlasdb" in reason


def test_refuses_atlasdb_even_when_test_url_points_elsewhere(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", DISPOSABLE)
    ok, reason = check_disposable_database(ATLAS)
    assert ok is False


def test_refuses_when_test_url_names_atlasdb(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", ATLAS)
    ok, reason = check_disposable_database(ATLAS)
    assert ok is False
    assert "names it" in reason


@pytest.mark.parametrize("name", ["postgres", "template0", "template1"])
def test_refuses_other_protected_names(name, monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    ok, _ = check_disposable_database(f"postgresql://u:p@localhost:5432/{name}")
    assert ok is False


def test_case_insensitive(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    ok, _ = check_disposable_database("postgresql://u:p@127.0.0.1:5433/ATLASDB")
    assert ok is False


def test_socket_url_is_allowed(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    ok, reason = check_disposable_database(DISPOSABLE)
    assert ok is True, reason


def test_ordinary_throwaway_is_allowed(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    ok, _ = check_disposable_database(DISPOSABLE_TCP)
    assert ok is True


def test_empty_url_is_refused():
    ok, _ = check_disposable_database("")
    assert ok is False


def test_unparseable_url_is_refused():
    ok, _ = check_disposable_database("mysql://nope")
    assert ok is False


def test_explicit_override_allows_a_protected_name(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    ok, reason = check_disposable_database(ATLAS, override=True)
    assert ok is True
    assert "WARNING" in reason


def test_require_raises_systemexit(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        require_disposable_database(ATLAS)
    assert "Nothing was written" in str(exc.value)


def test_require_passes_for_a_disposable_url(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    require_disposable_database(DISPOSABLE)  # must not raise


def test_override_flag_is_read_from_argv(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["seed_bulk_e2e.py", DOWNGRADE_FLAG])
    require_disposable_database(ATLAS)  # must not raise
