"""
app/tasks/sync_tasks.py
=======================
Celery task: process an offline sync batch asynchronously.

Each branch has its own queue (``sync.branch.{branch_id}``) so a
single worker per branch handles events sequentially, eliminating
row-level lock contention on branch_inventory rows.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="app.tasks.sync_tasks.process_sync_batch",
    max_retries=3,
    default_retry_delay=15,  # seconds
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_sync_batch(
    self,
    *,
    payload: dict[str, Any],
    organization_id: str,
    pushed_by: str,
) -> dict[str, Any]:
    """
    Process a gzip-decompressed, JSON-decoded sync push payload.

    Arguments:
        payload        — Dict matching PushRequest schema (branch_id + records list).
        organization_id — UUID string of the authenticated organization.
        pushed_by      — UUID string of the authenticated user.

    Returns a dict matching PushResponse schema serialised as JSON.

    Celery does not natively support async tasks, so we run the async
    sync service inside a fresh event loop per task execution.  In
    production this is acceptable because each worker process handles
    exactly one task at a time (worker_prefetch_multiplier=1).
    """
    import asyncio
    from app.schemas.sync_schemas import PushRequest
    from app.services.sync.sync_service import SyncService
    from app.db.session import AsyncSessionLocal

    async def _run() -> dict[str, Any]:
        request = PushRequest(**payload)
        async with AsyncSessionLocal() as db:
            result = await SyncService.push(
                db=db,
                request=request,
                organization_id=uuid.UUID(organization_id),
                pushed_by=uuid.UUID(pushed_by),
            )
        return result.model_dump(mode="json")

    try:
        # Try to get an existing loop (useful in some test environments)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Shouldn't happen in a Celery worker, but guard defensively
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                return future.result(timeout=120)
        return loop.run_until_complete(_run())
    except Exception as exc:
        logger.error(
            "Sync batch task failed (attempt %d/%d): %s",
            self.request.retries + 1,
            self.max_retries + 1,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc)
