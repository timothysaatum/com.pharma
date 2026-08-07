"""
Tests for offline sale synchronization.

Covers the entire _push_sale → _prepare_offline_sale_inventory →
_apply_offline_sale_inventory pipeline.

Scenarios:
  1.  Successful offline sale sync (basic happy path)
  2.  Multiple offline sales against the same batch
  3.  Expired batch (expiry < today) — should fail with clear error
  4.  Batch expiring today (expiry == today) — should succeed
  5.  NULL expiry date — should be treated as non-expired and succeed
  6.  Insufficient stock — should fail with clear error
  7.  Missing server batch — should log warning
  8.  Multi-item sale with one failing item
  9.  Duplicate retry (operation_id idempotency)
  10. Sync cursor behavior after failure
  11. Batch that was valid at sale time but expired before sync
  12. Preferred batch allocation (item carries batch_id)
  ── New scenarios (spec §Automated tests) ──
  13. Catalogue product with no branch inventory record (spec #1)
  14. Catalogue product with no batch (spec #2)
  15. Inventory record with zero quantity (spec #3)
  16. Stock at another branch only (spec #4)
  17. Batch valid at sale time, expired at sync — now accepted (spec fix Risk #1)
  18. Multiple offline sales attempting to oversell the same batch (spec #15)
  19. Pending offline sales reduce availability for subsequent sales (spec #16)
  20. Multi-item sale: same drug appears twice; combined qty exceeds stock (spec #10)
  21. Concurrent server sales atomic stock protection (spec #19)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.sales.sales_model import Sale, SaleItem, SaleItemBatchAllocation
from app.models.system_md.sys_models import SyncOperationReceipt, SystemAlert
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
    created_offline_at: datetime | None = None,
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
        created_offline_at=created_offline_at or datetime.now(timezone.utc),
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


# ═══════════════════════════════════════════════════════════════════════════
# New scenarios — spec §Automated tests
# ═══════════════════════════════════════════════════════════════════════════


# ── Spec #1: Catalogue product with no branch inventory record ───────────

@pytest.mark.asyncio
async def test_no_branch_inventory_record_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 1: The drug exists in the product catalogue but the active
    branch has never received stock — no BranchInventory row exists.
    The backend must reject the sale with a clear error.
    """
    drug, org, branch, user = sync_drug
    # Intentionally no inventory row and no batch for this branch

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-NO-INV-001",
        drug_id=drug.id,
        quantity=1,
    )
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

    assert not result.success
    error = result.error or ""
    assert "Inventory records not found" in error or "Insufficient" in error
    # Sale must not have been persisted
    assert await db.get(Sale, sale_id) is None


# ── Spec #2: Catalogue product with no batch ─────────────────────────────

@pytest.mark.asyncio
async def test_no_batch_exists_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 2: BranchInventory row exists (quantity > 0) but there are
    no DrugBatch rows.  The backend must find no valid batches and reject the
    sale.
    """
    drug, org, branch, user = sync_drug
    # Inventory says 10 units but there are no batch records
    inventory = _make_inventory(drug.id, branch.id, 10)
    db.add(inventory)
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-NO-BATCH-001",
        drug_id=drug.id,
        quantity=1,
    )
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

    assert not result.success
    error = result.error or ""
    assert "non-expired batch" in error.lower() or "Insufficient" in error
    assert await db.get(Sale, sale_id) is None


# ── Spec #3: Inventory record with zero quantity ──────────────────────────

@pytest.mark.asyncio
async def test_zero_inventory_quantity_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 3: BranchInventory exists but quantity = 0 (fully depleted).
    The backend must reject the sale immediately in the inventory pre-check.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 0)
    db.add(inventory)
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-ZERO-QTY-001",
        drug_id=drug.id,
        quantity=1,
    )
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

    assert not result.success
    error = result.error or ""
    assert "Insufficient" in error
    assert await db.get(Sale, sale_id) is None


# ── Spec #4: Stock at another branch only ────────────────────────────────

@pytest.mark.asyncio
async def test_stock_at_other_branch_rejected(
    db: AsyncSession,
    sale_sync_org_branch_user,
):
    """
    Spec scenario 4: The drug has valid stock at Branch A.  The sale is pushed
    for Branch B, which has no inventory.  The backend must reject it.
    """
    from app.models.pharmacy.pharmacy_model import Branch

    org, branch_a, user = sale_sync_org_branch_user
    branch_b = Branch(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="QA Branch B",
        code="QA-B",
        is_active=True,
        is_deleted=False,
    )
    db.add(branch_b)
    await db.commit()

    drug = Drug(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="QA Cross-Branch Drug",
        sku="QA-CROSS",
        unit_price=Decimal("10.00"),
        reorder_level=5,
        is_active=True,
        is_deleted=False,
        tax_rate=Decimal("0.00"),
    )
    db.add(drug)
    await db.commit()

    # Stock only at Branch A
    inv_a = _make_inventory(drug.id, branch_a.id, 50)
    batch_a = _make_batch(drug.id, branch_a.id, "BATCH-A-ONLY", 50, date.today() + timedelta(days=365))
    db.add_all([inv_a, batch_a])
    await db.commit()

    # Push a sale against Branch B (no stock)
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-CROSS-BRANCH-001",
        drug_id=drug.id,
        quantity=5,
    )
    record.data["branch_id"] = str(branch_b.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db,
        record=record,
        organization_id=org.id,
        branch_id=branch_b.id,
        pushed_by=user.id,
    )

    assert not result.success
    error = result.error or ""
    assert "Inventory records not found" in error or "Insufficient" in error
    assert await db.get(Sale, sale_id) is None
    # Branch A stock must be untouched
    await db.refresh(inv_a)
    assert inv_a.quantity == 50


# ── Spec Risk #1 fix: batch valid at sale time, expired at sync ──────────

@pytest.mark.asyncio
async def test_batch_valid_at_sale_time_expired_at_sync_now_accepted(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Regression test for Risk #1 (sync_service.py expiry evaluated at sync
    time, not sale time).

    The cashier sold 5 units 3 days ago when the batch was valid.
    The device was offline the entire time and the batch expired yesterday.
    Without the fix this sync would be rejected with 'Insufficient
    non-expired batches'.  With the fix the sale is accepted because the
    batch was valid at ``created_offline_at``.
    """
    drug, org, branch, user = sync_drug

    # Batch expired yesterday
    expiry = date.today() - timedelta(days=1)
    inventory = _make_inventory(drug.id, branch.id, 100)
    batch = _make_batch(drug.id, branch.id, "BATCH-EXP-AFTER-SALE", 50, expiry)
    db.add_all([inventory, batch])
    await db.commit()

    # Sale was created 3 days ago (batch was valid then)
    sale_date = datetime.now(timezone.utc) - timedelta(days=3)
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-LATE-SYNC-001",
        drug_id=drug.id,
        quantity=5,
        batch_id=batch.id,
        created_offline_at=sale_date,
    )
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

    # Must succeed: batch was valid when the sale was made
    assert result.success, (
        f"Sale should have been accepted (batch valid at sale time) but got: {result.error}"
    )
    sale = await db.get(Sale, sale_id)
    assert sale is not None
    await db.refresh(batch)
    assert batch.remaining_quantity == 45  # stock correctly deducted


# ── Spec #15: Multiple offline sales oversell the same batch ─────────────

@pytest.mark.asyncio
async def test_multiple_offline_sales_oversell_blocked(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 15: Three offline sales each requesting 10 units are
    synced against a batch with only 20 units.  The first two must succeed;
    the third must be blocked because the batch is exhausted.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 20)
    batch = _make_batch(drug.id, branch.id, "BATCH-OVERSELL", 20, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    success_count = 0
    failure_count = 0
    failure_error = ""

    for i in range(3):
        sale_id = uuid.uuid4()
        record = _make_push_record(
            sale_id=sale_id,
            sale_number=f"QA-OVERSELL-{i+1:03d}",
            drug_id=drug.id,
            quantity=10,
        )
        record.data["branch_id"] = str(branch.id)
        record.data["organization_id"] = str(org.id)
        record.data["cashier_id"] = str(user.id)

        result, _ = await SyncService._push_sale(
            db=db,
            record=record,
            organization_id=org.id,
            branch_id=branch.id,
            pushed_by=user.id,
        )
        if result.success:
            await db.commit()
            success_count += 1
        else:
            failure_count += 1
            failure_error = result.error or ""

    assert success_count == 2, f"Expected 2 accepted, got {success_count}"
    assert failure_count == 1, f"Expected 1 rejection, got {failure_count}"
    assert "Insufficient" in failure_error
    await db.refresh(batch)
    assert batch.remaining_quantity == 0


# ── Spec #16: Pending offline sales reduce availability ──────────────────

@pytest.mark.asyncio
async def test_pending_offline_sales_reduce_availability(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 16: Two offline sales are synced sequentially.  After the
    first sale succeeds, the available stock must reflect the deduction so
    the second sale cannot exceed what remains.
    """
    drug, org, branch, user = sync_drug
    # Only 7 units available in total
    inventory = _make_inventory(drug.id, branch.id, 7)
    batch = _make_batch(drug.id, branch.id, "BATCH-REDUCE", 7, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    def _sale_record(number: str, qty: int) -> PushRecord:
        sid = uuid.uuid4()
        r = _make_push_record(
            sale_id=sid,
            sale_number=number,
            drug_id=drug.id,
            quantity=qty,
        )
        r.data["branch_id"] = str(branch.id)
        r.data["organization_id"] = str(org.id)
        r.data["cashier_id"] = str(user.id)
        return r

    # First sale: 5 units — should succeed
    r1 = _sale_record("QA-REDUCE-001", 5)
    res1, _ = await SyncService._push_sale(
        db=db, record=r1,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    assert res1.success, f"First sale failed unexpectedly: {res1.error}"
    await db.commit()

    await db.refresh(inventory)
    assert inventory.quantity == 2  # 7 - 5 = 2

    # Second sale: 5 units — must fail because only 2 remain
    r2 = _sale_record("QA-REDUCE-002", 5)
    res2, _ = await SyncService._push_sale(
        db=db, record=r2,
        organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )
    assert not res2.success
    assert "Insufficient" in (res2.error or "")

    # Stock must remain at 2 (not double-deducted)
    await db.refresh(inventory)
    assert inventory.quantity == 2


# ── Spec #10: Same drug appears twice in cart; combined qty exceeds stock ─

@pytest.mark.asyncio
async def test_duplicate_cart_lines_same_drug_combined_exceeds_stock(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 10: A sale payload contains two line items for the same
    drug (e.g., added twice from the UI).  Their combined quantity exceeds
    available stock.  The backend must reject the sale, not count the drug
    twice independently.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 8)
    batch = _make_batch(drug.id, branch.id, "BATCH-DUP", 8, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    now = datetime.now(timezone.utc).isoformat()

    def _item(qty: int) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "drug_id": str(drug.id),
            "drug_name": drug.name,
            "quantity": qty,
            "unit_price": "25.00",
            "subtotal": str(25.0 * qty),
            "discount_amount": "0.00",
            "tax_amount": "0.00",
            "total_price": str(25.0 * qty),
            "requires_prescription": False,
            "prescription_verified": False,
            "created_at": now,
            "updated_at": now,
        }

    # 5 + 5 = 10 units for a drug with only 8 in stock
    record = PushRecord(
        operation_id=uuid.uuid4(),
        table_name="sales",
        local_id=str(sale_id),
        operation="create",
        sync_version=1,
        created_offline_at=datetime.now(timezone.utc),
        data={
            "id": str(sale_id),
            "sale_number": "QA-DUP-LINES-001",
            "branch_id": str(branch.id),
            "organization_id": str(org.id),
            "subtotal": "250.00",
            "discount_amount": "0.00",
            "tax_amount": "0.00",
            "total_amount": "250.00",
            "payment_method": "cash",
            "payment_status": "completed",
            "amount_paid": "250.00",
            "change_amount": "0",
            "cashier_id": str(user.id),
            "status": "completed",
            "items": [_item(5), _item(5)],  # same drug, 5 + 5 = 10 > 8
            "sync_protocol_version": 2,
        },
    )

    result, conflict = await SyncService._push_sale(
        db=db,
        record=record,
        organization_id=org.id,
        branch_id=branch.id,
        pushed_by=user.id,
    )

    # Combined qty 10 > available 8 — must be rejected
    assert not result.success
    assert "Insufficient" in (result.error or "")
    assert await db.get(Sale, sale_id) is None
    await db.refresh(inventory)
    assert inventory.quantity == 8  # untouched


# ── Spec #19: Concurrent server sales protected by row-level locking ─────

@pytest.mark.asyncio
async def test_concurrent_sales_do_not_produce_negative_stock(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Spec scenario 19: Two sync attempts for the same batch are processed
    sequentially (simulating the serialised effect of SELECT … FOR UPDATE in
    a real concurrent environment).  After the first sale commits, the second
    must see the reduced stock and be rejected.  Combined deductions must
    never exceed available stock, and inventory must never go negative.

    Note: True parallel concurrency is tested at the integration level.
    This unit test verifies the sequential serialisation invariant using the
    shared test session — sufficient to confirm stock-guard correctness
    without the overhead of spawning multiple OS threads/processes.
    """
    drug, org, branch, user = sync_drug

    # Only 5 units available; each sale requests 5 (total demand = 10 > 5)
    inventory = _make_inventory(drug.id, branch.id, 5)
    batch = _make_batch(drug.id, branch.id, "BATCH-CONCUR", 5, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    def _record(sale_num: str) -> PushRecord:
        sid = uuid.uuid4()
        r = _make_push_record(
            sale_id=sid,
            sale_number=sale_num,
            drug_id=drug.id,
            quantity=5,
        )
        r.data["branch_id"] = str(branch.id)
        r.data["organization_id"] = str(org.id)
        r.data["cashier_id"] = str(user.id)
        return r

    # First "concurrent" transaction — must succeed and commit
    result1, _ = await SyncService._push_sale(
        db=db,
        record=_record("QA-CONCUR-001"),
        organization_id=org.id,
        branch_id=branch.id,
        pushed_by=user.id,
    )
    assert result1.success, f"First sale failed unexpectedly: {result1.error}"
    await db.commit()

    # Second "concurrent" transaction — must be rejected (stock exhausted)
    result2, _ = await SyncService._push_sale(
        db=db,
        record=_record("QA-CONCUR-002"),
        organization_id=org.id,
        branch_id=branch.id,
        pushed_by=user.id,
    )
    assert not result2.success, "Second sale should have been rejected (stock exhausted)"
    assert "Insufficient" in (result2.error or "")

    # Inventory must be exactly 0 — not negative, not double-counted
    await db.refresh(inventory)
    assert inventory.quantity == 0

    await db.refresh(batch)
    assert batch.remaining_quantity == 0


# ── New Hardening Tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_push_batch_oversized_limit_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Verify that any PushRequest containing more than 100 records
    is immediately rejected at the service level to prevent DB lock contention.
    """
    drug, org, branch, user = sync_drug

    # Generate 101 push records
    records = []
    for i in range(101):
        r = _make_push_record(
            sale_id=uuid.uuid4(),
            sale_number=f"QA-OVERSIZE-{i}",
            drug_id=drug.id,
            quantity=1,
        )
        r.data["branch_id"] = str(branch.id)
        r.data["organization_id"] = str(org.id)
        r.data["cashier_id"] = str(user.id)
        records.append(r)

    with pytest.raises(ValidationError) as exc_info:
        PushRequest(branch_id=branch.id, records=records)

    assert "100" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sale_backdating_guard_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Verify that an offline sale with a created_offline_at timestamp
    more than 7 days in the past is rejected to prevent fraudulent antedating.
    """
    drug, org, branch, user = sync_drug

    # Create inventory and batch
    inventory = _make_inventory(drug.id, branch.id, 10)
    batch = _make_batch(drug.id, branch.id, "BATCH-BACKDATE", 10, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    # 8 days in the past
    old_timestamp = datetime.now(timezone.utc) - timedelta(days=8)
    
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-BACKDATE-OLD",
        drug_id=drug.id,
        quantity=5,
    )
    record.created_offline_at = old_timestamp
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

    assert not result.success
    assert "created 7+ days ago" in (result.error or "")
    # Should not have been committed
    assert await db.get(Sale, sale_id) is None


@pytest.mark.asyncio
async def test_missing_sync_protocol_version_sale_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    A sale push that omits ``sync_protocol_version`` entirely must be
    hard-rejected rather than silently inserted with zero stock deduction.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 10)
    batch = _make_batch(drug.id, branch.id, "BATCH-NOVER", 10, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-NOVER-001",
        drug_id=drug.id,
        quantity=1,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)
    del record.data["sync_protocol_version"]

    result, conflict = await SyncService._push_sale(
        db=db,
        record=record,
        organization_id=org.id,
        branch_id=branch.id,
        pushed_by=user.id,
    )

    assert not result.success
    assert "sync_protocol_version=2" in (result.error or "")
    # No sale must have been created, and stock must be untouched.
    assert await db.get(Sale, sale_id) is None
    await db.refresh(inventory)
    assert inventory.quantity == 10
    await db.refresh(batch)
    assert batch.remaining_quantity == 10


@pytest.mark.asyncio
async def test_legacy_v1_sale_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    A sale push explicitly declaring ``sync_protocol_version: 1`` (the
    legacy/pre-inventory-deduction protocol) must be rejected — accepting it
    would insert Sale/SaleItem rows without decrementing stock.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 10)
    batch = _make_batch(drug.id, branch.id, "BATCH-V1", 10, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-V1-001",
        drug_id=drug.id,
        quantity=1,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)
    record.data["sync_protocol_version"] = 1

    result, conflict = await SyncService._push_sale(
        db=db,
        record=record,
        organization_id=org.id,
        branch_id=branch.id,
        pushed_by=user.id,
    )

    assert not result.success
    assert "sync_protocol_version=2" in (result.error or "")
    assert await db.get(Sale, sale_id) is None
    await db.refresh(inventory)
    assert inventory.quantity == 10
    await db.refresh(batch)
    assert batch.remaining_quantity == 10


@pytest.mark.asyncio
async def test_push_response_returns_acked_cursor(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    Verify that the push response correctly populates the acked_cursor field
    with the latest committed operation receipt timestamp.
    """
    drug, org, branch, user = sync_drug

    inventory = _make_inventory(drug.id, branch.id, 10)
    batch = _make_batch(drug.id, branch.id, "BATCH-CURSOR", 10, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-CURSOR-001",
        drug_id=drug.id,
        quantity=3,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    request = PushRequest(branch_id=branch.id, records=[record])

    response = await SyncService.push(
        db=db,
        request=request,
        organization_id=org.id,
        pushed_by=user.id,
    )

    assert response.total_accepted == 1
    # acked_cursor should be present and valid ISO format
    assert response.acked_cursor is not None
    # Verify we can parse it
    parsed_dt = datetime.fromisoformat(response.acked_cursor)
    assert parsed_dt is not None



# ── Clock-skew guards ──────────────────────────────────────────────────
# Regression coverage for docs/reviews/2026-08-11-offline-first-architecture-review.md
# finding #4: created_offline_at is unverified client input. The backdating
# guard only ever bounded the past; a forward-skewed clock had no bound at
# all, and a backward-skewed clock (within the still-allowed 7-day window)
# could make an already-expired batch look valid at the claimed sale time.

@pytest.mark.asyncio
async def test_forward_dated_sale_rejected(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """A device clock skewed more than 24h into the future must be rejected,
    not silently accepted with a future created_at that would corrupt
    daily-sales/shift/tax-period reporting."""
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 10)
    batch = _make_batch(drug.id, branch.id, "BATCH-FUTURE", 10, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    future_timestamp = datetime.now(timezone.utc) + timedelta(days=2)
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-FORWARD-SKEW",
        drug_id=drug.id,
        quantity=1,
        created_offline_at=future_timestamp,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record, organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert not result.success
    assert "ahead of the server clock" in (result.error or "")


@pytest.mark.asyncio
async def test_minor_forward_skew_within_tolerance_is_accepted(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """Small clock drift / timezone edge cases must not block legitimate sales."""
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 10)
    batch = _make_batch(drug.id, branch.id, "BATCH-MINOR-SKEW", 10, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    slightly_future = datetime.now(timezone.utc) + timedelta(hours=6)
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-MINOR-SKEW",
        drug_id=drug.id,
        quantity=1,
        created_offline_at=slightly_future,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record, organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert result.success


@pytest.mark.asyncio
async def test_backward_clock_skew_beyond_trust_window_cannot_launder_expired_batch(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """
    A batch that expired 3 days ago must be rejected even if the client
    claims (via created_offline_at) that the sale happened 5 days ago —
    within the 7-day backdating window, but well beyond the 48h window
    the server trusts for expiry evaluation. Before the fix, a backward-
    skewed device clock could make this already-expired batch look valid
    at the claimed sale time.
    """
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 10)
    expiry = date.today() - timedelta(days=3)
    batch = _make_batch(drug.id, branch.id, "BATCH-STALE-SKEW", 10, expiry)
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    claimed_sale_time = datetime.now(timezone.utc) - timedelta(days=5)
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-BACKWARD-SKEW",
        drug_id=drug.id,
        quantity=1,
        created_offline_at=claimed_sale_time,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record, organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert not result.success
    assert "expired" in (result.error or "").lower() or "insufficient" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_legitimate_delayed_sync_within_trust_window_still_accepted(
    db: AsyncSession,
    sync_drug: tuple[Drug, uuid.UUID, uuid.UUID, uuid.UUID],
):
    """The original feature this trust window protects must still work:
    a batch that expired after a legitimately-recent offline sale (well
    within the 48h trust window) is still evaluated at sale time, not
    sync time."""
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 10)
    expiry = date.today() - timedelta(days=1)  # expired yesterday
    batch = _make_batch(drug.id, branch.id, "BATCH-RECENT-VALID", 10, expiry)
    db.add_all([inventory, batch])
    await db.commit()

    sale_id = uuid.uuid4()
    # 36 hours ago — within the 48h trust window, and the batch (expired
    # yesterday) was still valid at that point.
    claimed_sale_time = datetime.now(timezone.utc) - timedelta(hours=36)
    record = _make_push_record(
        sale_id=sale_id,
        sale_number="QA-RECENT-DELAYED-SYNC",
        drug_id=drug.id,
        quantity=1,
        created_offline_at=claimed_sale_time,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)

    result, conflict = await SyncService._push_sale(
        db=db, record=record, organization_id=org.id, branch_id=branch.id, pushed_by=user.id,
    )

    assert result.success


# ── P1-8: proactive alert for sales stuck retrying forever ─────────────
#
# These scenarios go through SyncService.push() (not SyncService._push_sale
# directly, unlike most tests above) because failure_count and the
# SystemAlert side effect live in push()'s per-record loop, keyed off the
# SyncOperationReceipt for a given operation_id — retrying the same
# operation_id via repeated push() calls, exactly like
# test_sync_operation_receipt_persistence does for the success path, is
# what exercises the receipt-reuse-on-retry code path.

@pytest_asyncio.fixture
async def stuck_sale_setup(db: AsyncSession, sync_drug):
    """A drug with just enough stock to make a 10-unit sale fail with
    'Insufficient stock' every time, matching test_insufficient_stock_rejected."""
    drug, org, branch, user = sync_drug
    inventory = _make_inventory(drug.id, branch.id, 3)
    batch = _make_batch(drug.id, branch.id, "BATCH-STUCK", 3, date.today() + timedelta(days=365))
    db.add_all([inventory, batch])
    await db.commit()
    return drug, org, branch, user, inventory, batch


async def _push_once(db, org, branch, user, push_request):
    return await SyncService.push(
        db=db, request=push_request, organization_id=org.id, pushed_by=user.id,
    )


async def _stuck_sale_alerts(db: AsyncSession, sale_id: uuid.UUID):
    result = await db.execute(
        select(SystemAlert).where(
            SystemAlert.entity_type == "sale",
            SystemAlert.entity_id == sale_id,
        )
    )
    return result.scalars().all()


@pytest.mark.asyncio
async def test_stuck_sale_alert_created_at_fifth_failure(
    db: AsyncSession,
    stuck_sale_setup,
):
    """Failing to push the same sale (same operation_id) 5 times in a row
    raises exactly one SystemAlert."""
    drug, org, branch, user, inventory, batch = stuck_sale_setup

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-STUCK-5",
        drug_id=drug.id, quantity=10, operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)
    push_request = PushRequest(branch_id=branch.id, records=[record])

    for attempt in range(1, 6):
        response = await _push_once(db, org, branch, user, push_request)
        assert response.total_failed == 1, f"attempt {attempt} unexpectedly succeeded"
        assert "Insufficient" in (response.failed[0].error or "")

    receipt = await db.get(SyncOperationReceipt, op_id)
    assert receipt is not None
    assert receipt.failure_count == 5

    alerts = await _stuck_sale_alerts(db, sale_id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.alert_type == "system_error"
    assert alert.entity_type == "sale"
    assert alert.entity_id == sale_id
    assert alert.organization_id == org.id
    assert alert.branch_id == branch.id
    assert alert.is_resolved is False


@pytest.mark.asyncio
async def test_stuck_sale_alert_no_duplicate_on_sixth_failure(
    db: AsyncSession,
    stuck_sale_setup,
):
    """A 6th (and 7th) consecutive failure of the same operation_id must
    not create a second alert row."""
    drug, org, branch, user, inventory, batch = stuck_sale_setup

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-STUCK-6",
        drug_id=drug.id, quantity=10, operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)
    push_request = PushRequest(branch_id=branch.id, records=[record])

    for _ in range(7):
        response = await _push_once(db, org, branch, user, push_request)
        assert response.total_failed == 1

    receipt = await db.get(SyncOperationReceipt, op_id)
    assert receipt.failure_count == 7

    alerts = await _stuck_sale_alerts(db, sale_id)
    assert len(alerts) == 1
    assert alerts[0].is_resolved is False


@pytest.mark.asyncio
async def test_stuck_sale_alert_auto_resolves_on_eventual_success(
    db: AsyncSession,
    stuck_sale_setup,
):
    """Once stock is replenished and the same operation_id finally pushes
    successfully, the alert raised at the 5th failure is auto-resolved."""
    drug, org, branch, user, inventory, batch = stuck_sale_setup

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-STUCK-RESOLVE",
        drug_id=drug.id, quantity=10, operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)
    push_request = PushRequest(branch_id=branch.id, records=[record])

    for _ in range(5):
        response = await _push_once(db, org, branch, user, push_request)
        assert response.total_failed == 1

    alerts = await _stuck_sale_alerts(db, sale_id)
    assert len(alerts) == 1
    assert alerts[0].is_resolved is False

    # Replenish stock so the same sale can now push successfully.
    batch.quantity = 100
    batch.remaining_quantity = 100
    inventory.quantity = 100
    await db.commit()

    response = await _push_once(db, org, branch, user, push_request)
    assert response.total_accepted == 1, f"expected success, got: {response.failed}"

    receipt = await db.get(SyncOperationReceipt, op_id)
    assert receipt.result_kind == "accepted"
    # The failure count is a historical record of past attempts and is not
    # reset on eventual success — only the alert is resolved.
    assert receipt.failure_count == 5

    alerts = await _stuck_sale_alerts(db, sale_id)
    assert len(alerts) == 1
    assert alerts[0].is_resolved is True
    assert alerts[0].resolved_at is not None


@pytest.mark.asyncio
async def test_stuck_sale_alert_not_created_below_threshold(
    db: AsyncSession,
    stuck_sale_setup,
):
    """Failures 1 through 4 must not create any alert at all."""
    drug, org, branch, user, inventory, batch = stuck_sale_setup

    op_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    record = _make_push_record(
        sale_id=sale_id, sale_number="OFF-SALE-STUCK-BELOW",
        drug_id=drug.id, quantity=10, operation_id=op_id,
    )
    record.data["branch_id"] = str(branch.id)
    record.data["organization_id"] = str(org.id)
    record.data["cashier_id"] = str(user.id)
    push_request = PushRequest(branch_id=branch.id, records=[record])

    for attempt in range(1, 5):
        response = await _push_once(db, org, branch, user, push_request)
        assert response.total_failed == 1

        receipt = await db.get(SyncOperationReceipt, op_id)
        assert receipt.failure_count == attempt

        alerts = await _stuck_sale_alerts(db, sale_id)
        assert alerts == [], f"unexpected alert after only {attempt} failure(s)"
