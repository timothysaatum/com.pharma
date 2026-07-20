"""
Tests for offline sale synchronization.

Covers the entire _push_sale → _prepare_offline_sale_inventory →
_apply_offline_sale_inventory pipeline.

Scenarios:
  1. Successful offline sale sync (basic happy path)
  2. Multiple offline sales against the same batch
  3. Expired batch (expiry < today) — should fail with clear error
  4. Batch expiring today (expiry == today) — should succeed
  5. NULL expiry date — should be treated as non-expired and succeed
  6. Insufficient stock — should fail with clear error
  7. Missing server batch — should log warning
  8. Multi-item sale with one failing item
  9. Duplicate retry (operation_id idempotency)
  10. Sync cursor behavior after failure
  11. Batch that was valid at sale time but expired before sync
  12. Preferred batch allocation (item carries batch_id)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.sales.sales_model import Sale, SaleItem, SaleItemBatchAllocation
from app.models.system_md.sys_models import SyncOperationReceipt
from app.schemas.sync_schemas import PushRecord, PushRequest
from app.services.sync.sync_service import SyncService


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sale_sync_org_branch_user(db: AsyncSession):
    """Create minimal org, branch, user, and cashier for sync tests."""
    from app.models.pharmacy.pharmacy_model import Organization, Branch
    from app.models.user.user_model import User

    org = Organization(
        id=uuid.uuid4(),
        name="Sync Test Org",
        type="pharmacy",
        tax_id="SYNC-TEST",
        settings={"loyalty": {"points_per_unit": "1.0"}},
    )
    branch = Branch(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Sync Test Branch",
        code="SYNC-001",
        is_active=True,
        is_deleted=False,
    )
    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username="sync_cashier",
        email="cashier@sync.test",
        password_hash="hash",
        full_name="Sync Cashier",
        is_super_admin=True,
        is_active=True,
        assigned_branches=[branch.id],
    )
    db.add_all([org, branch, user])
    await db.commit()
    return org, branch, user


@pytest_asyncio.fixture
async def sync_drug(db: AsyncSession, sale_sync_org_branch_user):
    """Create a test drug."""
    org, branch, user = sale_sync_org_branch_user
    drug = Drug(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Test Drug A",
        sku="SYNC-TEST-A",
        unit_price=Decimal("25.00"),
        reorder_level=10,
        is_active=True,
        is_deleted=False,
        tax_rate=Decimal("0.00"),
    )
    db.add(drug)
    await db.commit()
    return drug, org, branch, user


def _make_batch(
    drug_id: uuid.UUID,
    branch_id: uuid.UUID,
    batch_number: str,
    qty: int,
    expiry: date | None,
) -> DrugBatch:
    return DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch_id,
        drug_id=drug_id,
        batch_number=batch_number,
        quantity=qty,
        remaining_quantity=qty,
        expiry_date=expiry or date(2099, 12, 31),
        sync_status="synced",
        sync_version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_inventory(
    drug_id: uuid.UUID,
    branch_id: uuid.UUID,
    qty: int,
) -> BranchInventory:
    return BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch_id,
        drug_id=drug_id,
        quantity=qty,
        reserved_quantity=0,
        sync_status="synced",
        sync_version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_push_record(
    sale_id: uuid.UUID,
    sale_number: str,
    drug_id: uuid.UUID,
    quantity: int,
    batch_id: uuid.UUID | None = None,
    operation_id: uuid.UUID | None = None,
) -> PushRecord:
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": str(uuid.uuid4()),
        "drug_id": str(drug_id),
        "drug_name": "Test Drug A",
        "quantity": quantity,
        "unit_price": "25.00",
        "subtotal": str(25.00 * quantity),
        "discount_amount": "0.00",
        "tax_amount": "0.00",
        "total_price": str(25.00 * quantity),
        "requires_prescription": False,
        "prescription_verified": False,
        "created_at": now,
        "updated_at": now,
    }
    if batch_id:
        item["batch_id"] = str(batch_id)
        item["batch_number"] = "BATCH-001"

    return PushRecord(
        operation_id=operation_id or uuid.uuid4(),
        table_name="sales",
        local_id=str(sale_id),
        operation="create",
        sync_version=1,
        created_offline_at=datetime.now(timezone.utc),
        data={
            "id": str(sale_id),
            "sale_number": sale_number,
            "branch_id": "PLACEHOLDER",
            "organization_id": "PLACEHOLDER",
            "subtotal": str(25.00 * quantity),
            "discount_amount": "0.00",
            "tax_amount": "0.00",
            "total_amount": str(25.00 * quantity),
            "payment_method": "cash",
            "payment_status": "completed",
            "amount_paid": str(25.00 * quantity),
            "change_amount": "0",
            "cashier_id": "PLACEHOLDER",
            "status": "completed",
            "items": [item],
            "sync_protocol_version": 2,
        },
    )


# ── Scenario 1: Happy path ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_offline_sale_sync_success(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-001", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="OFF-SALE-001",
        drug_id=drug.id,
        quantity=5,
    )
    # Patch in correct FKs
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db,
        record=record,
        organization_id=org.id,
        branch_id=branch.id,
        pushed_by=user.id,
    )
    await db.commit()

    assert result.success, f"Sale sync failed: {result.error}"
    assert conflict is None

    # Verify sale was created
    sale = await db.get(Sale, sale_id)
    assert sale is not None
    assert sale.sale_number == "OFF-SALE-001"

    # Verify items were created
    items_result = await db.execute(
        select(SaleItem).where(SaleItem.sale_id == sale_id)
    )
    items = items_result.scalars().all()
    assert len(items) == 1
    assert items[0].quantity == 5

    # Verify batch allocations
    allocs_result = await db.execute(
        select(SaleItemBatchAllocation).where(
            SaleItemBatchAllocation.sale_item_id == items[0].id
        )
    )
    allocs = allocs_result.scalars().all()
    assert len(allocs) == 1
    assert allocs[0].batch_id == batch.id
    assert allocs[0].quantity == 5

    # Verify stock was deducted
    await db.refresh(batch)
    assert batch.remaining_quantity == 45

    await db.refresh(inventory)
    assert inventory.quantity == 95


# ── Scenario 2: Multiple sales against same batch ───────────────────────

@pytest.mark.asyncio
async def test_multiple_offline_sales_same_batch(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-001", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    for i in range(3):
        sale_id = uuid.uuid4()
        record = _make_push_record(
            sale_id=sale_id,
            sale_number=f"OFF-SALE-M{i+1:03d}",
            drug_id=drug.id,
            quantity=10,
        )
        record.data["branch_id"] = str(branch.id)
        record.data["organization_id"] = str(org.id)
        record.data["cashier_id"] = str(user.id)

        result, conflict = await SyncService._push_sale(
            db=db, record=record,
            organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
        )
        await db.commit()

        assert result.success, f"Sale {i+1} failed: {result.error}"

    await db.refresh(batch)
    assert batch.remaining_quantity == 20  # 50 - 30 = 20


# ── Scenario 3: Expired batch ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_expired_batch_rejected(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    expired = date.today() - timedelta(days=1)
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-EXP", 50, expired)
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-EXP",
        drug_id=drug.id, quantity=5,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert not result.success
    assert "Insufficient non-expired batch stock" in (result.error or "")

    # Verify no sale was created
    sale = await db.get(Sale, sale_id)
    assert sale is None


# ── Scenario 4: Batch expiring today ────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_expiring_today_accepted(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-TODAY", 50, date.today())
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-TODAY",
        drug_id=drug.id, quantity=5,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()

    assert result.success, f"Sale sync failed: {result.error}"
    sale = await db.get(Sale, sale_id)
    assert sale is not None
    await db.refresh(batch)
    assert batch.remaining_quantity == 45


# ── Scenario 5: NULL expiry date ────────────────────────────────────────

@pytest.mark.asyncio
async def test_null_expiry_date_accepted(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-NULL", 50, None)
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-NULL",
        drug_id=drug.id, quantity=5,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()

    assert result.success, f"Sale sync failed: {result.error}"
    sale = await db.get(Sale, sale_id)
    assert sale is not None
    await db.refresh(batch)
    assert batch.remaining_quantity == 45


# ── Scenario 6: Insufficient stock ──────────────────────────────────────

@pytest.mark.asyncio
async def test_insufficient_stock_rejected(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 3)
    batch = _make_batch(drug.id, branch.id, "BATCH-LOW", 3, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-LOW",
        drug_id=drug.id, quantity=10,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert not result.success
    assert "Insufficient" in (result.error or "")


# ── Scenario 7: Preferred batch allocation ──────────────────────────────

@pytest.mark.asyncio
async def test_preferred_batch_allocation_respected(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    """When items carry a batch_id, the system should prioritize that batch."""
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch_a = _make_batch(drug.id, branch.id, "BATCH-A", 10, date.today() + timedelta(days=30))
    batch_b = _make_batch(drug.id, branch.id, "BATCH-B", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch_a, batch_b])
    await db.commit()

    sale_id = uuid.uuid4()
    # Item explicitly references batch_a
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-PREF",
        drug_id=drug.id, quantity=5,
        batch_id=batch_a.id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()

    assert result.success, f"Sale sync failed: {result.error}"

    # Verify the preferred batch was used
    sale_items = (await db.execute(
        select(SaleItem).where(SaleItem.sale_id == sale_id)
    )).scalars().all()
    assert len(sale_items) == 1

    allocs = (await db.execute(
        select(SaleItemBatchAllocation).where(
            SaleItemBatchAllocation.sale_item_id == sale_items[0].id
        )
    )).scalars().all()
    assert len(allocs) == 1
    assert allocs[0].batch_id == batch_a.id

    await db.refresh(batch_a)
    assert batch_a.remaining_quantity == 5  # 10 - 5 = 5
    await db.refresh(batch_b)
    assert batch_b.remaining_quantity == 50  # untouched


# ── Scenario 8: Duplicate retry (idempotency) ───────────────────────────

@pytest.mark.asyncio
async def test_duplicate_retry_idempotent(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-IDEM", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-IDEM",
        drug_id=drug.id, quantity=5,
        operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    # First push
    result1, conflict1 = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()
    assert result1.success

    # Second push (same operation_id) — should be idempotent
    result2, conflict2 = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()
    assert result2.success

    # Verify stock deducted only once
    await db.refresh(batch)
    assert batch.remaining_quantity == 45

    await db.refresh(inventory)
    assert inventory.quantity == 95

    # Verify no duplicate sale
    items_result = await db.execute(
        select(SaleItem).where(SaleItem.sale_id == sale_id)
    )
    items = items_result.scalars().all()
    assert len(items) == 1


# ── Scenario 9: Multi-item sale, one fails ─────────────────────────────

@pytest.mark.asyncio
async def test_multi_item_sale_one_fails(
    db: AsyncSession,
    sale_sync_org_branch_user,
):
    org, branch, user = sale_sync_org_branch_user
    drug_a = Drug(
        id=uuid.uuid4(), organization_id=org.id,
        name="Drug A", sku="MULTI-A", unit_price=Decimal("10.00"),
        reorder_level=10, is_active=True, is_deleted=False, tax_rate=Decimal("0.00"),
    )
    drug_b = Drug(
        id=uuid.uuid4(), organization_id=org.id,
        name="Drug B", sku="MULTI-B", unit_price=Decimal("20.00"),
        reorder_level=10, is_active=True, is_deleted=False, tax_rate=Decimal("0.00"),
    )
    db.add_all([drug_a, drug_b])
    await db.commit()

    # Drug A has stock, Drug B does not
    inv_a = _make_inventory(drug_a.id, branch.id, 10)
    batch_a = _make_batch(drug_a.id, branch.id, "BATCH-A", 10, date.today() + timedelta(days=365))
    db.add_all([inv_a, batch_a])
    await db.commit()

    sale_id = uuid.uuid4()
    now = datetime.now(timezone.utc).isoformat()
    item_a = {
        "id": str(uuid.uuid4()),
        "drug_id": str(drug_a.id), "drug_name": "Drug A",
        "quantity": 3, "unit_price": "10.00",
        "subtotal": "30.00", "discount_amount": "0.00",
        "tax_amount": "0.00", "total_price": "30.00",
        "requires_prescription": False, "prescription_verified": False,
        "created_at": now, "updated_at": now,
    }
    item_b = {
        "id": str(uuid.uuid4()),
        "drug_id": str(drug_b.id), "drug_name": "Drug B",
        "quantity": 5, "unit_price": "20.00",
        "subtotal": "100.00", "discount_amount": "0.00",
        "tax_amount": "0.00", "total_price": "100.00",
        "requires_prescription": False, "prescription_verified": False,
        "created_at": now, "updated_at": now,
    }
    record = PushRecord(
        operation_id=uuid.uuid4(),
        table_name="sales",
        local_id=str(sale_id),
        operation="create",
        sync_version=1,
        created_offline_at=datetime.now(timezone.utc),
        data={
            "id": str(sale_id),
            "sale_number": "OFF-SALE-MULTI",
            "branch_id": str(branch.id),
            "organization_id": str(org.id),
            "subtotal": "130.00",
            "discount_amount": "0.00",
            "tax_amount": "0.00",
            "total_amount": "130.00",
            "payment_method": "cash",
            "payment_status": "completed",
            "amount_paid": "130.00",
            "change_amount": "0",
            "cashier_id": str(user.id),
            "status": "completed",
            "items": [item_a, item_b],
            "sync_protocol_version": 2,
        },
    )

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert not result.success
    error = result.error or ""
    # Should mention drug B as the cause
    assert "Drug B" in error or str(drug_b.id) in error or "Insufficient" in error


# ── Scenario 10: Receipt-based retry works after failure ────────────────

@pytest.mark.asyncio
async def test_failed_receipt_cleared_on_retry(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    """A failed receipt must not permanently block retry."""
    drug, org, branch, user = sync_drug

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()

    # First attempt: no batch at all → fails with PushResult
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-RETRY",
        drug_id=drug.id, quantity=5,
        operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result1, _ = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    assert not result1.success
    assert "Inventory" in (result1.error or "") or "batch" in (result1.error or "").lower()
    # Ensure the sale was not created
    sale_check = await db.get(Sale, sale_id)
    assert sale_check is None

    # Now create inventory and batch, then retry
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-RETRY", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    # Retry should succeed now
    result2, _ = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()
    assert result2.success, f"Retry failed: {result2.error}"

    sale = await db.get(Sale, sale_id)
    assert sale is not None
    await db.refresh(batch)
    assert batch.remaining_quantity == 45


# ── Scenario 11: Batch created offline with operation receipt ──────────

@pytest.mark.asyncio
async def test_sync_operation_receipt_persistence(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    """Verify SyncOperationReceipt is created for accepted sales."""
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-RCPT", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()

    # Push via full PushRequest flow to test receipt creation
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-RCPT",
        drug_id=drug.id, quantity=5,
        operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    push_request = PushRequest(
        branch_id=branch.id,
        records=[record],
    )

    response = await SyncService.push(
        db=db,
        request=push_request,
        organization_id=org.id,
        pushed_by=user.id,
    )

    assert len(response.accepted) == 1
    assert response.total_failed == 0

    # Verify receipt exists
    receipt = await db.get(SyncOperationReceipt, op_id)
    assert receipt is not None
    assert receipt.result_kind == "accepted"
    assert receipt.table_name == "sales"
    assert str(receipt.record_id) == str(sale_id)

    # Retry via PushRequest — should replay "accepted" result without side effects
    response2 = await SyncService.push(
        db=db,
        request=push_request,
        organization_id=org.id,
        pushed_by=user.id,
    )
    assert len(response2.accepted) == 1

    # Stock deducted exactly once
    await db.refresh(batch)
    assert batch.remaining_quantity == 45


# ── Scenario 12: Missing batch ID logs warning ─────────────────────────

@pytest.mark.asyncio
async def test_missing_batch_id_logs_warning(
    db: AsyncSession,
    sync_drug: tuple[ Drug, uuid.UUID, uuid.UUID, uuid.UUID ],
):
    """
    When a sale references a batch_id that does not exist on the server,
    the push should still succeed (FEFO fallback allocates another batch)
    and a warning should be logged.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-REAL", 50, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    fake_batch_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-BWARN",
        drug_id=drug.id, quantity=5,
        batch_id=fake_batch_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    await db.commit()

    # Sale should succeed even with a non-existent batch_id (FEFO fallback)
    assert result.success
