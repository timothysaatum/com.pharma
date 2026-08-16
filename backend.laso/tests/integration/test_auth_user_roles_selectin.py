"""
Integration test verifying User.roles selectinload and AuthService.authenticate_user
against PostgreSQL with UUID/RBAC schema.
"""
import uuid
import pytest
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.pharmacy.pharmacy_model import Organization
from app.models.user.user_model import User, Role, UserRole
from app.services.auth.auth_service import AuthService
from app.schemas.user_schema import LoginRequest
from app.models.core.mixins import pwd_context

pytestmark = pytest.mark.asyncio


async def test_user_roles_selectinload_and_authentication(db: AsyncSession):
    # 1. Setup organization, role, user, and user_role junction
    org_id = uuid.uuid4()
    org = Organization(
        id=org_id,
        name=f"Auth Test Org {uuid.uuid4().hex[:6]}",
        type="pharmacy",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        sync_version=1,
        sync_status="synced",
    )
    db.add(org)
    await db.flush()

    role_id = uuid.uuid4()
    role = Role(
        id=role_id,
        organization_id=org_id,
        name="Pharmacist",
        description="Pharmacist role",
        level=20,
        permissions=["process_sales", "view_inventory"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        sync_version=1,
        sync_status="synced",
    )
    db.add(role)
    await db.flush()

    user_id = uuid.uuid4()
    username = f"testuser_{uuid.uuid4().hex[:6]}"
    password = "SecurePassword123!"
    password_hash = pwd_context.hash(password)

    user = User(
        id=user_id,
        organization_id=org_id,
        username=username,
        email=f"{username}@example.com",
        password_hash=password_hash,
        full_name="Test User",
        assigned_branches="[]",
        is_active=True,
        is_deleted=False,
        is_super_admin=False,
        failed_login_attempts=0,
        two_factor_enabled=False,
        must_change_password=False,
        sync_version=1,
        sync_status="synced",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()

    user_role = UserRole(
        user_id=user_id,
        role_id=role_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(user_role)
    await db.commit()

    # 2. Test direct selectinload query (the exact query that triggered the asyncpg error)
    stmt = (
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id)
    )
    result = await db.execute(stmt)
    loaded_user = result.scalar_one()

    assert loaded_user is not None
    assert len(loaded_user.roles) == 1
    assert loaded_user.roles[0].id == role_id
    assert loaded_user.roles[0].name == "Pharmacist"

    # 3. Test AuthService.authenticate_user
    login_req = LoginRequest(username=username, password=password)
    auth_user, access_token, refresh_token = await AuthService.authenticate_user(
        db=db,
        login_data=login_req,
        ip_address="127.0.0.1",
        user_agent="pytest/test-runner",
    )

    assert auth_user.id == user_id
    assert len(auth_user.roles) == 1
    assert auth_user.roles[0].name == "Pharmacist"
    assert access_token is not None
    assert refresh_token is not None
