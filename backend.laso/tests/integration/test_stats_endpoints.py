from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.api.v1.endpoints import stats


class _SummaryResult:
    def one(self):
        return SimpleNamespace(
            total_sales=0,
            total_revenue=0,
            total_discount=0,
            total_tax=0,
            average_sale=0,
        )


class _PaymentMethodsResult:
    def all(self):
        return []


class _FakeDb:
    def __init__(self):
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            return _SummaryResult()
        return _PaymentMethodsResult()


@pytest.mark.asyncio
async def test_sales_summary_defaults_missing_dates_to_current_utc_day():
    fake_db = _FakeDb()
    before = datetime.now(timezone.utc).date()
    response = await stats.get_sales_summary(
        start_date=None,
        end_date=None,
        branch_id=uuid.uuid4(),
        db=fake_db,
        organization_id=uuid.uuid4(),
    )

    period = response["period"]
    start = period["start_date"]
    end = period["end_date"]
    assert start.date() == before
    assert end.date() == before
    assert start.time().isoformat() == "00:00:00"
    assert end.time().isoformat() == "23:59:59.999999"
    assert fake_db.execute_count == 2
