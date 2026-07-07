"""
Integration tests for syncing Sale and SaleItem records.
"""

import pytest
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import (
    BranchInventory,
    DrugBatch,
    StockAdjustment,
)
from app.models.inventory.ledger import InventoryMovement
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.precriptions.prescription_model import Prescription
from app.models.sales.sales_model import Sale, SaleItem, SaleItemBatchAllocation
from app.models.sales.sales_model import PurchaseOrder, Supplier
from app.models.system_md.sys_models import SyncOperationReceipt
from app.schemas.sync_schemas import PushRequest, PushRecord
from app.services.sync.sync_service import SyncService


@pytest.mark.asyncio
class TestSyncSaleItems:
    """Test suite for syncing sales with items."""

    async def test_push_sale_with_items(self, db: AsyncSession, setup_test_data):
        """Test pushing a sale record that includes nested items."""
        org, branch, user, drugs, customer = setup_test_data

        sale_id = uuid.uuid4()
        item_id = uuid.uuid4()

        # Prepare push record with items
        record_data = {
            "id": str(sale_id),
            "sale_number": "SYNC-SALE-001",
            "subtotal": 100.0,
            "discount_amount": 0.0,
            "tax_amount": 0.0,
            "total_amount": 100.0,
            "payment_method": "cash",
            "cashier_id": str(user.id),
            "status": "completed",
            "items": [
                {
                    "id": str(item_id),
                    "drug_id": str(drugs[0].id),
                    "drug_name": drugs[0].name,
                    "quantity": 2,
                    "unit_price": 50.0,
                    "subtotal": 100.0,
                    "total_price": 100.0,
                }
            ]
        }

        request = PushRequest(
            branch_id=branch.id,
            records=[
                PushRecord(
                    operation_id=uuid.uuid4(),
                    local_id=str(sale_id),
                    table_name="sales",
                    operation="create",
                    sync_version=1,
                    created_offline_at=datetime.now(timezone.utc),
                    data=record_data
                )
            ]
        )

        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 1

        # Verify Sale and SaleItem exist in DB
        result = await db.execute(
            select(Sale)
            .options(selectinload(Sale.items))
            .where(Sale.id == sale_id)
        )
        sale = result.scalar_one_or_none()

        assert sale is not None
        assert sale.sale_number == "SYNC-SALE-001"
        assert len(sale.items) == 1
        assert sale.items[0].id == item_id
        assert sale.items[0].drug_id == drugs[0].id
        assert sale.items[0].quantity == 2

    async def test_push_sale_reassigns_stale_cashier_to_syncing_user(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Offline sales with deleted/stale cashier IDs should not block forever."""
        org, branch, user, drugs, customer = setup_test_data

        sale_id = uuid.uuid4()
        stale_cashier_id = uuid.uuid4()

        record_data = {
            "id": str(sale_id),
            "sale_number": "SYNC-STALE-CASHIER-001",
            "subtotal": 50.0,
            "discount_amount": 0.0,
            "tax_amount": 0.0,
            "total_amount": 50.0,
            "payment_method": "cash",
            "cashier_id": str(stale_cashier_id),
            "status": "completed",
            "items": [
                {
                    "id": str(uuid.uuid4()),
                    "drug_id": str(drugs[0].id),
                    "drug_name": drugs[0].name,
                    "quantity": 1,
                    "unit_price": 50.0,
                    "subtotal": 50.0,
                    "total_price": 50.0,
                }
            ],
        }

        request = PushRequest(
            branch_id=branch.id,
            records=[
                PushRecord(
                    operation_id=uuid.uuid4(),
                    local_id=str(sale_id),
                    table_name="sales",
                    operation="create",
                    sync_version=1,
                    created_offline_at=datetime.now(timezone.utc),
                    data=record_data,
                )
            ],
        )

        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 1
        assert response.total_failed == 0
        assert response.accepted[0].fk_fixes == [
            f"cashier_id={stale_cashier_id}->{user.id}"
        ]

        sale = await db.get(Sale, sale_id)
        assert sale is not None
        assert sale.cashier_id == user.id

    async def test_push_rejects_nonexistent_sync_branch_without_partial_writes(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A stale branch token should fail cleanly before record handlers run."""
        org, _branch, user, drugs, _customer = setup_test_data
        sale_id = uuid.uuid4()
        missing_branch_id = uuid.uuid4()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=missing_branch_id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(sale_id),
                        table_name="sales",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(sale_id),
                            "sale_number": "SYNC-MISSING-BRANCH-001",
                            "subtotal": 50.0,
                            "discount_amount": 0.0,
                            "tax_amount": 0.0,
                            "total_amount": 50.0,
                            "payment_method": "cash",
                            "cashier_id": str(user.id),
                            "status": "completed",
                            "items": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "drug_id": str(drugs[0].id),
                                    "drug_name": drugs[0].name,
                                    "quantity": 1,
                                    "unit_price": 50.0,
                                    "subtotal": 50.0,
                                    "total_price": 50.0,
                                }
                            ],
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 1
        assert "Sync branch" in (response.failed[0].error or "")
        assert await db.get(Sale, sale_id) is None

    async def test_branch_owned_payload_branch_id_is_scoped_to_request_branch(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Offline payload branch_id is untrusted; request branch owns the data."""
        org, branch, user, drugs, _customer = setup_test_data
        other_branch = Branch(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Payload Branch",
            code="PAYLOAD",
            is_active=True,
            is_deleted=False,
        )
        db.add(other_branch)
        await db.commit()

        sale_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(sale_id),
                        table_name="sales",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(sale_id),
                            "branch_id": str(other_branch.id),
                            "sale_number": "SYNC-PAYLOAD-BRANCH-001",
                            "subtotal": 50.0,
                            "discount_amount": 0.0,
                            "tax_amount": 0.0,
                            "total_amount": 50.0,
                            "payment_method": "cash",
                            "cashier_id": str(user.id),
                            "status": "completed",
                            "items": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "drug_id": str(drugs[0].id),
                                    "drug_name": drugs[0].name,
                                    "quantity": 1,
                                    "unit_price": 50.0,
                                    "subtotal": 50.0,
                                    "total_price": 50.0,
                                }
                            ],
                        },
                    ),
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(batch_id),
                        table_name="drug_batches",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(batch_id),
                            "branch_id": str(other_branch.id),
                            "drug_id": str(drugs[1].id),
                            "batch_number": "SYNC-PAYLOAD-BRANCH-BATCH-001",
                            "quantity": 2,
                            "remaining_quantity": 2,
                            "expiry_date": str(date.today() + timedelta(days=365)),
                            "cost_price": 25.0,
                            "selling_price": 50.0,
                        },
                    ),
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 2
        assert response.total_failed == 0

        sale = await db.get(Sale, sale_id)
        batch = await db.get(DrugBatch, batch_id)
        assert sale is not None
        assert sale.branch_id == branch.id
        assert batch is not None
        assert batch.branch_id == branch.id

    async def test_branch_owned_record_ids_from_other_branch_are_rejected(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Local UUID collisions across branches fail without mutating owners."""
        org, branch, user, drugs, _customer = setup_test_data
        other_branch = Branch(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Collision Branch",
            code="COLLIDE",
            is_active=True,
            is_deleted=False,
        )
        supplier = Supplier(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Sync Supplier",
            is_active=True,
            is_deleted=False,
        )
        sale_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        adjustment_id = uuid.uuid4()
        inventory_id = uuid.uuid4()
        purchase_order_id = uuid.uuid4()
        db.add_all([
            other_branch,
            supplier,
            Sale(
                id=sale_id,
                organization_id=org.id,
                branch_id=other_branch.id,
                sale_number="OTHER-BRANCH-SALE",
                subtotal=Decimal("50.00"),
                discount_amount=Decimal("0.00"),
                tax_amount=Decimal("0.00"),
                total_amount=Decimal("50.00"),
                payment_method="cash",
                cashier_id=user.id,
                status="completed",
            ),
            DrugBatch(
                id=batch_id,
                branch_id=other_branch.id,
                drug_id=drugs[0].id,
                batch_number="OTHER-BRANCH-BATCH",
                quantity=2,
                remaining_quantity=2,
                expiry_date=date.today() + timedelta(days=365),
            ),
            StockAdjustment(
                id=adjustment_id,
                branch_id=other_branch.id,
                drug_id=drugs[1].id,
                adjustment_type="correction",
                quantity_change=1,
                previous_quantity=0,
                new_quantity=1,
                adjusted_by=user.id,
            ),
            BranchInventory(
                id=inventory_id,
                branch_id=other_branch.id,
                drug_id=drugs[2].id,
                quantity=4,
                reserved_quantity=0,
            ),
            PurchaseOrder(
                id=purchase_order_id,
                organization_id=org.id,
                branch_id=other_branch.id,
                po_number="OTHER-BRANCH-PO-COLLISION",
                supplier_id=supplier.id,
                subtotal=Decimal("10.00"),
                tax_amount=Decimal("0.00"),
                shipping_cost=Decimal("0.00"),
                total_amount=Decimal("10.00"),
                status="draft",
                ordered_by=user.id,
            ),
        ])
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(sale_id),
                        table_name="sales",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(sale_id),
                            "sale_number": "CURRENT-BRANCH-SALE",
                            "subtotal": 50.0,
                            "discount_amount": 0.0,
                            "tax_amount": 0.0,
                            "total_amount": 50.0,
                            "payment_method": "cash",
                            "cashier_id": str(user.id),
                            "status": "completed",
                            "items": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "drug_id": str(drugs[0].id),
                                    "drug_name": drugs[0].name,
                                    "quantity": 1,
                                    "unit_price": 50.0,
                                    "subtotal": 50.0,
                                    "total_price": 50.0,
                                }
                            ],
                        },
                    ),
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(batch_id),
                        table_name="drug_batches",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(batch_id),
                            "drug_id": str(drugs[0].id),
                            "batch_number": "CURRENT-BRANCH-BATCH",
                            "quantity": 2,
                            "remaining_quantity": 2,
                            "expiry_date": str(date.today() + timedelta(days=365)),
                        },
                    ),
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(adjustment_id),
                        table_name="stock_adjustments",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(adjustment_id),
                            "drug_id": str(drugs[1].id),
                            "adjustment_type": "return",
                            "quantity_change": 1,
                            "reason": "Wrong branch collision",
                        },
                    ),
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(inventory_id),
                        table_name="branch_inventory",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(inventory_id),
                            "drug_id": str(drugs[2].id),
                            "quantity": 5,
                            "reserved_quantity": 0,
                        },
                    ),
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(purchase_order_id),
                        table_name="purchase_orders",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(purchase_order_id),
                            "po_number": "CURRENT-BRANCH-PO-COLLISION",
                            "supplier_id": str(supplier.id),
                            "tax_amount": 0.0,
                            "shipping_cost": 0.0,
                            "items": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "drug_id": str(drugs[0].id),
                                    "quantity_ordered": 1,
                                    "unit_cost": 10.0,
                                }
                            ],
                        },
                    ),
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 5
        assert all("sync branch" in (result.error or "") for result in response.failed)

        other_sale = await db.get(Sale, sale_id)
        other_batch = await db.get(DrugBatch, batch_id)
        other_adjustment = await db.get(StockAdjustment, adjustment_id)
        other_inventory = await db.get(BranchInventory, inventory_id)
        other_po = await db.get(PurchaseOrder, purchase_order_id)
        assert other_sale is not None
        assert other_sale.branch_id == other_branch.id
        assert other_sale.sale_number == "OTHER-BRANCH-SALE"
        assert other_batch is not None
        assert other_batch.branch_id == other_branch.id
        assert other_batch.batch_number == "OTHER-BRANCH-BATCH"
        assert other_adjustment is not None
        assert other_adjustment.branch_id == other_branch.id
        assert other_adjustment.adjustment_type == "correction"
        assert other_inventory is not None
        assert other_inventory.branch_id == other_branch.id
        assert other_inventory.quantity == 4
        assert other_po is not None
        assert other_po.branch_id == other_branch.id
        assert other_po.po_number == "OTHER-BRANCH-PO-COLLISION"

    async def test_push_sale_clears_missing_prescription_id(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Offline sales should not fail when their optional Rx link is stale."""
        org, branch, user, drugs, customer = setup_test_data

        sale_id = uuid.uuid4()
        missing_prescription_id = uuid.uuid4()

        record_data = {
            "id": str(sale_id),
            "sale_number": "SYNC-MISSING-RX-001",
            "customer_id": str(customer.id),
            "subtotal": 50.0,
            "discount_amount": 0.0,
            "tax_amount": 0.0,
            "total_amount": 50.0,
            "payment_method": "cash",
            "cashier_id": str(user.id),
            "prescription_id": str(missing_prescription_id),
            "status": "completed",
            "items": [
                {
                    "id": str(uuid.uuid4()),
                    "drug_id": str(drugs[0].id),
                    "drug_name": drugs[0].name,
                    "quantity": 1,
                    "unit_price": 50.0,
                    "subtotal": 50.0,
                    "total_price": 50.0,
                }
            ],
        }

        request = PushRequest(
            branch_id=branch.id,
            records=[
                PushRecord(
                    operation_id=uuid.uuid4(),
                    local_id=str(sale_id),
                    table_name="sales",
                    operation="create",
                    sync_version=1,
                    created_offline_at=datetime.now(timezone.utc),
                    data=record_data,
                )
            ],
        )

        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 1
        assert response.total_failed == 0
        assert response.accepted[0].fk_fixes == [
            f"prescription_id={missing_prescription_id}"
        ]

        sale = await db.get(Sale, sale_id)
        assert sale is not None
        assert sale.prescription_id is None

    async def test_push_batch_sorts_customer_prescription_before_sale(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A mixed offline batch should resolve dependencies before the sale."""
        org, branch, user, drugs, _customer = setup_test_data
        customer_id = uuid.uuid4()
        prescription_id = uuid.uuid4()
        sale_id = uuid.uuid4()

        sale_record = PushRecord(
            operation_id=uuid.uuid4(),
            local_id=str(sale_id),
            table_name="sales",
            operation="create",
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={
                "id": str(sale_id),
                "sale_number": "SYNC-MIXED-BATCH-001",
                "customer_id": str(customer_id),
                "subtotal": 50.0,
                "discount_amount": 0.0,
                "tax_amount": 0.0,
                "total_amount": 50.0,
                "payment_method": "cash",
                "cashier_id": str(user.id),
                "prescription_id": str(prescription_id),
                "status": "completed",
                "items": [
                    {
                        "id": str(uuid.uuid4()),
                        "drug_id": str(drugs[0].id),
                        "drug_name": drugs[0].name,
                        "quantity": 1,
                        "unit_price": 50.0,
                        "subtotal": 50.0,
                        "discount_amount": 0.0,
                        "tax_amount": 0.0,
                        "total_price": 50.0,
                    }
                ],
            },
        )
        prescription_record = PushRecord(
            operation_id=uuid.uuid4(),
            local_id=str(prescription_id),
            table_name="prescriptions",
            operation="create",
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={
                "id": str(prescription_id),
                "branch_id": str(branch.id),
                "prescription_number": "RX-MIXED-BATCH-001",
                "customer_id": str(customer_id),
                "prescriber_name": "Dr Sync",
                "prescriber_license": "MD-SYNC-1",
                "issue_date": str(date.today()),
                "expiry_date": str(date.today() + timedelta(days=30)),
                "medications": [
                    {
                        "drug_id": str(drugs[0].id),
                        "drug_name": drugs[0].name,
                        "quantity": 1,
                    }
                ],
                "refills_allowed": 1,
                "refills_remaining": 1,
                "status": "active",
            },
        )
        customer_record = PushRecord(
            operation_id=uuid.uuid4(),
            local_id=str(customer_id),
            table_name="customers",
            operation="create",
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={
                "id": str(customer_id),
                "customer_type": "walk_in",
                "first_name": "Mixed",
                "last_name": "Batch",
                "phone": "0503333333",
                "loyalty_tier": "bronze",
                "loyalty_points": 0,
            },
        )

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[sale_record, prescription_record, customer_record],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 3
        assert response.total_failed == 0

        sale = await db.get(Sale, sale_id)
        prescription = await db.get(Prescription, prescription_id)
        assert sale is not None
        assert sale.customer_id == customer_id
        assert sale.prescription_id == prescription_id
        assert prescription is not None
        assert prescription.refills_remaining == 0
        assert prescription.status == "filled"

    async def test_push_prescription_defaults_missing_branch_to_sync_branch(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Server-side scope should fill branch_id when old clients omit it."""
        org, branch, user, drugs, customer = setup_test_data
        prescription_id = uuid.uuid4()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(prescription_id),
                        table_name="prescriptions",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(prescription_id),
                            "prescription_number": "RX-MISSING-BRANCH-001",
                            "customer_id": str(customer.id),
                            "prescriber_name": "Dr Offline",
                            "prescriber_license": "MD-OFFLINE-1",
                            "issue_date": str(date.today()),
                            "expiry_date": str(date.today() + timedelta(days=30)),
                            "medications": [
                                {
                                    "drug_id": str(drugs[0].id),
                                    "drug_name": drugs[0].name,
                                    "quantity": 1,
                                }
                            ],
                            "refills_allowed": 1,
                            "refills_remaining": 1,
                            "status": "active",
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 1
        assert response.total_failed == 0

        prescription = await db.get(Prescription, prescription_id)
        assert prescription is not None
        assert prescription.branch_id == branch.id

    async def test_push_prescription_does_not_accept_other_branch_record(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A stale prescription ID from another branch must not sync as success."""
        org, branch, user, drugs, customer = setup_test_data
        other_branch = Branch(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Other Rx Branch",
            code="RX002",
            is_active=True,
            is_deleted=False,
        )
        existing_id = uuid.uuid4()
        db.add(other_branch)
        db.add(Prescription(
            id=existing_id,
            organization_id=org.id,
            branch_id=other_branch.id,
            prescription_number="RX-OTHER-BRANCH-001",
            customer_id=customer.id,
            prescriber_name="Dr Other",
            prescriber_license="MD-OTHER-1",
            issue_date=date.today(),
            expiry_date=date.today() + timedelta(days=30),
            medications=[
                {
                    "drug_id": str(drugs[0].id),
                    "drug_name": drugs[0].name,
                    "quantity": 1,
                }
            ],
            refills_allowed=1,
            refills_remaining=1,
            status="active",
        ))
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(existing_id),
                        table_name="prescriptions",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "id": str(existing_id),
                            "prescription_number": "RX-CURRENT-BRANCH-001",
                            "customer_id": str(customer.id),
                            "prescriber_name": "Dr Current",
                            "prescriber_license": "MD-CURRENT-1",
                            "issue_date": str(date.today()),
                            "expiry_date": str(date.today() + timedelta(days=30)),
                            "medications": [
                                {
                                    "drug_id": str(drugs[0].id),
                                    "drug_name": drugs[0].name,
                                    "quantity": 1,
                                }
                            ],
                            "refills_allowed": 1,
                            "refills_remaining": 1,
                            "status": "active",
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 1

        existing = await db.get(Prescription, existing_id)
        assert existing is not None
        assert existing.branch_id == other_branch.id
        assert existing.prescription_number == "RX-OTHER-BRANCH-001"
        assert existing.prescriber_name == "Dr Other"

    async def test_push_sale_idempotency_with_items(self, db: AsyncSession, setup_test_data):
        """Test that re-pushing a sale doesn't duplicate items or fail."""
        org, branch, user, drugs, customer = setup_test_data

        sale_id = uuid.uuid4()

        record_data = {
            "id": str(sale_id),
            "sale_number": "IDEM-001",
            "subtotal": 50.0,
            "total_amount": 50.0,
            "payment_method": "card",
            "cashier_id": str(user.id),
            "items": [{
                "drug_id": str(drugs[1].id),
                "drug_name": drugs[1].name,
                "quantity": 1,
                "unit_price": 50.0,
                "subtotal": 50.0,
                "total_price": 50.0,
            }]
        }

        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(sale_id),
                table_name="sales",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data=record_data
            )]
        )

        # First push
        await SyncService.push(db, request, org.id, user.id)

        # Second push
        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 1 # Still accepted (idempotent)

        # Verify no duplicate items
        result = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale_id))
        items = result.scalars().all()
        assert len(items) == 1

    async def test_protocol_v2_sale_deducts_inventory_once_with_fefo(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A retry must replay its receipt without repeating stock side effects."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        inventory = BranchInventory(
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=5,
            reserved_quantity=0,
        )
        first_batch = DrugBatch(
            branch_id=branch.id,
            drug_id=drug.id,
            batch_number="FEFO-1",
            quantity=2,
            remaining_quantity=2,
            expiry_date=date.today() + timedelta(days=30),
            cost_price=Decimal("20.00"),
            selling_price=Decimal("50.00"),
        )
        second_batch = DrugBatch(
            branch_id=branch.id,
            drug_id=drug.id,
            batch_number="FEFO-2",
            quantity=3,
            remaining_quantity=3,
            expiry_date=date.today() + timedelta(days=60),
            cost_price=Decimal("22.00"),
            selling_price=Decimal("50.00"),
        )
        db.add_all([inventory, first_batch, second_batch])
        await db.commit()

        sale_id = uuid.uuid4()
        operation_id = uuid.uuid4()
        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=operation_id,
                local_id=str(sale_id),
                table_name="sales",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(sale_id),
                    "sale_number": "OFFLINE-V2-001",
                    "subtotal": 150.0,
                    "discount_amount": 0.0,
                    "tax_amount": 0.0,
                    "total_amount": 150.0,
                    "payment_method": "cash",
                    "cashier_id": str(user.id),
                    "status": "completed",
                    "sync_protocol_version": 2,
                    "items": [{
                        "id": str(uuid.uuid4()),
                        "drug_id": str(drug.id),
                        "drug_name": drug.name,
                        "quantity": 3,
                        "unit_price": 50.0,
                        "subtotal": 150.0,
                        "discount_amount": 0.0,
                        "tax_amount": 0.0,
                        "total_price": 150.0,
                    }],
                },
            )],
        )

        first_response = await SyncService.push(db, request, org.id, user.id)
        retry_response = await SyncService.push(db, request, org.id, user.id)

        assert (
            first_response.accepted[0].model_dump()
            == retry_response.accepted[0].model_dump()
        )
        await db.refresh(inventory)
        await db.refresh(first_batch)
        await db.refresh(second_batch)
        assert inventory.quantity == 2
        assert first_batch.remaining_quantity == 0
        assert second_batch.remaining_quantity == 2
        allocations = (await db.execute(
            select(SaleItemBatchAllocation).join(SaleItem).where(
                SaleItem.sale_id == sale_id
            )
        )).scalars().all()
        movements = (await db.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_id == sale_id
            )
        )).scalars().all()
        assert sorted(allocation.quantity for allocation in allocations) == [1, 2]
        assert sorted(movement.quantity_change for movement in movements) == [-2, -1]
        assert await db.get(SyncOperationReceipt, operation_id) is not None

    async def test_operation_id_cannot_be_reused_for_another_mutation(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Operation IDs are globally stable identities, not retry hints."""
        org, branch, user, drugs, customer = setup_test_data
        operation_id = uuid.uuid4()

        def customer_request(customer_id: uuid.UUID, phone: str) -> PushRequest:
            return PushRequest(
                branch_id=branch.id,
                records=[PushRecord(
                    operation_id=operation_id,
                    local_id=str(customer_id),
                    table_name="customers",
                    operation="create",
                    sync_version=1,
                    created_offline_at=datetime.now(timezone.utc),
                    data={
                        "id": str(customer_id),
                        "customer_type": "walk_in",
                        "first_name": "Offline",
                        "last_name": "Customer",
                        "phone": phone,
                        "loyalty_tier": "bronze",
                        "loyalty_points": 0,
                    },
                )],
            )

        first_id = uuid.uuid4()
        second_id = uuid.uuid4()
        first = await SyncService.push(
            db,
            customer_request(first_id, "0501111111"),
            org.id,
            user.id,
        )
        second = await SyncService.push(
            db,
            customer_request(second_id, "0502222222"),
            org.id,
            user.id,
        )

        assert first.total_accepted == 1
        assert second.total_failed == 1
        assert "different sync mutation" in (second.failed[0].error or "")
        assert await db.get(Customer, second_id) is None

    async def test_protocol_v2_batch_receipt_updates_inventory_once(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Batch, aggregate stock, adjustment, and ledger share one receipt."""
        org, branch, user, drugs, customer = setup_test_data
        inventory = BranchInventory(
            branch_id=branch.id,
            drug_id=drugs[1].id,
            quantity=4,
            reserved_quantity=0,
        )
        db.add(inventory)
        await db.commit()

        batch_id = uuid.uuid4()
        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(batch_id),
                table_name="drug_batches",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(batch_id),
                    "branch_id": str(branch.id),
                    "drug_id": str(drugs[1].id),
                    "batch_number": "OFFLINE-RECEIPT-001",
                    "quantity": 3,
                    "remaining_quantity": 3,
                    "expiry_date": str(date.today() + timedelta(days=365)),
                    "cost_price": 25.0,
                    "selling_price": 50.0,
                    "sync_protocol_version": 2,
                },
            )],
        )

        first = await SyncService.push(db, request, org.id, user.id)
        retry = await SyncService.push(db, request, org.id, user.id)

        assert first.total_accepted == retry.total_accepted == 1
        await db.refresh(inventory)
        assert inventory.quantity == 7
        adjustments = (await db.execute(
            select(StockAdjustment).where(
                StockAdjustment.branch_id == branch.id,
                StockAdjustment.drug_id == drugs[1].id,
            )
        )).scalars().all()
        movements = (await db.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_id == batch_id,
            )
        )).scalars().all()
        assert len(adjustments) == 1
        assert adjustments[0].quantity_change == 3
        assert len(movements) == 1
        assert movements[0].quantity_change == 3

    async def test_offline_adjustment_keeps_batch_and_inventory_parity(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A stock adjustment deducts FEFO batches and is replay-safe."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[2]
        inventory = BranchInventory(
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=5,
            reserved_quantity=0,
        )
        first_batch = DrugBatch(
            branch_id=branch.id,
            drug_id=drug.id,
            batch_number="ADJ-FEFO-1",
            quantity=2,
            remaining_quantity=2,
            expiry_date=date.today() + timedelta(days=20),
        )
        second_batch = DrugBatch(
            branch_id=branch.id,
            drug_id=drug.id,
            batch_number="ADJ-FEFO-2",
            quantity=3,
            remaining_quantity=3,
            expiry_date=date.today() + timedelta(days=40),
        )
        db.add_all([inventory, first_batch, second_batch])
        await db.commit()

        adjustment_id = uuid.uuid4()
        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(adjustment_id),
                table_name="stock_adjustments",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(adjustment_id),
                    "branch_id": str(branch.id),
                    "drug_id": str(drug.id),
                    "adjustment_type": "damage",
                    "quantity_change": -3,
                    "reason": "Damaged during transport",
                },
            )],
        )

        first = await SyncService.push(db, request, org.id, user.id)
        retry = await SyncService.push(db, request, org.id, user.id)

        assert first.total_accepted == retry.total_accepted == 1
        await db.refresh(inventory)
        await db.refresh(first_batch)
        await db.refresh(second_batch)
        assert inventory.quantity == 2
        assert first_batch.remaining_quantity == 0
        assert second_batch.remaining_quantity == 2
        assert await db.get(StockAdjustment, adjustment_id) is not None
        movements = (await db.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_id == adjustment_id,
            )
        )).scalars().all()
        assert sorted(movement.quantity_change for movement in movements) == [-2, -1]

    async def test_push_purchase_order_does_not_accept_other_branch_record(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A stale local PO ID from another branch must not be treated as idempotent."""
        org, branch, user, drugs, customer = setup_test_data
        other_branch = Branch(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Other Branch",
            code="TB002",
            is_active=True,
            is_deleted=False,
        )
        existing_id = uuid.uuid4()
        db.add(other_branch)
        db.add(PurchaseOrder(
            id=existing_id,
            organization_id=org.id,
            branch_id=other_branch.id,
            po_number="OTHER-BRANCH-PO",
            supplier_id=uuid.uuid4(),
            subtotal=Decimal("10.00"),
            tax_amount=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            total_amount=Decimal("10.00"),
            status="draft",
            ordered_by=user.id,
        ))
        await db.commit()

        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(existing_id),
                table_name="purchase_orders",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(existing_id),
                    "po_number": "CURRENT-BRANCH-PO",
                    "supplier_id": str(uuid.uuid4()),
                    "subtotal": 20.0,
                    "tax_amount": 0.0,
                    "shipping_cost": 0.0,
                    "total_amount": 20.0,
                    "status": "draft",
                },
            )],
        )

        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 0
        assert response.total_failed == 1

        existing = await db.get(PurchaseOrder, existing_id)
        assert existing is not None
        assert existing.branch_id == other_branch.id
        assert existing.po_number == "OTHER-BRANCH-PO"

    async def test_push_customer_does_not_accept_other_org_record(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A stale local customer ID from another org must not sync as success."""
        org, branch, user, drugs, customer = setup_test_data
        other_org = Organization(
            id=uuid.uuid4(),
            name="Other Pharmacy",
            type="pharmacy",
            tax_id="987654321",
            settings={},
        )
        existing_id = uuid.uuid4()
        db.add(other_org)
        db.add(Customer(
            id=existing_id,
            organization_id=other_org.id,
            first_name="Other",
            last_name="Customer",
            phone="0509999999",
            loyalty_tier="bronze",
            loyalty_points=0,
        ))
        await db.commit()

        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(existing_id),
                table_name="customers",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(existing_id),
                    "customer_type": "walk_in",
                    "first_name": "Current",
                    "last_name": "Customer",
                    "phone": "0501111111",
                    "loyalty_tier": "bronze",
                    "loyalty_points": 0,
                },
            )],
        )

        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 0
        assert response.total_failed == 1

        existing = await db.get(Customer, existing_id)
        assert existing is not None
        assert existing.organization_id == other_org.id
        assert existing.first_name == "Other"
