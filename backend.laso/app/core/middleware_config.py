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

    Order matters in FastAPI/Starlette: app.add_middleware wraps from last to
    first, so CORSMiddleware MUST be added LAST so that it becomes the outermost
    ASGI layer and short-circuits OPTIONS preflight requests before reaching
    custom BaseHTTPMiddleware layers.
    """
    cors_origins = get_cors_origins(settings)
    logger.info("CORS origins: %s", cors_origins)

    # ── GZip (decompresses incoming gzip requests; compresses responses ≥ 1 KB) ─
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ── Rate limiting (optional) ───────────────────────────────────────────────
    if settings.RATE_LIMIT_ENABLED:
        from app.middleware.rate_limit import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware)

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

    # ── Security headers ─────────────────────────────────────────────────────
    @app.middleware("http")
    async def add_security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    # ── Request-ID ────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def add_request_id_middleware(request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # ── CORS (added LAST to be OUTERMOST in ASGI stack) ──────────────────────
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
