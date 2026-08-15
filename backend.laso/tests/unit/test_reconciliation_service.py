import pytest
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock
from app.services.inventory.reconciliation_service import generate_reconciliation_report
from app.schemas.reconciliation_schemas import ReconciliationReportResponse
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch

class MockRow:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

@pytest.mark.asyncio
async def test_reconciliation_zero_drift():
    db = AsyncMock()
    branch_id = uuid.uuid4()
    org_id = uuid.uuid4()
    drug_id = uuid.uuid4()
    
    branch = Branch(id=branch_id, organization_id=org_id)
    
    branch_result = MagicMock()
    branch_result.scalar_one_or_none.return_value = branch
    
    inv_row = MockRow(drug_id=drug_id, drug_name="Test Drug", total_quantity=100, total_reserved=10)
    inv_result = MagicMock()
    inv_result.all.return_value = [inv_row]
    
    batch_row = MockRow(drug_id=drug_id, batch_sum=100)
    batch_result = MagicMock()
    batch_result.all.return_value = [batch_row]
    
    lease_row = MockRow(drug_id=drug_id, active_lease_qty=5)
    lease_result = MagicMock()
    lease_result.all.return_value = [lease_row]
    
    dlq_result = MagicMock()
    dlq_result.scalar_one.return_value = 0
    
    db.execute.side_effect = [
        branch_result,
        inv_result,
        batch_result,
        lease_result,
        dlq_result
    ]
    
    report = await generate_reconciliation_report(db, branch_id, date.today())
    assert report.total_drugs_checked == 1
    assert report.balanced_count == 1
    assert report.drift_count == 0
    assert report.has_drift is False
    assert report.dead_letter_count == 0
    assert report.items[0].drift == 0
    assert report.items[0].status == 'balanced'
    assert report.items[0].sellable_quantity == 90
    assert report.items[0].unleased_sellable == 85

@pytest.mark.asyncio
async def test_reconciliation_batch_mismatch():
    db = AsyncMock()
    branch_id = uuid.uuid4()
    org_id = uuid.uuid4()
    drug_id = uuid.uuid4()
    
    branch = Branch(id=branch_id, organization_id=org_id)
    
    branch_result = MagicMock()
    branch_result.scalar_one_or_none.return_value = branch
    
    inv_row = MockRow(drug_id=drug_id, drug_name="Test Drug", total_quantity=100, total_reserved=10)
    inv_result = MagicMock()
    inv_result.all.return_value = [inv_row]
    
    batch_row = MockRow(drug_id=drug_id, batch_sum=90)
    batch_result = MagicMock()
    batch_result.all.return_value = [batch_row]
    
    lease_row = MockRow(drug_id=drug_id, active_lease_qty=5)
    lease_result = MagicMock()
    lease_result.all.return_value = [lease_row]
    
    dlq_result = MagicMock()
    dlq_result.scalar_one.return_value = 2
    
    db.execute.side_effect = [
        branch_result,
        inv_result,
        batch_result,
        lease_result,
        dlq_result
    ]
    
    report = await generate_reconciliation_report(db, branch_id, date.today())
    assert report.has_drift is True
    assert report.drift_count == 1
    assert report.dead_letter_count == 2
    assert report.items[0].drift == 10
    assert report.items[0].status == 'batch_mismatch'
