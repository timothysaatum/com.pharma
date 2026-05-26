import asyncio
import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from main import app
from app.api.v1.endpoints import reports_endpoints
from app.services.reports import reports_service


@pytest.mark.asyncio
async def test_daily_sales_endpoint_returns_data(monkeypatch):
    # Create a fake user object
    fake_user = SimpleNamespace(organization_id=uuid.uuid4())

    async def fake_get_current_user():
        return fake_user

    # Patch dependency
    app.dependency_overrides[reports_endpoints.get_current_user] = lambda: fake_user

    sample = [
        {
            "sale_date": "2026-05-24",
            "branch_name": "Test Branch",
            "net_revenue": 171.0,
            "total_items": 6,
            "transaction_count": 4,
        }
    ]

    async def fake_daily(db, organization_id, start_date, end_date, branch_id=None, contract_id=None, cashier_id=None):
        return sample

    monkeypatch.setattr(reports_service.ReportsService, 'get_daily_sales_summary', staticmethod(fake_daily))

    async with AsyncClient(app=app, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/reports/daily-sales-summary",
            params={"start_date": "2026-05-23", "end_date": "2026-05-25"},
        )

    assert resp.status_code == 200
    assert resp.json() == sample

    # Cleanup override
    app.dependency_overrides.pop(reports_endpoints.get_current_user, None)
