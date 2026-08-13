"""
Integration tests — Phase 1.10 poison-event quarantine (event_dead_letter).

Covers all three quarantine paths in EventRouter:
  1. max_attempts_deferred — _append_and_defer bumps evaluation_attempts to
     MAX_EVALUATION_ATTEMPTS; event is moved to event_dead_letter and the
     push returns REJECTED_PERMANENT / error_code="quarantined".
  2. rejected_permanent — projector returns REJECTED_PERMANENT during the
     _drain_pending loop; event quarantined immediately regardless of
     attempt count.
  3. max_attempts_still_blocked — _drain_pending tries to re-evaluate a
     pending event but the dep is still unresolved; after bumping to MAX
     the row moves to dead_letter.

Confirmed invariants:
  - pending_projections row is GONE after quarantine.
  - event_log row is PRESERVED (audit trail).
  - Second drain pass on a quarantined event is a no-op (not in pending).

Requires a real Postgres backend (advisory locks, ARRAY &&, JSONB).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import (
    GENESIS_HASH,
    AggregateType,
    EventEnvelope,
    EventStatus,
    compute_hash_self,
)
from app.services.sync.eventlog import EventRouter
from app.services.sync.eventlog.router import MAX_EVALUATION_ATTEMPTS

import app.services.sync.eventlog.projectors  # noqa: F401 — registers all projectors


pytestmark = pytest.mark.asyncio


# ── helpers ───────────────────────────────────────────────────────────────────

def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip(
            "event dead-letter tests require a real Postgres backend "
            "(set DATABASE_URL to a postgresql+asyncpg URL)"
        )


def _new_ulid() -> str:
    from time import time
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_customer_envelope(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    customer_id: uuid.UUID | None = None,
    hash_prev: str = GENESIS_HASH,
    dependencies: list[str] | None = None,
    event_type: str = "customer_created",
) -> EventEnvelope:
    envelope = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=customer_id or uuid.uuid4(),
        aggregate_type=AggregateType.CUSTOMER,
        event_type=event_type,
        schema_version=1,
        payload={
            "organization_id": str(org_id),
            "customer_type": "registered",
            "first_name": "Alice",
            "last_name": "Test",
            "phone": "0500000001",
            "loyalty_points": 0,
            "loyalty_tier": "bronze",
        },
        dependencies=dependencies or [],
        authored_at=datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    envelope.hash_self = compute_hash_self(envelope, hash_prev)
    return envelope


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def spine_tables(db: AsyncSession):
    """Create event_log, pending_projections, and event_dead_letter for the
    test DB. Mirrors alembic migrations e1f2a3b4c5d6 + f2a3b4c5d6e7.
    """
    _requires_postgres()

    await db.execute(text("DROP TABLE IF EXISTS event_dead_letter CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS pending_projections CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS aggregate_snapshots CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))

    await db.execute(text("""
        CREATE TABLE event_log (
            event_id TEXT NOT NULL,
            org_id UUID NOT NULL,
            seq BIGINT NOT NULL,
            aggregate_id UUID NOT NULL,
            aggregate_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            schema_version SMALLINT NOT NULL DEFAULT 1,
            payload JSONB NOT NULL,
            dependencies TEXT[] NOT NULL DEFAULT '{}',
            authored_at TIMESTAMPTZ NOT NULL,
            authored_by UUID NOT NULL,
            branch_id UUID NOT NULL,
            hash_self TEXT NOT NULL,
            hash_prev TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (org_id, event_id),
            UNIQUE (org_id, seq),
            CHECK (char_length(event_id) = 26),
            CHECK (char_length(hash_self) = 64 AND char_length(hash_prev) = 64)
        )
    """))
    await db.execute(text(
        "CREATE INDEX ix_event_log_pull_cursor ON event_log (org_id, seq)"
    ))
    await db.execute(text("""
        CREATE TABLE pending_projections (
            org_id UUID NOT NULL,
            event_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            unresolved_deps TEXT[] NOT NULL,
            first_deferred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            evaluation_attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (org_id, event_id),
            FOREIGN KEY (org_id, event_id)
                REFERENCES event_log (org_id, event_id) ON DELETE CASCADE
        )
    """))
    await db.execute(text("""
        CREATE INDEX ix_pending_projections_unresolved_gin
            ON pending_projections USING gin (unresolved_deps)
    """))
    await db.execute(text("""
        CREATE TABLE event_dead_letter (
            org_id UUID NOT NULL,
            event_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            evaluation_attempts INTEGER NOT NULL,
            quarantine_reason TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            first_deferred_at TIMESTAMPTZ,
            quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (org_id, event_id),
            FOREIGN KEY (org_id, event_id)
                REFERENCES event_log (org_id, event_id) ON DELETE CASCADE
        )
    """))
    await db.commit()

    yield

    await db.execute(text("DROP TABLE IF EXISTS event_dead_letter CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS pending_projections CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS aggregate_snapshots CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


# ── helper: insert an event directly into event_log + pending_projections ──

async def _insert_pending_event(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    unresolved_deps: list[str],
    evaluation_attempts: int,
    event_type: str = "customer_created",
    aggregate_type: str = "customer",
) -> str:
    """Bypass the router and write directly into spine tables. Used to
    set up poison-event scenarios that the router's validation would
    normally block.
    """
    event_id = _new_ulid()
    fake_hash = "a" * 64
    await db.execute(
        text("""
            INSERT INTO event_log (
                event_id, org_id, seq, aggregate_id, aggregate_type,
                event_type, schema_version, payload, dependencies,
                authored_at, authored_by, branch_id, hash_self, hash_prev
            ) VALUES (
                :event_id, :org_id,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM event_log WHERE org_id = :org_id),
                :aggregate_id, :aggregate_type, :event_type, 1,
                CAST(:payload AS JSONB), :deps,
                NOW(), :authored_by, :branch_id, :hash_self, :hash_prev
            )
        """),
        {
            "event_id": event_id,
            "org_id": org_id,
            "aggregate_id": str(uuid.uuid4()),
            "aggregate_type": aggregate_type,
            "event_type": event_type,
            "payload": json.dumps({"organization_id": str(org_id)}),
            "deps": unresolved_deps,
            "authored_by": str(authored_by),
            "branch_id": str(branch_id),
            "hash_self": fake_hash,
            "hash_prev": fake_hash,
        },
    )
    await db.execute(
        text("""
            INSERT INTO pending_projections
                (org_id, event_id, aggregate_type, unresolved_deps,
                 evaluation_attempts)
            VALUES (:org_id, :event_id, :agg_type, :deps, :attempts)
        """),
        {
            "org_id": org_id,
            "event_id": event_id,
            "agg_type": aggregate_type,
            "deps": unresolved_deps,
            "attempts": evaluation_attempts,
        },
    )
    return event_id


# ── tests ─────────────────────────────────────────────────────────────────────

class TestEventDeadLetter:
    """Poison-event quarantine — Phase 1.10."""

    async def test_max_attempts_via_append_quarantines(
        self, db: AsyncSession, setup_test_data, spine_tables
    ):
        """Pushing a deferred event for the MAX_EVALUATION_ATTEMPTS-th time
        moves it to event_dead_letter and returns REJECTED_PERMANENT.
        """
        org, branch, user, _drugs, _c = setup_test_data
        fake_dep = _new_ulid()  # never in event_log

        envelope = _make_customer_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            dependencies=[fake_dep],
        )

        # First push — event appended, parked with attempts=1.
        r1 = (await EventRouter.process_batch(db, org.id, [envelope]))[0]
        await db.commit()
        assert r1.status == EventStatus.ACCEPTED_DEFERRED

        # Manually set attempts to MAX-1 so next push tips it over.
        await db.execute(
            text(
                "UPDATE pending_projections SET evaluation_attempts = :n "
                "WHERE org_id = :org AND event_id = :eid"
            ),
            {"n": MAX_EVALUATION_ATTEMPTS - 1, "org": org.id, "eid": envelope.event_id},
        )
        await db.commit()

        # Second push — upsert bumps attempts to MAX → quarantine.
        r2 = (await EventRouter.process_batch(db, org.id, [envelope]))[0]
        await db.commit()

        assert r2.status == EventStatus.REJECTED_PERMANENT, r2
        assert r2.error_code == "quarantined"

        # Dead letter row exists.
        dl = (
            await db.execute(
                text(
                    "SELECT quarantine_reason, evaluation_attempts "
                    "FROM event_dead_letter WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": envelope.event_id},
            )
        ).first()
        assert dl is not None, "event_dead_letter row missing"
        assert dl[0] == "max_attempts_deferred"
        assert dl[1] == MAX_EVALUATION_ATTEMPTS

        # Pending row is gone.
        pending = (
            await db.execute(
                text(
                    "SELECT 1 FROM pending_projections "
                    "WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": envelope.event_id},
            )
        ).first()
        assert pending is None, "pending_projections row should be deleted after quarantine"

        # event_log row is preserved (audit trail).
        log = (
            await db.execute(
                text("SELECT 1 FROM event_log WHERE org_id = :org AND event_id = :eid"),
                {"org": org.id, "eid": envelope.event_id},
            )
        ).first()
        assert log is not None, "event_log row must be preserved after quarantine"

    async def test_rejected_permanent_during_drain_quarantines(
        self, db: AsyncSession, setup_test_data, spine_tables
    ):
        """When a parked event's deps are resolved but the projector then
        returns REJECTED_PERMANENT (data-shape violation), the event is
        quarantined immediately regardless of attempt count.
        """
        org, branch, user, _drugs, _c = setup_test_data

        # Push a valid anchor event (D) so it has an event_id we can use
        # as the seed for _drain_pending.
        anchor = _make_customer_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )
        r = (await EventRouter.process_batch(db, org.id, [anchor]))[0]
        await db.commit()
        assert r.status == EventStatus.ACCEPTED

        # Insert a poisoned event P with dep on anchor, using an unknown
        # event_type that CustomerProjector will REJECTED_PERMANENT.
        poison_id = await _insert_pending_event(
            db,
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            unresolved_deps=[anchor.event_id],
            evaluation_attempts=1,
            event_type="customer_poisoned",  # unknown type → REJECTED_PERMANENT
            aggregate_type="customer",
        )
        await db.commit()

        # Manually trigger the drain as if anchor just got applied.
        await EventRouter._drain_pending(db, org.id, seed_event_id=anchor.event_id)
        await db.commit()

        # Poison event quarantined with reason="rejected_permanent".
        dl = (
            await db.execute(
                text(
                    "SELECT quarantine_reason, error_code "
                    "FROM event_dead_letter WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": poison_id},
            )
        ).first()
        assert dl is not None, "event_dead_letter row missing after REJECTED_PERMANENT drain"
        assert dl[0] == "rejected_permanent"
        assert dl[1] == "unknown_event_type"

        # Not in pending.
        pending = (
            await db.execute(
                text(
                    "SELECT 1 FROM pending_projections "
                    "WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": poison_id},
            )
        ).first()
        assert pending is None

    async def test_max_attempts_still_blocked_during_drain_quarantines(
        self, db: AsyncSession, setup_test_data, spine_tables
    ):
        """A parked event with two deps — one newly resolved, one still
        missing — gets its attempts bumped on each drain pass. When
        attempts reaches MAX, the router quarantines it.
        """
        org, branch, user, _drugs, _c = setup_test_data

        # Push anchor event D (resolves one dep).
        anchor = _make_customer_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )
        r = (await EventRouter.process_batch(db, org.id, [anchor]))[0]
        await db.commit()
        assert r.status == EventStatus.ACCEPTED

        # Insert event B with two deps: anchor (now resolved) + a fake dep
        # that will never appear. Set attempts to MAX-1 so the next bump
        # tips it into quarantine.
        fake_dep = _new_ulid()
        stuck_id = await _insert_pending_event(
            db,
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            unresolved_deps=[anchor.event_id, fake_dep],
            evaluation_attempts=MAX_EVALUATION_ATTEMPTS - 1,
            aggregate_type="customer",
        )
        await db.commit()

        # Drain with seed=anchor.event_id. B is a candidate (deps include
        # anchor), but still blocked on fake_dep → bump to MAX → quarantine.
        await EventRouter._drain_pending(db, org.id, seed_event_id=anchor.event_id)
        await db.commit()

        dl = (
            await db.execute(
                text(
                    "SELECT quarantine_reason, evaluation_attempts "
                    "FROM event_dead_letter WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": stuck_id},
            )
        ).first()
        assert dl is not None, "event_dead_letter row missing for max-attempts still-blocked"
        assert dl[0] == "max_attempts_deferred"
        assert dl[1] == MAX_EVALUATION_ATTEMPTS

        pending = (
            await db.execute(
                text(
                    "SELECT 1 FROM pending_projections "
                    "WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": stuck_id},
            )
        ).first()
        assert pending is None

    async def test_quarantined_event_not_reevaluated_on_subsequent_drain(
        self, db: AsyncSession, setup_test_data, spine_tables
    ):
        """Once quarantined, an event is absent from pending_projections
        and is therefore invisible to subsequent drain passes.
        """
        org, branch, user, _drugs, _c = setup_test_data
        fake_dep = _new_ulid()

        # Push and park an event.
        envelope = _make_customer_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            dependencies=[fake_dep],
        )
        (await EventRouter.process_batch(db, org.id, [envelope]))[0]
        await db.commit()

        # Tip attempts to MAX-1 then push once more → quarantined.
        await db.execute(
            text(
                "UPDATE pending_projections SET evaluation_attempts = :n "
                "WHERE org_id = :org AND event_id = :eid"
            ),
            {"n": MAX_EVALUATION_ATTEMPTS - 1, "org": org.id, "eid": envelope.event_id},
        )
        await db.commit()
        r2 = (await EventRouter.process_batch(db, org.id, [envelope]))[0]
        await db.commit()
        assert r2.status == EventStatus.REJECTED_PERMANENT

        # Verify quarantine row exists.
        dl_before = (
            await db.execute(
                text(
                    "SELECT quarantined_at FROM event_dead_letter "
                    "WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": envelope.event_id},
            )
        ).first()
        assert dl_before is not None

        # Push a second unrelated event to trigger a drain pass.
        another = _make_customer_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            hash_prev=envelope.hash_self,
        )
        r3 = (await EventRouter.process_batch(db, org.id, [another]))[0]
        await db.commit()
        assert r3.status == EventStatus.ACCEPTED

        # Dead letter row is untouched (no re-quarantine via DO NOTHING).
        dl_after = (
            await db.execute(
                text(
                    "SELECT quarantined_at FROM event_dead_letter "
                    "WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": envelope.event_id},
            )
        ).first()
        assert dl_after is not None
        assert dl_after[0] == dl_before[0], "quarantined_at should not change on re-drain"

        # Still absent from pending.
        pending = (
            await db.execute(
                text(
                    "SELECT 1 FROM pending_projections "
                    "WHERE org_id = :org AND event_id = :eid"
                ),
                {"org": org.id, "eid": envelope.event_id},
            )
        ).first()
        assert pending is None
