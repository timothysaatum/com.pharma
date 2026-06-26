"""
Integration tests for syncing Sale and SaleItem records.
"""

import pytest
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer.customer_model import Customer
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.sales.sales_model import Sale, SaleItem
from app.models.sales.sales_model import PurchaseOrder
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
