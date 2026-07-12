import asyncio
import logging
import logging.config
from typing import Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, IntegrityError

from app.db.session import AsyncSessionLocal

from app.core.config import get_settings
from app.db.session import engine
from app.api.v1 import router as v1_router
from app.middleware.rate_limit import RateLimitMiddleware
from app.utils.exceptions import (
    build_error_response,
    data_error_detail,
    integrity_error_detail,
)


# ============================================================================
# CONFIGURATION & SETTINGS
# ============================================================================

settings = get_settings()
HTTP_422_UNPROCESSABLE = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

# Ensure logs directory exists before configuring file logging
import os
os.makedirs("logs", exist_ok=True)

# Configure structured logging
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "detailed": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "detailed",
            "filename": "logs/app.log",
            "maxBytes": 10485760,  # 10MB
            "backupCount": 10,
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "ERROR",
            "formatter": "detailed",
            "filename": "logs/error.log",
            "maxBytes": 10485760,  # 10MB
            "backupCount": 10,
        },
    },
    "loggers": {
        "": {  # root logger
            "handlers": ["console", "file"] if settings.ENVIRONMENT == "production" else ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "app": {
            "handlers": ["console", "file", "error_file"] if settings.ENVIRONMENT == "production" else ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["console", "file"] if settings.ENVIRONMENT == "production" else ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

# Background CRR reconciliation task reference (cancelled on shutdown)
_crr_reconciliation_task: Optional[asyncio.Task] = None


# ============================================================================
# LIFECYCLE EVENTS
# ============================================================================

async def _crr_reconciliation_loop(interval: int) -> None:
    """Periodic background task that re-syncs shadow DB → Postgres.

    Runs immediately on first iteration (crash recovery), then every
    *interval* seconds.  Uses a dedicated DB session from the engine pool.
    """
    from app.services.sync.shadow_db import get_shadow_db

    # Run once immediately (handles crash recovery on restart)
    await _reconcile_all_tables()
    logger.info("CRR initial reconciliation complete")

    while True:
        await asyncio.sleep(interval)
        try:
            await _reconcile_all_tables()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("CRR reconciliation error: %s", exc, exc_info=True)


async def _reconcile_all_tables() -> None:
    """Reconcile all known CRR tables from shadow DB into Postgres."""
    try:
        from app.services.sync.shadow_db import get_shadow_db, get_crr_table_names
        shadow = await get_shadow_db()
        async with AsyncSessionLocal() as db:
            for table_name in get_crr_table_names():
                checked, updated = await shadow.reconcile_table(table_name, db)
                await db.commit()
                if checked > 0 or updated > 0:
                    logger.info(
                        "CRR reconcile %s: checked=%d updated=%d",
                        table_name, checked, updated,
                    )
    except Exception as exc:
        logger.error("CRR reconcile all tables failed: %s", exc, exc_info=True)


async def lifespan(app: FastAPI):
    """
    Manage application lifecycle - startup and shutdown events.
    """
    # Startup
    logger.info(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    database_url = make_url(settings.DATABASE_URL).render_as_string(
        hide_password=True
    )
    logger.info("Database: %s", database_url)
    
    try:
        # Database connection check
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection established")
        
        # Enable required PostgreSQL extensions
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            logger.info("PostgreSQL extensions verified")

        # Auto-create tables only in development
        from app.db.base import Base
        if settings.ENVIRONMENT.lower() == "development":
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

                # Create GIN index with trigram operator class (PG 15+ requires explicit ops)
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_drug_search "
                    "ON drugs USING gin (search_vector gin_trgm_ops)"
                ))

                logger.info("Database tables created/verified (development mode)")
        else:
            logger.info("Skipping auto-migration in production — use Alembic instead")
        
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")
        raise
    
    try:
        from app.utils.notifications import setup_notifications, EmailConfig, ArkeselConfig
        
        # Email config (optional)
        email_config = None
        if settings.SMTP_HOST and settings.SMTP_USER:
            email_config = EmailConfig(
                smtp_host=settings.SMTP_HOST,
                smtp_port=settings.SMTP_PORT,
                smtp_user=settings.SMTP_USER,
                smtp_password=settings.SMTP_PASSWORD,
                from_email=settings.FROM_EMAIL,
                from_name=settings.PROJECT_NAME
            )
            logger.info("Email notifications configured")
        
        # Arkesel SMS config
        arkesel_config = None
        if settings.ARKESEL_API_KEY:
            arkesel_config = ArkeselConfig(
                api_key=settings.ARKESEL_API_KEY,
                sender_id=settings.ARKESEL_SENDER_ID,
                base_url=settings.ARKESEL_BASE_URL
            )
            logger.info("Arkesel SMS configured")
        
        # Initialize
        setup_notifications(
            email_config=email_config,
            arkesel_config=arkesel_config
        )
        logger.info("Notification system initialized")
        
    except Exception as e:
        logger.warning(f"Notification setup failed (non-critical): {str(e)}")
    
    # Initialise CRDT shadow database (per ADR 0003)
    try:
        from app.services.sync.shadow_db import get_shadow_db
        shadow = await get_shadow_db()
        logger.info("Shadow SQLite database initialised")

        if not shadow.crr_available:
            logger.error(
                "CRR synchronization disabled: cr-sqlite extension is not "
                "loaded. Set CRSQLITE_EXTENSION_PATH to the server's native "
                "crsqlite library. CRR endpoints will return HTTP 503."
            )

        # Start background CRR reconciliation loop
        reconcile_interval = settings.CRR_RECONCILE_INTERVAL_SECONDS
        if reconcile_interval > 0 and shadow.crr_available:
            global _crr_reconciliation_task
            _crr_reconciliation_task = asyncio.create_task(
                _crr_reconciliation_loop(reconcile_interval)
            )
            logger.info(
                "CRR reconciliation task started (interval=%ds)", reconcile_interval
            )
    except Exception as e:
        logger.warning(f"Shadow DB initialisation failed (non-critical): {e}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application")
    if _crr_reconciliation_task is not None:
        _crr_reconciliation_task.cancel()
        try:
            await _crr_reconciliation_task
        except asyncio.CancelledError:
            pass
        logger.info("CRR reconciliation task stopped")
    await engine.dispose()
    logger.info("All resources cleaned up")


# ============================================================================
# FASTAPI APP INITIALIZATION
# ============================================================================

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Pharmacy management system with inventory, sales, and prescription management",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ============================================================================
# MIDDLEWARE CONFIGURATION
# ============================================================================

cors_origins = [
    "http://tauri.localhost",
    "tauri://localhost",
    "https://tauri.localhost",
]
if not settings.is_production:
    cors_origins.extend([
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ])
for origin in settings.CORS_ORIGINS:
    if origin not in cors_origins:
        cors_origins.append(origin)

# Custom middleware added first (will be inner in the stack)
@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    import uuid
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    import time
    start_time = time.time()
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.info(
        f"[{request_id}] {request.method} {request.url.path} - "
        f"Status: {response.status_code} - Duration: {process_time:.3f}s"
    )
    
    response.headers["X-Process-Time"] = str(process_time)
    return response


if settings.RATE_LIMIT_ENABLED:
    app.add_middleware(RateLimitMiddleware)

# CORS added last so it becomes the outermost middleware
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
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
)


# ============================================================================
# EXCEPTION HANDLERS
# ============================================================================

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Handle validation errors with detailed error information.
    Properly serializes all error data including ValueError objects.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.warning(
        f"[{request_id}] Validation error on {request.method} {request.url.path}"
    )
    
    # Convert errors to JSON-serializable format
    formatted_errors = []
    for error in exc.errors():
        # Create a clean error dict with only serializable data
        clean_error = {
            "loc": list(error.get("loc", [])),
            "msg": str(error.get("msg", "")),
            "type": error.get("type", "validation_error"),
        }
        
        # Safely handle the input field
        if "input" in error:
            input_val = error["input"]
            # Convert complex objects to string representation
            if hasattr(input_val, '__dict__'):
                clean_error["input"] = f"<{type(input_val).__name__} object>"
            else:
                try:
                    # Try to convert to JSON-safe format
                    import json
                    json.dumps(input_val)  # Test if serializable
                    clean_error["input"] = input_val
                except (TypeError, ValueError):
                    clean_error["input"] = str(input_val)
        
        # Safely handle context
        if "ctx" in error:
            try:
                # Convert all context values to strings to avoid serialization errors
                clean_error["ctx"] = {
                    k: str(v) for k, v in error["ctx"].items()
                }
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
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
    """
    Handle Pydantic ValidationError (from model validation).
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.warning(
        f"[{request_id}] Pydantic validation error on {request.method} {request.url.path}"
    )
    
    # Convert errors to JSON-serializable format
    formatted_errors = []
    for error in exc.errors():
        formatted_errors.append({
            "loc": list(error.get("loc", [])),
            "msg": str(error.get("msg", "")),
            "type": error.get("type", "validation_error"),
        })
    
    return JSONResponse(
        status_code=HTTP_422_UNPROCESSABLE,
        content={
            "detail": "Validation error",
            "errors": formatted_errors,
            "request_id": request_id,
        },
    )


@app.exception_handler(ValueError)
async def value_error_exception_handler(request: Request, exc: ValueError):
    """
    Handle ValueError (e.g., from field validators).
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.warning(
        f"[{request_id}] ValueError on {request.method} {request.url.path}: {str(exc)}"
    )
    
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": str(exc),
            "request_id": request_id,
            "type": "ValueError"
        },
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    """
    Handle database integrity violations (unique constraints, FK violations)
    with user-friendly messages instead of raw SQLAlchemy tracebacks.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    detail = integrity_error_detail(exc)

    logger.warning(
        f"[{request_id}] IntegrityError on {request.method} {request.url.path}: {detail}"
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
async def data_error_handler(request: Request, exc: DataError):
    """
    Handle database data errors (invalid data type, out of range, etc.)
    with user-friendly messages.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    detail = data_error_detail(exc)

    logger.warning(
        f"[{request_id}] DataError on {request.method} {request.url.path}: {detail}"
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
async def general_exception_handler(request: Request, exc: Exception):
    """
    Handle unexpected exceptions with proper logging.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    
    logger.error(
        f"[{request_id}] Unhandled exception on {request.method} {request.url.path}: {str(exc)}",
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
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=content,
    )


# ============================================================================
# ROUTES & ENDPOINTS
# ============================================================================

# API v1 routes
app.include_router(
    v1_router,
    prefix=settings.API_PREFIX,
)


# ============================================================================
# HEALTH CHECK ENDPOINTS
# ============================================================================

@app.get("/health", tags=["System"])
async def health_check():
    """
    Basic health check endpoint.
    Returns 200 OK if the service is running.
    """
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health/deep", tags=["System"])
async def deep_health_check():
    """
    Deep health check including database connectivity.
    """
    health_status = {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "checks": {
            "database": "unknown",
            "redis": "disabled" if not settings.REDIS_URL else "unknown",
        },
    }
    
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        health_status["checks"]["database"] = "healthy"
    except Exception as e:
        logger.error(f"Database health check failed: {str(e)}")
        health_status["status"] = "degraded"
        health_status["checks"]["database"] = (
            "unhealthy"
            if settings.is_production
            else f"unhealthy: {str(e)}"
        )

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=health_status,
        )

    if settings.REDIS_URL:
        redis_client = None
        try:
            from redis.asyncio import from_url

            redis_client = from_url(
                settings.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await redis_client.ping()
            health_status["checks"]["redis"] = "healthy"
        except Exception as exc:
            logger.error("Redis health check failed: %s", exc)
            health_status["status"] = "degraded"
            health_status["checks"]["redis"] = (
                "unhealthy"
                if settings.is_production
                else f"unhealthy: {exc}"
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=health_status,
            )
        finally:
            if redis_client is not None:
                await redis_client.aclose()

    return health_status


# ============================================================================
# OPENAPI CUSTOMIZATION
# ============================================================================

def custom_openapi():
    """
    Customize OpenAPI schema with additional metadata.
    """
    if app.openapi_schema:
        return app.openapi_schema
    
    openapi_schema = get_openapi(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=app.description,
        routes=app.routes,
    )
    
    openapi_schema["info"]["x-logo"] = {
        "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png"
    }
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# ============================================================================
# ROOT ENDPOINT
# ============================================================================

@app.get("/", tags=["System"])
async def root():
    """
    Root endpoint providing API information.
    """
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "version": settings.VERSION,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
        "deep_health": "/health/deep",
    }


# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    # Determine host and port
    host = "0.0.0.0" if settings.ENVIRONMENT == "production" else "127.0.0.1"
    port = 8000
    
    # Determine reload behavior
    reload = settings.ENVIRONMENT != "production"
    
    # Start server
    logger.info(f"Starting server on {host}:{port}")
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info" if settings.ENVIRONMENT == "production" else "debug",
        access_log=True,
        workers=1 if reload else 4,
    )
