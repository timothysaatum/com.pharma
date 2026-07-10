from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints import purchase_order_endpoints
from app.utils.pagination import PaginationParams


class _CapturingPaginator:
    last_query = None

    def __init__(self, _db):
        pass

    async def paginate(self, query, *, params, schema):
        self.__class__.last_query = query
        return SimpleNamespace(items=[], total=0)


def _user(*, assigned=(), elevated=False, super_admin=False):
    elevated_permissions = {"approve_purchase_orders"} if elevated else set()
    return SimpleNamespace(
        assigned_branches=list(assigned),
        is_super_admin=super_admin,
        has_permission=lambda permission: permission in elevated_permissions,
    )


async def _list(monkeypatch, *, user, organization_id, branch_id=None):
    monkeypatch.setattr(purchase_order_endpoints, "Paginator", _CapturingPaginator)
    _CapturingPaginator.last_query = None
    result = await purchase_order_endpoints.list_purchase_orders(
        pagination=PaginationParams(),
        branch_id=branch_id,
        status_filter=None,
        supplier_id=None,
        db=object(),
        current_user=user,
        organization_id=organization_id,
    )
    return result, _CapturingPaginator.last_query


def _compiled_params(query):
    return query.compile().params


@pytest.mark.asyncio
async def test_explicit_assigned_branch_is_allowed(monkeypatch):
    organization_id = uuid.uuid4()
    branch_id = uuid.uuid4()

    result, query = await _list(
        monkeypatch,
        user=_user(assigned=[branch_id]),
        organization_id=organization_id,
        branch_id=branch_id,
    )

    assert result.total == 0
    assert branch_id in _compiled_params(query).values()


@pytest.mark.asyncio
async def test_explicit_unassigned_branch_is_forbidden_for_non_elevated_user(monkeypatch):
    with pytest.raises(HTTPException) as exc_info:
        await _list(
            monkeypatch,
            user=_user(assigned=[uuid.uuid4()]),
            organization_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "You don't have access to purchase orders for this branch"


@pytest.mark.asyncio
async def test_explicit_unassigned_branch_is_allowed_for_elevated_user(monkeypatch):
    branch_id = uuid.uuid4()

    result, query = await _list(
        monkeypatch,
        user=_user(assigned=[], elevated=True),
        organization_id=uuid.uuid4(),
        branch_id=branch_id,
    )

    assert result.total == 0
    assert branch_id in _compiled_params(query).values()


@pytest.mark.asyncio
async def test_omitted_branch_scopes_non_elevated_user_to_assignments(monkeypatch):
    assigned = [uuid.uuid4(), uuid.uuid4()]

    _, query = await _list(
        monkeypatch,
        user=_user(assigned=assigned),
        organization_id=uuid.uuid4(),
    )

    parameter_values = list(_compiled_params(query).values())
    assert any(set(value) == set(assigned) for value in parameter_values if isinstance(value, list))


@pytest.mark.asyncio
async def test_omitted_branch_returns_empty_query_for_user_without_assignments(monkeypatch):
    result, query = await _list(
        monkeypatch,
        user=_user(assigned=[]),
        organization_id=uuid.uuid4(),
    )

    assert result.total == 0
    assert "false" in str(query).lower()


@pytest.mark.asyncio
async def test_omitted_branch_is_organization_wide_for_elevated_user(monkeypatch):
    organization_id = uuid.uuid4()

    _, query = await _list(
        monkeypatch,
        user=_user(assigned=[], super_admin=True),
        organization_id=organization_id,
    )

    sql = str(query.whereclause).lower()
    assert "purchase_orders.organization_id" in sql
    assert "purchase_orders.branch_id" not in sql


@pytest.mark.asyncio
async def test_view_reports_permission_also_grants_cross_branch_access(monkeypatch):
    branch_id = uuid.uuid4()
    user = SimpleNamespace(
        assigned_branches=[],
        is_super_admin=False,
        has_permission=lambda permission: permission == "view_reports",
    )

    _, query = await _list(
        monkeypatch,
        user=user,
        organization_id=uuid.uuid4(),
        branch_id=branch_id,
    )

    assert branch_id in _compiled_params(query).values()
