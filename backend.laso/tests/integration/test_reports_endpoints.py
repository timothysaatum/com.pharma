import asyncio
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, ASGITransport

from main import app
from app.api.v1.endpoints import reports_endpoints
from app.services.reports import reports_service


@pytest.mark.asyncio
async def test_daily_sales_endpoint_returns_data(monkeypatch):
    # Create a fake user object
    fake_user = SimpleNamespace(
        organization_id=uuid.uuid4(),
        has_permission=lambda perm: True,
        is_super_admin=True,
        assigned_branches=[],
    )

    async def fake_get_current_user():
        return fake_user

    # Patch dependency
    app.dependency_overrides[reports_endpoints.get_current_user] = lambda: fake_user

    branch_id = str(uuid.uuid4())
    sample_items = [
        {
            "sale_date": "2026-05-24",
            "branch_id": branch_id,
            "branch_name": "Test Branch",
            "contract_id": None,
            "contract_name": None,
            "cashier_id": None,
            "cashier_name": "Test Cashier",
            "transaction_count": 4,
            "gross_revenue": 171.0,
            "total_discount": 0.0,
            "total_tax": 0.0,
            "net_revenue": 171.0,
            "total_items": 6,
            "refund_count": 0
        }
    ]

    sample_response = {
        "items": sample_items,
        "total": 1,
        "page": 1,
        "page_size": 50,
        "total_pages": 1,
        "has_next": False,
        "has_prev": False
    }

    async def fake_daily(db, organization_id, start_date, end_date, branch_id=None, branch_ids=None, contract_id=None, cashier_id=None, pagination=None):
        from app.utils.pagination import PaginatedResponse
        return PaginatedResponse(**sample_response)

    monkeypatch.setattr(reports_service.ReportsService, 'get_daily_sales_summary', staticmethod(fake_daily))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/reports/daily-sales-summary",
            params={"start_date": "2026-05-23", "end_date": "2026-05-25"},
        )

    assert resp.status_code == 200
    assert resp.json() == sample_response

    # Cleanup override
    app.dependency_overrides.pop(reports_endpoints.get_current_user, None)


@pytest.mark.asyncio
async def test_drug_turnover_endpoint_scopes_to_assigned_branches(monkeypatch):
    branch_id = uuid.uuid4()
    fake_user = SimpleNamespace(
        organization_id=uuid.uuid4(),
        has_permission=lambda perm: True,
        is_super_admin=False,
        assigned_branches=[branch_id],
    )
    seen = {}

    app.dependency_overrides[reports_endpoints.get_current_user] = lambda: fake_user

    async def fake_turnover(
        db,
        organization_id,
        start_date,
        end_date,
        branch_id=None,
        branch_ids=None,
        pagination=None,
    ):
        from app.utils.pagination import PaginatedResponse

        seen["organization_id"] = organization_id
        seen["branch_id"] = branch_id
        seen["branch_ids"] = branch_ids
        return PaginatedResponse(
            items=[
                {
                    "drug_id": str(uuid.uuid4()),
                    "drug_name": "Paracetamol",
                    "drug_sku": "PARA-001",
                    "category": "Analgesics",
                    "units_sold": 3,
                    "revenue": 30.0,
                    "transaction_count": 1,
                    "avg_selling_price": 10.0,
                }
            ],
            total=1,
            page=1,
            page_size=50,
            total_pages=1,
            has_next=False,
            has_prev=False,
        )

    monkeypatch.setattr(
        reports_service.ReportsService,
        "get_drug_turnover",
        staticmethod(fake_turnover),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/reports/drug-turnover",
            params={
                "start_date": str(date.today()),
                "end_date": str(date.today()),
                "page": 1,
                "page_size": 50,
            },
        )

    assert resp.status_code == 200
    assert seen["organization_id"] == fake_user.organization_id
    assert seen["branch_id"] is None
    assert seen["branch_ids"] == [branch_id]

    app.dependency_overrides.pop(reports_endpoints.get_current_user, None)
