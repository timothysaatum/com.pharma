"""
celery_app.py
=============
Celery application for asynchronous sync queue processing.

Broker: Redis (REDIS_URL from settings).
Each branch gets its own queue: ``sync.branch.{branch_id}``.
A single Celery worker per branch processes events sequentially,
eliminating row-level lock contention on branch_inventory rows.

Usage (production):
  celery -A celery_app worker -Q sync.default --concurrency=1 -l info

Usage (per-branch worker, replace {branch_id} with actual UUID):
  celery -A celery_app worker -Q sync.branch.{branch_id} --concurrency=1 -l info
"""
from __future__ import annotations

import logging
from celery import Celery
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _make_celery() -> Celery:
    if not settings.REDIS_URL:
        raise RuntimeError(
            "REDIS_URL must be set in environment to use the Celery sync worker. "
            "Set REDIS_URL=redis://localhost:6379/0 for local development."
        )

    app = Celery(
        "laso_sync",
        broker=settings.REDIS_URL,
        backend=settings.REDIS_URL,
        include=["app.tasks.sync_tasks"],
    )

    app.conf.update(
        # Serialization
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],

        # Timezone
        timezone="UTC",
        enable_utc=True,

        # Task execution
        task_acks_late=True,               # Ack only after successful execution
        task_reject_on_worker_lost=True,   # Re-queue on unexpected worker death
        worker_prefetch_multiplier=1,      # One task at a time per worker process

        # Result TTL
        result_expires=3600,               # 1 hour

        # Routing: sales sync tasks go to branch-specific queues
        task_routes={
            "app.tasks.sync_tasks.process_sync_batch": {
                "queue": "sync.default",
            },
        },
    )

    return app


celery_app = _make_celery()
