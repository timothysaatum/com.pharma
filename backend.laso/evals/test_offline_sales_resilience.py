"""Periodic end-to-end eval for ambiguous POS connectivity outcomes.

Run before shipping offline sale changes:
``pytest -q evals/test_offline_sales_resilience.py``.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import func, select

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.ledger import InventoryMovement
from app.models.pricing.pricing_model import PriceContract
from app.models.sales.sales_model import Sale
from app.schemas.sales_schemas import SaleCreate, SaleItemCreate
from app.schemas.sync_schemas import PushRecord, PushRequest
from app.services.sales.sales_service import SalesService
from app.services.sync.sync_service import SyncService

pytest_plugins = ["tests.conftest"]


@pytest.mark.asyncio
async def test_lost_online_response_then_offline_reconciliation_is_exactly_once(
    db,
    setup_test_data,
):
    """The fallback envelope must acknowledge an online commit without replaying effects."""
    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[0]
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        quantity=20,
        reserved_quantity=0,
        selling_price=Decimal("50.00"),
    )
    batch = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="OFFLINE-EVAL-1",
        quantity=20,
        remaining_quantity=20,
        expiry_date=date.today() + timedelta(days=365),
    )
    contract = PriceContract(
        id=uuid.uuid4(),
        organization_id=org.id,
        contract_code="OFFLINE-EVAL",
        contract_name="Offline Eval Standard",
        contract_type="standard",
        effective_from=date.today() - timedelta(days=1),
        status="active",
        is_active=True,
        created_by=user.id,
    )
    db.add_all([inventory, batch, contract])
    await db.commit()

    sale_id = uuid.uuid4()
    online = await SalesService.process_sale(
        db,
        SaleCreate(
            client_sale_id=sale_id,
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drug.id, quantity=2)],
            payment_method="cash",
            amount_paid=Decimal("100.00"),
        ),
        user,
    )
    assert online.sale.id == sale_id

    # Simulate a dropped HTTP response: the desktop records the same sale ID
    # locally, reconnects, and submits its protocol-v2 envelope.
    sync_response = await SyncService.push(
        db,
        PushRequest(
            branch_id=branch.id,
            records=[PushRecord(
                operation_id=sale_id,
                local_id=str(sale_id),
                table_name="sales",
                operation="create",
                sync_version=1,
                created_offline_at=datetime.now(timezone.utc),
                data={
                    "id": str(sale_id),
                    "sale_number": "OFFLINE-AMBIGUOUS-RESPONSE",
                    "subtotal": 100.0,
                    "discount_amount": 0.0,
                    "tax_amount": 0.0,
                    "total_amount": 100.0,
                    "payment_method": "cash",
                    "cashier_id": str(user.id),
                    "status": "completed",
                    "sync_protocol_version": 2,
                    "items": [{
                        "id": str(uuid.uuid4()),
                        "drug_id": str(drug.id),
                        "drug_name": drug.name,
                        "quantity": 2,
                        "unit_price": 50.0,
                        "subtotal": 100.0,
                        "discount_amount": 0.0,
                        "tax_amount": 0.0,
                        "total_price": 100.0,
                    }],
                },
            )],
        ),
        org.id,
        user.id,
    )

    await db.refresh(inventory)
    await db.refresh(batch)
    sales = await db.scalar(
        select(func.count()).select_from(Sale).where(Sale.id == sale_id)
    )
    movements = await db.scalar(
        select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.source_id == sale_id,
            InventoryMovement.movement_type == "sale",
        )
    )

    assert sync_response.total_accepted == 1
    assert sync_response.total_failed == 0
    assert sales == 1
    assert movements == 1
    assert inventory.quantity == 18
    assert batch.remaining_quantity == 18
