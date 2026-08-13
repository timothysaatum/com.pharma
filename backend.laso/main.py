"""
main.py
=======
Laso Pharmacy Management — FastAPI application entry point.

Startup sequence:
  1. Logging (app/core/logging_config.py)
  2. App instantiation
  3. Middleware (app/core/middleware_config.py)
  4. Exception handlers (app/core/exception_handlers.py)
  5. Routers
  6. Lifecycle (DB, notifications)
"""
import logging

from fastapi import FastAPI, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.logging_config import configure_logging
from app.core.middleware_config import register_middleware
from app.core.exception_handlers import register_exception_handlers
from app.db.session import engine
from app.api.v1 import router as v1_router

# ── Logging must be configured before anything else ───────────────────────────
settings = get_settings()
configure_logging(settings.ENVIRONMENT)
logger = logging.getLogger(__name__)



# ── Application lifecycle ──────────────────────────────────────────────────────

async def lifespan(app: FastAPI):
    """Manage application lifecycle — startup and shutdown."""
    logger.info("Starting %s v%s", settings.PROJECT_NAME, settings.VERSION)
    logger.info("Environment: %s", settings.ENVIRONMENT)
    logger.info(
        "Database: %s",
        make_url(settings.DATABASE_URL).render_as_string(hide_password=True),
    )

    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection established")

        from app.db.session import is_sqlite

        if not is_sqlite:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                logger.info("PostgreSQL extensions verified")

        from app.db.base import Base
        if settings.ENVIRONMENT.lower() == "development":
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                if not is_sqlite:
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS idx_drug_search "
                        "ON drugs USING gin (search_vector gin_trgm_ops)"
                    ))
            logger.info("Database tables created/verified (development mode)")
        else:
            logger.info("Skipping auto-migration in production — use Alembic instead")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        raise

    try:
        from app.utils.notifications import setup_notifications, EmailConfig, ArkeselConfig

        email_config = None
        if settings.SMTP_HOST and settings.SMTP_USER:
            email_config = EmailConfig(
                smtp_host=settings.SMTP_HOST,
                smtp_port=settings.SMTP_PORT,
                smtp_user=settings.SMTP_USER,
                smtp_password=settings.SMTP_PASSWORD,
                from_email=settings.FROM_EMAIL,
                from_name=settings.PROJECT_NAME,
            )
            logger.info("Email notifications configured")

        arkesel_config = None
        if settings.ARKESEL_API_KEY:
            arkesel_config = ArkeselConfig(
                api_key=settings.ARKESEL_API_KEY,
                sender_id=settings.ARKESEL_SENDER_ID,
                base_url=settings.ARKESEL_BASE_URL,
            )
            logger.info("Arkesel SMS configured")

        setup_notifications(email_config=email_config, arkesel_config=arkesel_config)
        logger.info("Notification system initialized")
    except Exception as e:
        logger.warning("Notification setup failed (non-critical): %s", e)

    yield

    logger.info("Shutting down application")
    await engine.dispose()
    logger.info("All resources cleaned up")


# ── App instantiation ──────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "Pharmacy management system with inventory, sales, and prescription management"
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

register_middleware(app, settings)
register_exception_handlers(app)

# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(v1_router, prefix=settings.API_PREFIX)


# ── Health endpoints ───────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health_check():
    """Basic health check — returns 200 OK if the service is running."""
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health/deep", tags=["System"])
async def deep_health_check():
    """Deep health check including database and Redis connectivity."""
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
        logger.error("Database health check failed: %s", e)
        health_status["status"] = "degraded"
        health_status["checks"]["database"] = (
            "unhealthy" if settings.is_production else f"unhealthy: {e}"
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
                "unhealthy" if settings.is_production else f"unhealthy: {exc}"
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=health_status,
            )
        finally:
            if redis_client is not None:
                await redis_client.aclose()

    return health_status


# ── OpenAPI customization ─────────────────────────────────────────────────────

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=app.description,
        routes=app.routes,
    )
    schema["info"]["x-logo"] = {
        "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png"
    }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


# ── Root endpoint ──────────────────────────────────────────────────────────────

@app.get("/", tags=["System"])
async def root():
    """Root endpoint — API information."""
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "version": settings.VERSION,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
        "deep_health": "/health/deep",
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    host = "0.0.0.0" if settings.ENVIRONMENT == "production" else "127.0.0.1"
    reload = settings.ENVIRONMENT != "production"
    logger.info("Starting server on %s:8000", host)
    uvicorn.run(
        "main:app",
        host=host,
        port=8000,
        reload=reload,
        log_level="info" if settings.ENVIRONMENT == "production" else "debug",
        access_log=True,
        workers=1 if reload else 4,
    )
