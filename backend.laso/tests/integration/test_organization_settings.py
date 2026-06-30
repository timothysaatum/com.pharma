import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
import uuid

from sqlalchemy import select as _select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.organization_onboarding_endpoints import (
    get_organization_settings,
    update_organization_settings,
)
from app.models.pharmacy.pharmacy_model import Organization
from app.models.system_md.sys_models import AuditLog
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pricing.pricing_model import PriceContract
from app.models.customer.customer_model import Customer
from app.schemas.sales_schemas import SaleCreate, SaleItemCreate
from app.services.sales.sales_service import SalesService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(org_id: uuid.UUID, is_admin: bool = True) -> SimpleNamespace:
    perms = ["manage_organization"] if is_admin else []
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=org_id,
        is_super_admin=False,
        has_permission=lambda p: p in perms,
        assigned_branches=[],
        username="test_user",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_returns_defaults(db: AsyncSession):
    """GET returns enable_loyalty_program defaulting to false."""
    org = Organization(
        id=uuid.uuid4(), name="GET Test", type="pharmacy", tax_id="GET-TAX", settings={},
    )
    db.add(org)
    await db.commit()

    user = _make_user(org.id, is_admin=True)
    response = await get_organization_settings(org.id, db=db, current_user=user)
    assert response.enable_loyalty_program is False
    assert response.currency is None


@pytest.mark.asyncio
async def test_patch_enable_loyalty_persists_and_audits(db: AsyncSession):
    """PATCH enable_loyalty_program to true persists and creates audit log."""
    org = Organization(
        id=uuid.uuid4(), name="PATCH Test", type="pharmacy", tax_id="PATCH-TAX", settings={},
    )
    db.add(org)
    await db.commit()

    user = _make_user(org.id, is_admin=True)

    # Patch
    from app.schemas.organization_onboarding_schemas import OrganizationSettingsUpdate
    update_data = OrganizationSettingsUpdate(enable_loyalty_program=True)

    response = await update_organization_settings(org.id, update_data, db=db, current_user=user)

    # Verify DB updated (fresh query to avoid refresh/expiry issues)
    result = await db.execute(_select(Organization).where(Organization.id == org.id))
    fresh_org = result.scalar_one()
    assert fresh_org.settings.get("enable_loyalty_program") is True

    # Verify audit log
    audit_res = await db.execute(
        _select(AuditLog).where(
            AuditLog.organization_id == org.id,
            AuditLog.action == "organization_settings_updated",
        )
    )
    audit = audit_res.scalar_one_or_none()
    assert audit is not None, "Audit log entry should exist"
    changes = audit.changes
    assert "enable_loyalty_program" in changes
    assert changes["enable_loyalty_program"]["old"] is None
    assert changes["enable_loyalty_program"]["new"] is True


@pytest.mark.asyncio
async def test_patch_only_updates_provided_fields(db: AsyncSession):
    """PATCH with exclude_unset=True does not wipe other settings."""
    org = Organization(
        id=uuid.uuid4(),
        name="Partial Test",
        type="pharmacy",
        tax_id="PARTIAL-TAX",
        settings={"currency": "GHS", "timezone": "Africa/Accra"},
    )
    db.add(org)
    await db.commit()

    user = _make_user(org.id, is_admin=True)

    from app.schemas.organization_onboarding_schemas import OrganizationSettingsUpdate
    update_data = OrganizationSettingsUpdate(enable_loyalty_program=True)

    await update_organization_settings(org.id, update_data, db=db, current_user=user)

    # Query fresh from DB to bypass any refresh/expiry issues
    result = await db.execute(_select(Organization).where(Organization.id == org.id))
    fresh_org = result.scalar_one()
    assert fresh_org.settings.get("enable_loyalty_program") is True
    assert fresh_org.settings.get("currency") == "GHS"
    assert fresh_org.settings.get("timezone") == "Africa/Accra"


@pytest.mark.asyncio
async def test_patch_cross_org_raises_403_for_different_org_admin(db: AsyncSession):
    """Admin from org A cannot PATCH org B's settings."""
    org_a = Organization(
        id=uuid.uuid4(), name="Org A", type="pharmacy", tax_id="A", settings={},
    )
    org_b = Organization(
        id=uuid.uuid4(), name="Org B", type="pharmacy", tax_id="B", settings={},
    )
    db.add_all([org_a, org_b])
    await db.commit()

    from fastapi import HTTPException
    from app.schemas.organization_onboarding_schemas import OrganizationSettingsUpdate

    user_a = _make_user(org_a.id, is_admin=True)
    update_data = OrganizationSettingsUpdate(enable_loyalty_program=True)

    with pytest.raises(HTTPException) as exc:
        await update_organization_settings(org_b.id, update_data, db=db, current_user=user_a)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_cross_org_raises_403(db: AsyncSession):
    """Admin from org A cannot GET org B's settings."""
    org_a = Organization(
        id=uuid.uuid4(), name="Org A", type="pharmacy", tax_id="A", settings={},
    )
    org_b = Organization(
        id=uuid.uuid4(), name="Org B", type="pharmacy", tax_id="B", settings={},
    )
    db.add_all([org_a, org_b])
    await db.commit()

    from fastapi import HTTPException

    user_a = _make_user(org_a.id, is_admin=True)

    with pytest.raises(HTTPException) as exc:
        await get_organization_settings(org_b.id, db=db, current_user=user_a)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Loyalty integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sale_with_loyalty_disabled_awards_zero_points(db: AsyncSession, setup_test_data):
    """Sale processed while enable_loyalty_program=false awards 0 points."""
    org, branch, user, drugs, customer = setup_test_data

    org.settings = {**org.settings, "enable_loyalty_program": False}

    batch = DrugBatch(
        id=uuid.uuid4(),
        drug_id=drugs[0].id,
        branch_id=branch.id,
        batch_number="LOYALTY-OFF",
        expiry_date=datetime.now().date() + timedelta(days=365),
        quantity=100,
        remaining_quantity=100,
    )
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drugs[0].id,
        quantity=100,
        reserved_quantity=0,
        selling_price=Decimal("50.00"),
    )
    contract = PriceContract(
        id=uuid.uuid4(),
        organization_id=org.id,
        contract_code="STD-LOYALTY-OFF",
        contract_name="Standard",
        contract_type="standard",
        effective_from=datetime.now().date() - timedelta(days=1),
        status="active",
        is_active=True,
        created_by=user.id,
    )
    db.add_all([batch, inventory, contract])
    await db.commit()

    sale_data = SaleCreate(
        branch_id=branch.id,
        price_contract_id=contract.id,
        customer_id=customer.id,
        items=[SaleItemCreate(drug_id=drugs[0].id, quantity=5)],
        payment_method="cash",
        amount_paid=Decimal("250.00"),
    )

    response = await SalesService.process_sale(db, sale_data, user)

    assert response.success
    assert response.loyalty_points_awarded == 0

    await db.refresh(customer)
    assert customer.loyalty_points == 0


@pytest.mark.asyncio
async def test_sale_with_loyalty_enabled_awards_points(db: AsyncSession, setup_test_data):
    """Sale processed while enable_loyalty_program=true awards points."""
    org, branch, user, drugs, customer = setup_test_data

    org.settings = {**org.settings, "enable_loyalty_program": True}

    batch = DrugBatch(
        id=uuid.uuid4(),
        drug_id=drugs[0].id,
        branch_id=branch.id,
        batch_number="LOYALTY-ON",
        expiry_date=datetime.now().date() + timedelta(days=365),
        quantity=100,
        remaining_quantity=100,
    )
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drugs[0].id,
        quantity=100,
        reserved_quantity=0,
        selling_price=Decimal("50.00"),
    )
    contract = PriceContract(
        id=uuid.uuid4(),
        organization_id=org.id,
        contract_code="STD-LOYALTY-ON",
        contract_name="Standard",
        contract_type="standard",
        effective_from=datetime.now().date() - timedelta(days=1),
        status="active",
        is_active=True,
        created_by=user.id,
    )
    db.add_all([batch, inventory, contract])
    await db.commit()

    sale_data = SaleCreate(
        branch_id=branch.id,
        price_contract_id=contract.id,
        customer_id=customer.id,
        items=[SaleItemCreate(drug_id=drugs[0].id, quantity=5)],
        payment_method="cash",
        amount_paid=Decimal("250.00"),
    )

    response = await SalesService.process_sale(db, sale_data, user)

    assert response.success
    assert response.loyalty_points_awarded > 0

    await db.refresh(customer)
    assert customer.loyalty_points > 0
