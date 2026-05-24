"""
Drug Service
============
Business logic for drug and drug-category management.

Design principles
-----------------
* SKU and barcode are globally unique in the database (the model defines a
  bare ``unique=True`` column constraint, not a per-org composite index).
  Pre-checks therefore query without an organisation filter to match the DB
  constraint exactly, preventing an unhandled ``IntegrityError`` from reaching
  the caller.
* All sync tracking goes through ``mark_as_pending_sync()`` from
  ``SyncTrackingMixin``, which safely initialises ``sync_version`` to 1 on new
  unsaved objects instead of crashing with ``NoneType += 1``.
* ``bulk_update_drugs`` wraps every mutation in a single savepoint so the
  operation is fully atomic — either every drug in the batch is updated or
  none are committed.
* Soft-delete populates all three ``SoftDeleteMixin`` fields: ``is_deleted``,
  ``deleted_at``, and ``deleted_by``.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.inventory.branch_inventory import BranchInventory
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.schemas.base_schemas import Money
from app.schemas.drugs_schemas import (
    BulkDrugUpdate,
    DrugCategoryCreate,
    DrugCreate,
    DrugUpdate,
)

logger = logging.getLogger(__name__)


class DrugService:
    """Stateless service for drug and category management."""

    # =========================================================================
    # CREATE
    # =========================================================================

    @staticmethod
    async def create_drug(
        db: AsyncSession,
        drug_data: DrugCreate,
        created_by_user_id: uuid.UUID,
    ) -> Drug:
        """
        Create a new drug with full pre-flight validation.

        Validates:
        - SKU uniqueness globally (matches the DB's bare UNIQUE constraint).
        - Barcode uniqueness globally.
        - Category belongs to the same organisation and is not deleted.
        - Auto-calculates markup_percentage when both prices are supplied.

        Raises:
            HTTPException(400): Duplicate SKU or barcode.
            HTTPException(404): Category not found.
        """
        # --- SKU uniqueness (global — matches the DB UNIQUE constraint) -------
        if drug_data.sku:
            result = await db.execute(
                select(Drug).where(
                    Drug.sku == drug_data.sku,
                    Drug.is_deleted == False,
                )
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"SKU '{drug_data.sku}' is already in use.",
                )

        # --- Barcode uniqueness (global) --------------------------------------
        if drug_data.barcode:
            result = await db.execute(
                select(Drug).where(
                    Drug.barcode == drug_data.barcode,
                    Drug.is_deleted == False,
                )
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Barcode '{drug_data.barcode}' is already in use.",
                )

        # --- Category validation (org-scoped) ---------------------------------
        if drug_data.category_id:
            result = await db.execute(
                select(DrugCategory).where(
                    DrugCategory.id == drug_data.category_id,
                    DrugCategory.organization_id == drug_data.organization_id,
                    DrugCategory.is_deleted == False,
                )
            )
            if not result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Category not found.",
                )

        # --- Build dict and auto-calculate markup -----------------------------
        drug_dict = drug_data.model_dump()
        if (
            drug_data.cost_price
            and drug_data.unit_price
            and drug_data.cost_price > 0
        ):
            drug_dict["markup_percentage"] = round(
                ((drug_data.unit_price - drug_data.cost_price) / drug_data.cost_price)
                * 100,
                2,
            )

        drug = Drug(
            id=uuid.uuid4(),
            **drug_dict,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # mark_as_pending_sync() safely sets sync_version=1 on new objects
        drug.mark_as_pending_sync()

        db.add(drug)
        await db.commit()
        await db.refresh(drug)
        return drug

    # =========================================================================
    # READ
    # =========================================================================

    @staticmethod
    async def get_drug_by_id(
        db: AsyncSession,
        drug_id: uuid.UUID,
        organization_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> Optional[Drug]:
        """Return a single Drug scoped to the organisation, or None."""
        query = select(Drug).where(
            Drug.id == drug_id,
            Drug.organization_id == organization_id,
        )
        if not include_deleted:
            query = query.where(Drug.is_deleted == False)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def search_drugs(
        db: AsyncSession,
        organization_id: uuid.UUID,
        search: Optional[str] = None,
        category_id: Optional[uuid.UUID] = None,
        drug_type: Optional[str] = None,
        requires_prescription: Optional[bool] = None,
        is_active: Optional[bool] = True,
        min_price: Optional[Money] = None,
        max_price: Optional[Money] = None,
        manufacturer: Optional[str] = None,
        supplier: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[Drug]:
        """
        Search drugs with multiple optional filters.

        All text filters use ILIKE for case-insensitive partial matching.
        Results are ordered alphabetically by name.
        """
        query = select(Drug).where(Drug.organization_id == organization_id)

        if not include_deleted:
            query = query.where(Drug.is_deleted == False)

        if is_active is not None:
            query = query.where(Drug.is_active == is_active)

        if search:
            pattern = f"%{search}%"
            query = query.where(
                or_(
                    Drug.name.ilike(pattern),
                    Drug.generic_name.ilike(pattern),
                    Drug.brand_name.ilike(pattern),
                    Drug.sku.ilike(pattern),
                    Drug.barcode.ilike(pattern),
                    Drug.manufacturer.ilike(pattern),
                )
            )

        if category_id:
            query = query.where(Drug.category_id == category_id)

        if drug_type:
            query = query.where(Drug.drug_type == drug_type)

        if requires_prescription is not None:
            query = query.where(Drug.requires_prescription == requires_prescription)

        if min_price is not None:
            query = query.where(Drug.unit_price >= min_price)

        if max_price is not None:
            query = query.where(Drug.unit_price <= max_price)

        if manufacturer:
            query = query.where(Drug.manufacturer.ilike(f"%{manufacturer}%"))

        if supplier:
            query = query.where(Drug.supplier.ilike(f"%{supplier}%"))

        query = query.order_by(Drug.name)
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_drug_with_inventory(
        db: AsyncSession,
        drug_id: uuid.UUID,
        organization_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Return a drug alongside its aggregated inventory summary.

        When ``branch_id`` is supplied only that branch's inventory is
        included. Available quantity = total - reserved.
        """
        drug = await DrugService.get_drug_by_id(db, drug_id, organization_id)
        if not drug:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Drug not found.",
            )

        inv_query = select(BranchInventory).where(
            BranchInventory.drug_id == drug_id
        )
        if branch_id:
            inv_query = inv_query.where(BranchInventory.branch_id == branch_id)

        result = await db.execute(inv_query)
        inventories = result.scalars().all()

        total_qty     = sum(inv.quantity for inv in inventories)
        reserved_qty  = sum(inv.reserved_quantity for inv in inventories)
        available_qty = total_qty - reserved_qty

        if total_qty == 0:
            inv_status = "out_of_stock"
        elif total_qty <= drug.reorder_level:
            inv_status = "low_stock"
        else:
            inv_status = "in_stock"

        return {
            "drug":             drug,
            "total_quantity":   total_qty,
            "available_quantity": available_qty,
            "reserved_quantity":  reserved_qty,
            "inventory_status": inv_status,
            "needs_reorder":    total_qty <= drug.reorder_level,
            "inventories":      inventories,
        }

    # =========================================================================
    # UPDATE
    # =========================================================================

    @staticmethod
    async def update_drug(
        db: AsyncSession,
        drug_id: uuid.UUID,
        drug_data: DrugUpdate,
        organization_id: uuid.UUID,
        updated_by_user_id: uuid.UUID,
    ) -> Drug:
        """
        Update a drug with uniqueness and category validation.

        Only fields present in ``drug_data`` are written (``exclude_unset``).
        Markup percentage is recalculated automatically when either price
        changes.

        Raises:
            HTTPException(404): Drug not found.
            HTTPException(400): New SKU or barcode already in use.
        """
        drug = await DrugService.get_drug_by_id(db, drug_id, organization_id)
        if not drug:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Drug not found.",
            )

        # --- SKU uniqueness (global, exclude self) ----------------------------
        if drug_data.sku and drug_data.sku != drug.sku:
            result = await db.execute(
                select(Drug).where(
                    Drug.sku == drug_data.sku,
                    Drug.id != drug_id,
                    Drug.is_deleted == False,
                )
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"SKU '{drug_data.sku}' is already in use.",
                )

        # --- Barcode uniqueness (global, exclude self) ------------------------
        if drug_data.barcode and drug_data.barcode != drug.barcode:
            result = await db.execute(
                select(Drug).where(
                    Drug.barcode == drug_data.barcode,
                    Drug.id != drug_id,
                    Drug.is_deleted == False,
                )
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Barcode '{drug_data.barcode}' is already in use.",
                )

        update_data = drug_data.model_dump(exclude_unset=True)

        # --- Recalculate markup if either price changed -----------------------
        new_cost  = update_data.get("cost_price",  drug.cost_price)
        new_price = update_data.get("unit_price",  drug.unit_price)
        if new_cost and new_price and new_cost > 0:
            update_data["markup_percentage"] = round(
                ((new_price - new_cost) / new_cost) * 100, 2
            )

        for field, value in update_data.items():
            setattr(drug, field, value)

        drug.updated_at = datetime.now(timezone.utc)
        drug.mark_as_pending_sync()

        await db.commit()
        await db.refresh(drug)
        return drug

    @staticmethod
    async def bulk_update_drugs(
        db: AsyncSession,
        organization_id: uuid.UUID,
        bulk_update: BulkDrugUpdate,
        updated_by_user_id: uuid.UUID,
    ) -> Tuple[int, List[Dict]]:
        """
        Atomically update multiple drugs in a single savepoint.

        The entire batch is all-or-nothing: if any individual update fails,
        the savepoint is rolled back and no drugs are modified.

        Returns:
            Tuple of (successful_count, list of error dicts for any failures).
            On a full rollback, successful_count is 0 and errors lists all
            drug IDs that were attempted.

        Raises:
            HTTPException(400): If any update fails — the whole batch is
            rolled back to prevent partial writes.
        """
        errors: List[Dict] = []

        async with db.begin_nested():  # savepoint — rolls back everything on error
            for drug_id in bulk_update.drug_ids:
                try:
                    await DrugService.update_drug(
                        db=db,
                        drug_id=drug_id,
                        drug_data=bulk_update.updates,
                        organization_id=organization_id,
                        updated_by_user_id=updated_by_user_id,
                    )
                except HTTPException as exc:
                    errors.append({"drug_id": str(drug_id), "error": exc.detail})
                    raise  # re-raise to trigger savepoint rollback
                except Exception as exc:
                    logger.exception("Unexpected error updating drug %s", drug_id)
                    errors.append({"drug_id": str(drug_id), "error": str(exc)})
                    raise

        await db.commit()
        return len(bulk_update.drug_ids), errors

    # =========================================================================
    # DELETE
    # =========================================================================

    @staticmethod
    async def delete_drug(
        db: AsyncSession,
        drug_id: uuid.UUID,
        organization_id: uuid.UUID,
        deleted_by_user_id: uuid.UUID,
        hard_delete: bool = False,
    ) -> bool:
        """
        Soft-delete (default) or hard-delete a drug.

        A drug with existing inventory cannot be deleted — the caller must
        zero out all branch inventory first. Soft-delete populates all three
        ``SoftDeleteMixin`` fields.

        Raises:
            HTTPException(404): Drug not found.
            HTTPException(400): Drug has existing inventory.
        """
        drug = await DrugService.get_drug_by_id(db, drug_id, organization_id)
        if not drug:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Drug not found.",
            )

        result = await db.execute(
            select(func.sum(BranchInventory.quantity)).where(
                BranchInventory.drug_id == drug_id
            )
        )
        total_inventory = result.scalar() or 0
        if total_inventory > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot delete a drug with {total_inventory} units in stock. "
                    "Remove all branch inventory first."
                ),
            )

        if hard_delete:
            await db.delete(drug)
        else:
            drug.is_deleted  = True
            drug.deleted_at  = datetime.now(timezone.utc)
            drug.deleted_by  = deleted_by_user_id
            drug.updated_at  = datetime.now(timezone.utc)
            drug.mark_as_pending_sync()

        await db.commit()
        return True

    # =========================================================================
    # CATEGORY METHODS
    # =========================================================================

    @staticmethod
    async def create_category(
        db: AsyncSession,
        category_data: DrugCategoryCreate,
    ) -> DrugCategory:
        """
        Create a drug category, computing its ``level`` and ``path`` from
        its parent.

        Root categories (no parent) get level=0 and path='/'.
        Child categories inherit the parent's path and increment the level.

        Raises:
            HTTPException(404): Parent category not found or deleted.
        """
        if category_data.parent_id:
            result = await db.execute(
                select(DrugCategory).where(
                    DrugCategory.id == category_data.parent_id,
                    DrugCategory.organization_id == category_data.organization_id,
                    DrugCategory.is_deleted == False,
                )
            )
            parent = result.scalar_one_or_none()
            if not parent:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Parent category not found.",
                )
            level = parent.level + 1
            path  = f"{parent.path}{parent.id}/"
        else:
            level = 0
            path  = "/"

        category = DrugCategory(
            id=uuid.uuid4(),
            **category_data.model_dump(),
            level=level,
            path=path,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        category.mark_as_pending_sync()

        db.add(category)
        await db.commit()
        await db.refresh(category)
        return category

    @staticmethod
    async def get_category_tree(
        db: AsyncSession,
        organization_id: uuid.UUID,
        parent_id: Optional[uuid.UUID] = None,
    ) -> List[DrugCategory]:
        """
        Return the category tree starting from a given parent (default: roots).

        Eagerly loads three levels of children in a single round-trip using
        ``selectinload`` chains.  Add more ``.selectinload(DrugCategory.children)``
        links for deeper hierarchies.
        """
        query = (
            select(DrugCategory)
            .where(
                DrugCategory.organization_id == organization_id,
                DrugCategory.is_deleted == False,
                DrugCategory.parent_id == parent_id,
            )
            .options(
                selectinload(DrugCategory.children)
                .selectinload(DrugCategory.children)
                .selectinload(DrugCategory.children)
            )
            .order_by(DrugCategory.name)
        )

        result = await db.execute(query)
        return list(result.scalars().all())