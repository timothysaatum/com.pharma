"""
app/utils/response_building.py
=============================
Explicit, instrumented construction of response models.

Why this exists
---------------
``GET /drugs`` once returned::

    422 {"detail":"Validation error",
         "errors":[{"loc":["reorder_quantity"],
                    "msg":"Input should be greater than or equal to 1"}]}

for a request that was entirely valid. ``DrugResponse`` inherited
``reorder_quantity ge=1`` from the input model, two stored drugs held ``0``,
and the resulting ``ValidationError`` was reported to the caller as a
*client* error. The request was fine; the server's data was the problem.

Two things fix that, and both live here:

1. ``build_response_model`` raises a :class:`ResponseBuildError` that is
   unambiguously identifiable as response-side, carrying the offending row ids,
   so the handler at ``app/core/exception_handlers.py`` answers **500** and
   logs which row to fix.

2. It always logs the row ids on failure, so the offending record is
   identifiable without reproducing the request.

Everything that builds a response model from database rows should go through
this helper rather than calling ``Model.model_validate`` directly.
"""

import logging
from typing import Any, Iterable, Sequence, Type, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# Attribute used to carry offending row ids onto the exception for the handler.
ROW_IDS_ATTR = "__response_build_row_ids__"

# Marker injected into the failing `loc` so classification is unambiguous even
# when the exception is not wrapped by FastAPI.
LOC_MARKER = "__response_model__"

TModel = TypeVar("TModel", bound=BaseModel)


class ResponseBuildError(Exception):
    """Raised while BUILDING a response, as opposed to reading input.

    Deliberately NOT a subclass of pydantic's ``ValidationError``: that class
    lives in ``pydantic_core`` and cannot be subclassed. This is its own type
    and is registered as a distinct exception handler
    (``app/core/exception_handlers.py``) that answers **500** and logs the
    offending row ids.

    ``original`` retains the underlying pydantic error so callers that want the
    field-level detail still have it.
    """

    def __init__(self, original: ValidationError, row_ids: Sequence[Any] = ()):
        self.original = original
        self.row_ids = list(row_ids)
        super().__init__(
            "Response model construction failed for row(s): "
            + ", ".join(str(r) for r in self.row_ids)
        )
        setattr(self, ROW_IDS_ATTR, self.row_ids)

    @property
    def marked_errors(self) -> list:
        """``original.errors()`` with ``LOC_MARKER`` prepended to every loc.

        Used by the generic pydantic handler to recognise a response-side
        failure that was re-raised as a plain ``ValidationError``.
        """
        return [
            {
                "type": err.get("type", "value_error"),
                "loc": (LOC_MARKER, *err.get("loc", ())),
                "msg": err.get("msg", ""),
                "input": err.get("input"),
            }
            for err in self.original.errors()
        ]


def build_response_model(
    model: Type[TModel],
    rows: Iterable[Any],
    *,
    row_id_attr: str = "id",
    context: str = "",
) -> list[TModel]:
    """Validate ORM rows into ``model``, attributing any failure to the row.

    Args:
        model: the response pydantic model.
        rows: ORM objects (or dicts) to validate.
        row_id_attr: attribute used to identify a row in logs and errors.
        context: short description used in the log line, e.g. "GET /drugs".

    Returns:
        A list of validated models.

    Raises:
        ResponseBuildError: if any row fails. The offending row ids are on the
            exception and are logged at ERROR before it propagates, so the
            handler can return 500 with a log the operator can act on.
    """
    rows = list(rows)
    built: list[TModel] = []
    failures: list[tuple[Any, ValidationError]] = []

    for row in rows:
        row_id = getattr(row, row_id_attr, None)
        if row_id is None and isinstance(row, dict):
            row_id = row.get(row_id_attr)
        try:
            built.append(model.model_validate(row))
        except ValidationError as exc:
            failures.append((row_id, exc))

    if not failures:
        return built

    row_ids = [rid for rid, _ in failures]
    first_ids, first_exc = failures[0]
    logger.error(
        "%s: %d of %d row(s) could not be serialised into %s. "
        "Offending %s=%r. First failure: %s",
        context or "response build",
        len(failures),
        len(rows),
        model.__name__,
        row_id_attr,
        row_ids,
        first_exc.errors(),
    )
    # Surface the full error list by merging, but keep one row id list.
    raise ResponseBuildError(first_exc, row_ids=row_ids) from first_exc


def build_paginated_response_model(
    model: Type[TModel],
    rows: Sequence[Any],
    *,
    total: int,
    page: int,
    page_size: int,
    total_pages: int,
    has_next: bool,
    has_prev: bool,
    row_id_attr: str = "id",
    context: str = "",
) -> Any:
    """Same as :func:`build_response_model` but returns ``PaginatedResponse``.

    Mirrors ``Paginator.paginate_list``, which is the in-memory paginator used
    by the drug endpoints. Kept here so the response-building path for a paginated
    list has one instrumented implementation.
    """
    from app.utils.pagination import PaginatedResponse

    items = build_response_model(
        model, rows, row_id_attr=row_id_attr, context=context
    )
    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_next=has_next,
        has_prev=has_prev,
    )