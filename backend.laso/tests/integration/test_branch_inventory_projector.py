"""
Integration tests — BranchInventoryProjector.
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
import app.services.sync.eventlog.projectors  # registers all projectors

pytestmark = pytest.mark.asyncio

def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip("BranchInventoryProjector integration tests require a real Postgres backend")

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
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))

@pytest.fixture
def org_id() -> uuid.UUID:
    return uuid.uuid4()

@pytest.fixture
def branch_id() -> uuid.UUID:
    return uuid.uuid4()

@pytest.fixture
def drug_id() -> uuid.UUID:
    return uuid.uuid4()

@pytest.fixture
def inventory_id() -> uuid.UUID:
    return uuid.uuid4()

@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()

@pytest_asyncio.fixture
async def setup_data(
    db: AsyncSession,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    user_id: uuid.UUID,
    event_sync_tables: None,
):
    await db.execute(text("""
        INSERT INTO organizations (id, name, slug)
        VALUES (:org_id, 'Test Org', 'test-org')
    """), {"org_id": org_id})
    await db.execute(text("""
        INSERT INTO branches (id, organization_id, name)
        VALUES (:branch_id, :org_id, 'Test Branch')
    """), {"branch_id": branch_id, "org_id": org_id})
    await db.execute(text("""
        INSERT INTO users (id, organization_id, branch_id, email, password_hash, role)
        VALUES (:user_id, :org_id, :branch_id, 'user@test.com', 'xxx', 'cashier')
    """), {"user_id": user_id, "org_id": org_id, "branch_id": branch_id})
    await db.execute(text("""
        INSERT INTO drug_categories (id, organization_id, name)
        VALUES (:cat_id, :org_id, 'Cat')
    """), {"cat_id": uuid.uuid4(), "org_id": org_id})
    cat_id = (await db.execute(text("SELECT id FROM drug_categories WHERE organization_id = :org_id LIMIT 1"), {"org_id": org_id})).scalar_one()
    await db.execute(text("""
        INSERT INTO drugs (id, organization_id, category_id, name, generic_name, dosage_form, unit_price, is_active)
        VALUES (:drug_id, :org_id, :cat_id, 'Drug', 'Generic', 'Tablet', 10.0, true)
    """), {"drug_id": drug_id, "org_id": org_id, "cat_id": cat_id})
    await db.commit()

async def test_branch_inventory_projector(
    db: AsyncSession,
    setup_data: None,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    user_id: uuid.UUID,
    drug_id: uuid.UUID,
    inventory_id: uuid.UUID,
):
    router = EventRouter(db)
    
    # Create inventory metadata
    env = EventEnvelope(
        event_id="01HXXXXXXXINVCREATEDXXXXXX",
        aggregate_id=inventory_id,
        aggregate_type=AggregateType.BRANCH_INVENTORY,
        event_type="branch_inventory_created",
        schema_version=1,
        payload={
            "org_id": str(org_id),
            "branch_id": str(branch_id),
            "drug_id": str(drug_id),
            "shelf_location": "A1",
            "branch_selling_price": "12.0"
        },
        authored_at=datetime.now(timezone.utc),
        authored_by=user_id,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
        hash_prev=GENESIS_HASH,
    )
    env.hash_self = compute_hash_self(env.model_dump())
    resp = await router.process_batch(branch_id, [env])
    assert resp.results[0].status == EventStatus.ACCEPTED
    
    inv_row = (await db.execute(
        text("SELECT * FROM branch_inventory WHERE branch_id = :branch_id AND drug_id = :drug_id"),
        {"branch_id": branch_id, "drug_id": drug_id}
    )).fetchone()
    assert inv_row is not None
    assert inv_row.location == "A1"
    
    # Update inventory metadata
    update_env = EventEnvelope(
        event_id="01HXXXXXXXINVUPDATEDXXXXXX",
        aggregate_id=inventory_id,
        aggregate_type=AggregateType.BRANCH_INVENTORY,
        event_type="branch_inventory_updated",
        schema_version=1,
        payload={
            "org_id": str(org_id),
            "branch_id": str(branch_id),
            "drug_id": str(drug_id),
            "shelf_location": "B2",
            "branch_selling_price": "14.0"
        },
        authored_at=datetime.now(timezone.utc),
        authored_by=user_id,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
        hash_prev=env.hash_self,
    )
    update_env.hash_self = compute_hash_self(update_env.model_dump())
    resp2 = await router.process_batch(branch_id, [update_env])
    assert resp2.results[0].status == EventStatus.ACCEPTED
    
    inv_row2 = (await db.execute(
        text("SELECT * FROM branch_inventory WHERE branch_id = :branch_id AND drug_id = :drug_id"),
        {"branch_id": branch_id, "drug_id": drug_id}
    )).fetchone()
    assert inv_row2.location == "B2"
