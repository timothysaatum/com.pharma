"""Tests for app/models/db_types.py — PostgreSQL array literal fallback."""
import json
import uuid
import pytest
from app.models.db_types import _parse_pg_array, ARRAY, JSONB


# ── _parse_pg_array unit tests ──────────────────────────────────────────────

def test_parse_pg_array_simple_unquoted():
    assert _parse_pg_array('{a,b,c}') == ['a', 'b', 'c']


def test_parse_pg_array_quoted():
    assert _parse_pg_array('{"a b","c,d",e}') == ['a b', 'c,d', 'e']


def test_parse_pg_array_uuids():
    u1 = '550e8400-e29b-41d4-a716-446655440000'
    u2 = '6ba7b810-9dad-11d1-80b4-00c04fd430c8'
    assert _parse_pg_array(f'{{{u1},{u2}}}') == [u1, u2]


def test_parse_pg_array_null_literal():
    assert _parse_pg_array('{a,NULL,c}') == ['a', None, 'c']


def test_parse_pg_array_empty():
    assert _parse_pg_array('{}') == []


def test_parse_pg_array_single():
    assert _parse_pg_array('{hello}') == ['hello']


def test_parse_pg_array_escaped_quote():
    assert _parse_pg_array('{"say \\"hi\\"",more}') == ['say "hi"', 'more']


def test_parse_pg_array_with_spaces():
    assert _parse_pg_array('{ a , b , c }') == ['a', 'b', 'c']


# ── ARRAY TypeDecorator tests ───────────────────────────────────────────────

def test_array_json_path():
    """Normal JSON array string should still work."""
    td = ARRAY()
    result = td.process_result_value('["a","b","c"]', None)
    assert result == ['a', 'b', 'c']


def test_array_pg_literal_path():
    """PostgreSQL array literal should be parsed as a fallback."""
    td = ARRAY()
    result = td.process_result_value('{a,b,c}', None)
    assert result == ['a', 'b', 'c']


def test_array_pg_uuid_literals():
    td = ARRAY()
    u1 = '550e8400-e29b-41d4-a716-446655440000'
    u2 = '6ba7b810-9dad-11d1-80b4-00c04fd430c8'
    result = td.process_result_value(f'{{{u1},{u2}}}', None)
    assert result == [u1, u2]


def test_array_already_list():
    """If value is already a Python list, return as-is."""
    td = ARRAY()
    result = td.process_result_value(['x', 'y'], None)
    assert result == ['x', 'y']


def test_array_none():
    td = ARRAY()
    assert td.process_result_value(None, None) is None


def test_array_invalid_raises():
    td = ARRAY()
    with pytest.raises(json.JSONDecodeError):
        td.process_result_value('not json and not pg array', None)


# ── JSONB TypeDecorator tests ───────────────────────────────────────────────

def test_jsonb_normal_json():
    td = JSONB()
    result = td.process_result_value('{"key": "val"}', None)
    assert result == {"key": "val"}


def test_jsonb_pg_array_fallback():
    """JSONB also benefits from the same fallback."""
    td = JSONB()
    result = td.process_result_value('{a,b,c}', None)
    assert result == ['a', 'b', 'c']


def test_jsonb_already_dict():
    td = JSONB()
    result = td.process_result_value({"existing": "dict"}, None)
    assert result == {"existing": "dict"}


def test_jsonb_none():
    td = JSONB()
    assert td.process_result_value(None, None) is None


def test_jsonb_invalid_raises():
    td = JSONB()
    with pytest.raises(json.JSONDecodeError):
        td.process_result_value('garbage', None)
