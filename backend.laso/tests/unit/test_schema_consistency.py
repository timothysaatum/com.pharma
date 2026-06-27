import uuid
from datetime import timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Organization
from app.schemas.drugs_schemas import DrugResponse


@pytest.mark.asyncio
async def test_drug_identifiers_are_scoped_to_organization(
    db: AsyncSession,
    setup_test_data,
):
    org, _branch, _user, drugs, _customer = setup_test_data
    other_org = Organization(
        id=uuid.uuid4(),
        name="Second Pharmacy",
        type="pharmacy",
        tax_id="SECOND-TAX-ID",
        settings={},
    )
    same_identifiers = Drug(
        id=uuid.uuid4(),
        organization_id=other_org.id,
        name="Tenant-specific equivalent",
        sku=drugs[0].sku,
        barcode="SHARED-BARCODE",
        unit_price=Decimal("50.00"),
        reorder_level=10,
        is_active=True,
        is_deleted=False,
        tax_rate=Decimal("0.00"),
    )
    drugs[0].barcode = "SHARED-BARCODE"
    db.add_all([other_org, same_identifiers])

    await db.commit()

    assert same_identifiers.organization_id != org.id


@pytest.mark.asyncio
async def test_sync_schema_exposes_model_last_synced_timestamp(
    db: AsyncSession,
    setup_test_data,
):
    _org, _branch, _user, drugs, _customer = setup_test_data
    drug = drugs[0]
    drug.mark_as_synced()
    await db.commit()
    await db.refresh(drug)

    response = DrugResponse.model_validate(drug)

    assert response.synced_at is not None
    assert response.synced_at.tzinfo in (None, timezone.utc)
