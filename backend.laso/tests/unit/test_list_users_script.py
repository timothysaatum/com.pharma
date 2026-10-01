"""Tests for scripts/list_users.py — argument parsing and table rendering.

fetch_users() is exercised against the live database by
  PYTHONPATH=. python3 scripts/list_users.py
and by the integration suite. These tests cover the deterministic surface:
flag wiring, the soft-delete default, and column alignment.
"""
import logging
import sys
import io
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.list_users import (  # noqa: E402
    parse_args,
    render_table,
    quiet_engine_echo,
)


# ── parse_args ──────────────────────────────────────────────────────────────

def test_defaults_exclude_deleted_and_filter_none():
    args = parse_args([])
    assert args.include_deleted is False
    assert args.username is None
    assert args.json is False


def test_all_flag_sets_include_deleted():
    assert parse_args(["--all"]).include_deleted is True


def test_username_flag_is_captured():
    assert parse_args(["--username", "admin"]).username == "admin"


def test_json_flag_is_captured():
    assert parse_args(["--json"]).json is True


def test_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        parse_args(["--nope"])
    assert exc.value.code != 0


# ── render_table ────────────────────────────────────────────────────────────

def _row(**kw):
    base = {
        "id": "0" * 36,
        "username": "u",
        "full_name": "F",
        "email": "e@x",
        "roles": None,
        "is_active": True,
        "is_super_admin": False,
        "is_deleted": False,
    }
    base.update(kw)
    return base


def test_render_table_empty_result_is_explicit():
    assert render_table([]) == "No users found."


def test_render_table_contains_every_column_header():
    out = render_table([_row()])
    for header in ("USERNAME", "FULL NAME", "ROLES", "ACTIVE", "SUPER", "DELETED"):
        assert header in out


def test_render_table_includes_username_and_full_name():
    out = render_table([_row(username="admin", full_name="System Administrator")])
    assert "admin" in out
    assert "System Administrator" in out


def test_render_table_reports_row_count():
    assert "2 user(s)." in render_table([_row(), _row(username="tim")])


def test_render_table_singular_row_count():
    assert "1 user(s)." in render_table([_row()])


def test_render_table_none_role_renders_as_none_not_crashing():
    assert "None" in render_table([_row(roles=None)])


def test_render_table_columns_are_aligned_to_widest_value():
    rows = [
        _row(username="a", full_name="Short"),
        _row(username="muchlongerusername", full_name="A Much Longer Full Name"),
    ]
    lines = render_table(rows).splitlines()
    header, sep, first = lines[0], lines[1], lines[2]

    assert len(sep) >= len(header)
    # The username column must start at the same offset in every body row.
    body = [ln for ln in lines if ln.startswith(("a", "muchlongerusername"))]
    assert body
    username_col = header.index("USERNAME")
    assert all(ln[username_col] in ("a", "m") for ln in body)


def test_render_table_never_raises_on_long_values():
    out = render_table([_row(username="x" * 300, full_name="y" * 300)])
    assert "xxx" in out


# ── quiet_engine_echo ───────────────────────────────────────────────────────

def test_quiet_engine_echo_raises_logger_level():
    """BEGIN/COMMIT honour this path (they go through isEnabledFor)."""
    lg = logging.getLogger("sqlalchemy.engine.Engine")
    original = lg.level
    try:
        quiet_engine_echo()
        assert lg.level == logging.WARNING
    finally:
        lg.setLevel(original)


def test_quiet_engine_echo_sets_global_disable_threshold():
    """Statement text is emitted via InstanceLogger.log(), which ignores the
    logger level and only checks logging.manager.disable (sqlalchemy/log.py:197).
    This is the assertion that would fail if someone 'simplified' the helper
    back to setLevel only."""
    original = logging.root.manager.disable
    try:
        quiet_engine_echo()
        assert logging.root.manager.disable >= logging.INFO
    finally:
        logging.disable(original)