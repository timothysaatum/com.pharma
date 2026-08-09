"""
Integration tests for syncing Sale and SaleItem records.
"""

import pytest
import sqlite3
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
from app.models.prescriptions.prescription_model import Prescription
from app.models.sales.sales_model import Sale, SaleItem, SaleItemBatchAllocation
from app.models.sales.sales_model import PurchaseOrder, Supplier
from app.models.system_md.sys_models import SyncOperationReceipt, SystemAlert
from app.models.user.user_model import User
from app.schemas.sync_schemas import PushRequest, PushRecord, PullRequest, CrrPushRecord
from app.schemas import sync_error_codes
from app.services.sync.sync_service import SyncService
from app.services.sync.crr_sync_service import CrrSyncService
from app.services.sync.shadow_db import _CRR_TABLE_CONFIG, _resolve_extension_path, get_shadow_db


@pytest.mark.asyncio
class TestSyncSaleItems:
    """Test suite for syncing sales with items."""

    async def test_push_sale_with_items(self, db: AsyncSession, setup_test_data):
        """Test pushing a sale record that includes nested items."""
        org, branch, user, drugs, customer = setup_test_data

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="SYNC-SALE-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

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
            "sync_protocol_version": 2,
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

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="SYNC-STALE-CASHIER-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

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
            "sync_protocol_version": 2,
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

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="SYNC-PAYLOAD-BRANCH-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
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
                            "sync_protocol_version": 2,
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
        db.add_all([other_branch, supplier])
        # PostgreSQL enforces these foreign keys during the flush.  The models
        # intentionally do not expose ORM relationships, so SQLAlchemy cannot
        # infer that the branch and supplier must be inserted before the rows
        # below when they are all added in one batch.
        await db.flush()
        db.add_all([
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
                            "sync_protocol_version": 2,
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

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="SYNC-MISSING-RX-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

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
            "sync_protocol_version": 2,
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

    async def test_push_sale_rejects_rx_drug_without_valid_prescription(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Rx-only sales must not sync if their prescription is missing."""
        org, branch, user, drugs, customer = setup_test_data
        drugs[0].requires_prescription = True
        await db.commit()

        sale_id = uuid.uuid4()
        missing_prescription_id = uuid.uuid4()
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
                            "sale_number": "SYNC-MISSING-RX-REJECT-001",
                            "customer_id": str(customer.id),
                            "subtotal": 50.0,
                            "discount_amount": 0.0,
                            "tax_amount": 0.0,
                            "total_amount": 50.0,
                            "payment_method": "cash",
                            "cashier_id": str(user.id),
                            "prescription_id": str(missing_prescription_id),
                            "status": "completed",
                            "sync_protocol_version": 2,
                            "items": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "drug_id": str(drugs[0].id),
                                    "drug_name": drugs[0].name,
                                    "quantity": 1,
                                    "unit_price": 50.0,
                                    "subtotal": 50.0,
                                    "total_price": 50.0,
                                    "requires_prescription": True,
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
        assert "prescription-required" in (response.failed[0].error or "")
        assert response.failed[0].fk_fixes == [
            f"prescription_id={missing_prescription_id}"
        ]
        assert await db.get(Sale, sale_id) is None
        # Contract check for docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md
        # finding S4: this deferred-retry failure must carry the typed
        # error_code the client's sync engine actually branches on
        # (src/lib/syncErrorCodes.ts) — not just matching prose.
        assert response.failed[0].error_code == sync_error_codes.DEPENDENCY_NOT_SYNCED

    async def test_push_batch_sorts_customer_prescription_before_sale(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A mixed offline batch should resolve dependencies before the sale."""
        org, branch, user, drugs, _customer = setup_test_data

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="SYNC-MIXED-BATCH-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

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
                "sync_protocol_version": 2,
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

    async def test_offline_customer_prescription_sale_refund_flow_is_idempotent(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Real offline flow: customer, Rx, sale, refund, retry."""
        org, branch, user, drugs, _customer = setup_test_data
        drug = drugs[0]
        drug.requires_prescription = True

        inventory = BranchInventory(
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=2,
            reserved_quantity=0,
        )
        batch = DrugBatch(
            branch_id=branch.id,
            drug_id=drug.id,
            batch_number="OFFLINE-RX-REFUND-BATCH",
            quantity=2,
            remaining_quantity=2,
            expiry_date=date.today() + timedelta(days=365),
            cost_price=Decimal("20.00"),
            selling_price=Decimal("50.00"),
        )
        db.add_all([inventory, batch])
        await db.commit()

        customer_id = uuid.uuid4()
        prescription_id = uuid.uuid4()
        sale_id = uuid.uuid4()
        sale_item_id = uuid.uuid4()
        refund_id = uuid.uuid4()

        customer_record = PushRecord(
            operation_id=uuid.uuid4(),
            local_id=str(customer_id),
            table_name="customers",
            operation="create",
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={
                "id": str(customer_id),
                "customer_type": "registered",
                "first_name": "Offline",
                "last_name": "Refund",
                "phone": "0504444444",
                "loyalty_tier": "bronze",
                "loyalty_points": 0,
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
                "prescription_number": "RX-OFFLINE-REFUND-001",
                "customer_id": str(customer_id),
                "prescriber_name": "Dr Offline",
                "prescriber_license": "MD-OFF-REF-1",
                "issue_date": str(date.today()),
                "expiry_date": str(date.today() + timedelta(days=30)),
                "medications": [
                    {
                        "drug_id": str(drug.id),
                        "drug_name": drug.name,
                        "quantity": 1,
                    }
                ],
                "refills_allowed": 1,
                "refills_remaining": 1,
                "status": "active",
            },
        )
        sale_record = PushRecord(
            operation_id=uuid.uuid4(),
            local_id=str(sale_id),
            table_name="sales",
            operation="create",
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={
                "id": str(sale_id),
                "sale_number": "OFFLINE-RX-REFUND-SALE",
                "customer_id": str(customer_id),
                "subtotal": 50.0,
                "discount_amount": 0.0,
                "tax_amount": 0.0,
                "total_amount": 50.0,
                "payment_method": "cash",
                "cashier_id": str(user.id),
                "prescription_id": str(prescription_id),
                "status": "completed",
                "sync_protocol_version": 2,
                "items": [
                    {
                        "id": str(sale_item_id),
                        "drug_id": str(drug.id),
                        "drug_name": drug.name,
                        "quantity": 1,
                        "unit_price": 50.0,
                        "subtotal": 50.0,
                        "discount_amount": 0.0,
                        "tax_amount": 0.0,
                        "total_price": 50.0,
                        "requires_prescription": True,
                        "prescription_verified": True,
                    }
                ],
            },
        )
        refund_record = PushRecord(
            operation_id=uuid.uuid4(),
            local_id=str(refund_id),
            table_name="sale_refunds",
            operation="create",
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={
                "sale_id": str(sale_id),
                "reason": "Offline customer returned prescribed item",
                "items_to_refund": [
                    {
                        "sale_item_id": str(sale_item_id),
                        "quantity": 1,
                        "reason": "Returned unopened",
                        "restock": True,
                    }
                ],
                "refund_amount": 50.0,
                "refund_method": "cash",
                "manager_approval_user_id": str(user.id),
            },
        )

        request = PushRequest(
            branch_id=branch.id,
            records=[
                refund_record,
                sale_record,
                prescription_record,
                customer_record,
            ],
        )

        first_response = await SyncService.push(db, request, org.id, user.id)
        retry_response = await SyncService.push(db, request, org.id, user.id)

        assert first_response.total_accepted == 4
        assert first_response.total_failed == 0
        assert retry_response.total_accepted == 4
        assert retry_response.total_failed == 0

        sale = await db.get(Sale, sale_id)
        sale_item = await db.get(SaleItem, sale_item_id)
        prescription = await db.get(Prescription, prescription_id)
        await db.refresh(inventory)
        await db.refresh(batch)

        assert sale is not None
        assert sale.status == "refunded"
        assert sale.payment_status == "refunded"
        assert sale.refund_amount == Decimal("50.00")
        assert sale.refund_reference == f"OFFLINE-{refund_id}"
        assert sale_item is not None
        assert sale_item.refunded_quantity == 1
        assert prescription is not None
        assert prescription.refills_remaining == 1
        assert prescription.status == "active"
        assert inventory.quantity == 2
        assert batch.remaining_quantity == 2

        allocations = (await db.execute(
            select(SaleItemBatchAllocation).where(
                SaleItemBatchAllocation.sale_item_id == sale_item_id
            )
        )).scalars().all()
        movements = (await db.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_id == sale_id
            )
        )).scalars().all()
        adjustments = (await db.execute(
            select(StockAdjustment).where(
                StockAdjustment.branch_id == branch.id,
                StockAdjustment.drug_id == drug.id,
            )
        )).scalars().all()

        assert len(allocations) == 1
        assert allocations[0].refunded_quantity == 1
        assert sorted(movement.quantity_change for movement in movements) == [-1, 1]
        assert sorted(adj.quantity_change for adj in adjustments) == [-1, 1]

    async def test_offline_refund_rejects_sale_from_other_branch(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Refund commands cannot cross branch ownership boundaries."""
        org, branch, user, drugs, customer = setup_test_data
        other_branch = Branch(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Refund Other Branch",
            code="REF002",
            is_active=True,
            is_deleted=False,
        )
        sale_id = uuid.uuid4()
        sale_item_id = uuid.uuid4()
        db.add(other_branch)
        db.add(Sale(
            id=sale_id,
            organization_id=org.id,
            branch_id=other_branch.id,
            sale_number="OTHER-BRANCH-REFUND-SALE",
            customer_id=customer.id,
            subtotal=Decimal("50.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("50.00"),
            payment_method="cash",
            payment_status="completed",
            cashier_id=user.id,
            status="completed",
        ))
        db.add(SaleItem(
            id=sale_item_id,
            sale_id=sale_id,
            drug_id=drugs[0].id,
            drug_name=drugs[0].name,
            quantity=1,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("50.00"),
            total_price=Decimal("50.00"),
        ))
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(uuid.uuid4()),
                        table_name="sale_refunds",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "sale_id": str(sale_id),
                            "reason": "Offline refund wrong branch",
                            "items_to_refund": [
                                {
                                    "sale_item_id": str(sale_item_id),
                                    "quantity": 1,
                                    "reason": "Wrong branch",
                                    "restock": True,
                                }
                            ],
                            "refund_amount": 50.0,
                            "refund_method": "cash",
                            "manager_approval_user_id": str(user.id),
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 1
        assert "sync branch" in (response.failed[0].error or "")

        sale = await db.get(Sale, sale_id)
        sale_item = await db.get(SaleItem, sale_item_id)
        assert sale is not None
        assert sale.status == "completed"
        assert sale.refund_amount is None
        assert sale_item is not None
        assert sale_item.refunded_quantity == 0

    async def test_offline_refund_rejects_amount_mismatch_without_mutation(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Refund amount must match selected items exactly."""
        org, branch, user, drugs, customer = setup_test_data
        sale_id = uuid.uuid4()
        sale_item_id = uuid.uuid4()
        db.add(Sale(
            id=sale_id,
            organization_id=org.id,
            branch_id=branch.id,
            sale_number="REFUND-AMOUNT-MISMATCH-SALE",
            customer_id=customer.id,
            subtotal=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
            payment_method="cash",
            payment_status="completed",
            cashier_id=user.id,
            status="completed",
        ))
        db.add(SaleItem(
            id=sale_item_id,
            sale_id=sale_id,
            drug_id=drugs[0].id,
            drug_name=drugs[0].name,
            quantity=2,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("100.00"),
            total_price=Decimal("100.00"),
        ))
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(uuid.uuid4()),
                        table_name="sale_refunds",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "sale_id": str(sale_id),
                            "reason": "Offline refund incorrect amount",
                            "items_to_refund": [
                                {
                                    "sale_item_id": str(sale_item_id),
                                    "quantity": 1,
                                    "reason": "Amount mismatch",
                                    "restock": False,
                                }
                            ],
                            "refund_amount": 10.0,
                            "refund_method": "cash",
                            "manager_approval_user_id": str(user.id),
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 1
        assert "must equal the selected item value" in (
            response.failed[0].error or ""
        )

        sale = await db.get(Sale, sale_id)
        sale_item = await db.get(SaleItem, sale_item_id)
        assert sale is not None
        assert sale.status == "completed"
        assert sale.refund_amount is None
        assert sale_item is not None
        assert sale_item.refunded_quantity == 0

    async def test_offline_refund_rejects_over_refund_without_mutation(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Cumulative offline refunds cannot exceed the sale total."""
        org, branch, user, drugs, customer = setup_test_data
        sale_id = uuid.uuid4()
        sale_item_id = uuid.uuid4()
        db.add(Sale(
            id=sale_id,
            organization_id=org.id,
            branch_id=branch.id,
            sale_number="REFUND-OVER-TOTAL-SALE",
            customer_id=customer.id,
            subtotal=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
            payment_method="cash",
            payment_status="partial",
            cashier_id=user.id,
            status="partially_refunded",
            refund_amount=Decimal("75.00"),
        ))
        db.add(SaleItem(
            id=sale_item_id,
            sale_id=sale_id,
            drug_id=drugs[0].id,
            drug_name=drugs[0].name,
            quantity=2,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("100.00"),
            total_price=Decimal("100.00"),
            refunded_quantity=1,
        ))
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(uuid.uuid4()),
                        table_name="sale_refunds",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "sale_id": str(sale_id),
                            "reason": "Offline refund over sale total",
                            "items_to_refund": [
                                {
                                    "sale_item_id": str(sale_item_id),
                                    "quantity": 1,
                                    "reason": "Over total",
                                    "restock": False,
                                }
                            ],
                            "refund_amount": 50.0,
                            "refund_method": "cash",
                            "manager_approval_user_id": str(user.id),
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 1
        assert "cannot exceed sale total" in (response.failed[0].error or "")

        sale = await db.get(Sale, sale_id)
        sale_item = await db.get(SaleItem, sale_item_id)
        assert sale is not None
        assert sale.status == "partially_refunded"
        assert sale.refund_amount == Decimal("75.00")
        assert sale_item is not None
        assert sale_item.refunded_quantity == 1

    async def test_offline_refund_without_restock_does_not_touch_inventory(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Damaged/non-restocked returns should not create inventory effects."""
        org, branch, user, drugs, customer = setup_test_data
        sale_id = uuid.uuid4()
        sale_item_id = uuid.uuid4()
        inventory = BranchInventory(
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=0,
            reserved_quantity=0,
        )
        db.add(inventory)
        db.add(Sale(
            id=sale_id,
            organization_id=org.id,
            branch_id=branch.id,
            sale_number="REFUND-NO-RESTOCK-SALE",
            customer_id=customer.id,
            subtotal=Decimal("50.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("50.00"),
            payment_method="cash",
            payment_status="completed",
            cashier_id=user.id,
            status="completed",
        ))
        db.add(SaleItem(
            id=sale_item_id,
            sale_id=sale_id,
            drug_id=drugs[0].id,
            drug_name=drugs[0].name,
            quantity=1,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("50.00"),
            total_price=Decimal("50.00"),
        ))
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(uuid.uuid4()),
                        table_name="sale_refunds",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "sale_id": str(sale_id),
                            "reason": "Offline damaged return no restock",
                            "items_to_refund": [
                                {
                                    "sale_item_id": str(sale_item_id),
                                    "quantity": 1,
                                    "reason": "Damaged return",
                                    "restock": False,
                                }
                            ],
                            "refund_amount": 50.0,
                            "refund_method": "cash",
                            "manager_approval_user_id": str(user.id),
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 1
        assert response.total_failed == 0

        sale = await db.get(Sale, sale_id)
        sale_item = await db.get(SaleItem, sale_item_id)
        await db.refresh(inventory)
        movements = (await db.execute(
            select(InventoryMovement).where(InventoryMovement.source_id == sale_id)
        )).scalars().all()
        adjustments = (await db.execute(
            select(StockAdjustment).where(
                StockAdjustment.branch_id == branch.id,
                StockAdjustment.drug_id == drugs[0].id,
            )
        )).scalars().all()

        assert sale is not None
        assert sale.status == "refunded"
        assert sale_item is not None
        assert sale_item.refunded_quantity == 1
        assert inventory.quantity == 0
        assert movements == []
        assert adjustments == []

    async def test_offline_refund_missing_inventory_rolls_back_refund_state(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A restock failure must not leave a phantom refunded sale."""
        org, branch, user, drugs, customer = setup_test_data
        sale_id = uuid.uuid4()
        sale_item_id = uuid.uuid4()
        db.add(Sale(
            id=sale_id,
            organization_id=org.id,
            branch_id=branch.id,
            sale_number="REFUND-MISSING-INVENTORY-SALE",
            customer_id=customer.id,
            subtotal=Decimal("50.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("50.00"),
            payment_method="cash",
            payment_status="completed",
            cashier_id=user.id,
            status="completed",
        ))
        db.add(SaleItem(
            id=sale_item_id,
            sale_id=sale_id,
            drug_id=drugs[0].id,
            drug_name=drugs[0].name,
            quantity=1,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("50.00"),
            total_price=Decimal("50.00"),
        ))
        await db.commit()

        response = await SyncService.push(
            db,
            PushRequest(
                branch_id=branch.id,
                records=[
                    PushRecord(
                        operation_id=uuid.uuid4(),
                        local_id=str(uuid.uuid4()),
                        table_name="sale_refunds",
                        operation="create",
                        sync_version=1,
                        created_offline_at=datetime.now(timezone.utc),
                        data={
                            "sale_id": str(sale_id),
                            "reason": "Offline restock but inventory missing",
                            "items_to_refund": [
                                {
                                    "sale_item_id": str(sale_item_id),
                                    "quantity": 1,
                                    "reason": "Inventory missing",
                                    "restock": True,
                                }
                            ],
                            "refund_amount": 50.0,
                            "refund_method": "cash",
                            "manager_approval_user_id": str(user.id),
                        },
                    )
                ],
            ),
            org.id,
            user.id,
        )

        assert response.total_accepted == 0
        assert response.total_failed == 1
        assert "Inventory record missing" in (response.failed[0].error or "")

        sale = await db.get(Sale, sale_id)
        sale_item = await db.get(SaleItem, sale_item_id)
        movements = (await db.execute(
            select(InventoryMovement).where(InventoryMovement.source_id == sale_id)
        )).scalars().all()
        assert sale is not None
        assert sale.status == "completed"
        assert sale.refund_amount is None
        assert sale_item is not None
        assert sale_item.refunded_quantity == 0
        assert movements == []

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

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[1].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[1].id,
                batch_number="IDEM-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

        sale_id = uuid.uuid4()

        record_data = {
            "id": str(sale_id),
            "sale_number": "IDEM-001",
            "subtotal": 50.0,
            "total_amount": 50.0,
            "payment_method": "card",
            "cashier_id": str(user.id),
            "sync_protocol_version": 2,
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
        supplier = Supplier(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Other Branch Supplier",
            is_active=True,
            is_deleted=False,
        )
        existing_id = uuid.uuid4()
        db.add_all([other_branch, supplier])
        await db.flush()
        db.add(PurchaseOrder(
            id=existing_id,
            organization_id=org.id,
            branch_id=other_branch.id,
            po_number="OTHER-BRANCH-PO",
            supplier_id=supplier.id,
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
        await db.flush()
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


@pytest.mark.asyncio
class TestVoidFailedSale:
    """
    Regression coverage for docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md
    finding P3: voiding a dead-lettered offline sale must require manager
    approval and leave an audit trail, unlike the old client-only
    ``discardFailure`` which silently flipped a local flag.
    """

    async def test_self_approval_by_privileged_user_succeeds_and_is_audited(
        self, db: AsyncSession, setup_test_data
    ):
        from app.api.v1.endpoints.sync_endpoints import void_failed_sale
        from app.models.system_md.sys_models import AuditLog
        from app.schemas.sync_schemas import VoidFailedSaleRequest

        org, branch, user, drugs, customer = setup_test_data
        sale_id = uuid.uuid4()

        response = await void_failed_sale(
            VoidFailedSaleRequest(
                sale_id=sale_id,
                branch_id=branch.id,
                reason="Server permanently rejected: stock already reconciled manually",
                manager_approval_user_id=user.id,
                sale_number="OFF-SALE-999",
                total_amount="42.50",
                last_sync_error="insufficient stock",
                sync_attempts=10,
            ),
            current_user=user,
            db=db,
        )

        assert response.voided is True

        audit = await db.get(AuditLog, response.audit_log_id)
        assert audit is not None
        assert audit.action == "void_unsynced_sale"
        assert audit.entity_type == "Sale"
        assert audit.entity_id == sale_id
        assert audit.organization_id == org.id
        assert audit.changes["approved_by"] == str(user.id)
        assert audit.changes["sale_number"] == "OFF-SALE-999"
        assert audit.changes["sync_attempts"] == 10

    async def test_rejects_approver_without_refund_permission(
        self, db: AsyncSession, setup_test_data
    ):
        from fastapi import HTTPException
        from app.api.v1.endpoints.sync_endpoints import void_failed_sale
        from app.schemas.sync_schemas import VoidFailedSaleRequest

        org, branch, user, drugs, customer = setup_test_data

        unprivileged = User(
            id=uuid.uuid4(),
            organization_id=org.id,
            username="cashier",
            email="cashier@pharmacy.com",
            password_hash="hashed_pwd",
            full_name="Cashier",
            is_super_admin=False,
            is_active=True,
            assigned_branches=[branch.id],
        )
        db.add(unprivileged)
        await db.commit()
        # Mirror get_current_user's eager role load — .roles is otherwise a
        # lazy relationship, and this test calls the endpoint function
        # directly rather than through a real request.
        unprivileged._effective_permissions = set()

        with pytest.raises(HTTPException) as exc_info:
            await void_failed_sale(
                VoidFailedSaleRequest(
                    sale_id=uuid.uuid4(),
                    branch_id=branch.id,
                    reason="Trying to self-approve without permission",
                    manager_approval_user_id=unprivileged.id,
                ),
                current_user=unprivileged,
                db=db,
            )

        assert exc_info.value.status_code == 403
        assert "permission" in exc_info.value.detail.lower()

    async def test_rejects_when_caller_lacks_branch_access(
        self, db: AsyncSession, setup_test_data
    ):
        from fastapi import HTTPException
        from app.api.v1.endpoints.sync_endpoints import void_failed_sale
        from app.schemas.sync_schemas import VoidFailedSaleRequest

        org, branch, user, drugs, customer = setup_test_data

        no_branch_user = User(
            id=uuid.uuid4(),
            organization_id=org.id,
            username="no_branch",
            email="no_branch@pharmacy.com",
            password_hash="hashed_pwd",
            full_name="No Branch",
            is_super_admin=False,
            is_active=True,
            assigned_branches=[],
        )
        db.add(no_branch_user)
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await void_failed_sale(
                VoidFailedSaleRequest(
                    sale_id=uuid.uuid4(),
                    branch_id=branch.id,
                    reason="No branch access",
                    manager_approval_user_id=user.id,
                ),
                current_user=no_branch_user,
                db=db,
            )

        assert exc_info.value.status_code == 403
        assert "branch" in exc_info.value.detail.lower()


@pytest.mark.asyncio
class TestOfflineSalePriceReconciliation:
    """
    Regression coverage for docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md
    finding P1: offline sale prices must be reconciled against the same
    branch-level price pricing_calculator.py uses online (not the org-wide
    catalog price), and a mismatch must raise a persistent, reviewable
    SystemAlert rather than only a log line.
    """

    async def test_price_drift_beyond_tolerance_raises_system_alert(
        self, db: AsyncSession, setup_test_data
    ):
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]

        db.add_all([
            BranchInventory(
                id=uuid.uuid4(),
                branch_id=branch.id,
                drug_id=drug.id,
                quantity=100,
                reserved_quantity=0,
                selling_price=Decimal("10.00"),
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drug.id,
                batch_number="SYNC-PRICE-DRIFT-001-BATCH",
                quantity=100, remaining_quantity=100,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

        sale_id = uuid.uuid4()
        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(sale_id),
                table_name="sales",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(sale_id),
                    "sale_number": "SYNC-PRICE-DRIFT-001",
                    "subtotal": 25.0,
                    "discount_amount": 0.0,
                    "tax_amount": 0.0,
                    "total_amount": 25.0,
                    "payment_method": "cash",
                    "cashier_id": str(user.id),
                    "status": "completed",
                    "sync_protocol_version": 2,
                    "items": [
                        {
                            "id": str(uuid.uuid4()),
                            "drug_id": str(drug.id),
                            "drug_name": drug.name,
                            "quantity": 5,
                            "unit_price": 5.0,
                            "subtotal": 25.0,
                            "total_price": 25.0,
                        }
                    ],
                },
            )],
        )

        response = await SyncService.push(db, request, org.id, user.id)

        assert response.total_accepted == 1, "price drift must not block the sale"

        alerts = (await db.execute(
            select(SystemAlert).where(
                SystemAlert.organization_id == org.id,
                SystemAlert.alert_type == "security",
                SystemAlert.drug_id == drug.id,
            )
        )).scalars().all()
        assert len(alerts) == 1
        assert "SYNC-PRICE-DRIFT-001" in alerts[0].message
        assert "5.0" in alerts[0].message or "5" in alerts[0].message
        assert "10.00" in alerts[0].message or "10" in alerts[0].message
        assert alerts[0].is_resolved is False

    async def test_price_matching_branch_price_does_not_alert_even_when_catalog_price_differs(
        self, db: AsyncSession, setup_test_data
    ):
        """The old check compared against Drug.unit_price only — a branch
        with its own selling_price that legitimately differs from the org
        catalog price would have been a permanent false positive."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        drug.unit_price = Decimal("100.00")

        db.add_all([
            BranchInventory(
                id=uuid.uuid4(),
                branch_id=branch.id,
                drug_id=drug.id,
                quantity=100,
                reserved_quantity=0,
                selling_price=Decimal("5.00"),
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drug.id,
                batch_number="SYNC-PRICE-MATCH-001-BATCH",
                quantity=100, remaining_quantity=100,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

        sale_id = uuid.uuid4()
        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(sale_id),
                table_name="sales",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(sale_id),
                    "sale_number": "SYNC-PRICE-MATCH-001",
                    "subtotal": 25.0,
                    "discount_amount": 0.0,
                    "tax_amount": 0.0,
                    "total_amount": 25.0,
                    "payment_method": "cash",
                    "cashier_id": str(user.id),
                    "status": "completed",
                    "sync_protocol_version": 2,
                    "items": [
                        {
                            "id": str(uuid.uuid4()),
                            "drug_id": str(drug.id),
                            "drug_name": drug.name,
                            "quantity": 5,
                            "unit_price": 5.0,
                            "subtotal": 25.0,
                            "total_price": 25.0,
                        }
                    ],
                },
            )],
        )

        response = await SyncService.push(db, request, org.id, user.id)
        assert response.total_accepted == 1

        alerts = (await db.execute(
            select(SystemAlert).where(
                SystemAlert.organization_id == org.id,
                SystemAlert.alert_type == "security",
                SystemAlert.drug_id == drug.id,
            )
        )).scalars().all()
        assert alerts == []

    async def test_contract_priced_sale_skips_price_reconciliation(
        self, db: AsyncSession, setup_test_data
    ):
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]

        db.add_all([
            BranchInventory(
                id=uuid.uuid4(),
                branch_id=branch.id,
                drug_id=drug.id,
                quantity=100,
                reserved_quantity=0,
                selling_price=Decimal("10.00"),
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drug.id,
                batch_number="SYNC-PRICE-CONTRACT-001-BATCH",
                quantity=100, remaining_quantity=100,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

        sale_id = uuid.uuid4()
        request = PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=uuid.uuid4(),
                local_id=str(sale_id),
                table_name="sales",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(sale_id),
                    "sale_number": "SYNC-PRICE-CONTRACT-001",
                    "price_contract_id": str(uuid.uuid4()),
                    "subtotal": 25.0,
                    "discount_amount": 0.0,
                    "tax_amount": 0.0,
                    "total_amount": 25.0,
                    "payment_method": "cash",
                    "cashier_id": str(user.id),
                    "status": "completed",
                    "sync_protocol_version": 2,
                    "items": [
                        {
                            "id": str(uuid.uuid4()),
                            "drug_id": str(drug.id),
                            "drug_name": drug.name,
                            "quantity": 5,
                            "unit_price": 5.0,
                            "subtotal": 25.0,
                            "total_price": 25.0,
                        }
                    ],
                },
            )],
        )

        response = await SyncService.push(db, request, org.id, user.id)
        assert response.total_accepted == 1

        alerts = (await db.execute(
            select(SystemAlert).where(
                SystemAlert.organization_id == org.id,
                SystemAlert.alert_type == "security",
                SystemAlert.drug_id == drug.id,
            )
        )).scalars().all()
        assert alerts == [], "contract-priced sales must skip catalog/branch price reconciliation"


@pytest.mark.asyncio
class TestPullPagination:
    """
    Regression coverage for docs/reviews/2026-08-11-offline-first-architecture-review.md
    finding #5: the legacy /sync/pull path issued one unbounded query per
    table with has_more never set, so a brand-new device's first sync
    against a branch with a large sales history could time out and, since
    last_sync_at only advances on full success, restart from scratch every
    retry. This suite verifies pagination actually resumes and never loses
    or duplicates rows, including when several rows share one updated_at.
    """

    async def _make_sale(self, org, branch, user, number, updated_at):
        return Sale(
            id=uuid.uuid4(),
            organization_id=org.id,
            branch_id=branch.id,
            sale_number=number,
            subtotal=Decimal("10.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("10.00"),
            amount_paid=Decimal("10.00"),
            change_amount=Decimal("0.00"),
            payment_method="cash",
            cashier_id=user.id,
            status="completed",
            updated_at=updated_at,
        )

    async def test_pull_paginates_and_resumes_without_loss_or_duplication(
        self, db: AsyncSession, setup_test_data, monkeypatch
    ):
        from app.services.sync import sync_service as sync_service_module

        monkeypatch.setattr(sync_service_module, "_PULL_PAGE_SIZE", 3)

        org, branch, user, drugs, customer = setup_test_data
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sales = [
            await self._make_sale(org, branch, user, f"PAGE-SALE-{i}", base + timedelta(minutes=i))
            for i in range(7)
        ]
        db.add_all(sales)
        await db.commit()

        seen_numbers: list[str] = []
        since = None
        rounds = 0
        while True:
            rounds += 1
            assert rounds <= 10, "pagination did not terminate — likely an infinite loop"
            response = await SyncService.pull(
                db,
                PullRequest(branch_id=branch.id, tables=["sales"], last_sync_at=since),
                org.id,
            )
            seen_numbers.extend(s.sale_number for s in response.sales)
            since = response.sync_timestamp
            if not response.has_more:
                break

        assert rounds >= 3, "7 rows at page size 3 must take at least 3 round trips"
        assert sorted(seen_numbers) == sorted(f"PAGE-SALE-{i}" for i in range(7))
        assert len(seen_numbers) == len(set(seen_numbers)), "no sale should be delivered twice"

    async def test_pull_pagination_boundary_holds_back_tied_timestamps(
        self, db: AsyncSession, setup_test_data, monkeypatch
    ):
        """Rows 2 and 3 share the exact same updated_at and straddle the
        page boundary (limit=2) — both must still be delivered, in some
        page, exactly once."""
        from app.services.sync import sync_service as sync_service_module

        monkeypatch.setattr(sync_service_module, "_PULL_PAGE_SIZE", 2)

        org, branch, user, drugs, customer = setup_test_data
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tied_ts = base + timedelta(minutes=1)
        sales = [
            await self._make_sale(org, branch, user, "TIE-SALE-0", base),
            await self._make_sale(org, branch, user, "TIE-SALE-1", tied_ts),
            await self._make_sale(org, branch, user, "TIE-SALE-2", tied_ts),
            await self._make_sale(org, branch, user, "TIE-SALE-3", base + timedelta(minutes=2)),
        ]
        db.add_all(sales)
        await db.commit()

        seen_numbers: list[str] = []
        since = None
        rounds = 0
        while True:
            rounds += 1
            assert rounds <= 10
            response = await SyncService.pull(
                db,
                PullRequest(branch_id=branch.id, tables=["sales"], last_sync_at=since),
                org.id,
            )
            seen_numbers.extend(s.sale_number for s in response.sales)
            since = response.sync_timestamp
            if not response.has_more:
                break

        assert sorted(seen_numbers) == [f"TIE-SALE-{i}" for i in range(4)]


def _real_client_crsql_changes(table_ddls: dict) -> list[tuple]:
    """Open a throwaway SQLite db with the real vendored cr-sqlite extension
    loaded, apply each table's DDL, call crsql_as_crr, run the given INSERT,
    and return the genuine crsql_changes rows cr-sqlite captured.

    This mirrors what an actual client does (writeLocal.customer /
    writeLocal.prescription -> a plain INSERT on a CRR-enabled table) instead
    of hand-constructing CrrPushRecord payloads, which would risk testing a
    shape the real client-side cr-sqlite extension never actually produces
    (e.g. pk encoding).
    """
    ext_path = _resolve_extension_path()
    assert ext_path, "test requires the vendored crsqlite extension"
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    conn.load_extension(ext_path)
    conn.enable_load_extension(False)

    for table, (ddl, insert_sql, insert_params) in table_ddls.items():
        conn.executescript(ddl)
        conn.execute(f"SELECT crsql_as_crr('{table}')")
        conn.execute(insert_sql, insert_params)
    conn.commit()

    rows = conn.execute(
        'SELECT "table", pk, cid, val, col_version, db_version, site_id, cl, seq '
        'FROM crsql_changes ORDER BY db_version ASC, seq ASC'
    ).fetchall()
    conn.close()
    return rows


@pytest.mark.asyncio
class TestCrrPushCustomerPrescriptionDependency:
    """Regression coverage for a real bug report: a customer created
    offline, then a prescription created offline for that same new
    customer (the ordinary cashier workflow — add a walk-in customer, then
    take their prescription, all before reconnecting) never showed up
    server-side once the app reconnected, despite being visible locally the
    whole time. `writeLocal.customer`/`writeLocal.prescription` write
    directly to CRR-enabled tables (ui.laso/src/lib/localWrite.ts) and rely
    entirely on cr-sqlite's own change capture + SyncEngine.pushCrr() —
    unlike sales, they never touch the legacy sync_queue, so
    SyncService.push()-based tests (see test_push_prescription_* above)
    exercise a path the real client no longer uses for these two tables.
    """

    async def test_offline_customer_and_dependent_prescription_both_reach_postgres(
        self, db: AsyncSession, setup_test_data
    ):
        org, branch, _user, _drugs, _existing_customer = setup_test_data
        now = datetime.now(timezone.utc).isoformat()

        new_customer_id = str(uuid.uuid4())
        prescription_id = str(uuid.uuid4())

        rows = _real_client_crsql_changes({
            "customers": (
                _CRR_TABLE_CONFIG["customers"]["ddl"],
                """INSERT INTO customers
                   (id, organization_id, customer_type, first_name, last_name, phone,
                    is_active, is_deleted, sync_status, sync_version, updated_at, created_at)
                   VALUES (?, ?, 'walk_in', ?, ?, ?, 1, 0, 'pending', 1, ?, ?)""",
                (new_customer_id, str(org.id), "New", "Walkin", "0559999999", now, now),
            ),
            "prescriptions": (
                _CRR_TABLE_CONFIG["prescriptions"]["ddl"],
                """INSERT INTO prescriptions
                   (id, organization_id, branch_id, prescription_number, customer_id,
                    prescriber_name, prescriber_license, issue_date, expiry_date,
                    medications, refills_allowed, refills_remaining, status,
                    sync_status, sync_version, updated_at, created_at)
                   VALUES (?, ?, ?, 'OFFLINE-RX-1', ?, 'Dr. Test', 'LIC-1', ?, ?,
                           '[]', 0, 0, 'active', 'pending', 1, ?, ?)""",
                (
                    prescription_id, str(org.id), str(branch.id), new_customer_id,
                    now, now, now, now,
                ),
            ),
        })
        assert rows, "expected real crsql_changes rows from the fake client db"

        changes = [
            CrrPushRecord(
                table=r[0], pk=r[1], cid=r[2], val=r[3],
                col_version=r[4], db_version=r[5], site_id=r[6], cl=r[7], seq=r[8],
            )
            for r in rows
        ]

        response = await CrrSyncService.handle_crr_push(
            db, changes, organization_id=org.id, branch_id=branch.id,
        )

        failures = [r for r in response.results if not r.success]
        assert not failures, f"expected both rows to be accepted, got failures: {failures}"

        customer = await db.get(Customer, uuid.UUID(new_customer_id))
        assert customer is not None, "offline-created customer never reached Postgres"

        prescription = await db.get(Prescription, uuid.UUID(prescription_id))
        assert prescription is not None, "offline-created prescription never reached Postgres"
        assert prescription.customer_id == uuid.UUID(new_customer_id)

    async def test_offline_prescription_with_real_client_schema_reaches_postgres(
        self, db: AsyncSession, setup_test_data
    ):
        """Regression coverage for a real, independent bug found alongside the
        one above: the real CLIENT-side `prescriptions` table
        (ui.laso/src/lib/localDb.ts) has a `created_offline_at TEXT` column
        added by a later migration (`ALTER TABLE prescriptions ADD COLUMN
        created_offline_at TEXT`) that the server's shadow DB DDL
        (`_CRR_TABLE_CONFIG["prescriptions"]["ddl"]`) never gained. cr-sqlite
        tracks every column of a row on INSERT — including untouched/NULL
        ones — so every real offline-created prescription generates a
        crsql_changes row referencing `cid='created_offline_at'`, a column
        the shadow DB's `prescriptions` table doesn't have. Inserting that
        change into the shadow DB always failed with a generic "SQL logic
        error", permanently blocking every prescription push regardless of
        the site_id/db_version cursor fixes.

        The test above reuses the shadow DB's own DDL as a stand-in for the
        client, which can never catch this — both sides are the same
        string, so they can never drift. This test hardcodes the real
        client schema instead, to actually exercise the drift.
        """
        org, branch, _user, _drugs, existing_customer = setup_test_data
        now = datetime.now(timezone.utc).isoformat()
        prescription_id = str(uuid.uuid4())

        real_client_prescriptions_ddl = """
            CREATE TABLE prescriptions (
              id                    TEXT PRIMARY KEY NOT NULL,
              organization_id       TEXT NOT NULL DEFAULT '',
              branch_id             TEXT NOT NULL DEFAULT '',
              prescription_number   TEXT NOT NULL DEFAULT '',
              customer_id           TEXT NOT NULL DEFAULT '',
              prescriber_name       TEXT NOT NULL DEFAULT '',
              prescriber_license    TEXT NOT NULL DEFAULT '',
              prescriber_phone      TEXT,
              prescriber_address    TEXT,
              issue_date            TEXT NOT NULL DEFAULT '',
              expiry_date           TEXT NOT NULL DEFAULT '',
              medications           TEXT NOT NULL DEFAULT '[]',
              diagnosis             TEXT,
              notes                 TEXT,
              special_instructions  TEXT,
              refills_allowed       INTEGER NOT NULL DEFAULT 0,
              refills_remaining     INTEGER NOT NULL DEFAULT 0,
              last_refill_date      TEXT,
              status                TEXT NOT NULL DEFAULT 'active',
              verified_by           TEXT,
              verified_at           TEXT,
              created_offline_at    TEXT,
              sync_status           TEXT NOT NULL DEFAULT 'synced',
              sync_version          INTEGER NOT NULL DEFAULT 1,
              synced_at             TEXT,
              updated_at            TEXT NOT NULL DEFAULT '',
              created_at            TEXT NOT NULL DEFAULT ''
            );
        """

        rows = _real_client_crsql_changes({
            "prescriptions": (
                real_client_prescriptions_ddl,
                """INSERT INTO prescriptions
                   (id, organization_id, branch_id, prescription_number, customer_id,
                    prescriber_name, prescriber_license, issue_date, expiry_date,
                    medications, refills_allowed, refills_remaining, status,
                    sync_status, sync_version, updated_at, created_at)
                   VALUES (?, ?, ?, 'OFFLINE-RX-SCHEMA-DRIFT', ?, 'Dr. Test', 'LIC-1', ?, ?,
                           '[]', 0, 0, 'active', 'pending', 1, ?, ?)""",
                (
                    prescription_id, str(org.id), str(branch.id), str(existing_customer.id),
                    now, now, now, now,
                ),
            ),
        })
        assert any(r[2] == "created_offline_at" for r in rows), (
            "expected the real client schema to generate a change for "
            "created_offline_at — if this fails, the client schema no "
            "longer has that column and this test is no longer testing "
            "the drift it was written for"
        )

        changes = [
            CrrPushRecord(
                table=r[0], pk=r[1], cid=r[2], val=r[3],
                col_version=r[4], db_version=r[5], site_id=r[6], cl=r[7], seq=r[8],
            )
            for r in rows
        ]

        response = await CrrSyncService.handle_crr_push(
            db, changes, organization_id=org.id, branch_id=branch.id,
        )

        failures = [r for r in response.results if not r.success]
        assert not failures, f"expected the prescription to be accepted, got failures: {failures}"

        prescription = await db.get(Prescription, uuid.UUID(prescription_id))
        assert prescription is not None, "offline-created prescription never reached Postgres"

    async def test_pk_resend_after_shadow_row_already_tombstoned_is_not_an_error(
        self, db: AsyncSession, setup_test_data
    ):
        """Regression coverage for a real bug found live in a user's backend
        log: `_handle_sum_and_merge` (branch_inventory/drug_batches) and the
        customer-merge strategy both call `shadow.delete_crr_row` to retire
        a losing duplicate's shadow row after folding it into the winner --
        exactly the same shadow state a genuine client-authored delete
        leaves behind. Because pushCrr() only advances the client's cursor
        once a *whole* batch succeeds, an already-processed row like this
        gets resent verbatim on every retry for as long as anything else in
        the same batch keeps failing (which was itself happening for an
        unrelated reason at the time -- see the isoformat regression test
        above). cr-sqlite correctly refuses to resurrect it (the replayed
        create loses the causality race against the already-applied
        delete), so `resolve_row_id` finds nothing -- but the old code
        treated that as an unconditional, permanently-repeating hard error
        ("Unable to resolve encoded primary key for customers") instead of
        recognizing it as a no-op success.
        """
        org, branch, _user, _drugs, _existing_customer = setup_test_data
        now = datetime.now(timezone.utc).isoformat()
        customer_id = str(uuid.uuid4())

        rows = _real_client_crsql_changes({
            "customers": (
                _CRR_TABLE_CONFIG["customers"]["ddl"],
                """INSERT INTO customers
                   (id, organization_id, customer_type, first_name, last_name, phone,
                    is_active, is_deleted, sync_status, sync_version, updated_at, created_at)
                   VALUES (?, ?, 'walk_in', ?, ?, ?, 1, 0, 'pending', 1, ?, ?)""",
                (customer_id, str(org.id), "Retry", "Echo", "0551234567", now, now),
            ),
        })
        assert rows, "expected real crsql_changes rows from the fake client db"

        changes = [
            CrrPushRecord(
                table=r[0], pk=r[1], cid=r[2], val=r[3],
                col_version=r[4], db_version=r[5], site_id=r[6], cl=r[7], seq=r[8],
            )
            for r in rows
        ]

        first = await CrrSyncService.handle_crr_push(
            db, changes, organization_id=org.id, branch_id=branch.id,
        )
        assert not [r for r in first.results if not r.success], first.results
        assert await db.get(Customer, uuid.UUID(customer_id)) is not None

        # Retire the shadow row exactly the way sum_and_merge / the
        # customer-merge strategy do after folding a losing duplicate into
        # its winner (a genuine client-authored delete leaves the same
        # shadow state).
        shadow = await get_shadow_db()
        await shadow.delete_crr_row("customers", customer_id)

        # The client's cursor never advanced, so it resends the exact same
        # already-processed create-changes for this pk.
        retry = await CrrSyncService.handle_crr_push(
            db, changes, organization_id=org.id, branch_id=branch.id,
        )

        failures = [r for r in retry.results if not r.success]
        assert not failures, (
            "resending an already-tombstoned pk must be a no-op success, "
            f"not a permanent error: {failures}"
        )
