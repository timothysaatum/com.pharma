"""
Append Service — hash-chain verification, seq assignment, idempotent inserts.

Owns the write path into ``event_log``. Callers are the event router
(normal push flow) and the pull endpoint (read-only, does not append —
listed here only because both live in this module).

Per ADR 0007:
    - ``event_id`` is a client-generated ULID; ``(org_id, event_id)`` is
      the idempotency key. Duplicate submissions of the same event
      return the previously-assigned ``seq`` and are treated as
      accepted.
    - ``hash_prev`` for each event is the ``hash_self`` of the log tail
      at insert-time. GENESIS_HASH for the org's first event.
    - Client-supplied ``hash_self`` is re-computed server-side against
      the real ``hash_prev`` and must match — any mismatch is a client
      bug and yields REJECTED_PERMANENT.

Per-org monotonic ``seq`` assignment uses a Postgres advisory lock
keyed on ``org_id`` so a concurrent push from another branch cannot
interleave and duplicate a seq value.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import (
    GENESIS_HASH,
    EventEnvelope,
    compute_hash_self,
)

logger = logging.getLogger(__name__)


class AppendStatus(str, Enum):
    """Outcome of a single append attempt."""

    APPENDED = "appended"
    """Event was newly written to ``event_log``."""

    ALREADY_APPENDED = "already_appended"
    """Same ``event_id`` was already in ``event_log`` — idempotent."""

    HASH_MISMATCH = "hash_mismatch"
    """Client-supplied ``hash_self`` did not match server re-computation
    against the real ``hash_prev``. Envelope is malformed or tampered.
    """


@dataclass(slots=True)
class AppendResult:
    """Per-event append outcome. ``seq`` and ``received_at`` are the
    server-assigned values (or the previously-assigned ones on
    ALREADY_APPENDED).
    """

    event_id: str
    status: AppendStatus
    seq: Optional[int]
    received_at: Optional[datetime]
    hash_prev: Optional[str]
    expected_hash_self: Optional[str] = None
    """Populated on HASH_MISMATCH so the caller can surface the diff."""


class HashChainMismatch(Exception):
    """Raised only by internal helpers; the public API returns an
    AppendResult with status=HASH_MISMATCH instead of raising.
    """


class AppendService:
    """Stateless service — every method is a classmethod. Instances are
    unnecessary.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    async def append_batch(
        cls,
        db: AsyncSession,
        org_id: str | str,
        envelopes: List[EventEnvelope],
    ) -> List[AppendResult]:
        """Append a batch of envelopes for a single organization.

        Events are processed in the order supplied. The caller is
        responsible for ordering (client ships FIFO by ULID; see ADR
        0008). Each event either APPENDs, is recognized as an idempotent
        replay, or fails hash verification — in every case the next
        event proceeds, so one client-bug event does not block the rest
        of the batch.

        Uses a Postgres transaction-scoped advisory lock keyed on
        ``org_id`` so concurrent pushes to the same organization
        serialize their seq assignment.
        """
        if not envelopes:
            return []

        org_id = str(org_id)
        await cls._acquire_org_lock(db, org_id)

        tail_hash, tail_seq = await cls._load_log_tail(db, org_id)
        results: List[AppendResult] = []

        for envelope in envelopes:
            result = await cls._append_one(
                db,
                org_id,
                envelope,
                current_tail_hash=tail_hash,
                current_tail_seq=tail_seq,
            )
            results.append(result)
            if result.status == AppendStatus.APPENDED:
                assert result.seq is not None
                # Advance the in-memory tail so subsequent events in this
                # batch chain onto this event (rather than re-reading the
                # tail from the DB each iteration).
                tail_hash = envelope.hash_self
                tail_seq = result.seq

        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    async def _acquire_org_lock(db: AsyncSession, org_id: str) -> None:
        """Take a Postgres transaction-level advisory lock keyed on
        ``org_id``. Released automatically at commit or rollback.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(org_id)},
        )

    @staticmethod
    async def _load_log_tail(
        db: AsyncSession, org_id: str
    ) -> Tuple[str, int]:
        """Read the current tail of ``event_log`` for this org. Returns
        ``(hash_self_of_tail, seq_of_tail)``. For an empty log, returns
        ``(GENESIS_HASH, 0)`` — the caller's next event chains against
        GENESIS_HASH and takes seq=1.
        """
        row = (
            await db.execute(
                text(
                    """
                    SELECT hash_self, seq
                      FROM event_log
                     WHERE org_id = :org_id
                     ORDER BY seq DESC
                     LIMIT 1
                    """
                ),
                {"org_id": str(org_id)},
            )
        ).first()
        if row is None:
            return GENESIS_HASH, 0
        return row[0], row[1]

    @classmethod
    async def _append_one(
        cls,
        db: AsyncSession,
        org_id: str,
        envelope: EventEnvelope,
        current_tail_hash: str,
        current_tail_seq: int,
    ) -> AppendResult:
        # 1. Idempotency: if this event_id is already in the log, return
        #    its existing seq/received_at without re-appending.
        existing = (
            await db.execute(
                text(
                    """
                    SELECT seq, received_at, hash_prev
                      FROM event_log
                     WHERE org_id = :org_id AND event_id = :event_id
                    """
                ),
                {"org_id": str(org_id), "event_id": envelope.event_id},
            )
        ).first()
        if existing is not None:
            return AppendResult(
                event_id=envelope.event_id,
                status=AppendStatus.ALREADY_APPENDED,
                seq=existing[0],
                received_at=existing[1],
                hash_prev=existing[2],
            )

        # 2. Hash verification: re-compute the expected hash_self from
        #    the envelope + the real hash_prev (which the client couldn't
        #    know at outbox-write time). Reject if the client's hash_self
        #    doesn't match.
        expected_hash = compute_hash_self(envelope, current_tail_hash)
        if expected_hash != envelope.hash_self:
            logger.warning(
                "Sync: hash_self mismatch for event %s in org %s "
                "(client=%s expected=%s)",
                envelope.event_id,
                org_id,
                envelope.hash_self,
                expected_hash,
            )
            return AppendResult(
                event_id=envelope.event_id,
                status=AppendStatus.HASH_MISMATCH,
                seq=None,
                received_at=None,
                hash_prev=current_tail_hash,
                expected_hash_self=expected_hash,
            )

        # 3. Assign seq = tail + 1 and INSERT.
        seq = current_tail_seq + 1
        received_at = datetime.now(timezone.utc)
        try:
            await db.execute(
                text(
                    """
                    INSERT INTO event_log (
                        event_id, org_id, seq,
                        aggregate_id, aggregate_type, event_type,
                        schema_version, payload, dependencies,
                        authored_at, authored_by, branch_id,
                        hash_self, hash_prev, received_at
                    ) VALUES (
                        :event_id, :org_id, :seq,
                        :aggregate_id, :aggregate_type, :event_type,
                        :schema_version, CAST(:payload AS JSONB), :dependencies,
                        :authored_at, :authored_by, :branch_id,
                        :hash_self, :hash_prev, :received_at
                    )
                    """
                ),
                {
                    "event_id": envelope.event_id,
                    "org_id": str(org_id),
                    "seq": seq,
                    "aggregate_id": str(envelope.aggregate_id),
                    "aggregate_type": (
                        envelope.aggregate_type.value
                        if hasattr(envelope.aggregate_type, "value")
                        else envelope.aggregate_type
                    ),
                    "event_type": envelope.event_type,
                    "schema_version": envelope.schema_version,
                    "payload": _payload_json(envelope.payload),
                    "dependencies": envelope.dependencies,
                    "authored_at": envelope.authored_at,
                    "authored_by": str(envelope.authored_by) if envelope.authored_by is not None else None,
                    "branch_id": str(envelope.branch_id),
                    "hash_self": envelope.hash_self,
                    "hash_prev": current_tail_hash,
                    "received_at": received_at,
                },
            )
        except IntegrityError:
            # A concurrent inserter beat us to this event_id. Because we
            # hold the advisory lock, this should be impossible unless
            # the lock was released early or the same envelope was
            # supplied twice within the same batch. Re-read and return
            # ALREADY_APPENDED — the previously-committed row wins.
            await db.rollback()
            await cls._acquire_org_lock(db, org_id)
            row = (
                await db.execute(
                    text(
                        """
                        SELECT seq, received_at, hash_prev
                          FROM event_log
                         WHERE org_id = :org_id AND event_id = :event_id
                        """
                    ),
                    {"org_id": str(org_id), "event_id": envelope.event_id},
                )
            ).first()
            if row is None:
                raise
            return AppendResult(
                event_id=envelope.event_id,
                status=AppendStatus.ALREADY_APPENDED,
                seq=row[0],
                received_at=row[1],
                hash_prev=row[2],
            )

        return AppendResult(
            event_id=envelope.event_id,
            status=AppendStatus.APPENDED,
            seq=seq,
            received_at=received_at,
            hash_prev=current_tail_hash,
        )


def _payload_json(payload: dict) -> str:
    """Serialize the payload for the JSONB cast. We use plain json here
    (not canonical_json) because Postgres normalizes the stored form —
    only the hash chain requires canonical bytes, and the hash was
    already computed pre-insert.
    """
    import json

    return json.dumps(payload)
