"""
app/core/exception_handlers.py
================================
All FastAPI exception handlers extracted from main.py.

Register them by calling register_exception_handlers(app).
"""
import json
import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError

from app.utils.exceptions import (
    build_error_response,
    data_error_detail,
    integrity_error_detail,
)

logger = logging.getLogger(__name__)

HTTP_422_UNPROCESSABLE = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)


# ── Response-side detection ─────────────────────────────────────────────────
#
# Imported lazily and defensively: exception_handlers.py is imported by main.py
# during app construction, and response_building imports pydantic only, so this
# is cheap and acyclic. A failure to import must not stop the app booting.
try:  # pragma: no cover
    from fastapi.exceptions import ResponseValidationError as _ResponseValidationError
except Exception:  # pragma: no cover
    _ResponseValidationError = None

try:  # pragma: no cover - exercised indirectly
    from app.utils.response_building import (
        LOC_MARKER as RESPONSE_BUILD_LOC_MARKER,
        ROW_IDS_ATTR as RESPONSE_BUILD_ROW_IDS_ATTR,
        ResponseBuildError as _ResponseBuildError,
    )
except Exception:  # pragma: no cover
    RESPONSE_BUILD_LOC_MARKER = "__response_model__"
    RESPONSE_BUILD_ROW_IDS_ATTR = "__response_build_row_ids__"
    _ResponseBuildError = None


def _is_response_side(exc: BaseException, formatted_errors: list) -> bool:
    """True when this error was raised while BUILDING a response.

    Three signals, any one sufficient:

    1. The loc marker injected by ``build_response_model``.
    2. FastAPI wrapped it in ``ResponseValidationError`` — only ever raised
       while serialising a handler's return value, so definitive. This is the
       path every ``response_model=`` route takes.
    3. Row ids were stashed on the exception by ``build_response_model``.
    """
    if any(RESPONSE_BUILD_LOC_MARKER in loc for e in formatted_errors for loc in e["loc"]):
        return True

    try:
        from fastapi.exceptions import ResponseValidationError
    except Exception:  # pragma: no cover - FastAPI always provides this
        return False

    for holder in (exc, getattr(exc, "__cause__", None), exc.__context__):
        if isinstance(holder, ResponseValidationError):
            return True

    return any(
        getattr(holder, RESPONSE_BUILD_ROW_IDS_ATTR, None)
        for holder in (exc, getattr(exc, "__cause__", None), exc.__context__)
    )


def _row_ids_from_validation_error(exc: BaseException) -> list:
    """Best-effort extraction of offending row ids for the log line."""
    ids: list = []
    for holder in (exc, getattr(exc, "__cause__", None), exc.__context__):
        found = getattr(holder, RESPONSE_BUILD_ROW_IDS_ATTR, None)
        if found:
            ids = list(found)
            break
    return ids


def _response_side_error_response(
    request: Request, exc: BaseException, formatted_errors: list
) -> JSONResponse:
    """500 + a log naming the offending row. Never 422."""
    request_id = getattr(request.state, "request_id", "unknown")
    row_ids = _row_ids_from_validation_error(exc)
    logger.error(
        "[%s] Response construction failed on %s %s — this is a SERVER fault, "
        "not a client error. Failing fields: %s%s",
        request_id,
        request.method,
        request.url.path,
        [e["loc"] for e in formatted_errors],
        f" | offending row ids: {row_ids}" if row_ids else "",
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=build_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The server could not build the response for this request. "
                "This is a server-side data fault, not an error in your "
                "request."
            ),
            request_id=request_id,
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all application-level exception handlers to *app*."""

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle FastAPI request validation errors with detailed messages."""
        request_id = getattr(request.state, "request_id", "unknown")
        logger.warning(
            "[%s] Validation error on %s %s",
            request_id,
            request.method,
            request.url.path,
        )

        formatted_errors = []
        for error in exc.errors():
            clean_error = {
                "loc": list(error.get("loc", [])),
                "msg": str(error.get("msg", "")),
                "type": error.get("type", "validation_error"),
            }
            if "input" in error:
                input_val = error["input"]
                if hasattr(input_val, "__dict__"):
                    clean_error["input"] = f"<{type(input_val).__name__} object>"
                else:
                    try:
                        json.dumps(input_val)
                        clean_error["input"] = input_val
                    except (TypeError, ValueError):
                        clean_error["input"] = str(input_val)
            if "ctx" in error:
                try:
                    clean_error["ctx"] = {k: str(v) for k, v in error["ctx"].items()}
                except Exception:
                    pass
            formatted_errors.append(clean_error)

        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content={
                "detail": "Validation error",
                "errors": formatted_errors,
                "request_id": request_id,
            },
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_exception_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        """Handle a Pydantic ``ValidationError``, distinguishing WHERE it came from.

        A ``ValidationError`` reaches this handler from two very different
        places, and conflating them is actively harmful:

        1. **Request-side.** FastAPI validating a path/query/body parameter
           against a schema. The caller sent something wrong; 422 is correct.
           FastAPI normally raises ``RequestValidationError`` (handled above),
           which subclasses ``ValidationError``, so this branch catches the
           remainder.

        2. **Response-side.** FastAPI validating the handler's RETURN value
           against ``response_model``, or the handler itself calling
           ``model_validate`` on a row. FastAPI wraps that in
           ``ResponseValidationError``, whose ``__cause__`` is the original
           ``ValidationError``. Nothing the caller did is wrong — the server's
           own stored or derived data is. Returning 422 here tells the caller to
           fix a request that is already correct, and hides a server fault
           behind a client-error status.

        The original bug: ``DrugResponse`` inherited ``reorder_quantity ge=1``
        from the input model, so two drugs with ``reorder_quantity = 0`` made
        ``GET /drugs`` return 422 for every caller in the org.

        We detect the response-side case by checking whether the exception
        arrives wrapped in FastAPI's ``ResponseValidationError``, and fall back
        to inspecting the failing loc for the marker we attach at the explicit
        response-building sites (see ``app/utils/response_building.py``).
        """
        request_id = getattr(request.state, "request_id", "unknown")

        formatted_errors = [
            {
                "loc": list(e.get("loc", [])),
                "msg": str(e.get("msg", "")),
                "type": e.get("type", "validation_error"),
            }
            for e in exc.errors()
        ]

        # ── Case 2: raised while BUILDING A RESPONSE ──────────────────────
        # Always a server fault. 500, and log the offending field paths plus
        # any row ids we captured, so the bad row is identifiable in the log.
        if _is_response_side(exc, formatted_errors):
            return _response_side_error_response(request, exc, formatted_errors)

        # ── Case 1: genuine request-side validation ───────────────────────
        logger.warning(
            "[%s] Pydantic validation error on %s %s",
            request_id,
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content={
                "detail": "Validation error",
                "errors": formatted_errors,
                "request_id": request_id,
            },
        )

    # Registered against the CONCRETE class, not Exception. Starlette resolves
    # handlers by walking type(exc).__mro__, so this wins over the generic
    # Exception handler below for ResponseBuildError while leaving every other
    # exception to it.
    if _ResponseBuildError is not None:

        @app.exception_handler(_ResponseBuildError)
        async def response_build_error_handler(
            request: Request, exc: Exception
        ) -> JSONResponse:
            """A stored row could not be serialised into the response model.

            500, with the offending row ids logged. Never 422: the caller did
            nothing wrong.
            """
            formatted = [
                {
                    "loc": list(e.get("loc", [])),
                    "msg": str(e.get("msg", "")),
                    "type": e.get("type", ""),
                }
                for e in exc.marked_errors
            ]
            return _response_side_error_response(request, exc, formatted)

    # The `response_model=` path. FastAPI raises ResponseValidationError when it
    # cannot serialise a handler's return value; that class is NOT a
    # ValidationError, so it would otherwise reach the generic Exception
    # handler and surface str(exc) — a 500 by luck, with no actionable message.
    # Registered explicitly so every response_model route reports it as the
    # server fault it is.
    if _ResponseValidationError is not None:

        @app.exception_handler(_ResponseValidationError)
        async def fastapi_response_validation_handler(
            request: Request, exc: Exception
        ) -> JSONResponse:
            formatted = [
                {
                    "loc": list(e.get("loc", [])),
                    "msg": str(e.get("msg", "")),
                    "type": e.get("type", ""),
                }
                for e in exc.errors()  # type: ignore[attr-defined]
            ]
            return _response_side_error_response(request, exc, formatted)

    @app.exception_handler(ValueError)
    async def value_error_exception_handler(
        request: Request, exc: ValueError
    ) -> JSONResponse:
        """Handle ValueError (e.g., from field validators)."""
        request_id = getattr(request.state, "request_id", "unknown")
        logger.warning(
            "[%s] ValueError on %s %s: %s",
            request_id,
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": str(exc),
                "request_id": request_id,
                "type": "ValueError",
            },
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: IntegrityError
    ) -> JSONResponse:
        """Handle DB unique-constraint / FK violations with friendly messages."""
        request_id = getattr(request.state, "request_id", "unknown")
        detail = integrity_error_detail(exc)
        logger.warning(
            "[%s] IntegrityError on %s %s: %s",
            request_id,
            request.method,
            request.url.path,
            detail,
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=build_error_response(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
                request_id=request_id,
            ),
        )

    @app.exception_handler(DataError)
    async def data_error_handler(
        request: Request, exc: DataError
    ) -> JSONResponse:
        """Handle DB data errors (invalid type, out-of-range, etc.)."""
        request_id = getattr(request.state, "request_id", "unknown")
        detail = data_error_detail(exc)
        logger.warning(
            "[%s] DataError on %s %s: %s",
            request_id,
            request.method,
            request.url.path,
            detail,
        )
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content=build_error_response(
                status_code=HTTP_422_UNPROCESSABLE,
                detail=detail,
                request_id=request_id,
            ),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Handle unexpected exceptions — log them and return a safe response."""
        from app.core.config import get_settings

        settings = get_settings()
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "[%s] Unhandled exception on %s %s: %s",
            request_id,
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )

        env = settings.ENVIRONMENT
        detail = "Internal server error" if env == "production" else str(exc)
        content = build_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
            request_id=request_id,
            extra={"type": type(exc).__name__} if env != "production" else None,
        )

        # Manually inject CORS headers — the global CORSMiddleware does not
        # reach exception responses that escape BaseHTTPMiddleware.
        from app.core.middleware_config import get_cors_origins
        cors_origins = get_cors_origins(settings)
        origin = request.headers.get("origin", "")
        cors_headers: dict[str, str] = {}
        if origin in cors_origins or (
            not settings.is_production and origin and "localhost" in origin
        ):
            cors_headers["Access-Control-Allow-Origin"] = origin
            cors_headers["Access-Control-Allow-Credentials"] = "true"
            cors_headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            cors_headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Accept, X-Request-ID"
            )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=content,
            headers=cors_headers,
        )
