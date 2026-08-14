"""
Integration tests — SaleProjector (Phase 1b).

Covers sale_created and sale_voided events through the full
EventRouter → AppendService → SaleProjector path.

Requires a real Postgres backend (same constraints as
test_event_sync_spine.py — JSONB, advisory locks, CAST syntax).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

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

# Registers CustomerProjector AND SaleProjector against the registry.
import app.services.sync.eventlog.projectors  # noqa: F401


pytestmark = pytest.mark.asyncio


def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip(
            "SaleProjector integration tests require a real Postgres backend "
            "(set TEST_DATABASE_URL to a postgresql+asyncpg URL)"
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


@pytest_asyncio.fixture
async def sale_test_data(db: AsyncSession, setup_test_data):
    """Extend the base test data with a DrugBatch and BranchInventory row.

    Returns (org, branch, user, drug, batch_id, batch_number, expiry_date)
    where batch_id / branch_inventory are ready for stock deduction.
    """
    org, branch, user, drugs, _customer = setup_test_data
    drug = drugs[0]

    batch_id = uuid.uuid4()
    batch_number = "LOT-TEST-001"
    expiry_date = date(2028, 12, 31)
    initial_qty = 100

    await db.execute(
        text("""
            INSERT INTO drug_batches (
                id, branch_id, drug_id, batch_number,
                quantity, remaining_quantity,
                expiry_date,
                version_id, sync_version, sync_status,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID),
                :batch_number,
                :qty, :qty,
                :expiry_date,
                1, 1, 'synced',
                NOW(), NOW()
            )
        """),
        {
            "id": str(batch_id),
            "branch_id": str(branch.id),
            "drug_id": str(drug.id),
            "batch_number": batch_number,
            "qty": initial_qty,
            "expiry_date": expiry_date,
        },
    )

    await db.execute(
        text("""
            INSERT INTO branch_inventory (
                id, branch_id, drug_id,
                quantity, reserved_quantity,
                version_id, sync_version, sync_status,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID),
                :qty, 0,
                1, 1, 'synced',
                NOW(), NOW()
            )
        """),
        {
            "id": str(uuid.uuid4()),
            "branch_id": str(branch.id),
            "drug_id": str(drug.id),
            "qty": initial_qty,
        },
    )
    await db.commit()

    return org, branch, user, drug, batch_id, batch_number, expiry_date


# ── envelope helpers ──────────────────────────────────────────────────────────


def _new_ulid() -> str:
    from time import time
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_sale_created_envelope(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    drug_id: uuid.UUID,
    batch_id: uuid.UUID,
    batch_number: str,
    expiry_date: date,
    sale_id: uuid.UUID | None = None,
    sale_number: str = "TB001-20260813-0001",
    quantity: int = 5,
    unit_price: str = "50.00",
    hash_prev: str = GENESIS_HASH,
    payment_method: str = "cash",
    status: str = "completed",
    authored_at: datetime | None = None,
) -> EventEnvelope:
    sid = sale_id or uuid.uuid4()
    item_id = uuid.uuid4()
    alloc_id = uuid.uuid4()
    total = float(unit_price) * quantity

    envelope = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=sid,
        aggregate_type=AggregateType.SALE,
        event_type="sale_created",
        schema_version=1,
        payload={
            "organization_id": str(org_id),
            "branch_id": str(branch_id),
            "cashier_id": str(authored_by),
            "sale_number": sale_number,
            "payment_method": payment_method,
            "payment_status": "completed",
            "status": status,
            "subtotal": str(total),
            "discount_amount": "0.00",
            "tax_amount": "0.00",
            "total_amount": str(total),
            "amount_paid": str(total),
            "change_amount": "0.00",
            "items": [
                {
                    "item_id": str(item_id),
                    "drug_id": str(drug_id),
                    "drug_name": "Amoxicillin 500mg",
                    "drug_sku": "AMX-500",
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "subtotal": str(total),
                    "discount_percentage": "0.00",
                    "discount_amount": "0.00",
                    "tax_rate": "0.00",
                    "tax_amount": "0.00",
                    "total_price": str(total),
                    "requires_prescription": False,
                    "prescription_verified": False,
                    "batch_allocations": [
                        {
                            "allocation_id": str(alloc_id),
                            "batch_id": str(batch_id),
                            "batch_number": batch_number,
                            "batch_expiry_date": expiry_date.isoformat(),
                            "quantity": quantity,
                            "unit_cost_at_sale": "30.00",
                            "unit_price_at_sale": unit_price,
                        }
                    ],
                }
            ],
        },
        dependencies=[],
        authored_at=authored_at or datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    envelope.hash_self = compute_hash_self(envelope, hash_prev)
    return envelope


def _make_sale_voided_envelope(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    sale_id: uuid.UUID,
    sale_created_event_id: str,
    hash_prev: str,
) -> EventEnvelope:
    envelope = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=sale_id,
        aggregate_type=AggregateType.SALE,
        event_type="sale_voided",
        schema_version=1,
        payload={
            "voided_by": str(authored_by),
            "void_reason": "Test void",
        },
        dependencies=[sale_created_event_id],
        authored_at=datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    envelope.hash_self = compute_hash_self(envelope, hash_prev)
    return envelope


# ── tests ─────────────────────────────────────────────────────────────────────


class TestSaleSyncProjector:

    async def test_sale_created_projects_and_deducts_stock(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data
        qty_sold = 5

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            quantity=qty_sold,
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert len(results) == 1
        r = results[0]
        assert r.status == EventStatus.ACCEPTED, r

        # Sale header projected.
        sale_row = (
            await db.execute(
                text("SELECT sale_number, status, payment_method, subtotal"
                     " FROM sales WHERE id = :id"),
                {"id": str(envelope.aggregate_id)},
            )
        ).fetchone()
        assert sale_row is not None
        assert sale_row.sale_number == "TB001-20260813-0001"
        assert sale_row.status == "completed"
        assert sale_row.payment_method == "cash"

        # Sale item projected.
        item_row = (
            await db.execute(
                text("SELECT quantity FROM sale_items WHERE sale_id = :sid"),
                {"sid": str(envelope.aggregate_id)},
            )
        ).fetchone()
        assert item_row is not None
        assert item_row.quantity == qty_sold

        # Batch allocation projected.
        alloc_row = (
            await db.execute(
                text("""
                    SELECT a.quantity, a.batch_number
                      FROM sale_item_batch_allocations a
                      JOIN sale_items si ON si.id = a.sale_item_id
                     WHERE si.sale_id = :sid
                """),
                {"sid": str(envelope.aggregate_id)},
            )
        ).fetchone()
        assert alloc_row is not None
        assert alloc_row.quantity == qty_sold
        assert alloc_row.batch_number == batch_number

        # Drug batch deducted.
        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row is not None
        assert batch_row.remaining_quantity == 100 - qty_sold

        # Branch inventory deducted.
        inv_row = (
            await db.execute(
                text("""
                    SELECT quantity
                      FROM branch_inventory
                     WHERE branch_id = :bid AND drug_id = :did
                """),
                {"bid": str(branch.id), "did": str(drug.id)},
            )
        ).fetchone()
        assert inv_row is not None
        assert inv_row.quantity == 100 - qty_sold

        # Inventory movement written.
        mov_count = (
            await db.execute(
                text("""
                    SELECT COUNT(*) FROM inventory_movements
                     WHERE source_id = :sid
                       AND movement_type = 'sale'
                """),
                {"sid": str(envelope.aggregate_id)},
            )
        ).scalar()
        assert mov_count == 1

        # Stock adjustment written.
        adj_count = (
            await db.execute(
                text("""
                    SELECT COUNT(*) FROM stock_adjustments
                     WHERE reason LIKE :pattern
                       AND adjustment_type = 'correction'
                """),
                {"pattern": "%TB001-20260813-0001%"},
            )
        ).scalar()
        assert adj_count == 1

    async def test_sale_created_idempotent(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            quantity=3,
        )

        first = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()
        assert first[0].status == EventStatus.ACCEPTED

        # Push the same envelope again.
        second = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()
        assert second[0].status == EventStatus.ACCEPTED
        assert second[0].seq == first[0].seq  # same seq, not re-appended

        # Drug batch deducted exactly once.
        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 100 - 3

    async def test_sale_voided_restores_stock(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data
        qty_sold = 7
        sale_id = uuid.uuid4()

        created_env = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            sale_id=sale_id,
            quantity=qty_sold,
        )
        results = await EventRouter.process_batch(db, org.id, [created_env])
        await db.commit()
        assert results[0].status == EventStatus.ACCEPTED

        voided_env = _make_sale_voided_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            sale_id=sale_id,
            sale_created_event_id=created_env.event_id,
            hash_prev=created_env.hash_self,
        )
        void_results = await EventRouter.process_batch(db, org.id, [voided_env])
        await db.commit()
        assert void_results[0].status == EventStatus.ACCEPTED

        # Sale marked cancelled.
        sale_row = (
            await db.execute(
                text("SELECT status FROM sales WHERE id = :id"),
                {"id": str(sale_id)},
            )
        ).fetchone()
        assert sale_row is not None
        assert sale_row.status == "cancelled"

        # Batch quantity restored.
        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 100

        # Branch inventory restored.
        inv_row = (
            await db.execute(
                text("""
                    SELECT quantity FROM branch_inventory
                     WHERE branch_id = :bid AND drug_id = :did
                """),
                {"bid": str(branch.id), "did": str(drug.id)},
            )
        ).fetchone()
        assert inv_row.quantity == 100

    async def test_sale_voided_idempotent(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        """Voiding the same sale twice should not double-restore stock."""
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data
        qty_sold = 4
        sale_id = uuid.uuid4()

        created_env = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            sale_id=sale_id,
            quantity=qty_sold,
        )
        r1 = await EventRouter.process_batch(db, org.id, [created_env])
        await db.commit()
        assert r1[0].status == EventStatus.ACCEPTED

        voided_env = _make_sale_voided_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            sale_id=sale_id,
            sale_created_event_id=created_env.event_id,
            hash_prev=created_env.hash_self,
        )
        r2 = await EventRouter.process_batch(db, org.id, [voided_env])
        await db.commit()
        assert r2[0].status == EventStatus.ACCEPTED

        # Push void again with a new event_id (client retry).
        voided_env2 = _make_sale_voided_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            sale_id=sale_id,
            sale_created_event_id=created_env.event_id,
            hash_prev=voided_env.hash_self,
        )
        r3 = await EventRouter.process_batch(db, org.id, [voided_env2])
        await db.commit()
        assert r3[0].status == EventStatus.ACCEPTED

        # Stock should be back to 100, not 100 + qty (over-restored).
        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 100

    async def test_sale_invalid_payment_method_rejected_permanently(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            payment_method="bitcoin",  # invalid
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert "payment_method" in (results[0].error_code or "")

        # No sale written.
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM sales WHERE id = :id"),
                {"id": str(envelope.aggregate_id)},
            )
        ).scalar()
        assert count == 0

    async def test_sale_missing_items_rejected_permanently(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data

        # Build a valid envelope then strip items from payload.
        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
        )
        envelope.payload["items"] = []
        envelope.hash_self = compute_hash_self(envelope, GENESIS_HASH)

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert "items" in (results[0].error_code or "")

    async def test_sale_insufficient_batch_stock_rejected_transient(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        """Asking for more units than the batch has causes REJECTED_TRANSIENT
        (ValueError from UPDATE...WHERE remaining_quantity >= qty matches
        nothing → no row returned → projector raises → savepoint rollback).
        """
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            quantity=9999,  # far exceeds batch stock of 100
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_TRANSIENT

        # No sale or inventory change written.
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM sales WHERE id = :id"),
                {"id": str(envelope.aggregate_id)},
            )
        ).scalar()
        assert count == 0

        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 100  # untouched

    async def test_sale_from_expired_batch_is_rejected(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        """An offline terminal must not be able to push through a sale drawn
        from a batch that had already expired when the sale was rung up.

        The client alone chooses the batch, so without a server-side expiry
        guard a stale device could dispense expired medication and the
        projector would happily decrement it. The online path has always
        filtered on expiry (SalesService FEFO allocation); this closes the
        same hole on the offline replay path.
        """
        org, branch, user, drug, batch_id, batch_number, _ = sale_test_data

        # The batch expired a week before the sale was authored.
        expired_on = date.today() - timedelta(days=7)
        await db.execute(
            text("UPDATE drug_batches SET expiry_date = :exp WHERE id = :bid"),
            {"exp": expired_on, "bid": str(batch_id)},
        )
        await db.commit()

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expired_on,
            quantity=5,
            authored_at=datetime.now(timezone.utc),
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_TRANSIENT

        # Neither the sale nor the batch may be touched.
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM sales WHERE id = :id"),
                {"id": str(envelope.aggregate_id)},
            )
        ).scalar()
        assert count == 0

        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 100

    async def test_sale_authored_before_expiry_still_applies(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        """The expiry guard keys off when the sale was AUTHORED, not when it is
        projected.

        A branch that was offline for a fortnight may push sales that were
        entirely legitimate when rung up, even though the batch has since
        lapsed. Rejecting those would dead-letter real, already-dispensed
        sales and silently understate revenue.
        """
        org, branch, user, drug, batch_id, batch_number, _ = sale_test_data

        expired_on = date.today() - timedelta(days=3)
        await db.execute(
            text("UPDATE drug_batches SET expiry_date = :exp WHERE id = :bid"),
            {"exp": expired_on, "bid": str(batch_id)},
        )
        await db.commit()

        # Rung up 10 days ago — a week before the batch lapsed.
        authored = datetime.now(timezone.utc) - timedelta(days=10)

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expired_on,
            quantity=5,
            authored_at=authored,
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.ACCEPTED

        batch_row = (
            await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = :bid"),
                {"bid": str(batch_id)},
            )
        ).fetchone()
        assert batch_row.remaining_quantity == 95

    async def test_sale_org_scope_violation_rejected_permanently(
        self, db: AsyncSession, sale_test_data, event_sync_tables
    ):
        org, branch, user, drug, batch_id, batch_number, expiry_date = sale_test_data
        wrong_org = uuid.uuid4()

        envelope = _make_sale_created_envelope(
            org_id=org.id,
            branch_id=branch.id,
            authored_by=user.id,
            drug_id=drug.id,
            batch_id=batch_id,
            batch_number=batch_number,
            expiry_date=expiry_date,
        )
        # Tamper: set payload org to a different org.
        envelope.payload["organization_id"] = str(wrong_org)
        envelope.hash_self = compute_hash_self(envelope, GENESIS_HASH)

        results = await EventRouter.process_batch(db, org.id, [envelope])
        await db.commit()

        assert results[0].status == EventStatus.REJECTED_PERMANENT
        assert "org_scope" in (results[0].error_code or "")
