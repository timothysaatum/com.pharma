"""
Event Router — per-event orchestration.

Coordinates the append-service, dependency queue, and projector dispatch
for a batch of events. Returns per-event wire verdicts
(:class:`~app.schemas.event_envelope.EventPushResult`) that the push
endpoint hands back to the client.

Per-event flow (ADR 0006 / 0007):

1. Look up projector for ``envelope.aggregate_type``. Missing projector →
   ``REJECTED_PERMANENT`` (client bug).
2. Check ``envelope.dependencies`` against the event_log. A dep is
   *resolved* only when the referenced event has been APPENDED **and**
   its projector has already run (i.e. it is not sitting in
   ``pending_projections``). This mirrors the projector contract: the
   projector reads read-model state, so "dep is present" means the
   read model reflects it.
3. If any dep unresolved → append the event and park it in
   ``pending_projections``; return ``ACCEPTED_DEFERRED``.
4. Deps resolved → run ``projector.validate``. On:

   - ``REJECTED_PERMANENT`` → do **not** append; return the verdict.
   - ``DEFERRED`` (projector saw something the coarse dep check
     missed) → append + park; return ``ACCEPTED_DEFERRED``.
   - ``OK`` → append + apply; return ``ACCEPTED``.

5. On any successful apply, drain the pending queue: any parked event
   whose ``unresolved_deps`` list contained this ``event_id`` is
   re-evaluated. The drain is a transitive closure — applying event A
   may unblock B, which unblocks C.

6. Poison-event quarantine (Phase 1.10): events that exceed
   ``MAX_EVALUATION_ATTEMPTS`` deferred evaluations, receive
   ``REJECTED_PERMANENT`` during drain, or trigger repeated apply
   exceptions are moved to ``event_dead_letter`` and removed from
   ``pending_projections``. The ``event_log`` row is preserved for audit.

Batch semantics: one envelope's failure never blocks the next envelope.
Each envelope is processed independently; the results list preserves
the input order.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import (
    EventEnvelope,
    EventPushResult,
    EventStatus,
)
from app.services.sync.eventlog.append_service import (
    AppendResult,
    AppendService,
    AppendStatus,
)
from app.services.sync.eventlog.projector import (
    Projector,
    ProjectorRegistry,
    ProjectorStatus,
)

logger = logging.getLogger(__name__)

# Events that remain in pending_projections beyond this many evaluations are
# moved to event_dead_letter. Set conservatively: normal deferred chains
# resolve in 1-3 hops; 25 gives ample headroom for long dependency chains
# arriving out-of-order without burying genuine poison events indefinitely.
MAX_EVALUATION_ATTEMPTS = 25


class EventRouter:
    """Stateless orchestrator. Every method is a classmethod."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    async def process_batch(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        envelopes: Sequence[EventEnvelope],
    ) -> List[EventPushResult]:
        """Process a batch of envelopes for a single organization.

        Serialization: acquires the per-org advisory lock once for the
        whole batch. The lock is transaction-scoped, so a caller-owned
        commit or rollback releases it.
        """
        if not envelopes:
            return []

        await AppendService._acquire_org_lock(db, org_id)
        tail_hash, tail_seq = await AppendService._load_log_tail(db, org_id)

        results: List[EventPushResult] = []
        for envelope in envelopes:
            # Nested transaction per event so an unexpected projector
            # exception rolls back only that event's writes — the
            # advisory lock (held on the outer transaction) survives.
            try:
                async with db.begin_nested():
                    push_result, append_result = await cls._process_one(
                        db, org_id, envelope, tail_hash, tail_seq
                    )
            except Exception:
                logger.exception(
                    "Sync: unhandled error processing event %s (org %s); "
                    "rolling back savepoint and marking REJECTED_TRANSIENT.",
                    envelope.event_id,
                    org_id,
                )
                results.append(
                    EventPushResult(
                        event_id=envelope.event_id,
                        status=EventStatus.REJECTED_TRANSIENT,
                        error_code="internal_error",
                        error_message=(
                            "Server encountered an unexpected error while "
                            "processing this event; safe to retry."
                        ),
                    )
                )
                continue

            results.append(push_result)
            if append_result is not None and append_result.status == AppendStatus.APPENDED:
                # Advance in-memory tail so subsequent events chain onto
                # this one within the same batch.
                tail_hash = envelope.hash_self
                assert append_result.seq is not None
                tail_seq = append_result.seq

        return results

    # ------------------------------------------------------------------
    # Per-event processing
    # ------------------------------------------------------------------

    @classmethod
    async def _process_one(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        envelope: EventEnvelope,
        current_tail_hash: str,
        current_tail_seq: int,
    ) -> tuple[EventPushResult, AppendResult | None]:
        # 1. Projector lookup.
        projector = ProjectorRegistry.get(envelope.aggregate_type)
        if projector is None:
            return (
                EventPushResult(
                    event_id=envelope.event_id,
                    status=EventStatus.REJECTED_PERMANENT,
                    error_code="no_projector",
                    error_message=(
                        f"No projector registered for aggregate_type="
                        f"{envelope.aggregate_type}"
                    ),
                ),
                None,
            )

        # 2. Coarse dependency check against event_log.
        unresolved = await cls._check_dependencies(
            db, org_id, envelope.dependencies
        )

        if unresolved:
            return await cls._append_and_defer(
                db,
                org_id,
                envelope,
                current_tail_hash,
                current_tail_seq,
                unresolved_deps=unresolved,
            )

        # 3. Projector-level validation (business rules).
        verdict = await projector.validate(envelope, db)

        if verdict.status == ProjectorStatus.REJECTED_PERMANENT:
            return (
                EventPushResult(
                    event_id=envelope.event_id,
                    status=EventStatus.REJECTED_PERMANENT,
                    error_code=verdict.error_code,
                    error_message=verdict.error_message,
                ),
                None,
            )

        if verdict.status == ProjectorStatus.DEFERRED:
            return await cls._append_and_defer(
                db,
                org_id,
                envelope,
                current_tail_hash,
                current_tail_seq,
                unresolved_deps=verdict.unresolved_deps or [],
            )

        # 4. OK — append + apply.
        append = await AppendService._append_one(
            db, org_id, envelope, current_tail_hash, current_tail_seq
        )

        if append.status == AppendStatus.HASH_MISMATCH:
            return (
                EventPushResult(
                    event_id=envelope.event_id,
                    status=EventStatus.REJECTED_PERMANENT,
                    error_code="hash_mismatch",
                    error_message=(
                        f"Client-supplied hash_self did not match server "
                        f"re-computation. expected={append.expected_hash_self}"
                    ),
                ),
                append,
            )

        if append.status == AppendStatus.APPENDED:
            # Let projector.apply raise on unexpected errors — the
            # outer savepoint in process_batch will roll back this
            # event's writes and mark it REJECTED_TRANSIENT. Catching
            # here would leave the savepoint in an aborted state and
            # every subsequent DB call in the batch would fail with
            # "current transaction is aborted".
            await projector.apply(envelope, db)

            # 5. Drain the pending queue — this apply may unblock parked
            #    events (transitive closure).
            await cls._drain_pending(db, org_id, seed_event_id=envelope.event_id)

        # ALREADY_APPENDED path: nothing to apply (previous accept ran
        # the projector). Report as ACCEPTED so the client can advance
        # its outbox.
        return (
            EventPushResult(
                event_id=envelope.event_id,
                status=EventStatus.ACCEPTED,
                seq=append.seq,
                received_at=append.received_at,
            ),
            append,
        )

    # ------------------------------------------------------------------
    # Dependency check
    # ------------------------------------------------------------------

    @staticmethod
    async def _check_dependencies(
        db: AsyncSession,
        org_id: uuid.UUID,
        declared_deps: List[str],
    ) -> List[str]:
        """Return the subset of ``declared_deps`` that are NOT resolved.

        A dep is resolved iff its event_id is in ``event_log`` AND is
        NOT in ``pending_projections`` (i.e. it has been projected).
        """
        if not declared_deps:
            return []

        rows = await db.execute(
            text(
                """
                SELECT el.event_id
                  FROM event_log el
                 WHERE el.org_id = :org_id
                   AND el.event_id = ANY(:deps)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pending_projections pp
                        WHERE pp.org_id = el.org_id
                          AND pp.event_id = el.event_id
                   )
                """
            ),
            {"org_id": org_id, "deps": declared_deps},
        )
        resolved = {row[0] for row in rows.fetchall()}
        return [dep for dep in declared_deps if dep not in resolved]

    # ------------------------------------------------------------------
    # Append + park in pending_projections
    # ------------------------------------------------------------------

    @classmethod
    async def _append_and_defer(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        envelope: EventEnvelope,
        current_tail_hash: str,
        current_tail_seq: int,
        unresolved_deps: List[str],
    ) -> tuple[EventPushResult, AppendResult | None]:
        append = await AppendService._append_one(
            db, org_id, envelope, current_tail_hash, current_tail_seq
        )
        if append.status == AppendStatus.HASH_MISMATCH:
            return (
                EventPushResult(
                    event_id=envelope.event_id,
                    status=EventStatus.REJECTED_PERMANENT,
                    error_code="hash_mismatch",
                    error_message=(
                        f"Client-supplied hash_self did not match server "
                        f"re-computation. expected={append.expected_hash_self}"
                    ),
                ),
                append,
            )

        aggregate_type_value = (
            envelope.aggregate_type.value
            if hasattr(envelope.aggregate_type, "value")
            else envelope.aggregate_type
        )
        pending_row = (
            await db.execute(
                text(
                    """
                    INSERT INTO pending_projections (
                        org_id, event_id, aggregate_type,
                        unresolved_deps, first_deferred_at, last_evaluated_at,
                        evaluation_attempts
                    ) VALUES (
                        :org_id, :event_id, :aggregate_type,
                        :unresolved_deps, NOW(), NOW(), 1
                    )
                    ON CONFLICT (org_id, event_id) DO UPDATE SET
                        unresolved_deps = EXCLUDED.unresolved_deps,
                        last_evaluated_at = NOW(),
                        evaluation_attempts = pending_projections.evaluation_attempts + 1
                    RETURNING evaluation_attempts, first_deferred_at
                    """
                ),
                {
                    "org_id": org_id,
                    "event_id": envelope.event_id,
                    "aggregate_type": aggregate_type_value,
                    "unresolved_deps": unresolved_deps,
                },
            )
        ).first()

        if pending_row and pending_row[0] >= MAX_EVALUATION_ATTEMPTS:
            event_type_str = (
                envelope.event_type.value
                if hasattr(envelope.event_type, "value")
                else str(envelope.event_type)
            )
            await cls._quarantine(
                db=db,
                org_id=org_id,
                event_id=envelope.event_id,
                aggregate_type=aggregate_type_value,
                event_type=event_type_str,
                evaluation_attempts=pending_row[0],
                reason="max_attempts_deferred",
                error_code="deferred_too_many_times",
                error_message=(
                    f"Event deferred {pending_row[0]} times without resolution; "
                    f"still waiting on deps: {unresolved_deps}"
                ),
                first_deferred_at=pending_row[1],
            )
            return (
                EventPushResult(
                    event_id=envelope.event_id,
                    status=EventStatus.REJECTED_PERMANENT,
                    error_code="quarantined",
                    error_message=(
                        f"Event quarantined after {pending_row[0]} deferred "
                        f"evaluations. Dependencies could not be resolved."
                    ),
                ),
                append,
            )

        return (
            EventPushResult(
                event_id=envelope.event_id,
                status=EventStatus.ACCEPTED_DEFERRED,
                seq=append.seq,
                received_at=append.received_at,
                pending_on=unresolved_deps,
            ),
            append,
        )

    # ------------------------------------------------------------------
    # Drain pending_projections
    # ------------------------------------------------------------------

    @classmethod
    async def _drain_pending(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        seed_event_id: str,
    ) -> None:
        """Re-evaluate parked events after ``seed_event_id`` was applied.

        Transitive: applying a parked event may unblock further parked
        events. Loops until a full pass makes no progress.
        """
        newly_resolved: List[str] = [seed_event_id]

        while newly_resolved:
            # Find pending rows whose unresolved_deps intersect with the
            # newly-resolved set. Join event_log to get event_type for
            # quarantine records.
            candidate_rows = (
                await db.execute(
                    text(
                        """
                        SELECT pp.event_id, pp.aggregate_type, pp.unresolved_deps,
                               el.event_type, pp.first_deferred_at,
                               pp.evaluation_attempts
                          FROM pending_projections pp
                          JOIN event_log el
                            ON el.org_id = pp.org_id
                           AND el.event_id = pp.event_id
                         WHERE pp.org_id = :org_id
                           AND pp.unresolved_deps && :newly_resolved
                        """
                    ),
                    {
                        "org_id": org_id,
                        "newly_resolved": newly_resolved,
                    },
                )
            ).fetchall()

            newly_resolved = []
            for row in candidate_rows:
                (
                    event_id,
                    agg_type,
                    deps,
                    event_type,
                    first_deferred_at,
                    current_attempts,
                ) = row[0], row[1], list(row[2]), row[3], row[4], row[5]

                # Re-check deps: some may have landed via this drain
                # iteration; some may still be missing.
                remaining = await cls._check_dependencies(db, org_id, deps)
                if remaining:
                    # Still blocked — bump attempts and check quarantine threshold.
                    bump_row = (
                        await db.execute(
                            text(
                                """
                                UPDATE pending_projections
                                   SET unresolved_deps = :remaining,
                                       last_evaluated_at = NOW(),
                                       evaluation_attempts =
                                           evaluation_attempts + 1
                                 WHERE org_id = :org_id
                                   AND event_id = :event_id
                                RETURNING evaluation_attempts, first_deferred_at
                                """
                            ),
                            {
                                "org_id": org_id,
                                "event_id": event_id,
                                "remaining": remaining,
                            },
                        )
                    ).first()
                    if bump_row and bump_row[0] >= MAX_EVALUATION_ATTEMPTS:
                        await cls._quarantine(
                            db=db,
                            org_id=org_id,
                            event_id=event_id,
                            aggregate_type=agg_type,
                            event_type=event_type,
                            evaluation_attempts=bump_row[0],
                            reason="max_attempts_deferred",
                            error_code="deferred_too_many_times",
                            error_message=(
                                f"Event deferred {bump_row[0]} times without "
                                f"resolution; still waiting on: {remaining}"
                            ),
                            first_deferred_at=bump_row[1],
                        )
                    continue

                # Deps resolved — re-hydrate envelope from event_log and
                # dispatch to projector.
                projector = ProjectorRegistry.get(agg_type)
                if projector is None:
                    logger.error(
                        "Sync: pending event %s (org %s) has no projector "
                        "for aggregate_type=%s; leaving parked.",
                        event_id,
                        org_id,
                        agg_type,
                    )
                    continue

                envelope = await cls._load_envelope(db, org_id, event_id)
                if envelope is None:
                    logger.error(
                        "Sync: pending event %s (org %s) missing from "
                        "event_log; removing stale pending row.",
                        event_id,
                        org_id,
                    )
                    await cls._delete_pending(db, org_id, event_id)
                    continue

                dispatched = await cls._dispatch_pending(
                    db,
                    org_id,
                    projector,
                    envelope,
                    pending_attempts=current_attempts,
                    first_deferred_at=first_deferred_at,
                )
                if dispatched:
                    await cls._delete_pending(db, org_id, event_id)
                    newly_resolved.append(event_id)

    @classmethod
    async def _dispatch_pending(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        projector: Projector,
        envelope: EventEnvelope,
        pending_attempts: int = 0,
        first_deferred_at=None,
    ) -> bool:
        """Run validate + apply on a parked event whose deps are now
        resolved. Returns True if the projector ran successfully and the
        pending row can be deleted.
        """
        agg_type_str = (
            envelope.aggregate_type.value
            if hasattr(envelope.aggregate_type, "value")
            else str(envelope.aggregate_type)
        )
        event_type_str = (
            envelope.event_type.value
            if hasattr(envelope.event_type, "value")
            else str(envelope.event_type)
        )

        verdict = await projector.validate(envelope, db)
        if verdict.status == ProjectorStatus.REJECTED_PERMANENT:
            # The event is durable in event_log but its projector now
            # rejects it. This is a projector-logic change or a
            # data-shape violation. Quarantine immediately — a permanent
            # rejection will never self-heal.
            logger.warning(
                "Sync: parked event %s (org %s) REJECTED_PERMANENT during "
                "drain (attempts=%d): %s",
                envelope.event_id,
                org_id,
                pending_attempts,
                verdict.error_message,
            )
            await cls._quarantine(
                db=db,
                org_id=org_id,
                event_id=envelope.event_id,
                aggregate_type=agg_type_str,
                event_type=event_type_str,
                evaluation_attempts=pending_attempts,
                reason="rejected_permanent",
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                first_deferred_at=first_deferred_at,
            )
            return False

        if verdict.status == ProjectorStatus.DEFERRED:
            # Projector still says deferred despite our coarse check.
            # Update the pending row and keep it parked.
            bump_row = (
                await db.execute(
                    text(
                        """
                        UPDATE pending_projections
                           SET unresolved_deps = :deps,
                               last_evaluated_at = NOW(),
                               evaluation_attempts = evaluation_attempts + 1
                         WHERE org_id = :org_id
                           AND event_id = :event_id
                        RETURNING evaluation_attempts, first_deferred_at
                        """
                    ),
                    {
                        "org_id": org_id,
                        "event_id": envelope.event_id,
                        "deps": verdict.unresolved_deps or [],
                    },
                )
            ).first()
            if bump_row and bump_row[0] >= MAX_EVALUATION_ATTEMPTS:
                await cls._quarantine(
                    db=db,
                    org_id=org_id,
                    event_id=envelope.event_id,
                    aggregate_type=agg_type_str,
                    event_type=event_type_str,
                    evaluation_attempts=bump_row[0],
                    reason="max_attempts_deferred",
                    error_code="deferred_too_many_times",
                    error_message=(
                        f"Projector still DEFERRED after {bump_row[0]} attempts; "
                        f"deps: {verdict.unresolved_deps}"
                    ),
                    first_deferred_at=bump_row[1] or first_deferred_at,
                )
            return False

        # Wrap apply in a nested savepoint so an unexpected DB exception
        # rolls back only the apply writes and leaves the outer transaction
        # valid for subsequent quarantine/bump SQL.
        try:
            async with db.begin_nested():
                await projector.apply(envelope, db)
        except Exception as exc:
            logger.exception(
                "Sync: projector.apply raised on drain for parked event "
                "%s (org %s); bumping attempts.",
                envelope.event_id,
                org_id,
            )
            bump_row = (
                await db.execute(
                    text(
                        """
                        UPDATE pending_projections
                           SET evaluation_attempts = evaluation_attempts + 1,
                               last_evaluated_at = NOW()
                         WHERE org_id = :org_id
                           AND event_id = :event_id
                        RETURNING evaluation_attempts, first_deferred_at
                        """
                    ),
                    {"org_id": org_id, "event_id": envelope.event_id},
                )
            ).first()
            if bump_row and bump_row[0] >= MAX_EVALUATION_ATTEMPTS:
                await cls._quarantine(
                    db=db,
                    org_id=org_id,
                    event_id=envelope.event_id,
                    aggregate_type=agg_type_str,
                    event_type=event_type_str,
                    evaluation_attempts=bump_row[0],
                    reason="apply_exception",
                    error_code="apply_exception",
                    error_message=str(exc),
                    first_deferred_at=bump_row[1] or first_deferred_at,
                )
            return False
        return True

    # ------------------------------------------------------------------
    # Quarantine helper
    # ------------------------------------------------------------------

    @classmethod
    async def _quarantine(
        cls,
        db: AsyncSession,
        org_id: uuid.UUID,
        event_id: str,
        aggregate_type: str,
        event_type: str,
        evaluation_attempts: int,
        reason: str,
        error_code: Optional[str],
        error_message: Optional[str],
        first_deferred_at,
    ) -> None:
        """Move a poison event from pending_projections to event_dead_letter."""
        await db.execute(
            text(
                """
                INSERT INTO event_dead_letter (
                    org_id, event_id, aggregate_type, event_type,
                    evaluation_attempts, quarantine_reason,
                    error_code, error_message, first_deferred_at
                ) VALUES (
                    :org_id, :event_id, :aggregate_type, :event_type,
                    :evaluation_attempts, :reason,
                    :error_code, :error_message, :first_deferred_at
                )
                ON CONFLICT (org_id, event_id) DO NOTHING
                """
            ),
            {
                "org_id": org_id,
                "event_id": event_id,
                "aggregate_type": aggregate_type,
                "event_type": event_type,
                "evaluation_attempts": evaluation_attempts,
                "reason": reason,
                "error_code": error_code,
                "error_message": error_message,
                "first_deferred_at": first_deferred_at,
            },
        )
        await cls._delete_pending(db, org_id, event_id)
        logger.warning(
            "Sync: event %s (org %s) quarantined — reason=%s attempts=%d",
            event_id,
            org_id,
            reason,
            evaluation_attempts,
        )

    # ------------------------------------------------------------------
    # Small SQL helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _delete_pending(
        db: AsyncSession, org_id: uuid.UUID, event_id: str
    ) -> None:
        await db.execute(
            text(
                """
                DELETE FROM pending_projections
                 WHERE org_id = :org_id AND event_id = :event_id
                """
            ),
            {"org_id": org_id, "event_id": event_id},
        )

    @staticmethod
    async def _load_envelope(
        db: AsyncSession, org_id: uuid.UUID, event_id: str
    ) -> EventEnvelope | None:
        row = (
            await db.execute(
                text(
                    """
                    SELECT event_id, org_id, seq, aggregate_id,
                           aggregate_type, event_type, schema_version,
                           payload, dependencies, authored_at,
                           authored_by, branch_id, hash_self, hash_prev,
                           received_at
                      FROM event_log
                     WHERE org_id = :org_id AND event_id = :event_id
                    """
                ),
                {"org_id": org_id, "event_id": event_id},
            )
        ).first()
        if row is None:
            return None
        return EventEnvelope.model_validate(
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
