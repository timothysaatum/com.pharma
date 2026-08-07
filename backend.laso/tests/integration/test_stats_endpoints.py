from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

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


def _fake_user(*, has_permission=True):
    """Minimal user stand-in matching the SimpleNamespace fixture pattern used
    across this suite (see tests/unit/test_purchase_order_branch_authorization.py)."""
    return SimpleNamespace(
        organization_id=uuid.uuid4(),
        is_super_admin=False,
        has_permission=lambda permission: has_permission,
    )


def _current_user_dependency(endpoint):
    """Pull the exact `current_user` Depends() callable wired to *endpoint*,
    so the authorization test exercises the real dependency, not a duplicate."""
    import inspect

    param = inspect.signature(endpoint).parameters["current_user"]
    return param.default.dependency


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
        current_user=_fake_user(has_permission=True),
    )

    period = response["period"]
    start = period["start_date"]
    end = period["end_date"]
    assert start.date() == before
    assert end.date() == before
    assert start.time().isoformat() == "00:00:00"
    assert end.time().isoformat() == "23:59:59.999999"
    assert fake_db.execute_count == 2


# ── Authorization: get_sales_summary / get_top_selling_drugs ──────────────
# Regression coverage: these endpoints previously had no permission check at
# all beyond org-scoping, so any authenticated user (regardless of role)
# could read sales figures and top-selling-drug data for their organization.


@pytest.mark.asyncio
async def test_sales_summary_rejects_user_without_permission():
    checker = _current_user_dependency(stats.get_sales_summary)

    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=_fake_user(has_permission=False))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_sales_summary_allows_user_with_permission():
    checker = _current_user_dependency(stats.get_sales_summary)
    user = _fake_user(has_permission=True)

    result = await checker(current_user=user)

    assert result is user


@pytest.mark.asyncio
async def test_top_selling_drugs_rejects_user_without_permission():
    checker = _current_user_dependency(stats.get_top_selling_drugs)

    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=_fake_user(has_permission=False))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_top_selling_drugs_allows_user_with_permission():
    checker = _current_user_dependency(stats.get_top_selling_drugs)
    user = _fake_user(has_permission=True)

    result = await checker(current_user=user)

    assert result is user
