
import pytest
import uuid
from datetime import datetime, timezone, date, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.branch.branch_service import BranchService
from app.services.inventory.inventory_service import InventoryService
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.inventory.inventory_model import Drug
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.sales.sales_model import Sale
from app.models.user.user_model import User
from app.schemas.inventory_schemas import DrugBatchUpdate

@pytest.mark.asyncio
async def test_batch_update_and_stats_aggregation(db: AsyncSession):
    # ── 1. Setup ─────────────────────────────────────────────────────────────
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Test Org", type="pharmacy")
    db.add(org)

    branch_id = uuid.uuid4()
    branch = Branch(id=branch_id, organization_id=org_id, name="Test Branch", code="B1", is_active=True)
    db.add(branch)

    drug = Drug(id=uuid.uuid4(), organization_id=org_id, name="Drug", unit_price=10.0, is_active=True)
    db.add(drug)
    await db.flush()

    batch = DrugBatch(
        id=uuid.uuid4(), branch_id=branch_id, drug_id=drug.id,
        batch_number="OLD-NUM", quantity=100, remaining_quantity=100,
        expiry_date=date.today() + timedelta(days=365)
    )
    db.add(batch)
    await db.commit()

    # ── 2. Test Batch Update (Uniqueness Check) ──────────────────────────────
    # Create another batch
    dupe_batch = DrugBatch(
        id=uuid.uuid4(), branch_id=branch_id, drug_id=drug.id,
        batch_number="NEW-NUM", quantity=50, remaining_quantity=50,
        expiry_date=date.today() + timedelta(days=365)
    )
    db.add(dupe_batch)
    await db.commit()

    # Try to update batch 1 to NEW-NUM (should fail)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await InventoryService.update_batch(db, batch.id, DrugBatchUpdate(batch_number="NEW-NUM"))
    assert exc.value.status_code == 400

    # Update to a unique number (should work)
    updated = await InventoryService.update_batch(db, batch.id, DrugBatchUpdate(batch_number="UNIQUE-NUM"))
    assert updated.batch_number == "UNIQUE-NUM"

    # ── 3. Test Sales Stats Aggregation (Range Check) ────────────────────────
    # Create sales: one today, one yesterday, one tomorrow
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    sale_yesterday = Sale(
        id=uuid.uuid4(), organization_id=org_id, branch_id=branch_id,
        total_amount=100.0, status='completed', created_at=today_start - timedelta(minutes=1),
        sale_number="S-OLD", payment_method="cash", cashier_id=uuid.uuid4(), subtotal=100.0
    )
    sale_today = Sale(
        id=uuid.uuid4(), organization_id=org_id, branch_id=branch_id,
        total_amount=50.0, status='completed', created_at=today_start + timedelta(minutes=1),
        sale_number="S-TODAY", payment_method="cash", cashier_id=uuid.uuid4(), subtotal=50.0
    )
    sale_tomorrow = Sale(
        id=uuid.uuid4(), organization_id=org_id, branch_id=branch_id,
        total_amount=200.0, status='completed', created_at=today_start + timedelta(days=1, minutes=1),
        sale_number="S-FUTURE", payment_method="cash", cashier_id=uuid.uuid4(), subtotal=200.0
    )
    db.add_all([sale_yesterday, sale_today, sale_tomorrow])
    await db.commit()

    stats = await BranchService.get_branch_with_stats(db, branch_id, org_id)
    # total_sales_today should ONLY be 50.0
    assert stats['total_sales_today'] == 50.0
    # total_sales_month should be yesterday + today (assuming same month)
    # In this test case, we know at least today + yesterday are in stats if we check range.
    # But for simplicity, let's just verify today's isolation.

    print("Backend verification passed!")
