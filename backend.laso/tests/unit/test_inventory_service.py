import pytest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select
from app.services.inventory.inventory_service import InventoryService
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.schemas.inventory_schemas import DrugBatchCreate

@pytest.mark.asyncio
async def test_adjust_inventory_positive_correction_full_batch(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[0]

    # 1. Create a batch
    batch_data = DrugBatchCreate(
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="BATCH001",
        quantity=100,
        remaining_quantity=100,
        expiry_date=date.today() + timedelta(days=365)
    )
    await InventoryService.create_batch(db, batch_data)

    # 2. Perform a positive correction adjustment (+10)
    # This would have failed before because remaining_quantity (110) > quantity (100)
    await InventoryService.adjust_inventory(
        db=db,
        branch_id=branch.id,
        drug_id=drug.id,
        quantity_change=10,
        adjustment_type="correction",
        reason="Found extra stock",
        adjusted_by=user.id
    )

    # 3. Verify batch and inventory
    batch = await db.scalar(select(DrugBatch).where(DrugBatch.batch_number == "BATCH001"))
    assert batch.remaining_quantity == 110
    assert batch.quantity == 110  # Should have been incremented to match

    inv = await db.scalar(select(BranchInventory).where(BranchInventory.drug_id == drug.id))
    assert inv.quantity == 110

@pytest.mark.asyncio
async def test_adjust_inventory_no_batches_creates_system_batch(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[1]

    # Ensure drug is added to branch but has no stock/batches
    await InventoryService.add_drug_to_branch(db, branch.id, drug.id, org.id)

    # 1. Perform a positive adjustment
    await InventoryService.adjust_inventory(
        db=db,
        branch_id=branch.id,
        drug_id=drug.id,
        quantity_change=5,
        adjustment_type="correction",
        reason="Initial discovered stock",
        adjusted_by=user.id
    )

    # 2. Verify a system adjustment batch was created
    batch = await db.scalar(select(DrugBatch).where(DrugBatch.drug_id == drug.id))
    assert batch is not None
    assert batch.batch_number.startswith("ADJ-")
    assert batch.remaining_quantity == 5
    assert batch.quantity == 5

    inv = await db.scalar(select(BranchInventory).where(BranchInventory.drug_id == drug.id))
    assert inv.quantity == 5

@pytest.mark.asyncio
async def test_create_batch_records_audit_trail(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[2]

    # 1. Create a batch
    batch_data = DrugBatchCreate(
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="BATCH_AUDIT",
        quantity=50,
        expiry_date=date.today() + timedelta(days=365)
    )
    await InventoryService.create_batch(db, batch_data)

    # 2. Verify StockAdjustment was recorded
    adjustment = await db.scalar(
        select(StockAdjustment).where(
            StockAdjustment.drug_id == drug.id,
            StockAdjustment.adjustment_type == "purchase_receipt"
        )
    )
    assert adjustment is not None
    assert adjustment.quantity_change == 50
    assert adjustment.previous_quantity == 0
    assert adjustment.new_quantity == 50
    assert "BATCH_AUDIT" in adjustment.reason
