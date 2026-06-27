import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user.user_model import User
from app.services.org.organization_onboarding_service import (
    OrganizationOnboardingService,
)


@pytest.mark.asyncio
async def test_onboarding_creates_admin_roles_and_default_branch(
    db: AsyncSession,
):
    service = OrganizationOnboardingService(db)

    result = await service.create_organization_with_admin(
        org_data={
            "name": "Onboarding Pharmacy",
            "type": "pharmacy",
            "tax_id": "ONBOARDING-TAX-ID",
            "subscription_tier": "trial",
        },
        admin_data={
            "username": "onboarding-admin",
            "email": "onboarding@example.com",
            "full_name": "Onboarding Administrator",
            "password": "Secure-Admin-Password-2026!",
        },
        idempotency_key="onboarding-test-operation",
    )

    admin = (await db.execute(
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == result["admin_user"].id)
    )).scalar_one()

    assert len(result["branches"]) == 1
    assert len(admin.assigned_branches) == 1
    assert {role.name for role in admin.roles} == {"Admin"}
    assert admin.verify_password("Secure-Admin-Password-2026!") is True

    with pytest.raises(HTTPException) as duplicate:
        await service.create_organization_with_admin(
            org_data={
                "name": "Different Name",
                "type": "pharmacy",
                "subscription_tier": "trial",
            },
            admin_data={
                "username": "different-admin",
                "email": "different@example.com",
                "full_name": "Different Administrator",
                "password": "Secure-Admin-Password-2026!",
            },
            idempotency_key="onboarding-test-operation",
        )
    assert duplicate.value.status_code == 409
