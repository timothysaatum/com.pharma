"""
test_server_fefo_allocation.py

Tests server-side FEFO allocation (v2) for offline sales.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from time import time

import pytest
import pytest_asyncio
from sqlalchemy import select, text
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
import app.services.sync.eventlog.projectors  # noqa: F401


def _new_ulid() -> str:
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_sale_envelope_v2(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    cashier_id: uuid.UUID,
    drug_id: uuid.UUID,
    drug_name: str,
    quantity: int = 5,
    provisional_batch_allocations: list = None,
) -> EventEnvelope:
    env = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.SALE,
        event_type="sale_created",
        schema_version=1,
        payload={
            "sync_protocol_version": 2,
            "organization_id": str(org_id),
            "branch_id": str(branch_id),
            "cashier_id": str(cashier_id),
            "sale_number": "SALE-V2-001",
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
                    "drug_sku": "SKU",
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
                    "provisional_batch_allocations": provisional_batch_allocations or [],
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


@pytest_asyncio.fixture
async def setup_fefo(db: AsyncSession):
    org = Organization(id=uuid.uuid4(), name="Org", type="pharmacy", tax_id="TAX", settings={})
    db.add(org)
    await db.flush()

    branch = Branch(id=uuid.uuid4(), organization_id=org.id, name="Branch", code="B", is_active=True, is_deleted=False)
    cashier = User(id=uuid.uuid4(), organization_id=org.id, username="c", email="c@x.test", password_hash="h", full_name="C", is_super_admin=True, is_active=True, assigned_branches=[])
    drug = Drug(id=uuid.uuid4(), organization_id=org.id, name="Drug", sku="SKU", unit_price=Decimal("10.00"), reorder_level=5, is_active=True, is_deleted=False, tax_rate=Decimal("0.00"))
    db.add_all([branch, cashier, drug])
    await db.flush()

    batch_early = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="B-EARLY",
        quantity=10,
        remaining_quantity=10,
        expiry_date=date.today() + timedelta(days=30),
        sync_status="synced",
    )
    batch_late = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="B-LATE",
        quantity=20,
        remaining_quantity=20,
        expiry_date=date.today() + timedelta(days=365),
        sync_status="synced",
    )
    
    inv = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        quantity=30,
        reserved_quantity=0,
        sync_status="synced",
    )
    db.add_all([batch_early, batch_late, inv])
    await db.commit()

    return org, branch, cashier, drug, batch_early, batch_late


@pytest.mark.asyncio
async def test_v2_fefo_allocation(db: AsyncSession, setup_fefo):
    org, branch, cashier, drug, batch_early, batch_late = setup_fefo

    event = _make_sale_envelope_v2(
        org_id=org.id,
        branch_id=branch.id,
        cashier_id=cashier.id,
        drug_id=drug.id,
        drug_name=drug.name,
        quantity=15,
    )

    projector = ProjectorRegistry.get(AggregateType.SALE)
    await projector.apply(event, db)
    await db.commit()

    await db.refresh(batch_early)
    await db.refresh(batch_late)

    # In v2 FEFO, quantity=15. Early has 10, Late has 20.
    # Should consume 10 from early, 5 from late.
    assert batch_early.remaining_quantity == 0
    assert batch_late.remaining_quantity == 15

    # check allocations
    allocs = (await db.execute(text("SELECT batch_id, quantity FROM sale_item_batch_allocations"))).fetchall()
    assert len(allocs) == 2

    # idempotent replay
    await projector.apply(event, db)
    await db.commit()
    
    b_early = await db.scalar(select(DrugBatch).where(DrugBatch.id == batch_early.id))
    b_late = await db.scalar(select(DrugBatch).where(DrugBatch.id == batch_late.id))
    
    assert b_early.remaining_quantity == 0
    assert batch_late.remaining_quantity == 15


@pytest.mark.asyncio
async def test_insufficient_stock(db: AsyncSession, setup_fefo):
    org, branch, cashier, drug, batch_early, batch_late = setup_fefo

    event = _make_sale_envelope_v2(
        org_id=org.id,
        branch_id=branch.id,
        cashier_id=cashier.id,
        drug_id=drug.id,
        drug_name=drug.name,
        quantity=50,
    )

    projector = ProjectorRegistry.get(AggregateType.SALE)
    with pytest.raises(ValueError, match="Insufficient stock"):
        await projector.apply(event, db)

