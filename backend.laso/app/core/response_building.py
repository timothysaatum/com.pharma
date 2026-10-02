"""
app/core/response_building.py
=============================
Helpers for turning ORM rows into API response models.

A row that cannot be serialized into its response schema is a server-side data
problem, not a bad request. These helpers make that explicit: they log the
offending row's primary key and the exact failing field paths, then re-raise so
the app-level handler in app/core/exception_handlers.py reports 500.

The logging lives here rather than only in the handler because the handler sees
a ValidationError with no idea which row produced it, and the row id is the one
thing that makes the failure actionable.
"""
import logging
from typing import Any, Iterable, Optional, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _row_id(obj: Any) -> Optional[str]:
    """Best-effort identity of an ORM row, for the log line."""
    for attr in ("id", "event_id", "aggregate_id"):
        value = getattr(obj, attr, None)
        if value is not None:
            return str(value)
    return None


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts) or str(exc)


def build_response(
    schema: type[T],
    obj: Any,
    *,
    context: Optional[str] = None,
) -> T:
    """Validate a single ORM row into *schema*, logging the row id on failure.

    Raises:
        pydantic.ValidationError: re-raised unchanged, so callers and the global
            handler behave exactly as before, minus the misleading 422.
    """
    try:
        return schema.model_validate(obj)
    except ValidationError as exc:
        logger.error(
            "Response building failed: %s could not be validated as %s "
            "(row_id=%s): %s",
            type(obj).__name__,
            schema.__name__,
            _row_id(obj),
            _describe(exc),
            exc_info=True,
        )
        if context:
            logger.error("Response building failed in context: %s", context)
        raise


def build_responses(
    schema: type[T],
    objs: Iterable[Any],
    *,
    context: Optional[str] = None,
) -> list[T]:
    """Validate many ORM rows, logging which one failed. See build_response."""
    return [build_response(schema, obj, context=context) for obj in objs]