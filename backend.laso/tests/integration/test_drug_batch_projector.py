"""
Integration tests — DrugBatchProjector.

Covers drug_batch_created and drug_batch_updated through the full
EventRouter → AppendService → DrugBatchProjector path.
Requires a real Postgres backend.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

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
import app.services.sync.eventlog.projectors  # noqa: F401

pytestmark = pytest.mark.asyncio


def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip("DrugBatchProjector integration tests require a real Postgres backend")


@pytest_asyncio.fixture
async def event_sync_tables(db: AsyncSession):
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
    await db.execute(text("CREATE INDEX ix_event_log_pull_cursor ON event_log (org_id, seq)"))
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
            FOREIGN KEY (org_id, event_id) REFERENCES event_log (org_id, event_id) ON DELETE CASCADE
        )
    """))
    await db.commit()
    yield
    await db.execute(text("DROP TABLE IF EXISTS pending_projections CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS aggregate_snapshots CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


def _new_ulid() -> str:
    from time import time
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_env(
    *,
    aggregate_id: uuid.UUID,
    aggregate_type: AggregateType,
    event_type: str,
    payload: dict,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    hash_prev: str = GENESIS_HASH,
) -> EventEnvelope:
    env = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        event_type=event_type,
        schema_version=1,
        payload=payload,
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    env.hash_self = compute_hash_self(env, hash_prev)
    return env


async def test_drug_batch_created_and_updated(
    db: AsyncSession,
    setup_test_data,
    event_sync_tables,
):
    org, branch, user, drugs, _ = setup_test_data
    drug = drugs[0]
    batch_id = uuid.uuid4()

    created_payload = {
        "org_id": str(org.id),
        "branch_id": str(branch.id),
        "drug_id": str(drug.id),
        "batch_number": "BATCH-001",
        "quantity": 100,
        "remaining_quantity": 100,
        "expiry_date": "2027-12-31",
        "cost_price": 5.0,
        "selling_price": 10.0,
        "received_date": date.today().isoformat(),
    }

    env1 = _make_env(
        aggregate_id=batch_id,
        aggregate_type=AggregateType.DRUG_BATCH,
        event_type="drug_batch_created",
        payload=created_payload,
        org_id=org.id,
        branch_id=branch.id,
        authored_by=user.id,
    )

    results = await EventRouter.process_batch(db, org.id, [env1])
    assert results[0].status == EventStatus.ACCEPTED

    # Verify batch inserted
    row = (await db.execute(
        text("SELECT batch_number, remaining_quantity FROM drug_batches WHERE id = :id"),
        {"id": str(batch_id)},
    )).mappings().first()
    assert row is not None
    assert row["batch_number"] == "BATCH-001"
    assert row["remaining_quantity"] == 100

    # Verify branch_inventory quantity updated
    inv_row = (await db.execute(
        text("SELECT quantity FROM branch_inventory WHERE branch_id = :b_id AND drug_id = :d_id"),
        {"b_id": str(branch.id), "d_id": str(drug.id)},
    )).mappings().first()
    assert inv_row is not None
    assert inv_row["quantity"] >= 100

    # Update batch
    updated_payload = {
        **created_payload,
        "remaining_quantity": 80,
    }
    env2 = _make_env(
        aggregate_id=batch_id,
        aggregate_type=AggregateType.DRUG_BATCH,
        event_type="drug_batch_updated",
        payload=updated_payload,
        org_id=org.id,
        branch_id=branch.id,
        authored_by=user.id,
        hash_prev=env1.hash_self,
    )

    results2 = await EventRouter.process_batch(db, org.id, [env2])
    assert results2[0].status == EventStatus.ACCEPTED

    # Verify updated quantity
    row2 = (await db.execute(
        text("SELECT remaining_quantity FROM drug_batches WHERE id = :id"),
        {"id": str(batch_id)},
    )).mappings().first()
    assert row2["remaining_quantity"] == 80
