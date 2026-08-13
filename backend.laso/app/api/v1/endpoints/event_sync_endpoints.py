"""
Event-Sourced Sync API — push / pull.

Mounts at ``/api/v1/sync`` (parent router). Exposes:

- ``POST /sync/events`` — client pushes a batch of envelopes; server
  returns per-event verdicts and the seq to resume pull from.
- ``GET  /sync/events`` — client polls for events with seq > cursor;
  server streams up to ``limit`` envelopes plus a ``has_more`` flag.

Both endpoints scope to ``current_user.organization_id`` — the JWT is
the sole source of truth for org isolation. Envelopes whose
``org_id`` disagrees are REJECTED_PERMANENT by the projector layer.

See ADR 0008 for the wire contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user, get_db
from app.models.user.user_model import User
from app.schemas.event_envelope import (
    MAX_PUSH_BATCH,
    EventEnvelope,
    EventPullResponse,
    EventPushRequest,
    EventPushResponse,
    EventStatus,
)
from app.services.sync.eventlog import EventRouter

# Importing the projectors package registers every concrete projector
# against ProjectorRegistry at import-time. Do NOT remove — without this
# side-effect import the registry is empty and every push returns
# ``no_projector``.
import app.services.sync.eventlog.projectors  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["Event-Sourced Sync"])


DEFAULT_PULL_LIMIT = 200
MAX_PULL_LIMIT = 500


def _user_can_use_branch(current_user: User, branch_id) -> bool:
    """Whether ``branch_id`` is in the user's assigned branches list.

    ``assigned_branches`` may be empty for org-admin users — treat those
    as having access to all branches in their org.
    """
    assigned = current_user.assigned_branches or []
    if not assigned:
        return True
    return str(branch_id) in {str(value) for value in assigned}


# ── POST /sync/events ────────────────────────────────────────────────────────

@router.post(
    "/events",
    response_model=EventPushResponse,
    summary="Push a batch of client-generated events",
    description=(
        "Client submits FIFO-ordered envelopes for a single organization. "
        "Server returns per-event verdicts (accepted / accepted_deferred / "
        "rejected_permanent / rejected_transient) — one bug event never "
        "blocks the rest of the batch."
    ),
)
async def push_events(
    request: EventPushRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EventPushResponse:
    if not _user_can_use_branch(current_user, request.branch_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    if len(request.events) > MAX_PUSH_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Batch of {len(request.events)} events exceeds the "
                f"per-request limit of {MAX_PUSH_BATCH}."
            ),
        )

    org_id = current_user.organization_id

    # Verify every envelope claims the caller's org. Mismatch is a
    # client bug or an outright attempt to write another org's log.
    for envelope in request.events:
        if str(envelope.org_id) != str(org_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Event {envelope.event_id} declares org_id "
                    f"{envelope.org_id} which does not match the "
                    f"authenticated user's org."
                ),
            )

    results = await EventRouter.process_batch(db, org_id, request.events)

    # Compute the seq the client should resume pull from. If any event
    # was accepted we advance to the max seq we just wrote; otherwise
    # we probe the current log tail.
    max_seq = 0
    for r in results:
        if (
            r.status in (EventStatus.ACCEPTED, EventStatus.ACCEPTED_DEFERRED)
            and r.seq is not None
            and r.seq > max_seq
        ):
            max_seq = r.seq

    if max_seq == 0:
        tail_seq = await _current_tail_seq(db, org_id)
        max_seq = tail_seq

    await db.commit()

    return EventPushResponse(
        server_clock=datetime.now(timezone.utc),
        results=results,
        next_pull_seq=max_seq,
    )


# ── GET /sync/events ─────────────────────────────────────────────────────────

@router.get(
    "/events",
    response_model=EventPullResponse,
    summary="Pull events since a seq cursor",
    description=(
        "Returns events with ``seq > after_seq`` for the caller's org, "
        "ordered by seq. ``has_more`` indicates whether another page is "
        "available; the client should re-poll with ``after_seq="
        "next_after_seq`` until ``has_more=false``."
    ),
)
async def pull_events(
    after_seq: int = Query(0, ge=0, description="Return events with seq > this value."),
    limit: int = Query(
        DEFAULT_PULL_LIMIT,
        ge=1,
        le=MAX_PULL_LIMIT,
        description="Max envelopes per page.",
    ),
    aggregate_types: Optional[List[str]] = Query(
        None,
        description=(
            "Optional filter — return only events whose aggregate_type "
            "is in this list."
        ),
    ),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EventPullResponse:
    org_id = current_user.organization_id

    # Fetch limit + 1 to detect whether more pages exist without a
    # second round-trip.
    fetch_limit = limit + 1

    if aggregate_types:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT event_id, org_id, seq, aggregate_id,
                           aggregate_type, event_type, schema_version,
                           payload, dependencies, authored_at,
                           authored_by, branch_id, hash_self, hash_prev,
                           received_at
                      FROM event_log
                     WHERE org_id = :org_id
                       AND seq > :after_seq
                       AND aggregate_type = ANY(:agg_types)
                     ORDER BY seq
                     LIMIT :fetch_limit
                    """
                ),
                {
                    "org_id": org_id,
                    "after_seq": after_seq,
                    "agg_types": aggregate_types,
                    "fetch_limit": fetch_limit,
                },
            )
        ).fetchall()
    else:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT event_id, org_id, seq, aggregate_id,
                           aggregate_type, event_type, schema_version,
                           payload, dependencies, authored_at,
                           authored_by, branch_id, hash_self, hash_prev,
                           received_at
                      FROM event_log
                     WHERE org_id = :org_id
                       AND seq > :after_seq
                     ORDER BY seq
                     LIMIT :fetch_limit
                    """
                ),
                {
                    "org_id": org_id,
                    "after_seq": after_seq,
                    "fetch_limit": fetch_limit,
                },
            )
        ).fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]

    envelopes: List[EventEnvelope] = [
        EventEnvelope.model_validate(
            {
                "event_id": row[0],
                "org_id": row[1],
                "seq": row[2],
                "aggregate_id": row[3],
                "aggregate_type": row[4],
                "event_type": row[5],
                "schema_version": row[6],
                "payload": row[7],
                "dependencies": list(row[8] or []),
                "authored_at": row[9],
                "authored_by": row[10],
                "branch_id": row[11],
                "hash_self": row[12],
                "hash_prev": row[13],
                "received_at": row[14],
            }
        )
        for row in page_rows
    ]

    next_after_seq = page_rows[-1][2] if page_rows else after_seq

    return EventPullResponse(
        server_clock=datetime.now(timezone.utc),
        events=envelopes,
        has_more=has_more,
        next_after_seq=next_after_seq,
    )


# ── helpers ──────────────────────────────────────────────────────────────────

async def _current_tail_seq(db: AsyncSession, org_id) -> int:
    row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(MAX(seq), 0)
                  FROM event_log
                 WHERE org_id = :org_id
                """
            ),
            {"org_id": org_id},
        )
    ).first()
    return int(row[0]) if row else 0
