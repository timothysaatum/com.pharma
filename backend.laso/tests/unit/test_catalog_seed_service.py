from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.inventory.branch_inventory import BranchInventory
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.services.catalog_seed.catalog import CATALOG, CATEGORIES
from app.services.catalog_seed.service import CatalogSeedService
from app.services.drug.drug_service import DrugService
from scripts.seed_test_catalog import _catalog_snapshot


@pytest.mark.asyncio
async def test_seed_creates_ui_visible_catalog_and_branch_inventory(
    db, setup_test_data
):
    organization, branch = setup_test_data[0], setup_test_data[1]

    result = await CatalogSeedService.seed(db, organization.id)
    await db.commit()

    visible_drugs = await DrugService.search_drugs(db, organization.id)
    demo_drugs = [
        drug for drug in visible_drugs if drug.sku and drug.sku.startswith("DEMO-")
    ]
    inventory_count = await db.scalar(
        select(func.count(BranchInventory.id)).where(
            BranchInventory.branch_id == branch.id,
            BranchInventory.drug_id.in_([drug.id for drug in demo_drugs]),
        )
    )
    category_count = await db.scalar(
        select(func.count(DrugCategory.id)).where(
            DrugCategory.organization_id == organization.id,
            DrugCategory.name.in_([category.name for category in CATEGORIES]),
            DrugCategory.is_deleted.is_(False),
        )
    )

    assert result.drug_created == len(CATALOG)
    assert len(demo_drugs) == len(CATALOG)
    assert inventory_count == len(CATALOG)
    assert category_count == len(CATEGORIES)
    assert all(drug.is_active and not drug.is_deleted for drug in demo_drugs)


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_preserves_changed_stock(db, setup_test_data):
    organization, branch = setup_test_data[0], setup_test_data[1]
    await CatalogSeedService.seed(db, organization.id)
    await db.commit()

    drug = await db.scalar(
        select(Drug).where(
            Drug.organization_id == organization.id,
            Drug.sku == CATALOG[0].sku,
        )
    )
    inventory = await db.scalar(
        select(BranchInventory).where(
            BranchInventory.branch_id == branch.id,
            BranchInventory.drug_id == drug.id,
        )
    )
    drug.name = "Outdated demo name"
    inventory.quantity = 7
    await db.commit()

    second = await CatalogSeedService.seed(db, organization.id)
    await db.commit()
    await db.refresh(drug)
    await db.refresh(inventory)

    demo_count = await db.scalar(
        select(func.count(Drug.id)).where(
            Drug.organization_id == organization.id,
            Drug.sku.in_([entry.sku for entry in CATALOG]),
        )
    )
    assert second.drug_created == 0
    assert second.drug_updated == 1
    assert second.inventory_created == 0
    assert second.inventory_existing == len(CATALOG)
    assert demo_count == len(CATALOG)
    assert drug.name == CATALOG[0].name
    assert inventory.quantity == 7


@pytest.mark.asyncio
async def test_seed_prices_are_decimal_safe(db, setup_test_data):
    organization = setup_test_data[0]
    await CatalogSeedService.seed(db, organization.id)
    await db.commit()

    drugs = list(
        (
            await db.scalars(
                select(Drug).where(
                    Drug.organization_id == organization.id,
                    Drug.sku.in_([entry.sku for entry in CATALOG]),
                )
            )
        ).all()
    )
    assert all(isinstance(drug.unit_price, Decimal) for drug in drugs)
    assert all(drug.unit_price >= drug.cost_price >= 0 for drug in drugs)
    assert all(drug.markup_percentage >= 0 for drug in drugs)


@pytest.mark.asyncio
async def test_rollback_snapshot_contains_every_modified_table_column(
    db, setup_test_data
):
    organization = setup_test_data[0]
    await CatalogSeedService.seed(db, organization.id)
    await db.commit()

    snapshot = await _catalog_snapshot(db, organization.id)
    rows_by_type = {
        record_type: next(row for row in snapshot if row["record_type"] == record_type)
        for record_type in ("category", "drug", "inventory")
    }

    assert set(DrugCategory.__table__.columns.keys()) <= rows_by_type["category"].keys()
    assert set(Drug.__table__.columns.keys()) <= rows_by_type["drug"].keys()
    assert (
        set(BranchInventory.__table__.columns.keys())
        <= rows_by_type["inventory"].keys()
    )
