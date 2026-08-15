from pydantic import BaseModel
from typing import List, Optional
from uuid import UUID
from datetime import date

class DrugReconciliationItem(BaseModel):
    drug_id: UUID
    drug_name: str
    inventory_quantity: int
    batch_sum_quantity: int
    sellable_quantity: int
    unleased_sellable: int
    drift: int
    status: str  # 'balanced', 'batch_mismatch', 'sellable_mismatch'

class ReconciliationReportResponse(BaseModel):
    branch_id: UUID
    report_date: str
    total_drugs_checked: int
    balanced_count: int
    drift_count: int
    dead_letter_count: int
    items: List[DrugReconciliationItem]
    has_drift: bool
