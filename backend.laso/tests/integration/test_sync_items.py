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

from app.models.sales.sales_model import Sale, SaleItem
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
