"""
test_phase2_backward_compat.py

Ensure v1 events still respect client-provided allocations.
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
import app.services.sync.eventlog.projectors  # noqa: F401


def _new_ulid() -> str:
    base = f"{int(time() * 1000):013X}"
    tail = uuid.uuid4().hex[:13].upper()
    return (base + tail)[:26]


def _make_sale_envelope_v1(
    *,
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    cashier_id: uuid.UUID,
    drug_id: uuid.UUID,
    drug_name: str,
    batch_id: uuid.UUID,
    quantity: int = 5,
) -> EventEnvelope:
    env = EventEnvelope(
        event_id=_new_ulid(),
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.SALE,
        event_type="sale_created",
        schema_version=1,
        payload={
            # sync_protocol_version is absent -> v1
            "organization_id": str(org_id),
            "branch_id": str(branch_id),
            "cashier_id": str(cashier_id),
            "sale_number": "SALE-V1-001",
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
                    "batch_allocations": [
                        {
                            "allocation_id": str(uuid.uuid4()),
                            "batch_id": str(batch_id),
                            "batch_number": "B-LATE",
                            "batch_expiry_date": (date.today() + timedelta(days=365)).isoformat(),
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


@pytest_asyncio.fixture
async def setup_v1(db: AsyncSession):
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
async def test_v1_backward_compat(db: AsyncSession, setup_v1):
    org, branch, cashier, drug, batch_early, batch_late = setup_v1

    # In v1, the client explicitly chooses the late batch
    event = _make_sale_envelope_v1(
        org_id=org.id,
        branch_id=branch.id,
        cashier_id=cashier.id,
        drug_id=drug.id,
        drug_name=drug.name,
        batch_id=batch_late.id,
        quantity=5,
    )

    projector = ProjectorRegistry.get(AggregateType.SALE)
    await projector.apply(event, db)
    await db.commit()

    await db.refresh(batch_early)
    await db.refresh(batch_late)
    
    # Assert early batch was NOT touched, and late batch was deducted, proving client intent was respected
    assert batch_early.remaining_quantity == 10
    assert batch_late.remaining_quantity == 15
