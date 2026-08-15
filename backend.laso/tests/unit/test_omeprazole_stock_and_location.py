from __future__ import annotations

import pytest
from decimal import Decimal
from datetime import date, timedelta
from app.models.inventory.inventory_model import Drug
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.services.inventory.inventory_service import InventoryService


@pytest.mark.asyncio
async def test_omeprazole_stock_220_and_location(db, setup_test_data):
    organization, branch = setup_test_data[0], setup_test_data[1]

    # Create Omeprazole drug
    drug = Drug(
        organization_id=organization.id,
        sku="DEMO-OMEP-20-CAP",
        name="Omeprazole 20 mg Capsules",
        generic_name="Omeprazole",
        drug_type="otc",
        dosage_form="capsule",
        strength="20 mg",
        unit_price=Decimal("18.00"),
        cost_price=Decimal("10.00"),
    )
    db.add(drug)
    await db.flush()

    # Create BranchInventory with 220 units and Shelf G-02 location
    inv = BranchInventory(
        branch_id=branch.id,
        drug_id=drug.id,
        quantity=220,
        reserved_quantity=0,
        location="Shelf G-02",
    )
    db.add(inv)

    # Create unexpired DrugBatch with 220 remaining quantity
    batch = DrugBatch(
        branch_id=branch.id,
        drug_id=drug.id,
        batch_number="DEMO-OMEP-20-CAP-01",
        quantity=220,
        remaining_quantity=220,
        expiry_date=date.today() + timedelta(days=365),
        cost_price=10.0,
        selling_price=18.0,
    )
    db.add(batch)
    await db.commit()

    from app.utils.pagination import PaginationParams

    # Fetch branch inventory via InventoryService
    result = await InventoryService.get_branch_inventory_paginated(
        db,
        branch_id=branch.id,
        pagination=PaginationParams(page=1, page_size=10),
        search="Omeprazole",
    )

    assert result.total == 1
    item = result.items[0]
    assert item.drug_sku == "DEMO-OMEP-20-CAP"
    assert item.quantity == 220
    assert item.location == "Shelf G-02"
