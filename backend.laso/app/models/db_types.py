"""
Database type compatibility layer for multiple database backends.
Handles UUID, JSONB, ARRAY, and INET types across SQLite and PostgreSQL.
"""
from sqlalchemy import TypeDecorator, String, Text
import uuid
import json


class _UUIDEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)

def _dumps(value: object) -> str:
    return json.dumps(value, cls=_UUIDEncoder)


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
            return json.loads(value)
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
            return json.loads(value)
        return value


class INET(TypeDecorator):
    impl = String(45)
    cache_ok = True