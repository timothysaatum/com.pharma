"""
Integration tests — PrescriptionProjector + StockProjector (Phase 1c).

Requires a real Postgres backend (same constraints as other spine tests).
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

import app.services.sync.eventlog.projectors  # noqa: F401 — registers all projectors


pytestmark = pytest.mark.asyncio


def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip("Phase 1c integration tests require a real Postgres backend")


# ── shared fixtures ───────────────────────────────────────────────────────────


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
    await db.commit()

    yield

    await db.execute(text("DROP TABLE IF EXISTS pending_projections CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS aggregate_snapshots CASCADE"))
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


@pytest_asyncio.fixture
async def stock_test_data(db: AsyncSession, setup_test_data):
    """Adds a DrugBatch + BranchInventory row for stock adjustment tests."""
    org, branch, user, drugs, _customer = setup_test_data
    drug = drugs[0]

    batch_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO drug_batches (
                id, branch_id, drug_id, batch_number,
                quantity, remaining_quantity, expiry_date,
                version_id, sync_version, sync_status,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:bid AS UUID), CAST(:did AS UUID),
                :bnum, :qty, :qty, :exp,
                1, 1, 'synced', NOW(), NOW()
            )
        """),
        {"id": str(batch_id), "bid": str(branch.id), "did": str(drug.id),
         "bnum": "LOT-STOCK-001", "qty": 80, "exp": date(2028, 6, 30)},
    )
    await db.execute(
        text("""
            INSERT INTO branch_inventory (
                id, branch_id, drug_id, quantity, reserved_quantity,
                version_id, sync_version, sync_status, created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:bid AS UUID), CAST(:did AS UUID),
                :qty, 0, 1, 1, 'synced', NOW(), NOW()
            )
        """),
        {"id": str(uuid.uuid4()), "bid": str(branch.id), "did": str(drug.id), "qty": 80},
    )
    await db.commit()
    return org, branch, user, drug, batch_id


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
    dependencies: list[str] | None = None,
    hash_prev: str = GENESIS_HASH,
) -> EventEnvelope:
    env = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        event_type=event_type,
        schema_version=1,
        payload=payload,
        dependencies=dependencies or [],
        authored_at=datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    env.hash_self = compute_hash_self(env, hash_prev)
    return env


# ── Prescription tests ────────────────────────────────────────────────────────


class TestPrescriptionProjector:

    def _rx_payload(self, org_id, branch_id, customer_id) -> dict:
        return {
            "organization_id": str(org_id),
            "branch_id": str(branch_id),
            "customer_id": str(customer_id),
            "prescription_number": "RX-2026-001",
            "prescriber_name": "Dr. Kwame Mensah",
            "prescriber_license": "MED-12345",
            "issue_date": "2026-01-01",
            "expiry_date": "2026-07-01",
            "medications": [{"drug_name": "Amoxicillin", "dosage": "500mg", "quantity": 10}],
            "refills_allowed": 2,
            "refills_remaining": 2,
            "status": "active",
        }

    async def test_prescription_created_projects(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, customer = setup_test_data
        rx_id = uuid.uuid4()

        env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_created",
            payload=self._rx_payload(org.id, branch.id, customer.id),
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()

        assert results[0].status == EventStatus.ACCEPTED, results[0]

        row = (
            await db.execute(
                text("SELECT prescription_number, status, refills_remaining"
                     " FROM prescriptions WHERE id = :id"),
                {"id": str(rx_id)},
            )
        ).fetchone()
        assert row is not None
        assert row.prescription_number == "RX-2026-001"
        assert row.status == "active"
        assert row.refills_remaining == 2

    async def test_prescription_created_idempotent(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, customer = setup_test_data
        rx_id = uuid.uuid4()
        env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_created",
            payload=self._rx_payload(org.id, branch.id, customer.id),
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        r1 = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()
        assert r1[0].status == EventStatus.ACCEPTED

        r2 = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()
        assert r2[0].status == EventStatus.ACCEPTED
        assert r2[0].seq == r1[0].seq

        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM prescriptions WHERE id = :id"),
                {"id": str(rx_id)},
            )
        ).scalar()
        assert count == 1

    async def test_prescription_updated(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, customer = setup_test_data
        rx_id = uuid.uuid4()

        created_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_created",
            payload=self._rx_payload(org.id, branch.id, customer.id),
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        r1 = await EventRouter.process_batch(db, org.id, [created_env])
        await db.commit()
        assert r1[0].status == EventStatus.ACCEPTED

        updated_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_updated",
            payload={"notes": "Take with food"},
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            dependencies=[created_env.event_id],
            hash_prev=created_env.hash_self,
        )
        r2 = await EventRouter.process_batch(db, org.id, [updated_env])
        await db.commit()
        assert r2[0].status == EventStatus.ACCEPTED

        row = (
            await db.execute(
                text("SELECT notes FROM prescriptions WHERE id = :id"),
                {"id": str(rx_id)},
            )
        ).fetchone()
        assert row.notes == "Take with food"

    async def test_prescription_cancelled(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, customer = setup_test_data
        rx_id = uuid.uuid4()

        created_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_created",
            payload=self._rx_payload(org.id, branch.id, customer.id),
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        await EventRouter.process_batch(db, org.id, [created_env])
        await db.commit()

        cancel_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_cancelled",
            payload={},
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            dependencies=[created_env.event_id],
            hash_prev=created_env.hash_self,
        )
        r = await EventRouter.process_batch(db, org.id, [cancel_env])
        await db.commit()
        assert r[0].status == EventStatus.ACCEPTED

        row = (
            await db.execute(
                text("SELECT status FROM prescriptions WHERE id = :id"),
                {"id": str(rx_id)},
            )
        ).fetchone()
        assert row.status == "cancelled"

    async def test_prescription_refill_used_decrements_and_fills(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, customer = setup_test_data
        rx_id = uuid.uuid4()
        payload = self._rx_payload(org.id, branch.id, customer.id)
        payload["refills_allowed"] = 1
        payload["refills_remaining"] = 1

        created_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_created",
            payload=payload,
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        await EventRouter.process_batch(db, org.id, [created_env])
        await db.commit()

        refill_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_refill_used",
            payload={"refill_date": "2026-03-01", "verified_by": str(user.id)},
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            dependencies=[created_env.event_id],
            hash_prev=created_env.hash_self,
        )
        r = await EventRouter.process_batch(db, org.id, [refill_env])
        await db.commit()
        assert r[0].status == EventStatus.ACCEPTED

        row = (
            await db.execute(
                text("SELECT refills_remaining, status FROM prescriptions WHERE id = :id"),
                {"id": str(rx_id)},
            )
        ).fetchone()
        assert row.refills_remaining == 0
        assert row.status == "filled"

    async def test_prescription_invalid_status_rejected_permanently(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        org, branch, user, _drugs, customer = setup_test_data
        payload = self._rx_payload(org.id, branch.id, customer.id)
        payload["status"] = "waiting"  # invalid

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_created",
            payload=payload,
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert "status" in (results[0].error_code or "")

    async def test_prescription_update_before_create_deferred(
        self, db: AsyncSession, setup_test_data, event_sync_tables
    ):
        """An update whose dependency (create) hasn't landed yet is deferred."""
        org, branch, user, _drugs, _c = setup_test_data
        rx_id = uuid.uuid4()
        fake_create_event_id = _new_ulid()

        updated_env = _make_env(
            aggregate_id=rx_id,
            aggregate_type=AggregateType.PRESCRIPTION,
            event_type="prescription_updated",
            payload={"notes": "Take with food"},
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
            dependencies=[fake_create_event_id],
        )
        results = await EventRouter.process_batch(db, org.id, [updated_env])
        await db.commit()

        assert results[0].status == EventStatus.ACCEPTED_DEFERRED


# ── StockProjector tests ──────────────────────────────────────────────────────


class TestStockProjector:

    def _adj_payload(self, org_id, branch_id, drug_id, adjusted_by,
                     batch_id, adj_type="damage", qty_change=-5,
                     prev_qty=80, new_qty=75) -> dict:
        batch_before = 80
        batch_after = 75
        return {
            "organization_id": str(org_id),
            "branch_id": str(branch_id),
            "drug_id": str(drug_id),
            "adjusted_by": str(adjusted_by),
            "adjustment_type": adj_type,
            "quantity_change": qty_change,
            "previous_quantity": prev_qty,
            "new_quantity": new_qty,
            "reason": "Test adjustment",
            "batch_changes": [
                {
                    "batch_id": str(batch_id),
                    "quantity_change": qty_change,
                    "batch_before": batch_before,
                    "batch_after": batch_after,
                }
            ],
        }

    async def test_stock_adjusted_deducts_and_audits(
        self, db: AsyncSession, stock_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id = stock_test_data

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.STOCK,
            event_type="stock_adjusted",
            payload=self._adj_payload(org.id, branch.id, drug.id, user.id, batch_id),
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()

        assert results[0].status == EventStatus.ACCEPTED, results[0]

        # stock_adjustment record written
        adj_row = (
            await db.execute(
                text("SELECT adjustment_type, quantity_change"
                     " FROM stock_adjustments WHERE id = :id"),
                {"id": str(env.aggregate_id)},
            )
        ).fetchone()
        assert adj_row is not None
        assert adj_row.adjustment_type == "damage"
        assert adj_row.quantity_change == -5

        # branch_inventory updated
        inv_row = (
            await db.execute(
                text("SELECT quantity FROM branch_inventory"
                     " WHERE branch_id = :bid AND drug_id = :did"),
                {"bid": str(branch.id), "did": str(drug.id)},
            )
        ).fetchone()
        assert inv_row.quantity == 75

        # drug_batch updated
        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :id"),
                {"id": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 75

        # inventory_movement written
        mov_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM inventory_movements"
                     " WHERE source_id = :sid AND movement_type = 'damage'"),
                {"sid": str(env.aggregate_id)},
            )
        ).scalar()
        assert mov_count == 1

    async def test_stock_adjusted_idempotent(
        self, db: AsyncSession, stock_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id = stock_test_data
        adj_id = uuid.uuid4()

        env = _make_env(
            aggregate_id=adj_id,
            aggregate_type=AggregateType.STOCK,
            event_type="stock_adjusted",
            payload=self._adj_payload(org.id, branch.id, drug.id, user.id, batch_id),
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        r1 = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()
        assert r1[0].status == EventStatus.ACCEPTED

        r2 = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()
        assert r2[0].status == EventStatus.ACCEPTED
        assert r2[0].seq == r1[0].seq

        # branch_inventory should NOT be double-deducted
        inv_row = (
            await db.execute(
                text("SELECT quantity FROM branch_inventory"
                     " WHERE branch_id = :bid AND drug_id = :did"),
                {"bid": str(branch.id), "did": str(drug.id)},
            )
        ).fetchone()
        assert inv_row.quantity == 75  # 80 - 5, not 70

    async def test_stock_correction_without_batch_changes(
        self, db: AsyncSession, stock_test_data, event_sync_tables
    ):
        """Correction with no batch_changes still writes one inventory_movement."""
        org, branch, user, drug, _batch_id = stock_test_data

        payload = {
            "organization_id": str(org.id),
            "branch_id": str(branch.id),
            "drug_id": str(drug.id),
            "adjusted_by": str(user.id),
            "adjustment_type": "correction",
            "quantity_change": 10,
            "previous_quantity": 80,
            "new_quantity": 90,
            "reason": "Count reconciliation",
            "batch_changes": [],
        }
        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.STOCK,
            event_type="stock_adjusted",
            payload=payload,
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()
        assert results[0].status == EventStatus.ACCEPTED

        inv_row = (
            await db.execute(
                text("SELECT quantity FROM branch_inventory"
                     " WHERE branch_id = :bid AND drug_id = :did"),
                {"bid": str(branch.id), "did": str(drug.id)},
            )
        ).fetchone()
        assert inv_row.quantity == 90

        mov_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM inventory_movements"
                     " WHERE source_id = :sid"),
                {"sid": str(env.aggregate_id)},
            )
        ).scalar()
        assert mov_count == 1

    async def test_stock_purchase_receipt_creates_new_batch(
        self, db: AsyncSession, stock_test_data, event_sync_tables
    ):
        org, branch, user, drug, _existing_batch = stock_test_data
        new_batch_id = uuid.uuid4()

        payload = {
            "organization_id": str(org.id),
            "branch_id": str(branch.id),
            "drug_id": str(drug.id),
            "adjusted_by": str(user.id),
            "adjustment_type": "purchase_receipt",
            "quantity_change": 50,
            "previous_quantity": 80,
            "new_quantity": 130,
            "reason": "PO received",
            "batch_changes": [
                {
                    "batch_id": str(new_batch_id),
                    "quantity_change": 50,
                    "batch_before": 0,
                    "batch_after": 50,
                }
            ],
            "new_batch": {
                "batch_id": str(new_batch_id),
                "batch_number": "LOT-2026-NEW",
                "expiry_date": "2029-12-31",
                "quantity": 50,
                "cost_price": "28.00",
            },
        }
        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.STOCK,
            event_type="stock_adjusted",
            payload=payload,
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )

        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()
        assert results[0].status == EventStatus.ACCEPTED

        # New batch was created
        nb_row = (
            await db.execute(
                text("SELECT batch_number, remaining_quantity"
                     " FROM drug_batches WHERE id = :id"),
                {"id": str(new_batch_id)},
            )
        ).fetchone()
        assert nb_row is not None
        assert nb_row.batch_number == "LOT-2026-NEW"
        assert nb_row.remaining_quantity == 50

        # Inventory updated to 130
        inv_row = (
            await db.execute(
                text("SELECT quantity FROM branch_inventory"
                     " WHERE branch_id = :bid AND drug_id = :did"),
                {"bid": str(branch.id), "did": str(drug.id)},
            )
        ).fetchone()
        assert inv_row.quantity == 130

    async def test_stock_invalid_adjustment_type_rejected_permanently(
        self, db: AsyncSession, stock_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id = stock_test_data

        payload = self._adj_payload(org.id, branch.id, drug.id, user.id, batch_id)
        payload["adjustment_type"] = "mystery"

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.STOCK,
            event_type="stock_adjusted",
            payload=payload,
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert "adjustment_type" in (results[0].error_code or "")

    async def test_stock_org_scope_violation_rejected_permanently(
        self, db: AsyncSession, stock_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id = stock_test_data

        payload = self._adj_payload(org.id, branch.id, drug.id, user.id, batch_id)
        payload["organization_id"] = str(uuid.uuid4())  # wrong org

        env = _make_env(
            aggregate_id=uuid.uuid4(),
            aggregate_type=AggregateType.STOCK,
            event_type="stock_adjusted",
            payload=payload,
            org_id=org.id, branch_id=branch.id, authored_by=user.id,
        )
        results = await EventRouter.process_batch(db, org.id, [env])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert "org_scope" in (results[0].error_code or "")
