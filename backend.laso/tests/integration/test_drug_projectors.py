"""
Integration tests — DrugProjector + DrugCategoryProjector.

Covers drug_created, drug_updated, drug_category_created,
drug_category_updated, and validation rejection paths through the full
EventRouter.process_batch → AppendService → Projector chain.

Requires a real Postgres backend (same constraints as
test_event_sync_spine.py — JSONB, advisory locks, CAST syntax).
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

import app.services.sync.eventlog.projectors  # noqa: F401  — registers all projectors


pytestmark = pytest.mark.asyncio


def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip(
            "DrugProjector integration tests require a real Postgres backend "
            "(set DATABASE_URL to a postgresql+asyncpg URL)"
        )


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def event_sync_tables(db: AsyncSession):
    """Mirror of the alembic migration — creates event spine tables."""
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

    await db.execute(text("DROP TABLE IF EXISTS pending_projections CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS aggregate_snapshots CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


# ── envelope helpers ──────────────────────────────────────────────────────────


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


# ── DrugProjector tests ───────────────────────────────────────────────────────


class TestDrugProjector:

    def _drug_payload(self, org_id: uuid.UUID, *, name: str = "Amoxicillin 500mg") -> dict:
        return {
            "organization_id": str(org_id),
            "name": name,
            "generic_name": "Amoxicillin",
            "brand_name": "Amoxil",
            "drug_type": "prescription",
            "dosage_form": "capsule",
            "strength": "500mg",
            "unit_price": 12.50,
            "cost_price": 8.00,
            "reorder_level": 20,
            "reorder_quantity": 100,
            "unit_of_measure": "unit",
            "requires_prescription": True,
            "is_active": True,
        }

    async def test_drug_created_inserts_row(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data
        drug_id = uuid.uuid4()

        env = _make_env(
            aggregate_id=drug_id,
            aggregate_type=AggregateType.DRUG,
            event_type="drug_created",
            payload=self._drug_payload(org.id),
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        assert results[0].status == EventStatus.ACCEPTED

        row = (await db.execute(
            text("SELECT name, drug_type, unit_price, sync_status FROM drugs WHERE id = CAST(:id AS UUID)"),
            {"id": str(drug_id)},
        )).mappings().first()

        assert row is not None
        assert row["name"] == "Amoxicillin 500mg"
        assert row["drug_type"] == "prescription"
        assert float(row["unit_price"]) == pytest.approx(12.50)
        assert row["sync_status"] == "synced"

    async def test_drug_created_idempotent(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data
        drug_id = uuid.uuid4()
        payload = self._drug_payload(org.id)

        env1 = _make_env(
            aggregate_id=drug_id,
            aggregate_type=AggregateType.DRUG,
            event_type="drug_created",
            payload=payload,
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )
        env2 = _make_env(
            aggregate_id=drug_id,
            aggregate_type=AggregateType.DRUG,
            event_type="drug_created",
            payload=payload,
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            hash_prev=env1.hash_self,
        )

        r1 = await EventRouter.process_batch(db, org.id, [env1])
        r2 = await EventRouter.process_batch(db, org.id, [env2])

        assert r1[0].status == EventStatus.ACCEPTED
        assert r2[0].status == EventStatus.ACCEPTED

        count = (await db.execute(
            text("SELECT COUNT(*) FROM drugs WHERE id = CAST(:id AS UUID)"),
            {"id": str(drug_id)},
        )).scalar_one()
        assert count == 1

    async def test_drug_updated_mutates_row(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data
        drug_id = uuid.uuid4()

        create_env = _make_env(
            aggregate_id=drug_id,
            aggregate_type=AggregateType.DRUG,
            event_type="drug_created",
            payload=self._drug_payload(org.id, name="Ibuprofen 200mg"),
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )
        await EventRouter.process_batch(db, org.id, [create_env])

        update_env = _make_env(
            aggregate_id=drug_id,
            aggregate_type=AggregateType.DRUG,
            event_type="drug_updated",
            payload={
                **self._drug_payload(org.id, name="Ibuprofen 400mg"),
                "unit_price": 18.00,
                "is_active": False,
            },
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            hash_prev=create_env.hash_self,
        )
        results = await EventRouter.process_batch(db, org.id, [update_env])
        assert results[0].status == EventStatus.ACCEPTED

        row = (await db.execute(
            text("SELECT name, unit_price, is_active, sync_status FROM drugs WHERE id = CAST(:id AS UUID)"),
            {"id": str(drug_id)},
        )).mappings().first()

        assert row["name"] == "Ibuprofen 400mg"
        assert float(row["unit_price"]) == pytest.approx(18.00)
        assert row["is_active"] is False
        assert row["sync_status"] == "synced"

    async def test_drug_validate_rejects_missing_name(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.DRUG,
            event_type="drug_created",
            payload={"organization_id": str(org.id)},  # no name
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "missing_name"

    async def test_drug_validate_rejects_missing_org(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.DRUG,
            event_type="drug_created",
            payload={"name": "Aspirin"},  # no organization_id
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "missing_org"


# ── DrugCategoryProjector tests ───────────────────────────────────────────────


class TestDrugCategoryProjector:

    def _cat_payload(self, org_id: uuid.UUID, *, name: str = "Antibiotics") -> dict:
        return {
            "organization_id": str(org_id),
            "name": name,
            "description": "Antimicrobial drugs",
            "level": 0,
        }

    async def test_drug_category_created_inserts_row(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data
        cat_id = uuid.uuid4()

        env = _make_env(
            aggregate_id=cat_id,
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_created",
            payload=self._cat_payload(org.id),
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        assert results[0].status == EventStatus.ACCEPTED

        row = (await db.execute(
            text("SELECT name, description, level, sync_status FROM drug_categories WHERE id = CAST(:id AS UUID)"),
            {"id": str(cat_id)},
        )).mappings().first()

        assert row is not None
        assert row["name"] == "Antibiotics"
        assert row["description"] == "Antimicrobial drugs"
        assert row["level"] == 0
        assert row["sync_status"] == "synced"

    async def test_drug_category_created_idempotent(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data
        cat_id = uuid.uuid4()
        payload = self._cat_payload(org.id)

        env1 = _make_env(
            aggregate_id=cat_id,
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_created",
            payload=payload,
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )
        env2 = _make_env(
            aggregate_id=cat_id,
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_created",
            payload=payload,
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            hash_prev=env1.hash_self,
        )

        r1 = await EventRouter.process_batch(db, org.id, [env1])
        r2 = await EventRouter.process_batch(db, org.id, [env2])

        assert r1[0].status == EventStatus.ACCEPTED
        assert r2[0].status == EventStatus.ACCEPTED

        count = (await db.execute(
            text("SELECT COUNT(*) FROM drug_categories WHERE id = CAST(:id AS UUID)"),
            {"id": str(cat_id)},
        )).scalar_one()
        assert count == 1

    async def test_drug_category_updated_mutates_row(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data
        cat_id = uuid.uuid4()

        create_env = _make_env(
            aggregate_id=cat_id,
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_created",
            payload=self._cat_payload(org.id, name="OTC Drugs"),
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )
        await EventRouter.process_batch(db, org.id, [create_env])

        update_env = _make_env(
            aggregate_id=cat_id,
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_updated",
            payload={
                **self._cat_payload(org.id, name="OTC & Supplements"),
                "description": "Over-the-counter and nutritional supplements",
                "level": 1,
            },
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            hash_prev=create_env.hash_self,
        )
        results = await EventRouter.process_batch(db, org.id, [update_env])
        assert results[0].status == EventStatus.ACCEPTED

        row = (await db.execute(
            text("SELECT name, description, level FROM drug_categories WHERE id = CAST(:id AS UUID)"),
            {"id": str(cat_id)},
        )).mappings().first()

        assert row["name"] == "OTC & Supplements"
        assert row["description"] == "Over-the-counter and nutritional supplements"
        assert row["level"] == 1

    async def test_drug_category_validate_rejects_missing_org(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_created",
            payload={"name": "Vitamins"},  # no organization_id
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "missing_org"

    async def test_drug_category_validate_rejects_missing_name(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, _customer = setup_test_data

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.DRUG_CATEGORY,
            event_type="drug_category_created",
            payload={"organization_id": str(org.id)},  # no name
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert results[0].error_code == "missing_name"
