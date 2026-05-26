"""
Unit tests for the reports service helpers.
These tests verify response shaping and field mappings for report rows.
"""

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.reports.reports_service import ReportsService


class DummyRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.mark.asyncio
async def test_get_contract_performance_maps_backend_rows_to_response():
    db = AsyncMock()
    result = MagicMock()
    contract_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    result.fetchall.return_value = [
        DummyRow(
            id=contract_id,
            contract_code="STAFF-10",
            contract_name="Staff Discount",
            contract_type="staff",
            sales_count=3,
            revenue=375.0,
            discount_given=45.0,
            avg_discount=15.0,
            customer_count=3,
        )
    ]
    db.execute.return_value = result

    output = await ReportsService.get_contract_performance(
        db=db,
        organization_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        contract_id=None,
    )

    db.execute.assert_awaited_once()
    assert output == [
        {
            "contract_id": str(contract_id),
            "contract_code": "STAFF-10",
            "contract_name": "Staff Discount",
            "contract_type": "staff",
            "sales_count": 3,
            "revenue": 375.0,
            "discount_given": 45.0,
            "avg_discount": 15.0,
            "customer_count": 3,
        }
    ]


@pytest.mark.asyncio
async def test_get_daily_sales_summary_includes_total_items_and_branch_names():
    db = AsyncMock()
    result = MagicMock()
    sale_date = date(2025, 2, 10)
    branch_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    cash_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    contract_id = uuid.UUID("55555555-5555-5555-5555-555555555555")

    result.fetchall.return_value = [
        DummyRow(
            sale_date=sale_date,
            branch_id=branch_id,
            branch_name="Main Branch",
            price_contract_id=contract_id,
            contract_name="Corporate Plan",
            cashier_id=cash_id,
            cashier_name="Jane Doe",
            transaction_count=7,
            gross_revenue=420.0,
            total_discount=20.0,
            total_tax=30.0,
            net_revenue=430.0,
            total_items=42,
            refund_count=0,
        )
    ]
    db.execute.return_value = result

    output = await ReportsService.get_daily_sales_summary(
        db=db,
        organization_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        start_date=date(2025, 2, 1),
        end_date=date(2025, 2, 28),
    )

    db.execute.assert_awaited_once()
    assert output == [
        {
            "sale_date": str(sale_date),
            "branch_id": str(branch_id),
            "branch_name": "Main Branch",
            "contract_id": str(contract_id),
            "contract_name": "Corporate Plan",
            "cashier_id": str(cash_id),
            "cashier_name": "Jane Doe",
            "transaction_count": 7,
            "gross_revenue": 420.0,
            "total_discount": 20.0,
            "total_tax": 30.0,
            "net_revenue": 430.0,
            "total_items": 42,
            "refund_count": 0,
        }
    ]
