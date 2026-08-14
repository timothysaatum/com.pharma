"""
Server-side event emitter for reference-data mutations.

When an API endpoint writes a drug/category directly to Postgres (existing
behavior), it calls ``ServerEventEmitter.emit()`` to append a matching event
to ``event_log`` so clients can pull the change via GET /sync/events.

The endpoint owns the primary write; the emitter appends the notification in
a separate step.  Any exception from the emitter is logged and swallowed so a
failed notification never rolls back the primary write.

ADR 0006: server-emitted events share the same hash chain as client-pushed
events.  The emitter acquires the per-org advisory lock before computing
``hash_self`` and inserting into ``event_log``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import (
    AggregateType,
    EventEnvelope,
    compute_hash_self,
)
from app.services.sync.eventlog.append_service import AppendService, AppendStatus

logger = logging.getLogger(__name__)

# Nil UUID used as branch_id for org-level events (drugs, categories) that are
# not scoped to a specific branch.
_ORG_LEVEL_BRANCH = uuid.UUID(int=0)


def _generate_event_id() -> str:
    """26-char server-generated event ID (passes EventEnvelope length validation)."""
    return uuid.uuid4().hex[:26].upper()


class ServerEventEmitter:
    """Emit reference-data events from server-side API endpoints.

    Stateless — every method is a classmethod.
    """

    @classmethod
    async def emit(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        event_type: str,
        aggregate_type: AggregateType,
        aggregate_id: uuid.UUID,
        payload: Dict[str, Any],
        authored_by: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        dependencies: Optional[List[str]] = None,
    ) -> None:
        """Append a single server-emitted event to ``event_log``.

        Acquires the per-org advisory lock so this emission serializes
        correctly with any concurrent client pushes.  Exceptions are
        caught and logged; the caller's primary write is never rolled back
        by a failed emit.
        """
        try:
            await cls._emit_inner(
                db=db,
                org_id=org_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                authored_by=authored_by,
                branch_id=branch_id or _ORG_LEVEL_BRANCH,
                dependencies=dependencies or [],
            )
        except Exception:
            logger.exception(
                "ServerEventEmitter: failed to emit %s for aggregate %s "
                "(org=%s) — primary write was not rolled back",
                event_type,
                aggregate_id,
                org_id,
            )

    @classmethod
    async def _emit_inner(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        event_type: str,
        aggregate_type: AggregateType,
        aggregate_id: uuid.UUID,
        payload: Dict[str, Any],
        authored_by: uuid.UUID,
        branch_id: uuid.UUID,
        dependencies: List[str],
    ) -> None:
        now = datetime.now(timezone.utc)
        event_id = _generate_event_id()

        # Acquire the per-org advisory lock (transaction-scoped, released at commit).
        org_id_str = str(org_id)
        await AppendService._acquire_org_lock(db, org_id_str)
        tail_hash, tail_seq = await AppendService._load_log_tail(db, org_id_str)

        # Build a placeholder envelope to compute hash_self against the real tail.
        placeholder = EventEnvelope(
            event_id=event_id,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            event_type=event_type,
            schema_version=1,
            payload=payload,
            dependencies=dependencies,
            authored_at=now,
            authored_by=authored_by,
            branch_id=branch_id,
            org_id=org_id,
            hash_self="0" * 64,
        )
        real_hash = compute_hash_self(placeholder, tail_hash)

        envelope = EventEnvelope(
            event_id=event_id,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            event_type=event_type,
            schema_version=1,
            payload=payload,
            dependencies=dependencies,
            authored_at=now,
            authored_by=authored_by,
            branch_id=branch_id,
            org_id=org_id,
            hash_self=real_hash,
        )

        result = await AppendService._append_one(
            db,
            org_id_str,
            envelope,
            current_tail_hash=tail_hash,
            current_tail_seq=tail_seq,
        )

        if result.status not in (AppendStatus.APPENDED, AppendStatus.ALREADY_APPENDED):
            raise RuntimeError(
                f"ServerEventEmitter: append returned unexpected status "
                f"{result.status} for {event_type}/{aggregate_id}"
            )

        logger.debug(
            "ServerEventEmitter: emitted %s for %s (seq=%s)",
            event_type,
            aggregate_id,
            result.seq,
        )
