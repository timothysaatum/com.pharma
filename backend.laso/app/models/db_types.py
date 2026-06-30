"""
Database type compatibility layer for multiple database backends.
Handles UUID, JSONB, ARRAY, and INET types across SQLite and PostgreSQL.
"""
from sqlalchemy import TypeDecorator, String, Text
import uuid
import json
import re


class _UUIDEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)

def _dumps(value: object) -> str:
    return json.dumps(value, cls=_UUIDEncoder)


_PG_ARRAY_RE = re.compile(r'^\{.*\}$')

def _parse_pg_array(value: str) -> list:
    """Parse a PostgreSQL array literal (e.g. '{a,b,c}') into a Python list.
    Handles quoted elements, escaped quotes, and NULL literals.
    """
    inner = value[1:-1]
    if not inner.strip():
        return []
    elements = []
    i = 0
    while i < len(inner):
        if inner[i] in (' ', ','):
            i += 1
            continue
        if inner[i] == '"':
            i += 1
            buf = []
            while i < len(inner):
                if inner[i] == '\\':
                    i += 1
                    if i < len(inner):
                        buf.append(inner[i])
                    i += 1
                elif inner[i] == '"':
                    i += 1
                    break
                else:
                    buf.append(inner[i])
                    i += 1
            elements.append(''.join(buf))
        else:
            buf = []
            while i < len(inner) and inner[i] not in (',', ' '):
                buf.append(inner[i])
                i += 1
            token = ''.join(buf)
            if token.upper() == 'NULL':
                elements.append(None)
            else:
                elements.append(token)
    return elements


class UUID(TypeDecorator):
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


class JSONB(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, str):
            return value
        return _dumps(value)          # ← was json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                if _PG_ARRAY_RE.match(value):
                    return _parse_pg_array(value)
                raise
        return value


class ARRAY(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, item_type=None, **kwargs):
        super().__init__(**kwargs)
        self.item_type = item_type

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, str):
            return value
        return _dumps(value)          # ← was json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                if _PG_ARRAY_RE.match(value):
                    return _parse_pg_array(value)
                raise
        return value


class INET(TypeDecorator):
    impl = String(45)
    cache_ok = True