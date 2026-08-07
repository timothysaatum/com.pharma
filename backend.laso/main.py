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
  6. Lifecycle (DB, shadow DB, notifications, CRR reconciliation)
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, Request, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import bindparam, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging_config import configure_logging
from app.core.middleware_config import register_middleware
from app.core.exception_handlers import register_exception_handlers
from app.db.session import engine, AsyncSessionLocal
from app.api.v1 import router as v1_router

# ── Logging must be configured before anything else ───────────────────────────
settings = get_settings()
configure_logging(settings.ENVIRONMENT)
logger = logging.getLogger(__name__)

# Background CRR reconciliation task reference (cancelled on shutdown)
_crr_reconciliation_task: Optional[asyncio.Task] = None

# Background CRR compaction task reference (cancelled on shutdown) — P1-7
_crr_compaction_task: Optional[asyncio.Task] = None


# ── CRR background reconciliation ─────────────────────────────────────────────

async def _crr_reconciliation_loop(interval: int) -> None:
    """Periodic background task: re-syncs shadow DB → Postgres."""
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


# ── CRR background compaction (P1-7) ──────────────────────────────────────────
#
# Ack-bounded retention for the CRDT change log. Compaction is bounded by
# acknowledgment (has every ACTIVE branch confirmed receiving a change?),
# never by wall-clock age — an offline-for-months branch must still be able
# to pull everything it missed on reconnect. Runs as its own task, wrapped
# in its own try/except, so a compaction failure can never take down
# reconciliation (see _crr_reconciliation_loop above).

async def _crr_compaction_loop(interval: int) -> None:
    """Periodic background task: prunes fully-acknowledged CRDT log entries."""
    while True:
        await asyncio.sleep(interval)
        try:
            await _run_crr_compaction()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("CRR compaction error: %s", exc, exc_info=True)


async def _compute_active_branch_min_watermark(
    db: AsyncSession,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Global minimum last_acked_db_version across ACTIVE branches.

    ``ShadowDB.db_version`` is one monotonic counter shared across every
    org/branch (it is a single global singleton — see shadow_db.py's
    module docstring), so this watermark is likewise a single global
    number, not scoped per-org. A branch with no watermark row yet (never
    completed a pull) counts as 0, which correctly blocks ALL compaction
    until it does — matching the "acknowledgment, not time" requirement.
    Soft-deleted/deactivated branches are excluded so a permanently-closed
    branch can't block compaction forever.

    Returns (watermark, oldest_branch_info_or_None) so the caller can log
    which branch is furthest behind, for operational visibility.
    """
    from app.models.pharmacy.pharmacy_model import Branch
    from app.models.system_md.sys_models import CrrBranchSyncWatermark

    result = await db.execute(
        select(
            Branch.id,
            Branch.name,
            CrrBranchSyncWatermark.last_acked_db_version,
            CrrBranchSyncWatermark.updated_at,
        )
        .select_from(Branch)
        .outerjoin(
            CrrBranchSyncWatermark, CrrBranchSyncWatermark.branch_id == Branch.id
        )
        .where(Branch.is_active.is_(True), Branch.is_deleted.is_(False))
    )
    rows = result.all()
    if not rows:
        # No active branches at all -- nothing to protect, nothing to compact for.
        return 0, None

    def _acked(row) -> int:
        return int(row[2]) if row[2] is not None else 0

    watermark = min(_acked(row) for row in rows)
    oldest = min(rows, key=_acked)
    return watermark, {
        "branch_id": oldest[0],
        "name": oldest[1],
        "last_acked_db_version": _acked(oldest),
        "updated_at": oldest[3],
    }


async def _compact_postgres_audit_tables(
    db: AsyncSession, shadow, watermark: int
) -> Dict[str, int]:
    """Ack-bounded DELETE for the plain-Postgres audit tables.

    Lower risk than the __crsql_clock compaction since these are ordinary
    Postgres rows, not CRDT bookkeeping:

    - ``audit_logs`` is itself a CRR table (published Postgres -> shadow
      for delivery to clients, see _CRR_TABLE_CONFIG["audit_logs"]), so a
      row is safe to delete once its shadow-side db_version is <=
      watermark. ``ShadowDB.get_prunable_row_ids`` fails closed: a row
      that was never published into the shadow at all is never returned,
      so it can't be deleted before anyone had a chance to pull it.
      Deleting via ``delete_crr_row`` afterwards keeps the shadow's own
      audit_logs table bounded too (it leaves a tombstone that the next
      __crsql_clock compaction pass cleans up in turn).
    - ``crr_renumber_audit`` is never pulled by any client at all (it is
      server-internal debugging/coordination history for keep_both_renumber
      merges), so it has no natural per-row shadow db_version to check.
      Its rows are stamped at write time with ``db_version_at_creation``
      (the shadow DB's global db_version at the moment the event was
      logged — see ShadowDB.get_row_db_version / _log_renumbering) and are
      pruned the same way: once the watermark has advanced past that
      stamp. Legacy rows written before this stamp existed (NULL) are
      never pruned.
    """
    deleted: Dict[str, int] = {}

    audit_log_ids = await shadow.get_prunable_row_ids("audit_logs", watermark)
    if audit_log_ids:
        await db.execute(
            text("DELETE FROM audit_logs WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": audit_log_ids},
        )
        for row_id in audit_log_ids:
            await shadow.delete_crr_row("audit_logs", row_id)
        deleted["audit_logs"] = len(audit_log_ids)

    # crr_renumber_audit is created lazily (see shadow_db._ensure_audit_table),
    # only once the first keep_both_renumber collision ever happens -- on a
    # fresh/quiet database it may not exist yet, which is expected and not
    # an error. Run in a SAVEPOINT so a missing-table failure here rolls
    # back only this statement, not the audit_logs deletion already done
    # above in the same outer transaction.
    try:
        async with db.begin_nested():
            renumber_result = await db.execute(
                text("""
                    DELETE FROM crr_renumber_audit
                    WHERE db_version_at_creation IS NOT NULL
                      AND db_version_at_creation <= :watermark
                """),
                {"watermark": watermark},
            )
        if renumber_result.rowcount:
            deleted["crr_renumber_audit"] = renumber_result.rowcount
    except DBAPIError:
        pass

    return deleted


async def _run_crr_compaction() -> None:
    """One compaction pass: clock tombstones + ack-bounded audit tables."""
    from app.services.sync.shadow_db import get_shadow_db

    shadow = await get_shadow_db()
    if not shadow.crr_available:
        return

    async with AsyncSessionLocal() as db:
        watermark, oldest = await _compute_active_branch_min_watermark(db)

        if oldest is not None:
            age = None
            if oldest["updated_at"] is not None:
                updated_at = oldest["updated_at"]
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - updated_at
            logger.info(
                "CRR compaction watermark=%d oldest_unacked_branch=%s "
                "last_acked_db_version=%d age=%s",
                watermark, oldest["branch_id"], oldest["last_acked_db_version"], age,
            )

        if watermark <= 0:
            logger.info("CRR compaction skipped: watermark is 0 (no branch has acked yet)")
            return

        clock_deleted = await shadow.compact_crr_tables(watermark)
        if clock_deleted:
            logger.info("CRR clock compaction pruned tombstones: %s", clock_deleted)

        pg_deleted = await _compact_postgres_audit_tables(db, shadow, watermark)
        await db.commit()
        if pg_deleted:
            logger.info("CRR postgres audit compaction: %s", pg_deleted)


# ── Application lifecycle ──────────────────────────────────────────────────────

async def lifespan(app: FastAPI):
    """Manage application lifecycle — startup and shutdown."""
    global _crr_reconciliation_task, _crr_compaction_task

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

    try:
        from app.services.sync.shadow_db import get_shadow_db
        shadow = await get_shadow_db()
        logger.info("Shadow SQLite database initialised")

        if not shadow.crr_available:
            logger.error(
                "CRR synchronization disabled: cr-sqlite extension is not loaded. "
                "Set CRSQLITE_EXTENSION_PATH. CRR endpoints will return HTTP 503."
            )

        reconcile_interval = settings.CRR_RECONCILE_INTERVAL_SECONDS
        if reconcile_interval > 0 and shadow.crr_available:
            _crr_reconciliation_task = asyncio.create_task(
                _crr_reconciliation_loop(reconcile_interval)
            )
            logger.info(
                "CRR reconciliation task started (interval=%ds)", reconcile_interval
            )

        compaction_interval = settings.CRR_COMPACTION_INTERVAL_SECONDS
        if compaction_interval > 0 and shadow.crr_available:
            _crr_compaction_task = asyncio.create_task(
                _crr_compaction_loop(compaction_interval)
            )
            logger.info(
                "CRR compaction task started (interval=%ds)", compaction_interval
            )
    except Exception as e:
        logger.warning("Shadow DB initialisation failed (non-critical): %s", e)

    yield

    logger.info("Shutting down application")
    if _crr_reconciliation_task is not None:
        _crr_reconciliation_task.cancel()
        try:
            await _crr_reconciliation_task
        except asyncio.CancelledError:
            pass
        logger.info("CRR reconciliation task stopped")
    if _crr_compaction_task is not None:
        _crr_compaction_task.cancel()
        try:
            await _crr_compaction_task
        except asyncio.CancelledError:
            pass
        logger.info("CRR compaction task stopped")
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
