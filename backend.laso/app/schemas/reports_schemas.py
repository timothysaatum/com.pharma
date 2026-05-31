from pydantic import Field, ConfigDict
from datetime import date
from typing import Optional, List
import uuid
from app.schemas.base_schemas import BaseSchema

class DailySalesSummaryRow(BaseSchema):
    sale_date: str
    branch_id: str
    branch_name: str
    contract_id: Optional[str] = None
    contract_name: Optional[str] = None
    cashier_id: Optional[str] = None
    cashier_name: str
    transaction_count: int
    gross_revenue: float
    total_discount: float
    total_tax: float
    net_revenue: float
    total_items: int
    refund_count: int

    model_config = ConfigDict(from_attributes=True)

class DrugTurnoverRow(BaseSchema):
    drug_id: str
    drug_name: str
    drug_sku: Optional[str] = None
    category: Optional[str] = None
    units_sold: int
    revenue: float
    transaction_count: int
    avg_selling_price: float

    model_config = ConfigDict(from_attributes=True)
