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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

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


class RequestContextMiddleware:
    """Pure ASGI middleware handling Request ID, logging, and security headers.

    Avoids Starlette BaseHTTPMiddleware TaskGroup race conditions and
    'No response returned' exceptions under concurrent client requests.
    """
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        start_time = time.time()
        status_code = 200

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
                process_time = time.time() - start_time
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                headers.append((b"x-process-time", f"{process_time:.3f}".encode("latin-1")))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"x-content-type-options", b"nosniff"))
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            process_time = time.time() - start_time
            logger.info(
                "[%s] %s %s - Status: %s - Duration: %.3fs",
                request_id,
                scope.get("method", ""),
                scope.get("path", ""),
                status_code,
                process_time,
            )


def register_middleware(app: FastAPI, settings) -> None:
    """Attach CORS, request-context, rate-limit, and gzip middleware to *app*.

    Order matters in FastAPI/Starlette: app.add_middleware wraps from last to
    first, so CORSMiddleware MUST be added LAST so that it becomes the outermost
    ASGI layer and short-circuits OPTIONS preflight requests.
    """
    cors_origins = get_cors_origins(settings)
    logger.info("CORS origins: %s", cors_origins)

    # ── GZip (decompresses incoming gzip requests; compresses responses ≥ 1 KB) ─
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ── Rate limiting (optional) ───────────────────────────────────────────────
    if settings.RATE_LIMIT_ENABLED:
        from app.middleware.rate_limit import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware)

    # ── Request Context (request-id, timing, logging, security headers) ────────
    app.add_middleware(RequestContextMiddleware)

    # ── CORS (added LAST to be OUTERMOST in ASGI stack) ──────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=(
            None
            if settings.is_production
            else r"^(http://(localhost|127\.0\.0\.1):\d+|https?://tauri\.localhost|tauri://localhost)$"
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
