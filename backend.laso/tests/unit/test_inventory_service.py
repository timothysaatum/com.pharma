import pytest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select
from app.services.inventory.inventory_service import InventoryService
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.ledger import InventoryMovement
from app.models.pharmacy.pharmacy_model import Branch
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

    movement = await db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.drug_id == drug.id,
            InventoryMovement.movement_type == "purchase_receipt",
        )
    )
    assert movement is not None
    assert movement.quantity_change == 50
    assert movement.quantity_before == 0
    assert movement.quantity_after == 50
    assert movement.batch_id is not None


@pytest.mark.asyncio
async def test_transfer_stock_preserves_batch_identity_and_records_movements(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[0]

    destination = Branch(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Destination",
        code="TB002",
        is_active=True,
        is_deleted=False,
    )
    db.add(destination)

    batch = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="MOVE-001",
        quantity=20,
        remaining_quantity=20,
        expiry_date=date.today() + timedelta(days=180),
        cost_price=Decimal("10.00"),
        selling_price=Decimal("15.00"),
        supplier="Supplier A",
    )
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        quantity=20,
        reserved_quantity=0,
    )
    db.add_all([batch, inventory])
    await db.commit()

    await InventoryService.transfer_stock(
        db=db,
        from_branch_id=branch.id,
        to_branch_id=destination.id,
        drug_id=drug.id,
        quantity=6,
        reason="Rebalance",
        transferred_by=user.id,
    )

    source_batch = await db.scalar(
        select(DrugBatch).where(
            DrugBatch.branch_id == branch.id,
            DrugBatch.batch_number == "MOVE-001",
        )
    )
    dest_batch = await db.scalar(
        select(DrugBatch).where(
            DrugBatch.branch_id == destination.id,
            DrugBatch.batch_number == "MOVE-001",
        )
    )
    assert source_batch.remaining_quantity == 14
    assert dest_batch is not None
    assert dest_batch.remaining_quantity == 6
    assert dest_batch.expiry_date == source_batch.expiry_date
    assert dest_batch.supplier == source_batch.supplier

    movements = (
        await db.execute(
            select(InventoryMovement)
            .where(InventoryMovement.drug_id == drug.id)
            .order_by(InventoryMovement.movement_type)
        )
    ).scalars().all()
    assert {movement.movement_type for movement in movements} == {
        "transfer_in",
        "transfer_out",
    }
    assert sum(movement.quantity_change for movement in movements) == 0


@pytest.mark.asyncio
async def test_batch_listing_with_explicit_empty_branch_scope_returns_no_rows(
    db, setup_test_data
):
    """An unassigned user must not fall through to an unrestricted query."""
    from app.utils.pagination import PaginationParams

    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[0]
    db.add(DrugBatch(
        id=uuid.uuid4(), branch_id=branch.id, drug_id=drug.id,
        batch_number="EMPTY-SCOPE-REGRESSION", quantity=4,
        remaining_quantity=4, expiry_date=date.today() + timedelta(days=90),
    ))
    await db.commit()

    result = await InventoryService.get_batches_paginated(
        db=db,
        drug_id=drug.id,
        pagination=PaginationParams(page=1, page_size=50),
        branch_ids=[],
        include_expired=True,
        include_empty=True,
    )

    assert result.total == 0
    assert result.items == []
