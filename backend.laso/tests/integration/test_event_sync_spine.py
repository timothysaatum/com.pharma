"""
Integration tests — event-sourced sync spine (Phase 1a).

Covers the end-to-end push/pull loop through the router + append
service + CustomerProjector. Requires a real Postgres backend because
the spine relies on JSONB casts, ARRAY columns, and
``pg_advisory_xact_lock`` — SQLite does not implement any of those.

The ``event_log``, ``pending_projections`` and ``aggregate_snapshots``
tables live only in the alembic migration (no ORM models), so the
``event_sync_tables`` fixture creates them inline for the test DB.
"""

from __future__ import annotations

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

# Import the projectors package so CustomerProjector registers itself.
import app.services.sync.eventlog.projectors  # noqa: F401


pytestmark = pytest.mark.asyncio


def _requires_postgres():
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip(
            "event-sourced spine requires a real Postgres backend "
            "(set TEST_DATABASE_URL to a postgresql+asyncpg URL)"
        )


@pytest_asyncio.fixture
async def event_sync_tables(db: AsyncSession):
    """Create the event-sourced spine tables. Mirrors the alembic
    migration ``e1f2a3b4c5d6`` — kept minimal (no COMMENTs / INCLUDE
    indexes) for the test DB.
    """
    _requires_postgres()

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
            UNIQUE (org_id, seq)
        )
    """))
    await db.execute(text("""
        CREATE INDEX ix_event_log_pull_cursor ON event_log (org_id, seq)
    """))
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
    await db.commit()

    yield

    # Teardown: drop in reverse (the outer conftest already drops all
    # ORM tables, so this only handles the raw-SQL ones).
    await db.execute(text("DROP TABLE IF EXISTS pending_projections CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS aggregate_snapshots CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


# ── envelope helpers ─────────────────────────────────────────────────────────

def _new_ulid() -> str:
    """Test-only ULID generator. Real ULID monotonicity is enforced by
    the client's clock; here we just need 26-char lex-orderable ids.
    """
    from time import time

    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_customer_created_envelope(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    customer_id: uuid.UUID | None = None,
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    phone: str = "0501234567",
    hash_prev: str = GENESIS_HASH,
) -> EventEnvelope:
    envelope = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=customer_id or uuid.uuid4(),
        aggregate_type=AggregateType.CUSTOMER,
        event_type="customer_created",
        schema_version=1,
        payload={
            "organization_id": str(org_id),
            "customer_type": "registered",
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "loyalty_points": 0,
            "loyalty_tier": "bronze",
        },
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,  # placeholder — overwritten below
    )
    envelope.hash_self = compute_hash_self(envelope, hash_prev)
    return envelope


# ── tests ────────────────────────────────────────────────────────────────────

class TestEventSyncSpine:
    """End-to-end coverage for the append/router/projector loop."""

    async def test_push_creates_customer_and_pull_returns_it(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _existing_customer = setup_test_data

        envelope = _make_customer_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert len(results) == 1
        r = results[0]
        assert r.status == EventStatus.ACCEPTED, r
        assert r.seq == 1
        assert r.received_at is not None

        # Read model was written.
        row = (
            await db.execute(
                text(
                    "SELECT first_name, last_name, phone "
                    "FROM customers WHERE id = :id"
                ),
                {"id": str(envelope.aggregate_id)},
            )
        ).first()
        assert row is not None
        assert row[0] == "Ada"
        assert row[1] == "Lovelace"
        assert row[2] == "0501234567"

        # Event landed in event_log with hash_prev=GENESIS_HASH.
        log_row = (
            await db.execute(
                text(
                    "SELECT seq, hash_prev, hash_self "
                    "FROM event_log WHERE event_id = :eid"
                ),
                {"eid": envelope.event_id},
            )
        ).first()
        assert log_row is not None
        assert log_row[0] == 1
        assert log_row[1] == GENESIS_HASH
        assert log_row[2] == envelope.hash_self

    async def test_pull_returns_events_ordered_by_seq(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _c = setup_test_data

        # Push three envelopes, chained.
        e1 = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            first_name="One",
        )
        e2 = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            first_name="Two", hash_prev=e1.hash_self,
        )
        e3 = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            first_name="Three", hash_prev=e2.hash_self,
        )
        results = await EventRouter.process_batch(db, org.id, [e1, e2, e3])
        await db.commit()
        assert all(r.status == EventStatus.ACCEPTED for r in results), results

        rows = (
            await db.execute(
                text(
                    "SELECT event_id, seq, hash_prev "
                    "FROM event_log WHERE org_id = :org_id ORDER BY seq"
                ),
                {"org_id": str(org.id)},
            )
        ).fetchall()
        assert [r[0] for r in rows] == [e1.event_id, e2.event_id, e3.event_id]
        assert [r[1] for r in rows] == [1, 2, 3]
        # Chain: e1.hash_prev=GENESIS, e2.hash_prev=e1.hash_self, e3.hash_prev=e2.hash_self
        assert rows[0][2] == GENESIS_HASH
        assert rows[1][2] == e1.hash_self
        assert rows[2][2] == e2.hash_self

    async def test_duplicate_event_is_idempotent(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _c = setup_test_data
        envelope = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        first = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()
        assert first[0].status == EventStatus.ACCEPTED
        assert first[0].seq == 1

        # Same envelope pushed again — should be recognized as
        # already-appended and NOT create a duplicate customer row or
        # advance seq.
        second = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()
        assert second[0].status == EventStatus.ACCEPTED
        assert second[0].seq == 1

        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM event_log WHERE org_id = :org"),
                {"org": str(org.id)},
            )
        ).scalar_one()
        assert count == 1

        cust_count = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM customers WHERE id = :id"
                ),
                {"id": str(envelope.aggregate_id)},
            )
        ).scalar_one()
        assert cust_count == 1

    async def test_hash_mismatch_is_rejected_permanently(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _c = setup_test_data
        envelope = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        # Tamper with hash_self after computation.
        envelope.hash_self = "0" * 64  # not the real hash

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "hash_mismatch"

        # Nothing landed in event_log.
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM event_log WHERE org_id = :org"),
                {"org": str(org.id)},
            )
        ).scalar_one()
        assert count == 0

    async def test_update_before_create_defers_until_create_lands(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _c = setup_test_data

        customer_id = uuid.uuid4()
        create_env = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            customer_id=customer_id, first_name="Ada",
        )

        # Build an update event that arrives BEFORE the create. It
        # declares create_env.event_id as a dependency, so the router
        # should park it in pending_projections.
        update_env = EventEnvelope(
            event_id=_new_ulid(),
            aggregate_id=customer_id,
            aggregate_type=AggregateType.CUSTOMER,
            event_type="customer_updated",
            schema_version=1,
            payload={"first_name": "Augusta"},
            dependencies=[create_env.event_id],
            authored_at=datetime.now(timezone.utc),
            authored_by=user.id,
            branch_id=branch.id,
            org_id=org.id,
            hash_self="0" * 64,
        )
        update_env.hash_self = compute_hash_self(update_env, GENESIS_HASH)

        # Push only the update — dep is missing.
        r1 = await EventRouter.process_batch(db, org.id, [update_env])
        await db.commit()
        assert r1[0].status == EventStatus.ACCEPTED_DEFERRED
        assert r1[0].pending_on == [create_env.event_id]

        # Customer row was NOT written (projector never ran).
        row = (
            await db.execute(
                text("SELECT 1 FROM customers WHERE id = :id"),
                {"id": str(customer_id)},
            )
        ).first()
        assert row is None

        # Now push the create. The dep-check landing should drain the
        # parked update in the same call.
        # (The create's hash_prev is the update's hash_self because the
        # update was appended first.)
        create_env2 = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            customer_id=customer_id, first_name="Ada",
            hash_prev=update_env.hash_self,
        )
        # Preserve create_env.event_id for the dep to match.
        create_env2.event_id = create_env.event_id
        create_env2.hash_self = compute_hash_self(create_env2, update_env.hash_self)

        r2 = await EventRouter.process_batch(db, org.id, [create_env2])
        await db.commit()
        assert r2[0].status == EventStatus.ACCEPTED

        # Customer row now exists AND has the update applied (first_name=Augusta).
        row = (
            await db.execute(
                text(
                    "SELECT first_name FROM customers WHERE id = :id"
                ),
                {"id": str(customer_id)},
            )
        ).first()
        assert row is not None
        assert row[0] == "Augusta"

        # pending_projections row was drained.
        pending_count = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM pending_projections "
                    "WHERE org_id = :org"
                ),
                {"org": str(org.id)},
            )
        ).scalar_one()
        assert pending_count == 0

    async def test_invalid_customer_type_rejected_permanent(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _c = setup_test_data
        envelope = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        envelope.payload["customer_type"] = "not_a_real_type"
        envelope.hash_self = compute_hash_self(envelope, GENESIS_HASH)

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "invalid_customer_type"

        # No event_log write.
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM event_log WHERE org_id = :org"),
                {"org": str(org.id)},
            )
        ).scalar_one()
        assert count == 0

    async def test_org_scope_violation_rejected_permanent(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _c = setup_test_data

        envelope = _make_customer_created_envelope(
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        # Point the payload at a different org than the envelope.
        envelope.payload["organization_id"] = str(uuid.uuid4())
        envelope.hash_self = compute_hash_self(envelope, GENESIS_HASH)

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "org_scope_violation"
