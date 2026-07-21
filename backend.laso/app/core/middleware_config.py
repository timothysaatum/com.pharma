"""
app/core/middleware_config.py
==============================
Middleware registration extracted from main.py.

Exports:
  get_cors_origins(settings) -> list[str]  — build the CORS allow-list
  register_middleware(app, settings)       — attach all middleware to *app*
"""
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

logger = logging.getLogger(__name__)


def get_cors_origins(settings) -> list[str]:
    """Return the CORS allowed-origins list for the current environment."""
    origins: list[str] = [
        "http://tauri.localhost",
        "tauri://localhost",
        "https://tauri.localhost",
    ]
    if not settings.is_production:
        origins.extend([
            "http://localhost:1420",
            "http://127.0.0.1:1420",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ])
    for origin in settings.CORS_ORIGINS:
        if origin not in origins:
            origins.append(origin)
    return origins


def register_middleware(app: FastAPI, settings) -> None:
    """Attach CORS, request-id, logging, and rate-limit middleware to *app*.

    Order matters: CORS must be added first (outermost) so that OPTIONS
    preflight requests are short-circuited before reaching custom middleware.
    """
    cors_origins = get_cors_origins(settings)
    logger.info("CORS origins: %s", cors_origins)

    # ── GZip (decompresses incoming gzip requests; compresses responses ≥ 1 KB) ─
    # Must be added before CORS so it wraps all responses, including preflight.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ── CORS ──────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=(
            None
            if settings.is_production
            else r"^http://(localhost|127\.0\.0\.1):\d+$"
        ),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "X-Request-ID",
            "Content-Encoding",   # Allow gzip-compressed sync push payloads
        ],
    )

    # ── Request-ID ────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def add_request_id_middleware(request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # ── Request logging ───────────────────────────────────────────────────────
    @app.middleware("http")
    async def log_requests_middleware(request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        request_id = getattr(request.state, "request_id", "unknown")
        logger.info(
            "[%s] %s %s - Status: %s - Duration: %.3fs",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            process_time,
        )
        response.headers["X-Process-Time"] = str(process_time)
        return response

    # ── Rate limiting (optional) ───────────────────────────────────────────────
    if settings.RATE_LIMIT_ENABLED:
        from app.middleware.rate_limit import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware)
