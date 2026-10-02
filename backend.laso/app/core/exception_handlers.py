"""
app/core/exception_handlers.py
================================
All FastAPI exception handlers extracted from main.py.

Register them by calling register_exception_handlers(app).
"""
import json
import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError, ResponseValidationError
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
        """Pydantic ValidationError reaching the app during a request.

        This is a RESPONSE-building failure, never a request failure: request
        validation raises RequestValidationError (handled above, and correctly
        reported as 422). A row that fails to serialize is a server-side data
        problem, so it is reported as 500 and logged with the offending field
        paths. Returning 422 here made a GET report the caller as the cause and
        hid the bad row behind a validation error the caller could not act on.
        """
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "[%s] Response serialization failed on %s %s "
            "(server-side data does not satisfy the response schema): %s",
            request_id,
            request.method,
            request.url.path,
            exc.errors(),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Internal server error: failed to serialize response.",
                "type": "ResponseValidationError",
                "request_id": request_id,
            },
        )

    @app.exception_handler(ResponseValidationError)
    async def response_validation_exception_handler(
        request: Request, exc: ResponseValidationError
    ) -> JSONResponse:
        """FastAPI response_model validation failure.

        Raised when a route declares response_model= and the returned object
        cannot be validated into it. Explicitly 500 with the failing paths
        logged, so it is distinguishable from a client error in the logs.
        """
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "[%s] ResponseValidationError on %s %s (response_model): %s",
            request_id,
            request.method,
            request.url.path,
            exc.errors(),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Internal server error: response did not match the declared response_model.",
                "type": "ResponseValidationError",
                "request_id": request_id,
            },
        )

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
