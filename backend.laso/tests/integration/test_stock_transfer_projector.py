"""
Integration tests — StockTransferProjector (Phase 1d).

Covers stock_transfer events through the full
EventRouter → AppendService → StockTransferProjector path.

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

import app.services.sync.eventlog.projectors  # noqa: F401 — registers all projectors

pytestmark = pytest.mark.asyncio


def _requires_postgres() -> None:
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip(
            "StockTransferProjector integration tests require a real Postgres backend "
            "(set TEST_DATABASE_URL to a postgresql+asyncpg URL)"
        )


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def event_sync_tables(db: AsyncSession):
    """Creates event spine tables (mirrors the alembic migration)."""
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
async def transfer_test_data(db: AsyncSession, setup_test_data, event_sync_tables):
    """
    Extends base test data with a second branch, a drug batch at the source branch,
    and branch_inventory rows at both branches.

    Returns: (org, source_branch, dest_branch, user, drug, batch_id)
    """
    org, source_branch, user, drugs, _customer = setup_test_data
    drug = drugs[0]

    # Create destination branch
    from app.models.pharmacy.pharmacy_model import Branch
    dest_branch = Branch(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Destination Branch",
        code="DB001",
        is_active=True,
        is_deleted=False,
    )
    db.add(dest_branch)
    await db.flush()

    batch_id = uuid.uuid4()
    initial_src_qty = 80
    initial_dst_qty = 20

    # Source batch
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
                :batch_number, :qty, :qty,
                :expiry_date,
                1, 1, 'synced',
                NOW(), NOW()
            )
        """),
        {
            "id": str(batch_id),
            "branch_id": str(source_branch.id),
            "drug_id": str(drug.id),
            "batch_number": "LOT-XFER-001",
            "qty": initial_src_qty,
            "expiry_date": date(2028, 12, 31),
        },
    )

    # Source branch_inventory
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
            "branch_id": str(source_branch.id),
            "drug_id": str(drug.id),
            "qty": initial_src_qty,
        },
    )

    # Destination branch_inventory
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
            "branch_id": str(dest_branch.id),
            "drug_id": str(drug.id),
            "qty": initial_dst_qty,
        },
    )
    await db.commit()

    return org, source_branch, dest_branch, user, drug, batch_id


# ── envelope helpers ──────────────────────────────────────────────────────────


def _new_ulid() -> str:
    from time import time
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_transfer_envelope(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    authored_by: uuid.UUID,
    source_branch_id: uuid.UUID,
    dest_branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    batch_id: uuid.UUID,
    src_adj_id: uuid.UUID | None = None,
    dst_adj_id: uuid.UUID | None = None,
    quantity: int = 20,
    src_prev_qty: int = 80,
    src_new_qty: int = 60,
    dst_prev_qty: int = 20,
    dst_new_qty: int = 40,
    hash_prev: str = GENESIS_HASH,
    reason: str = "Restocking destination branch",
    batch_changes: list | None = None,
) -> EventEnvelope:
    src_adj_id = src_adj_id or uuid.uuid4()
    dst_adj_id = dst_adj_id or uuid.uuid4()

    if batch_changes is None:
        batch_changes = [
            {
                "batch_id": str(batch_id),
                "quantity_change": -quantity,
                "batch_before": src_prev_qty,
                "batch_after": src_new_qty,
            }
        ]

    payload = {
        "organization_id": str(org_id),
        "source_branch_id": str(source_branch_id),
        "destination_branch_id": str(dest_branch_id),
        "drug_id": str(drug_id),
        "transferred_by": str(authored_by),
        "quantity": quantity,
        "reason": reason,
        "destination_adjustment_id": str(dst_adj_id),
        "source_previous_quantity": src_prev_qty,
        "source_new_quantity": src_new_qty,
        "destination_previous_quantity": dst_prev_qty,
        "destination_new_quantity": dst_new_qty,
        "batch_changes": batch_changes,
    }

    envelope = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=src_adj_id,
        aggregate_type=AggregateType.STOCK_TRANSFER,
        event_type="stock_transfer",
        schema_version=1,
        payload=payload,
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=authored_by,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    envelope.hash_self = compute_hash_self(envelope, hash_prev)
    return envelope


# ── tests ─────────────────────────────────────────────────────────────────────


class TestStockTransferProjector:

    async def test_transfer_projects_deducts_source_credits_dest(
        self, db: AsyncSession, transfer_test_data
    ):
        """Happy path: transfer deducts source inventory and credits destination."""
        org, src_branch, dst_branch, user, drug, batch_id = transfer_test_data

        envelope = _make_transfer_envelope(
            org_id=org.id,
            branch_id=src_branch.id,
            authored_by=user.id,
            source_branch_id=src_branch.id,
            dest_branch_id=dst_branch.id,
            drug_id=drug.id,
            batch_id=batch_id,
            quantity=20,
            src_prev_qty=80,
            src_new_qty=60,
            dst_prev_qty=20,
            dst_new_qty=40,
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        result = results[0]
        assert result.status == EventStatus.ACCEPTED, result.error_message

        # Source inventory deducted
        src_qty = (await db.execute(
            text("SELECT quantity FROM branch_inventory WHERE branch_id = :bid AND drug_id = :did"),
            {"bid": str(src_branch.id), "did": str(drug.id)},
        )).scalar()
        assert src_qty == 60

        # Destination inventory credited
        dst_qty = (await db.execute(
            text("SELECT quantity FROM branch_inventory WHERE branch_id = :bid AND drug_id = :did"),
            {"bid": str(dst_branch.id), "did": str(drug.id)},
        )).scalar()
        assert dst_qty == 40

        # Source batch deducted
        batch_qty = (await db.execute(
            text("SELECT remaining_quantity FROM drug_batches WHERE id = :id"),
            {"id": str(batch_id)},
        )).scalar()
        assert batch_qty == 60

        # Two stock_adjustment records (source + destination)
        adj_count = (await db.execute(
            text("SELECT COUNT(*) FROM stock_adjustments WHERE drug_id = :did"),
            {"did": str(drug.id)},
        )).scalar()
        assert adj_count == 2

        # transfer_out movement at source
        out_qty = (await db.execute(
            text("SELECT quantity_change FROM inventory_movements WHERE branch_id = :bid AND movement_type = 'transfer_out'"),
            {"bid": str(src_branch.id)},
        )).scalar()
        assert out_qty == -20

        # transfer_in movement at destination
        in_qty = (await db.execute(
            text("SELECT quantity_change FROM inventory_movements WHERE branch_id = :bid AND movement_type = 'transfer_in'"),
            {"bid": str(dst_branch.id)},
        )).scalar()
        assert in_qty == 20

    async def test_transfer_idempotent(
        self, db: AsyncSession, transfer_test_data
    ):
        """Replaying the same event must not double-deduct inventory."""
        org, src_branch, dst_branch, user, drug, batch_id = transfer_test_data

        src_adj_id = uuid.uuid4()
        dst_adj_id = uuid.uuid4()
        envelope = _make_transfer_envelope(
            org_id=org.id,
            branch_id=src_branch.id,
            authored_by=user.id,
            source_branch_id=src_branch.id,
            dest_branch_id=dst_branch.id,
            drug_id=drug.id,
            batch_id=batch_id,
            src_adj_id=src_adj_id,
            dst_adj_id=dst_adj_id,
            quantity=20,
            src_prev_qty=80,
            src_new_qty=60,
            dst_prev_qty=20,
            dst_new_qty=40,
        )

        r1 = await EventRouter.process_batch(db, org.id, [envelope])
        assert r1[0].status == EventStatus.ACCEPTED

        # Same event_id → spine deduplicates
        r2 = await EventRouter.process_batch(db, org.id, [envelope])
        assert r2[0].status == EventStatus.ACCEPTED

        # Inventory not double-deducted
        src_qty = (await db.execute(
            text("SELECT quantity FROM branch_inventory WHERE branch_id = :bid AND drug_id = :did"),
            {"bid": str(src_branch.id), "did": str(drug.id)},
        )).scalar()
        assert src_qty == 60

        dst_qty = (await db.execute(
            text("SELECT quantity FROM branch_inventory WHERE branch_id = :bid AND drug_id = :did"),
            {"bid": str(dst_branch.id), "did": str(drug.id)},
        )).scalar()
        assert dst_qty == 40

        # Still only 2 adjustment rows
        adj_count = (await db.execute(
            text("SELECT COUNT(*) FROM stock_adjustments WHERE drug_id = :did"),
            {"did": str(drug.id)},
        )).scalar()
        assert adj_count == 2

    async def test_transfer_org_scope_violation_rejected(
        self, db: AsyncSession, transfer_test_data
    ):
        """Envelope org_id != payload organization_id → REJECTED_PERMANENT."""
        org, src_branch, dst_branch, user, drug, batch_id = transfer_test_data

        envelope = _make_transfer_envelope(
            org_id=org.id,
            branch_id=src_branch.id,
            authored_by=user.id,
            source_branch_id=src_branch.id,
            dest_branch_id=dst_branch.id,
            drug_id=drug.id,
            batch_id=batch_id,
        )
        envelope.payload["organization_id"] = str(uuid.uuid4())

        results = await EventRouter.process_batch(db, org.id, [envelope])
        result = results[0]
        assert result.status == EventStatus.REJECTED_PERMANENT
        assert result.error_code == "org_scope_violation"

    async def test_transfer_same_branch_rejected(
        self, db: AsyncSession, transfer_test_data
    ):
        """source_branch_id == destination_branch_id → REJECTED_PERMANENT."""
        org, src_branch, dst_branch, user, drug, batch_id = transfer_test_data

        envelope = _make_transfer_envelope(
            org_id=org.id,
            branch_id=src_branch.id,
            authored_by=user.id,
            source_branch_id=src_branch.id,
            dest_branch_id=src_branch.id,  # same as source
            drug_id=drug.id,
            batch_id=batch_id,
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        result = results[0]
        assert result.status == EventStatus.REJECTED_PERMANENT
        assert result.error_code == "same_branch_transfer"

    async def test_transfer_missing_quantity_rejected(
        self, db: AsyncSession, transfer_test_data
    ):
        """quantity = 0 → REJECTED_PERMANENT."""
        org, src_branch, dst_branch, user, drug, batch_id = transfer_test_data

        envelope = _make_transfer_envelope(
            org_id=org.id,
            branch_id=src_branch.id,
            authored_by=user.id,
            source_branch_id=src_branch.id,
            dest_branch_id=dst_branch.id,
            drug_id=drug.id,
            batch_id=batch_id,
        )
        envelope.payload["quantity"] = 0

        results = await EventRouter.process_batch(db, org.id, [envelope])
        result = results[0]
        assert result.status == EventStatus.REJECTED_PERMANENT
        assert result.error_code == "invalid_quantity"

    async def test_transfer_no_batch_changes_writes_aggregate_movements(
        self, db: AsyncSession, transfer_test_data
    ):
        """When batch_changes is empty, projector writes one transfer_out + one transfer_in."""
        org, src_branch, dst_branch, user, drug, batch_id = transfer_test_data

        envelope = _make_transfer_envelope(
            org_id=org.id,
            branch_id=src_branch.id,
            authored_by=user.id,
            source_branch_id=src_branch.id,
            dest_branch_id=dst_branch.id,
            drug_id=drug.id,
            batch_id=batch_id,
            quantity=15,
            src_prev_qty=80,
            src_new_qty=65,
            dst_prev_qty=20,
            dst_new_qty=35,
            batch_changes=[],
        )

        results = await EventRouter.process_batch(db, org.id, [envelope])
        result = results[0]
        assert result.status == EventStatus.ACCEPTED, result.error_message

        # Source inventory deducted
        src_qty = (await db.execute(
            text("SELECT quantity FROM branch_inventory WHERE branch_id = :bid AND drug_id = :did"),
            {"bid": str(src_branch.id), "did": str(drug.id)},
        )).scalar()
        assert src_qty == 65

        # One transfer_out at source
        out_count = (await db.execute(
            text("SELECT COUNT(*) FROM inventory_movements WHERE branch_id = :bid AND movement_type = 'transfer_out'"),
            {"bid": str(src_branch.id)},
        )).scalar()
        assert out_count == 1

        # One transfer_in at dest
        in_count = (await db.execute(
            text("SELECT COUNT(*) FROM inventory_movements WHERE branch_id = :bid AND movement_type = 'transfer_in'"),
            {"bid": str(dst_branch.id)},
        )).scalar()
        assert in_count == 1
