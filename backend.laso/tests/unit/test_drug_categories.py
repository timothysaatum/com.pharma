import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.drugs_schemas import DrugCategoryCreate, DrugCategoryUpdate
from app.services.drug.drug_service import DrugService


@pytest.mark.asyncio
async def test_category_move_updates_descendant_paths(
    db: AsyncSession,
    setup_test_data,
):
    org, _branch, _user, _drugs, _customer = setup_test_data
    first_root = await DrugService.create_category(
        db,
        DrugCategoryCreate(organization_id=org.id, name="Medicines"),
    )
    second_root = await DrugService.create_category(
        db,
        DrugCategoryCreate(organization_id=org.id, name="Supplies"),
    )
    child = await DrugService.create_category(
        db,
        DrugCategoryCreate(
            organization_id=org.id,
            name="Analgesics",
            parent_id=first_root.id,
        ),
    )
    grandchild = await DrugService.create_category(
        db,
        DrugCategoryCreate(
            organization_id=org.id,
            name="Non-opioid",
            parent_id=child.id,
        ),
    )

    moved = await DrugService.update_category(
        db,
        child.id,
        org.id,
        DrugCategoryUpdate(parent_id=second_root.id),
    )
    await db.refresh(grandchild)

    assert moved.path == f"/{second_root.id}/"
    assert moved.level == 1
    assert grandchild.path == f"/{second_root.id}/{child.id}/"
    assert grandchild.level == 2


@pytest.mark.asyncio
async def test_category_move_rejects_cycles(
    db: AsyncSession,
    setup_test_data,
):
    org, _branch, _user, _drugs, _customer = setup_test_data
    parent = await DrugService.create_category(
        db,
        DrugCategoryCreate(organization_id=org.id, name="Parent"),
    )
    child = await DrugService.create_category(
        db,
        DrugCategoryCreate(
            organization_id=org.id,
            name="Child",
            parent_id=parent.id,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await DrugService.update_category(
            db,
            parent.id,
            org.id,
            DrugCategoryUpdate(parent_id=child.id),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_category_delete_requires_unused_leaf(
    db: AsyncSession,
    setup_test_data,
):
    org, _branch, user, _drugs, _customer = setup_test_data
    parent = await DrugService.create_category(
        db,
        DrugCategoryCreate(organization_id=org.id, name="Parent"),
    )
    child = await DrugService.create_category(
        db,
        DrugCategoryCreate(
            organization_id=org.id,
            name="Child",
            parent_id=parent.id,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await DrugService.delete_category(db, parent.id, org.id, user.id)
    assert exc_info.value.status_code == 409

    await DrugService.delete_category(db, child.id, org.id, user.id)
    await db.refresh(child)
    assert child.is_deleted is True
    assert child.deleted_by == user.id
