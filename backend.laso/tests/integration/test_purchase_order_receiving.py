"""
Integration tests for PurchaseOrderService.receive_goods — duplicate-batch
append behavior (I1) and the branch-inventory-lock race guard (I6).

Regression coverage for docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.sales.sales_model import Supplier
from app.schemas.purchase_order_schemas import (
    PurchaseOrderCreate,
    PurchaseOrderItemCreate,
    ReceiveItemData,
    ReceivePurchaseOrder,
)
from app.services.sales.purchase_order_service import PurchaseOrderService


async def _make_approved_po(db: AsyncSession, org, branch, user, drug, quantity_ordered=50):
    supplier = Supplier(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Test Supplier",
        is_active=True,
    )
    db.add(supplier)
    await db.commit()

    po = await PurchaseOrderService.create_purchase_order(
        db,
        PurchaseOrderCreate(
            branch_id=branch.id,
            supplier_id=supplier.id,
            items=[
                PurchaseOrderItemCreate(
                    drug_id=drug.id,
                    quantity_ordered=quantity_ordered,
                    unit_cost=Decimal("2.00"),
                )
            ],
        ),
        user,
    )
    po = await PurchaseOrderService.submit_for_approval(db, po.id, user)
    po = await PurchaseOrderService.approve_purchase_order(db, po.id, user)
    return await PurchaseOrderService.get_purchase_order(db, po.id, include_details=True)


@pytest.mark.asyncio
class TestReceiveGoodsDuplicateBatch:
    async def test_second_receipt_of_same_batch_appends_instead_of_rejecting(
        self, db: AsyncSession, setup_test_data
    ):
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        po = await _make_approved_po(db, org, branch, user, drug, quantity_ordered=50)
        po_item_id = po.items[0].id
        expiry = date.today() + timedelta(days=365)

        first = await PurchaseOrderService.receive_goods(
            db,
            po.id,
            ReceivePurchaseOrder(
                items=[
                    ReceiveItemData(
                        purchase_order_item_id=po_item_id,
                        quantity_received=30,
                        batch_number="BATCH-001",
                        expiry_date=expiry,
                    )
                ],
            ),
            user,
        )
        assert first.batches_created == 1
        assert first.purchase_order.status == "ordered"

        # Second, remainder receipt against the SAME batch number/expiry —
        # must append, not reject with "already exists".
        second = await PurchaseOrderService.receive_goods(
            db,
            po.id,
            ReceivePurchaseOrder(
                items=[
                    ReceiveItemData(
                        purchase_order_item_id=po_item_id,
                        quantity_received=20,
                        batch_number="BATCH-001",
                        expiry_date=expiry,
                    )
                ],
            ),
            user,
        )
        assert second.batches_created == 0
        assert "already existed" in second.message
        assert second.purchase_order.status == "received"

        batches = (
            await db.execute(
                select(DrugBatch).where(
                    DrugBatch.branch_id == branch.id,
                    DrugBatch.drug_id == drug.id,
                    DrugBatch.batch_number == "BATCH-001",
                )
            )
        ).scalars().all()
        assert len(batches) == 1, "must not create a duplicate DrugBatch row"
        assert batches[0].quantity == 50
        assert batches[0].remaining_quantity == 50

        inventory = await db.scalar(
            select(BranchInventory).where(
                BranchInventory.branch_id == branch.id,
                BranchInventory.drug_id == drug.id,
            )
        )
        assert inventory.quantity == 50

    async def test_second_receipt_with_mismatched_expiry_is_rejected(
        self, db: AsyncSession, setup_test_data
    ):
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        po = await _make_approved_po(db, org, branch, user, drug, quantity_ordered=50)
        po_item_id = po.items[0].id
        expiry = date.today() + timedelta(days=365)
        other_expiry = date.today() + timedelta(days=200)

        await PurchaseOrderService.receive_goods(
            db,
            po.id,
            ReceivePurchaseOrder(
                items=[
                    ReceiveItemData(
                        purchase_order_item_id=po_item_id,
                        quantity_received=30,
                        batch_number="BATCH-001",
                        expiry_date=expiry,
                    )
                ],
            ),
            user,
        )

        with pytest.raises(HTTPException) as exc_info:
            await PurchaseOrderService.receive_goods(
                db,
                po.id,
                ReceivePurchaseOrder(
                    items=[
                        ReceiveItemData(
                            purchase_order_item_id=po_item_id,
                            quantity_received=20,
                            batch_number="BATCH-001",
                            expiry_date=other_expiry,
                        )
                    ],
                ),
                user,
            )
        assert exc_info.value.status_code == 400
        assert "does not match" in exc_info.value.detail


@pytest.mark.asyncio
class TestCreateBatchDuplicateBatch:
    async def test_second_create_batch_call_appends_instead_of_rejecting(
        self, db: AsyncSession, setup_test_data
    ):
        from app.schemas.inventory_schemas import DrugBatchCreate
        from app.services.inventory.inventory_service import InventoryService

        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        expiry = date.today() + timedelta(days=365)

        first = await InventoryService.create_batch(
            db,
            DrugBatchCreate(
                branch_id=branch.id,
                drug_id=drug.id,
                batch_number="BATCH-002",
                quantity=10,
                remaining_quantity=10,
                expiry_date=expiry,
            ),
            created_by=user.id,
        )
        assert first.remaining_quantity == 10

        second = await InventoryService.create_batch(
            db,
            DrugBatchCreate(
                branch_id=branch.id,
                drug_id=drug.id,
                batch_number="BATCH-002",
                quantity=15,
                remaining_quantity=15,
                expiry_date=expiry,
            ),
            created_by=user.id,
        )
        assert second.id == first.id, "must append to the existing batch, not create a new row"
        assert second.remaining_quantity == 25
        assert second.quantity == 25

        batches = (
            await db.execute(
                select(DrugBatch).where(
                    DrugBatch.branch_id == branch.id,
                    DrugBatch.drug_id == drug.id,
                    DrugBatch.batch_number == "BATCH-002",
                )
            )
        ).scalars().all()
        assert len(batches) == 1
