import pytest
import uuid
from datetime import date, timedelta
from sqlalchemy import select
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.inventory.inventory_model import Drug
from app.services.sync._sellable_qty import compute_sellable_quantities

@pytest.mark.asyncio
async def test_compute_sellable_quantities(db):
    # Setup test data
    org_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    drug_id = uuid.uuid4()
    
    org = Organization(id=org_id, name="Test Org", type="pharmacy", is_active=True)
    db.add(org)
    
    branch = Branch(id=branch_id, organization_id=org_id, name="Test Branch", code="TB1", is_active=True, is_deleted=False)
    db.add(branch)
    
    drug = Drug(id=drug_id, organization_id=org_id, name="Test Drug", unit_price=10.0, tax_rate=0.0)
    db.add(drug)
    
    bi = BranchInventory(id=uuid.uuid4(), branch_id=branch_id, drug_id=drug_id, quantity=100, reserved_quantity=0)
    db.add(bi)
    
    today = date.today()
    
    batch_valid = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch_id,
        drug_id=drug_id,
        batch_number="VALID1",
        quantity=40,
        remaining_quantity=40,
        expiry_date=today + timedelta(days=365)
    )
    db.add(batch_valid)
    
    batch_expired = DrugBatch(
        id=uuid.uuid4(),
        branch_id=branch_id,
        drug_id=drug_id,
        batch_number="EXPIRED1",
        quantity=60,
        remaining_quantity=60,
        expiry_date=today - timedelta(days=1)
    )
    db.add(batch_expired)
    
    await db.commit()

    # Call the computation logic
    result = await compute_sellable_quantities(db, branch_id, [drug_id])
    
    # Assert sellable_quantity == 40 (not 100, not 0)
    assert result.get(drug_id) == 40
