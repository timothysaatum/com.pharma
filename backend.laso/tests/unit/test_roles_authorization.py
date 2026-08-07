"""
Authorization coverage for app/api/v1/endpoints/roles.py.

Regression coverage: `get_roles` / `get_role` previously depended only on
`get_current_user` with no permission check, so any authenticated user could
read the full role/permission configuration for their organization. They now
require Permission.MANAGE_ORGANIZATION, matching the write endpoints
(`create_role` / `update_role` / `delete_role`) in the same file.
"""
import inspect
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints import roles


def _fake_user(*, has_permission=True):
    return SimpleNamespace(
        organization_id=uuid.uuid4(),
        is_super_admin=False,
        has_permission=lambda permission: has_permission,
    )


def _current_user_dependency(endpoint):
    """Pull the exact `current_user` Depends() callable wired to *endpoint*."""
    param = inspect.signature(endpoint).parameters["current_user"]
    return param.default.dependency


@pytest.mark.asyncio
async def test_get_roles_rejects_user_without_manage_organization():
    checker = _current_user_dependency(roles.get_roles)

    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=_fake_user(has_permission=False))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_roles_allows_user_with_manage_organization():
    checker = _current_user_dependency(roles.get_roles)
    user = _fake_user(has_permission=True)

    result = await checker(current_user=user)

    assert result is user


@pytest.mark.asyncio
async def test_get_role_rejects_user_without_manage_organization():
    checker = _current_user_dependency(roles.get_role)

    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=_fake_user(has_permission=False))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_role_allows_user_with_manage_organization():
    checker = _current_user_dependency(roles.get_role)
    user = _fake_user(has_permission=True)

    result = await checker(current_user=user)

    assert result is user
