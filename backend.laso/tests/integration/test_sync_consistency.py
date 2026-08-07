"""
Integration tests for verifying data parity between "offline" pushed data and server-side storage.
"""

import pytest
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.sales.sales_model import Sale, SaleItem
from app.schemas.sync_schemas import PushRequest, PushRecord
from app.services.sync.sync_service import SyncService


@pytest.mark.asyncio
class TestSyncConsistency:
    """Test suite for data consistency after sync."""

    async def test_sale_data_parity(self, db: AsyncSession, setup_test_data):
        """Verify that all relevant fields are correctly persisted after an offline push."""
        org, branch, user, drugs, customer = setup_test_data

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="CONSISTENCY-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

        sale_id = uuid.uuid4()

        # Comprehensive sale data matching what the frontend would send
        record_data = {
            "id": str(sale_id),
            "sale_number": "CONSISTENCY-001",
            "customer_id": str(customer.id),
            "customer_name": f"{customer.first_name} {customer.last_name}",
            "subtotal": 150.75,
            "total_discount_amount": 10.25,
            "tax_amount": 5.50,
            "total_amount": 146.00,
            "payment_method": "mobile_money",
            "payment_status": "completed",
            "amount_paid": 150.00,
            "change_amount": 4.00,
            "payment_reference": "TXN-123456",
            "cashier_id": str(user.id),
            "notes": "Offline transaction test",
            "status": "completed",
            "sync_protocol_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "items": [
                {
                    "id": str(uuid.uuid4()),
                    "drug_id": str(drugs[0].id),
                    "drug_name": drugs[0].name,
                    "quantity": 3,
                    "unit_price": 50.25,
                    "subtotal": 150.75,
                    "discount_amount": 10.25,
                    "tax_amount": 5.50,
                    "total_price": 146.00,
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

        await SyncService.push(db, request, org.id, user.id)

        # Verify from DB
        result = await db.execute(
            select(Sale)
            .options(selectinload(Sale.items))
            .where(Sale.id == sale_id)
        )
        sale = result.scalar_one()

        assert sale.sale_number == record_data["sale_number"]
        assert float(sale.subtotal) == record_data["subtotal"]
        assert float(sale.discount_amount) == record_data["total_discount_amount"]
        assert float(sale.total_amount) == record_data["total_amount"]
        assert sale.payment_method == record_data["payment_method"]
        assert sale.payment_reference == record_data["payment_reference"]
        assert len(sale.items) == 1
        assert sale.items[0].drug_id == drugs[0].id
        assert float(sale.items[0].unit_price) == record_data["items"][0]["unit_price"]
        assert sale.items[0].quantity == record_data["items"][0]["quantity"]
