"""
test_cross_tenant_batch_guard.py

Regression guard for the cross-tenant batch deduction bug.

A sale_created event from org_b that references a batch belonging to
org_a/branch_a must be rejected — the batch should not be mutated and
a ValueError must be raised by the SaleProjector.

This tests the branch_id + drug_id predicates added to the
UPDATE drug_batches WHERE clause in projectors/sale.py (Phase 0).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from time import time

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User
from app.schemas.event_envelope import (
    GENESIS_HASH,
    AggregateType,
    EventEnvelope,
    compute_hash_self,
)
from app.services.sync.eventlog.projector import ProjectorRegistry
import app.services.sync.eventlog.projectors  # noqa: F401 — registers all projectors


def _new_ulid() -> str:
    """26-character monotonic ULID — same helper used in integration tests."""
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_sale_envelope(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    cashier_id: uuid.UUID,
    drug_id: uuid.UUID,
    drug_name: str,
    batch_id: uuid.UUID,
    sale_number: str = "SALE-CROSS-001",
    quantity: int = 5,
) -> EventEnvelope:
    """Build a minimal sale_created EventEnvelope with one batch allocation."""
    env = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.SALE,
        event_type="sale_created",
        schema_version=1,
        payload={
            "organization_id": str(org_id),
            "branch_id": str(branch_id),
            "cashier_id": str(cashier_id),
            "sale_number": sale_number,
            "payment_method": "cash",
            "payment_status": "completed",
            "status": "completed",
            "subtotal": "50.00",
            "discount_amount": "0.00",
            "tax_amount": "0.00",
            "total_amount": "50.00",
            "amount_paid": "50.00",
            "change_amount": "0.00",
            "items": [
                {
                    "item_id": str(uuid.uuid4()),
                    "drug_id": str(drug_id),
                    "drug_name": drug_name,
                    "drug_sku": "SKU-CROSS",
                    "quantity": quantity,
                    "unit_price": "10.00",
                    "subtotal": "50.00",
                    "discount_percentage": "0.00",
                    "discount_amount": "0.00",
                    "tax_rate": "0.00",
                    "tax_amount": "0.00",
                    "total_price": "50.00",
                    "requires_prescription": False,
                    "prescription_verified": False,
                    "batch_allocations": [
                        {
                            "allocation_id": str(uuid.uuid4()),
                            "batch_id": str(batch_id),
                            "batch_number": "BATCH-A-001",
                            "batch_expiry_date": (
                                date.today() + timedelta(days=365)
                            ).isoformat(),
                            "quantity": quantity,
                            "unit_cost_at_sale": "8.00",
                            "unit_price_at_sale": "10.00",
                        }
                    ],
                }
            ],
        },
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=cashier_id,
        branch_id=branch_id,
        org_id=org_id,
        hash_self="0" * 64,
    )
    env.hash_self = compute_hash_self(env, GENESIS_HASH)
    return env


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def cross_tenant_setup(db: AsyncSession):
    """
    Two independent orgs:
      org_a / branch_a  → owns batch_a (100 units of drug_a)
      org_b / branch_b  → will attempt to draw from batch_a
    """
    org_a = Organization(
        id=uuid.uuid4(),
        name="Org A Pharmacy",
        type="pharmacy",
        tax_id="ORG-A-TAX",
        settings={},
    )
    org_b = Organization(
        id=uuid.uuid4(),
        name="Org B Pharmacy",
        type="pharmacy",
        tax_id="ORG-B-TAX",
        settings={},
    )
    db.add_all([org_a, org_b])
    await db.flush()

    branch_a = Branch(
        id=uuid.uuid4(),
        organization_id=org_a.id,
        name="Org A Branch",
        code="A-001",
        is_active=True,
        is_deleted=False,
    )
    branch_b = Branch(
        id=uuid.uuid4(),
        organization_id=org_b.id,
        name="Org B Branch",
        code="B-001",
        is_active=True,
        is_deleted=False,
    )
    cashier_b = User(
        id=uuid.uuid4(),
        organization_id=org_b.id,
        username="cashier_b_cross",
        email="cashier_cross@orgb.test",
        password_hash="hash",
        full_name="Cashier B",
        is_super_admin=True,
        is_active=True,
        assigned_branches=[],
    )
    drug_a = Drug(
        id=uuid.uuid4(),
        organization_id=org_a.id,
        name="Drug Alpha",
        sku="ALPHA-CROSS-001",
        unit_price=Decimal("10.00"),
        reorder_level=5,
        is_active=True,
        is_deleted=False,
        tax_rate=Decimal("0.00"),
    )
    db.add_all([branch_a, branch_b, cashier_b, drug_a])
    await db.flush()

    batch_a = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch_a.id,
        drug_id=drug_a.id,
        batch_number="BATCH-A-001",
        quantity=100,
        remaining_quantity=100,
        expiry_date=date.today() + timedelta(days=365),
        sync_status="synced",
    )
    # branch_inventory for branch_a (the projector needs it for the branch-level deduction)
    inv_a = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch_a.id,
        drug_id=drug_a.id,
        quantity=100,
        reserved_quantity=0,
        sync_status="synced",
    )
    db.add_all([batch_a, inv_a])
    await db.commit()

    return org_a, branch_a, org_b, branch_b, cashier_b, drug_a, batch_a


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sale_rejects_cross_tenant_batch(
    db: AsyncSession,
    cross_tenant_setup,
):
    """
    A sale from org_b/branch_b that carries batch_a.id (owned by branch_a)
    must raise ValueError — the WHERE clause added in Phase 0 ensures the
    UPDATE does not match, so the projector treats the batch as unknown.

    Failure mode pre-fix: the UPDATE would match on id alone and silently
    decrement org_a's stock on behalf of org_b.
    """
    org_a, branch_a, org_b, branch_b, cashier_b, drug_a, batch_a = cross_tenant_setup

    event = _make_sale_envelope(
        org_id=org_b.id,
        branch_id=branch_b.id,       # ← org_b's branch
        cashier_id=cashier_b.id,
        drug_id=drug_a.id,
        drug_name=drug_a.name,
        batch_id=batch_a.id,          # ← org_a's batch (cross-tenant reference)
    )

    projector = ProjectorRegistry.get(AggregateType.SALE)

    # Capture the ID now — after rollback the ORM object is expired and
    # accessing .id would trigger a lazy-load in a sync context.
    batch_a_id = batch_a.id

    # Must raise: the batch belongs to branch_a, not branch_b
    with pytest.raises(ValueError, match="Unknown batch"):
        await projector.apply(event, db)

    # Rollback the failed transaction so we can read committed state
    await db.rollback()

    # Critical: org_a's batch stock must be completely untouched
    refreshed = await db.scalar(
        select(DrugBatch).where(DrugBatch.id == batch_a_id)
    )
    assert refreshed is not None
    assert refreshed.remaining_quantity == 100, (
        f"Cross-tenant deduction occurred: remaining_quantity={refreshed.remaining_quantity}"
    )
