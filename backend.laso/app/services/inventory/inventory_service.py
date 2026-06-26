"""
Inventory Service
=================
Business logic for branch inventory, batch tracking, stock adjustments,
inter-branch transfers, and reporting.

Design principles
-----------------
* ``adjust_inventory`` and all write paths use ``SELECT ... FOR UPDATE``
  row-level locks so concurrent requests cannot produce lost updates or
  negative-stock races.
* ``transfer_stock`` is fully atomic: both the source deduction and destination
  credit run inside a single ``begin_nested()`` savepoint.  A failure on
  either side rolls back both writes before any commit is issued.
* ``create_batch`` **adds** the incoming quantity to ``BranchInventory``
  (additive receipt).  It never overwrites existing stock.
* ``consume_from_batch`` keeps ``DrugBatch.remaining_quantity`` and
  ``BranchInventory.quantity`` in sync by updating both in the same
  transaction.
* ``adjustment_type`` is validated against the model's ``CheckConstraint``
  allowlist before any DB write, producing a clean 400 rather than an
  unhandled ``IntegrityError``.
* All sync tracking uses ``mark_as_pending_sync()`` from
  ``SyncTrackingMixin``, which safely initialises ``sync_version`` to 1 on
  new unsaved objects instead of crashing with ``NoneType += 1``.
* Batch expiry boundary in dispensing-path queries uses ``> date.today()``
  (strictly future), consistent with the sales service.  Reporting queries
  use ``>= date.today()`` so items expiring today appear in warnings.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.inventory.branch_inventory import (
    BranchInventory,
    DrugBatch,
    StockAdjustment,
)
from app.models.inventory.ledger import InventoryMovement
from app.models.inventory.inventory_model import Drug
from app.schemas.inventory_schemas import (
    BranchInventoryWithDetails,
    DrugBatchCreate,
    DrugBatchResponse,
    DrugBatchUpdate,
    ExpiringBatchItem,
    ExpiringBatchReport,
    InventoryValuationItem,
    InventoryValuationResponse,
    LowStockItem,
    LowStockReport,
)
from app.utils.pagination import PaginatedResponse, PaginationParams

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model constraint: StockAdjustment.adjustment_type
# Must stay in sync with the CheckConstraint in branch_inventory.py
# ---------------------------------------------------------------------------
_VALID_ADJUSTMENT_TYPES: frozenset[str] = frozenset(
    {
        "damage",
        "expired",
        "theft",
        "return",
        "correction",
        "transfer",
        "purchase_receipt",
    }
)


class InventoryService:
    """Stateless service for inventory management."""

    @staticmethod
    async def _get_branch_organization_id(
        db: AsyncSession,
        branch_id: uuid.UUID,
    ) -> uuid.UUID:
        from app.models.pharmacy.pharmacy_model import Branch

        organization_id = await db.scalar(
            select(Branch.organization_id).where(Branch.id == branch_id)
        )
        if organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found.",
            )
        return organization_id

    @staticmethod
    async def _record_inventory_movement(
        db: AsyncSession,
        *,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        movement_type: str,
        quantity_change: int,
        quantity_before: int,
        quantity_after: int,
        organization_id: Optional[uuid.UUID] = None,
        batch_id: Optional[uuid.UUID] = None,
        batch_quantity_before: Optional[int] = None,
        batch_quantity_after: Optional[int] = None,
        unit_cost: Optional[Decimal] = None,
        unit_price: Optional[Decimal] = None,
        source_type: Optional[str] = None,
        source_id: Optional[uuid.UUID] = None,
        source_line_id: Optional[uuid.UUID] = None,
        reference_number: Optional[str] = None,
        reason: Optional[str] = None,
        created_by: Optional[uuid.UUID] = None,
        context_metadata: Optional[dict] = None,
    ) -> InventoryMovement:
        if organization_id is None:
            organization_id = await InventoryService._get_branch_organization_id(
                db, branch_id
            )

        movement = InventoryMovement(
            id=uuid.uuid4(),
            organization_id=organization_id,
            branch_id=branch_id,
            drug_id=drug_id,
            batch_id=batch_id,
            movement_type=movement_type,
            quantity_change=quantity_change,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            batch_quantity_before=batch_quantity_before,
            batch_quantity_after=batch_quantity_after,
            unit_cost=unit_cost,
            unit_price=unit_price,
            source_type=source_type,
            source_id=source_id,
            source_line_id=source_line_id,
            reference_number=reference_number,
            reason=reason,
            created_by=created_by,
            context_metadata=context_metadata,
            occurred_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(movement)
        await db.flush()
        return movement

    @staticmethod
    async def _get_fefo_batch_selling_prices(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_ids: List[uuid.UUID],
    ) -> Dict[uuid.UUID, Decimal]:
        """
        Return the earliest valid batch selling price for each drug.

        This mirrors the POS sale price fallback: when a branch-level override
        is not set, the earliest-expiry non-empty batch can define the branch's
        current selling price before falling back to the catalogue base price.
        """
        if not drug_ids:
            return {}

        result = await db.execute(
            select(DrugBatch.drug_id, DrugBatch.selling_price)
            .where(
                DrugBatch.branch_id == branch_id,
                DrugBatch.drug_id.in_(drug_ids),
                DrugBatch.remaining_quantity > 0,
                DrugBatch.expiry_date > date.today(),
            )
            .order_by(DrugBatch.drug_id, DrugBatch.expiry_date.asc())
        )

        prices: Dict[uuid.UUID, Decimal] = {}
        seen: set[uuid.UUID] = set()
        for row in result:
            if row.drug_id in seen:
                continue
            seen.add(row.drug_id)
            if row.selling_price is not None:
                prices[row.drug_id] = Decimal(str(row.selling_price))

        return prices

    @staticmethod
    def _resolve_effective_selling_price(
        catalog_unit_price: Decimal,
        branch_selling_price: Optional[Decimal] = None,
        batch_selling_price: Optional[Decimal] = None,
    ) -> Decimal:
        if branch_selling_price is not None:
            return branch_selling_price
        if batch_selling_price is not None:
            return batch_selling_price
        return catalog_unit_price

    @staticmethod
    async def get_effective_selling_prices(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drugs: List[Drug],
    ) -> Dict[uuid.UUID, Decimal]:
        """Resolve customer-facing unit prices for drugs at a branch."""
        drug_ids = [drug.id for drug in drugs]
        if not drug_ids:
            return {}

        branch_price_rows = await db.execute(
            select(BranchInventory.drug_id, BranchInventory.selling_price).where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id.in_(drug_ids),
            )
        )
        branch_prices: Dict[uuid.UUID, Decimal] = {
            row.drug_id: Decimal(str(row.selling_price))
            for row in branch_price_rows
            if row.selling_price is not None
        }
        batch_prices = await InventoryService._get_fefo_batch_selling_prices(
            db=db,
            branch_id=branch_id,
            drug_ids=drug_ids,
        )

        return {
            drug.id: InventoryService._resolve_effective_selling_price(
                catalog_unit_price=Decimal(str(drug.unit_price)),
                branch_selling_price=branch_prices.get(drug.id),
                batch_selling_price=batch_prices.get(drug.id),
            )
            for drug in drugs
        }

    # =========================================================================
    # READ — Branch inventory
    # =========================================================================

    @staticmethod
    async def get_branch_inventory(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: Optional[uuid.UUID] = None,
        include_zero_stock: bool = False,
    ) -> List[BranchInventory]:
        """
        Return inventory rows for a branch (non-paginated, for internal use).
        """
        query = select(BranchInventory).where(
            BranchInventory.branch_id == branch_id
        )
        if drug_id:
            query = query.where(BranchInventory.drug_id == drug_id)
        if not include_zero_stock:
            query = query.where(BranchInventory.quantity > 0)

        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_branch_inventory_paginated(
        db: AsyncSession,
        branch_id: uuid.UUID,
        pagination: PaginationParams,
        drug_id: Optional[uuid.UUID] = None,
        include_zero_stock: bool = False,
        search: Optional[str] = None,
        low_stock_only: bool = False,
        drug_type: Optional[str] = None,
    ) -> PaginatedResponse[BranchInventoryWithDetails]:
        """
        Paginated inventory for a branch with joined drug and branch details.

        Supports free-text search across drug name, generic name, SKU and
        barcode, plus a ``low_stock_only`` flag that filters to rows where
        ``quantity <= Drug.reorder_level``.
        """
        from app.models.pharmacy.pharmacy_model import Branch

        query = (
            select(BranchInventory)
            .join(Drug,   BranchInventory.drug_id   == Drug.id)
            .join(Branch, BranchInventory.branch_id == Branch.id)
            .options(
                joinedload(BranchInventory.drug),
                joinedload(BranchInventory.branch),
            )
            .where(BranchInventory.branch_id == branch_id)
        )

        count_base = (
            select(func.count())
            .select_from(BranchInventory)
            .join(Drug, BranchInventory.drug_id == Drug.id)
            .where(BranchInventory.branch_id == branch_id)
        )

        if drug_id:
            query      = query.where(BranchInventory.drug_id == drug_id)
            count_base = count_base.where(BranchInventory.drug_id == drug_id)

        if not include_zero_stock:
            query      = query.where(BranchInventory.quantity > 0)
            count_base = count_base.where(BranchInventory.quantity > 0)

        if search:
            pattern = f"%{search}%"
            cond = or_(
                Drug.name.ilike(pattern),
                Drug.generic_name.ilike(pattern),
                Drug.sku.ilike(pattern),
                Drug.barcode.ilike(pattern),
            )
            query      = query.where(cond)
            count_base = count_base.where(cond)

        if low_stock_only:
            query      = query.where(BranchInventory.quantity <= Drug.reorder_level)
            count_base = count_base.where(BranchInventory.quantity <= Drug.reorder_level)

        if drug_type:
            if drug_type == "prescription":
                cond = or_(
                    Drug.drug_type == "prescription",
                    Drug.requires_prescription.is_(True),
                )
            else:
                cond = Drug.drug_type == drug_type
            query      = query.where(cond)
            count_base = count_base.where(cond)

        query = query.order_by(Drug.name)

        total: int = (await db.execute(count_base)).scalar_one()
        offset      = (pagination.page - 1) * pagination.page_size
        rows        = list(
            (await db.execute(query.offset(offset).limit(pagination.page_size)))
            .scalars()
            .unique()
            .all()
        )
        batch_selling_prices = await InventoryService._get_fefo_batch_selling_prices(
            db=db,
            branch_id=branch_id,
            drug_ids=[inv.drug_id for inv in rows],
        )

        items: List[BranchInventoryWithDetails] = []
        for inv in rows:
            drug: Drug             = inv.drug
            branch                 = inv.branch
            catalog_unit_price = Decimal(str(drug.unit_price))
            branch_unit_price = (
                Decimal(str(inv.selling_price))
                if inv.selling_price is not None
                else None
            )
            effective_unit_price = (
                InventoryService._resolve_effective_selling_price(
                    catalog_unit_price=catalog_unit_price,
                    branch_selling_price=branch_unit_price,
                    batch_selling_price=batch_selling_prices.get(inv.drug_id),
                )
            )
            items.append(
                BranchInventoryWithDetails(
                    id=inv.id,
                    branch_id=inv.branch_id,
                    drug_id=inv.drug_id,
                    quantity=inv.quantity,
                    reserved_quantity=inv.reserved_quantity,
                    location=inv.location,
                    selling_price=branch_unit_price,
                    created_at=inv.created_at,
                    updated_at=inv.updated_at,
                    sync_version=inv.sync_version,
                    sync_status=inv.sync_status,
                    drug_name=drug.name,
                    drug_sku=drug.sku,
                    drug_type=drug.drug_type or "otc",
                    requires_prescription=drug.requires_prescription,
                    catalog_unit_price=catalog_unit_price,
                    # Backwards-compatible POS price field. Older clients use
                    # drug_unit_price directly, so expose the branch-effective
                    # sell price here and keep the catalogue price above.
                    drug_unit_price=effective_unit_price,
                    effective_unit_price=effective_unit_price,
                    # Included so the frontend can render accurate per-drug stock
                    # status badges without a separate drugApi.getById() call.
                    drug_reorder_level=drug.reorder_level,
                    branch_name=branch.name,
                    branch_code=branch.code,
                )
            )

        total_pages = max(1, -(-total // pagination.page_size))
        return PaginatedResponse(
            items=items,
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
            total_pages=total_pages,
            has_next=pagination.page < total_pages,
            has_prev=pagination.page > 1,
        )

    # =========================================================================
    # WRITE — Inventory management
    # =========================================================================

    @staticmethod
    async def set_inventory(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        quantity: int,
        location: Optional[str] = None,
    ) -> BranchInventory:
        """
        Administrative SET operation: overwrite the inventory count.

        This is for manual corrections (e.g. stocktake reconciliation).
        To add stock from a goods receipt, use ``create_batch`` instead —
        that method is additive.

        Raises:
            HTTPException(400): quantity is negative.
        """
        if quantity < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Inventory quantity cannot be negative.",
            )

        result = await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id   == drug_id,
            )
            .with_for_update()
        )
        inventory = result.scalar_one_or_none()
        previous_quantity = inventory.quantity if inventory else 0

        if inventory:
            inventory.quantity = quantity
            if location:
                inventory.location = location
            inventory.updated_at = datetime.now(timezone.utc)
            inventory.mark_as_pending_sync()
        else:
            inventory = BranchInventory(
                id=uuid.uuid4(),
                branch_id=branch_id,
                drug_id=drug_id,
                quantity=quantity,
                reserved_quantity=0,
                location=location,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            inventory.mark_as_pending_sync()
            db.add(inventory)

        # Upsert system adjustment batch to maintain DrugBatch parity
        batch_res = await db.execute(
            select(DrugBatch)
            .where(
                DrugBatch.branch_id == branch_id,
                DrugBatch.drug_id == drug_id,
                DrugBatch.batch_number.like("ADJ-%"),
            )
            .with_for_update()
        )
        sys_batch = batch_res.scalar_one_or_none()
        if sys_batch:
            sys_batch.remaining_quantity = quantity
            if sys_batch.remaining_quantity > sys_batch.quantity:
                sys_batch.quantity = sys_batch.remaining_quantity
            sys_batch.updated_at = datetime.now(timezone.utc)
            sys_batch.mark_as_pending_sync()
        else:
            sys_batch = DrugBatch(
                id=uuid.uuid4(),
                branch_id=branch_id,
                drug_id=drug_id,
                batch_number=f"ADJ-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                quantity=quantity,
                remaining_quantity=quantity,
                expiry_date=date.today() + timedelta(days=365 * 10),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            sys_batch.mark_as_pending_sync()
            db.add(sys_batch)

        quantity_change = quantity - previous_quantity
        if quantity_change != 0:
            await InventoryService._record_inventory_movement(
                db=db,
                branch_id=branch_id,
                drug_id=drug_id,
                movement_type="correction",
                quantity_change=quantity_change,
                quantity_before=previous_quantity,
                quantity_after=quantity,
                batch_id=sys_batch.id,
                batch_quantity_before=None,
                batch_quantity_after=sys_batch.remaining_quantity,
                source_type="stock_adjustment",
                reason="Manual inventory set",
            )

        # Resolve any existing low stock alerts if quantity is now healthy
        await InventoryService._resolve_inventory_alerts(db, branch_id, drug_id, quantity)

        await db.commit()
        await db.refresh(inventory)
        return inventory

    @staticmethod
    async def add_drug_to_branch(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        organization_id: uuid.UUID,
        location: Optional[str] = None,
        selling_price: Optional[Decimal] = None,
    ) -> BranchInventory:
        """Make an organization catalogue drug available for sale at a branch."""
        drug = await db.scalar(
            select(Drug).where(
                Drug.id == drug_id,
                Drug.organization_id == organization_id,
                Drug.is_active == True,
                Drug.is_deleted == False,
            )
        )
        if not drug:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Drug not found in this organization.",
            )

        existing = await db.scalar(
            select(BranchInventory).where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id == drug_id,
            )
        )
        if existing:
            return existing

        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch_id,
            drug_id=drug_id,
            quantity=0,
            reserved_quantity=0,
            location=location,
            selling_price=selling_price,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        inventory.mark_as_pending_sync()
        db.add(inventory)
        await db.commit()
        await db.refresh(inventory)
        return inventory

    @staticmethod
    async def update_branch_drug_settings(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        location: Optional[str] = None,
        selling_price: Optional[Decimal] = None,
    ) -> BranchInventory:
        inventory = await db.scalar(
            select(BranchInventory).where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id == drug_id,
            )
        )
        if not inventory:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Drug is not available at this branch.",
            )

        inventory.location = location
        inventory.selling_price = selling_price
        inventory.updated_at = datetime.now(timezone.utc)
        inventory.mark_as_pending_sync()
        await db.commit()
        await db.refresh(inventory)
        return inventory

    @staticmethod
    async def remove_drug_from_branch(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
    ) -> None:
        result = await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id == drug_id,
            )
            .with_for_update()
        )
        inventory = result.scalar_one_or_none()
        if not inventory:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Drug is not available at this branch.",
            )
        if inventory.quantity > 0 or inventory.reserved_quantity > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove a branch drug while it still has stock or reservations.",
            )

        active_batches = await db.scalar(
            select(func.count()).select_from(DrugBatch).where(
                DrugBatch.branch_id == branch_id,
                DrugBatch.drug_id == drug_id,
                DrugBatch.remaining_quantity > 0,
            )
        )
        if active_batches:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove a branch drug while active batches remain.",
            )

        await db.delete(inventory)
        await db.commit()

    @staticmethod
    async def adjust_inventory(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        quantity_change: int,
        adjustment_type: str,
        reason: str,
        adjusted_by: uuid.UUID,
        transfer_to_branch_id: Optional[uuid.UUID] = None,
    ) -> Tuple[StockAdjustment, BranchInventory]:
        """
        Apply a signed quantity change with a full audit trail.

        Acquires a ``SELECT ... FOR UPDATE`` row lock to prevent race
        conditions under concurrent load.  ``adjustment_type`` is validated
        against the model's CheckConstraint before any write.

        Args:
            quantity_change: Positive to add, negative to remove.
            adjustment_type: One of 'damage', 'expired', 'theft', 'return',
                             'correction', 'transfer'.
            transfer_to_branch_id: Required when adjustment_type='transfer'.

        Raises:
            HTTPException(400): Invalid type, would result in negative stock,
                                or transfer missing destination.
            HTTPException(404): No inventory record for this drug at the branch.
        """
        if adjustment_type not in _VALID_ADJUSTMENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid adjustment type '{adjustment_type}'. "
                    f"Must be one of: {sorted(_VALID_ADJUSTMENT_TYPES)}."
                ),
            )

        if adjustment_type == "transfer" and not transfer_to_branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="transfer_to_branch_id is required for transfer adjustments.",
            )

        async with db.begin_nested():
            adjustment, inventory = await InventoryService._apply_adjustment(
                db=db,
                branch_id=branch_id,
                drug_id=drug_id,
                quantity_change=quantity_change,
                adjustment_type=adjustment_type,
                reason=reason,
                adjusted_by=adjusted_by,
                transfer_to_branch_id=transfer_to_branch_id,
            )

        await db.commit()
        await db.refresh(adjustment)
        await db.refresh(inventory)
        return adjustment, inventory

    @staticmethod
    async def transfer_stock(
        db: AsyncSession,
        from_branch_id: uuid.UUID,
        to_branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        quantity: int,
        reason: str,
        transferred_by: uuid.UUID,
    ) -> Tuple[StockAdjustment, StockAdjustment]:
        """
        Transfer stock between branches atomically.

        Both the source deduction and the destination credit are written inside
        a single savepoint.  If either write fails, neither is committed.

        Raises:
            HTTPException(400): Same-branch, non-positive quantity, or
                                insufficient available stock.
            HTTPException(404): Drug not found in the source branch.
        """
        if from_branch_id == to_branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Source and destination branches must be different.",
            )

        if quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer quantity must be positive.",
            )

        async with db.begin_nested():  # savepoint — both sides or neither
            source_org_id = await InventoryService._get_branch_organization_id(
                db, from_branch_id
            )
            dest_org_id = await InventoryService._get_branch_organization_id(
                db, to_branch_id
            )
            if source_org_id != dest_org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot transfer stock between different organizations.",
                )

            # -- Pre-flight: check available stock at source with a lock -------
            src_res = await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == from_branch_id,
                    BranchInventory.drug_id   == drug_id,
                )
                .with_for_update()
            )
            source_inventory = src_res.scalar_one_or_none()
            if not source_inventory:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Drug not found in source branch inventory.",
                )

            available = source_inventory.quantity - source_inventory.reserved_quantity
            if available < quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient available stock. "
                        f"Available: {available}, Requested: {quantity}."
                    ),
                )

            # -- Ensure destination inventory record exists -------------------
            dst_res = await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == to_branch_id,
                    BranchInventory.drug_id   == drug_id,
                )
                .with_for_update()
            )
            dest_inventory = dst_res.scalar_one_or_none()
            if not dest_inventory:
                dest_inventory = BranchInventory(
                    id=uuid.uuid4(),
                    branch_id=to_branch_id,
                    drug_id=drug_id,
                    quantity=0,
                    reserved_quantity=0,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                dest_inventory.mark_as_pending_sync()
                db.add(dest_inventory)
                await db.flush()  # persist so _apply_adjustment can find it

            source_previous = source_inventory.quantity
            dest_previous = dest_inventory.quantity
            source_new = source_previous - quantity
            dest_new = dest_previous + quantity

            source_adjustment = StockAdjustment(
                id=uuid.uuid4(),
                branch_id=from_branch_id,
                drug_id=drug_id,
                adjustment_type="transfer",
                quantity_change=-quantity,
                previous_quantity=source_previous,
                new_quantity=source_new,
                reason=f"Transfer to branch {to_branch_id}: {reason}",
                adjusted_by=transferred_by,
                transfer_to_branch_id=to_branch_id,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            dest_adjustment = StockAdjustment(
                id=uuid.uuid4(),
                branch_id=to_branch_id,
                drug_id=drug_id,
                adjustment_type="transfer",
                quantity_change=quantity,
                previous_quantity=dest_previous,
                new_quantity=dest_new,
                reason=f"Transfer from branch {from_branch_id}: {reason}",
                adjusted_by=transferred_by,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add_all([source_adjustment, dest_adjustment])
            await db.flush()

            source_batches_res = await db.execute(
                select(DrugBatch)
                .where(
                    DrugBatch.branch_id == from_branch_id,
                    DrugBatch.drug_id == drug_id,
                    DrugBatch.remaining_quantity > 0,
                    DrugBatch.expiry_date > date.today(),
                )
                .order_by(DrugBatch.expiry_date.asc(), DrugBatch.created_at.asc())
                .with_for_update()
            )
            source_batches = source_batches_res.scalars().all()

            qty_to_move = quantity
            running_source_qty = source_previous
            running_dest_qty = dest_previous

            for source_batch in source_batches:
                if qty_to_move <= 0:
                    break

                take = min(source_batch.remaining_quantity, qty_to_move)
                source_batch_before = source_batch.remaining_quantity
                source_batch.remaining_quantity -= take
                source_batch.updated_at = datetime.now(timezone.utc)
                source_batch.mark_as_pending_sync()

                dest_batch_res = await db.execute(
                    select(DrugBatch)
                    .where(
                        DrugBatch.branch_id == to_branch_id,
                        DrugBatch.drug_id == drug_id,
                        DrugBatch.batch_number == source_batch.batch_number,
                    )
                    .with_for_update()
                )
                dest_batch = dest_batch_res.scalar_one_or_none()
                if dest_batch:
                    dest_batch_before = dest_batch.remaining_quantity
                    dest_batch.remaining_quantity += take
                    dest_batch.quantity += take
                    dest_batch.updated_at = datetime.now(timezone.utc)
                    dest_batch.mark_as_pending_sync()
                else:
                    dest_batch_before = 0
                    dest_batch = DrugBatch(
                        id=uuid.uuid4(),
                        branch_id=to_branch_id,
                        drug_id=drug_id,
                        batch_number=source_batch.batch_number,
                        quantity=take,
                        remaining_quantity=take,
                        manufacturing_date=source_batch.manufacturing_date,
                        expiry_date=source_batch.expiry_date,
                        cost_price=source_batch.cost_price,
                        selling_price=source_batch.selling_price,
                        supplier=source_batch.supplier,
                        purchase_order_id=source_batch.purchase_order_id,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    dest_batch.mark_as_pending_sync()
                    db.add(dest_batch)
                    await db.flush()

                source_before = running_source_qty
                running_source_qty -= take
                dest_before = running_dest_qty
                running_dest_qty += take

                await InventoryService._record_inventory_movement(
                    db=db,
                    organization_id=source_org_id,
                    branch_id=from_branch_id,
                    drug_id=drug_id,
                    movement_type="transfer_out",
                    quantity_change=-take,
                    quantity_before=source_before,
                    quantity_after=running_source_qty,
                    batch_id=source_batch.id,
                    batch_quantity_before=source_batch_before,
                    batch_quantity_after=source_batch.remaining_quantity,
                    unit_cost=(
                        Decimal(str(source_batch.cost_price))
                        if source_batch.cost_price is not None
                        else None
                    ),
                    unit_price=(
                        Decimal(str(source_batch.selling_price))
                        if source_batch.selling_price is not None
                        else None
                    ),
                    source_type="stock_adjustment",
                    source_id=source_adjustment.id,
                    source_line_id=dest_adjustment.id,
                    reference_number=source_batch.batch_number,
                    reason=reason,
                    created_by=transferred_by,
                    context_metadata={"to_branch_id": str(to_branch_id)},
                )
                await InventoryService._record_inventory_movement(
                    db=db,
                    organization_id=dest_org_id,
                    branch_id=to_branch_id,
                    drug_id=drug_id,
                    movement_type="transfer_in",
                    quantity_change=take,
                    quantity_before=dest_before,
                    quantity_after=running_dest_qty,
                    batch_id=dest_batch.id,
                    batch_quantity_before=dest_batch_before,
                    batch_quantity_after=dest_batch.remaining_quantity,
                    unit_cost=(
                        Decimal(str(dest_batch.cost_price))
                        if dest_batch.cost_price is not None
                        else None
                    ),
                    unit_price=(
                        Decimal(str(dest_batch.selling_price))
                        if dest_batch.selling_price is not None
                        else None
                    ),
                    source_type="stock_adjustment",
                    source_id=dest_adjustment.id,
                    source_line_id=source_adjustment.id,
                    reference_number=dest_batch.batch_number,
                    reason=reason,
                    created_by=transferred_by,
                    context_metadata={"from_branch_id": str(from_branch_id)},
                )

                qty_to_move -= take

            if qty_to_move > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Insufficient valid batch stock for transfer. "
                        f"Unable to allocate {qty_to_move} unit(s)."
                    ),
                )

            source_inventory.quantity = source_new
            source_inventory.updated_at = datetime.now(timezone.utc)
            source_inventory.mark_as_pending_sync()

            dest_inventory.quantity = dest_new
            dest_inventory.updated_at = datetime.now(timezone.utc)
            dest_inventory.mark_as_pending_sync()

            await InventoryService._resolve_inventory_alerts(
                db, to_branch_id, drug_id, dest_new
            )

        # Both sides succeeded — commit once
        await db.commit()
        await db.refresh(source_adjustment)
        await db.refresh(dest_adjustment)
        return source_adjustment, dest_adjustment

    @staticmethod
    async def reserve_inventory(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        quantity: int,
    ) -> BranchInventory:
        """
        Reserve stock for a pending order or prescription.

        Acquires a row lock and validates that sufficient unreserved stock
        exists before incrementing ``reserved_quantity``.

        Raises:
            HTTPException(400): Insufficient available (unreserved) stock.
            HTTPException(404): No inventory record at this branch.
        """
        result = await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id   == drug_id,
            )
            .with_for_update()
        )
        inventory = result.scalar_one_or_none()
        if not inventory:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inventory record not found.",
            )

        available = inventory.quantity - inventory.reserved_quantity
        if available < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Insufficient available stock. "
                    f"Available: {available}, Requested: {quantity}."
                ),
            )

        inventory.reserved_quantity += quantity
        inventory.updated_at = datetime.now(timezone.utc)
        inventory.mark_as_pending_sync()

        await db.commit()
        await db.refresh(inventory)
        return inventory

    @staticmethod
    async def release_reserved_inventory(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        quantity: int,
    ) -> BranchInventory:
        """
        Release previously reserved stock (e.g. order cancelled).

        Raises:
            HTTPException(400): Releasing more than is currently reserved.
            HTTPException(404): No inventory record at this branch.
        """
        result = await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id   == drug_id,
            )
            .with_for_update()
        )
        inventory = result.scalar_one_or_none()
        if not inventory:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inventory record not found.",
            )

        if inventory.reserved_quantity < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot release {quantity} units — only "
                    f"{inventory.reserved_quantity} are reserved."
                ),
            )

        inventory.reserved_quantity -= quantity
        inventory.updated_at = datetime.now(timezone.utc)
        inventory.mark_as_pending_sync()

        await db.commit()
        await db.refresh(inventory)
        return inventory

    # =========================================================================
    # BATCH MANAGEMENT
    # =========================================================================

    @staticmethod
    async def create_batch(
        db: AsyncSession,
        batch_data: DrugBatchCreate,
        created_by: Optional[uuid.UUID] = None,
    ) -> DrugBatch:
        """
        Create a drug batch and **add** its quantity to ``BranchInventory``.

        This is an additive goods-receipt operation.  If an inventory record
        already exists, the incoming quantity is added to it.  If not, a new
        record is created.

        The batch creation and inventory update are committed together in a
        single savepoint.

        Raises:
            HTTPException(400): A batch with the same number already exists
                                for this drug at this branch.
        """
        # Duplicate batch check
        result = await db.execute(
            select(DrugBatch).where(
                DrugBatch.branch_id    == batch_data.branch_id,
                DrugBatch.drug_id      == batch_data.drug_id,
                DrugBatch.batch_number == batch_data.batch_number,
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Batch '{batch_data.batch_number}' already exists "
                    "for this drug at this branch."
                ),
            )

        async with db.begin_nested():
            # Create the batch
            batch = DrugBatch(
                id=uuid.uuid4(),
                **batch_data.model_dump(),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            batch.mark_as_pending_sync()
            db.add(batch)
            await db.flush()  # get batch.id before touching inventory

            # ADD incoming quantity to BranchInventory (never overwrite)
            inv_res = await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == batch_data.branch_id,
                    BranchInventory.drug_id == batch_data.drug_id,
                )
                .with_for_update()
            )
            inventory = inv_res.scalar_one_or_none()

            previous_quantity = inventory.quantity if inventory else 0

            if not inventory:
                inventory = BranchInventory(
                    id=uuid.uuid4(),
                    branch_id=batch_data.branch_id,
                    drug_id=batch_data.drug_id,
                    quantity=0,
                    reserved_quantity=0,
                    selling_price=batch_data.selling_price,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                inventory.mark_as_pending_sync()
                db.add(inventory)

            # Recalculate total from all batches to resolve drift
            new_quantity = await InventoryService._recalculate_inventory_quantity(
                db=db,
                branch_id=batch_data.branch_id,
                drug_id=batch_data.drug_id,
            )

            if inventory.selling_price is None and batch_data.selling_price is not None:
                inventory.selling_price = batch_data.selling_price

            # Record audit trail for the purchase receipt
            # Use a dummy system user ID if no specific user is associated with PO receipt
            # Or assume the caller will handle it if we want to be strict.
            # For now, we try to get the 'ordered_by' from PO if available.
            adjusted_by = created_by or uuid.UUID(int=0)  # Default system UUID
            # Try to find who received it if PO exists
            if batch_data.purchase_order_id:
                from app.models.sales.sales_model import PurchaseOrder

                po = await db.get(PurchaseOrder, batch_data.purchase_order_id)
                if po and po.ordered_by:
                    adjusted_by = po.ordered_by

            adjustment = StockAdjustment(
                id=uuid.uuid4(),
                branch_id=batch_data.branch_id,
                drug_id=batch_data.drug_id,
                adjustment_type="purchase_receipt",
                quantity_change=batch_data.quantity,
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                reason=f"Purchase receipt: Batch {batch_data.batch_number}",
                adjusted_by=adjusted_by,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(adjustment)
            await db.flush()

            await InventoryService._record_inventory_movement(
                db=db,
                branch_id=batch_data.branch_id,
                drug_id=batch_data.drug_id,
                movement_type="purchase_receipt",
                quantity_change=batch_data.quantity,
                quantity_before=previous_quantity,
                quantity_after=new_quantity,
                batch_id=batch.id,
                batch_quantity_before=0,
                batch_quantity_after=batch.remaining_quantity,
                unit_cost=(
                    Decimal(str(batch_data.cost_price))
                    if batch_data.cost_price is not None
                    else None
                ),
                unit_price=(
                    Decimal(str(batch_data.selling_price))
                    if batch_data.selling_price is not None
                    else None
                ),
                source_type=(
                    "purchase_order" if batch_data.purchase_order_id else "batch"
                ),
                source_id=batch_data.purchase_order_id or batch.id,
                source_line_id=batch.id,
                reference_number=batch_data.batch_number,
                reason=f"Purchase receipt: Batch {batch_data.batch_number}",
                created_by=adjusted_by if adjusted_by.int != 0 else None,
            )

        await db.commit()
        await db.refresh(batch)
        return batch

    @staticmethod
    async def update_batch(
        db: AsyncSession,
        batch_id: uuid.UUID,
        batch_data: DrugBatchUpdate,
    ) -> DrugBatch:
        """
        Update drug batch fields.

        Handles corrections for batch_number, remaining_quantity, dates,
        pricing, and supplier. Synchronises BranchInventory total quantity
        by summing all batches to ensure accuracy and resolve drift.

        Raises:
            HTTPException(404): Batch not found.
            HTTPException(400): Duplicate batch number if changed.
        """
        # Lock the batch for update to prevent concurrent modifications
        result = await db.execute(
            select(DrugBatch)
            .where(DrugBatch.id == batch_id)
            .with_for_update()
        )
        batch = result.scalar_one_or_none()

        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch not found.",
            )

        old_remaining_quantity = batch.remaining_quantity
        old_inventory_quantity = await db.scalar(
            select(BranchInventory.quantity).where(
                BranchInventory.branch_id == batch.branch_id,
                BranchInventory.drug_id == batch.drug_id,
            )
        )
        old_inventory_quantity = int(old_inventory_quantity or 0)

        update_data = batch_data.model_dump(exclude_unset=True)

        # ── Pre-flight: Check remaining_quantity <= quantity ─────────────────
        final_rem = update_data.get("remaining_quantity", batch.remaining_quantity)
        final_total = update_data.get("quantity", batch.quantity)
        if final_rem > final_total:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Remaining quantity ({final_rem}) cannot exceed initial total quantity ({final_total}).",
            )

        # ── Pre-flight: Check uniqueness if batch_number is being changed ────
        new_batch_num = update_data.get("batch_number")
        if new_batch_num and new_batch_num != batch.batch_number:
            dupe_res = await db.execute(
                select(DrugBatch.id).where(
                    DrugBatch.branch_id == batch.branch_id,
                    DrugBatch.drug_id == batch.drug_id,
                    DrugBatch.batch_number == new_batch_num,
                    DrugBatch.id != batch_id,
                )
            )
            if dupe_res.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Batch '{new_batch_num}' already exists for this drug at this branch.",
                )

        async with db.begin_nested():
            # Update batch fields
            for field, value in update_data.items():
                setattr(batch, field, value)

            batch.updated_at = datetime.now(timezone.utc)
            batch.mark_as_pending_sync()

            # Flush changes so the subsequent sum query sees the updated remaining_quantity
            await db.flush()

            # ── Sync with BranchInventory ──
            # We re-calculate the total quantity from the sum of all batches
            # to resolve any discrepancies/drift between batch counts and the inventory total.
            new_inventory_quantity = await InventoryService._recalculate_inventory_quantity(
                db=db,
                branch_id=batch.branch_id,
                drug_id=batch.drug_id,
            )

            # Resolve reference to update other fields if needed
            inv_res = await db.execute(
                select(BranchInventory).where(
                    BranchInventory.branch_id == batch.branch_id,
                    BranchInventory.drug_id == batch.drug_id,
                ).with_for_update()
            )
            inventory = inv_res.scalar_one()

            if "selling_price" in update_data and update_data["selling_price"] is not None:
                inventory.selling_price = update_data["selling_price"]
                inventory.updated_at = datetime.now(timezone.utc)
                inventory.mark_as_pending_sync()

            quantity_change = batch.remaining_quantity - old_remaining_quantity
            if quantity_change != 0:
                await InventoryService._record_inventory_movement(
                    db=db,
                    branch_id=batch.branch_id,
                    drug_id=batch.drug_id,
                    movement_type="correction",
                    quantity_change=quantity_change,
                    quantity_before=old_inventory_quantity,
                    quantity_after=new_inventory_quantity,
                    batch_id=batch.id,
                    batch_quantity_before=old_remaining_quantity,
                    batch_quantity_after=batch.remaining_quantity,
                    unit_cost=(
                        Decimal(str(batch.cost_price))
                        if batch.cost_price is not None
                        else None
                    ),
                    unit_price=(
                        Decimal(str(batch.selling_price))
                        if batch.selling_price is not None
                        else None
                    ),
                    source_type="batch",
                    source_id=batch.id,
                    reference_number=batch.batch_number,
                    reason="Batch quantity correction",
                )

        await db.commit()
        await db.refresh(batch)
        return batch

    @staticmethod
    async def consume_from_batch(
        db: AsyncSession,
        batch_id: uuid.UUID,
        quantity: int,
    ) -> DrugBatch:
        """
        Consume stock from a specific batch, keeping ``BranchInventory`` in sync.

        Both ``DrugBatch.remaining_quantity`` and ``BranchInventory.quantity``
        are decremented atomically in the same savepoint.

        Raises:
            HTTPException(400): Batch has insufficient remaining quantity.
            HTTPException(404): Batch not found or no inventory record.
        """
        async with db.begin_nested():
            batch_res = await db.execute(
                select(DrugBatch)
                .where(DrugBatch.id == batch_id)
                .with_for_update()
            )
            batch = batch_res.scalar_one_or_none()
            if not batch:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Batch not found.",
                )

            if batch.expiry_date <= date.today():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot consume from an expired batch.",
                )

            if batch.remaining_quantity < quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient batch quantity. "
                        f"Available: {batch.remaining_quantity}, "
                        f"Requested: {quantity}."
                    ),
                )

            previous_batch_quantity = batch.remaining_quantity
            batch.remaining_quantity -= quantity
            batch.updated_at          = datetime.now(timezone.utc)
            batch.mark_as_pending_sync()

            # Keep BranchInventory in sync
            inv_res = await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == batch.branch_id,
                    BranchInventory.drug_id   == batch.drug_id,
                )
                .with_for_update()
            )
            inventory = inv_res.scalar_one_or_none()
            if not inventory:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "BranchInventory record missing for this drug. "
                        "Data integrity issue — contact an administrator."
                    ),
                )

            previous_inventory_quantity = inventory.quantity

            # Keep BranchInventory in sync via recalculation to prevent drift
            new_inventory_quantity = await InventoryService._recalculate_inventory_quantity(
                db=db,
                branch_id=batch.branch_id,
                drug_id=batch.drug_id,
            )

            await InventoryService._record_inventory_movement(
                db=db,
                branch_id=batch.branch_id,
                drug_id=batch.drug_id,
                movement_type="batch_consume",
                quantity_change=-quantity,
                quantity_before=previous_inventory_quantity,
                quantity_after=new_inventory_quantity,
                batch_id=batch.id,
                batch_quantity_before=previous_batch_quantity,
                batch_quantity_after=batch.remaining_quantity,
                unit_cost=(
                    Decimal(str(batch.cost_price))
                    if batch.cost_price is not None
                    else None
                ),
                unit_price=(
                    Decimal(str(batch.selling_price))
                    if batch.selling_price is not None
                    else None
                ),
                source_type="batch",
                source_id=batch.id,
                reference_number=batch.batch_number,
                reason="Direct batch consumption",
            )

        await db.commit()
        await db.refresh(batch)
        return batch

    @staticmethod
    async def get_batches_for_drug(
        db: AsyncSession,
        drug_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        include_expired: bool = False,
        include_empty: bool = False,
    ) -> List[DrugBatch]:
        """
        Return batches for a drug (non-paginated, for internal use).

        Defaults to non-expired (``expiry_date > today``), non-empty batches
        ordered FEFO (earliest expiry first).
        """
        query = select(DrugBatch).where(DrugBatch.drug_id == drug_id)

        if branch_id:
            query = query.where(DrugBatch.branch_id == branch_id)

        if not include_expired:
            # Strictly future — consistent with the sales service
            query = query.where(DrugBatch.expiry_date > date.today())

        if not include_empty:
            query = query.where(DrugBatch.remaining_quantity > 0)

        query = query.order_by(DrugBatch.expiry_date, DrugBatch.created_at)

        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_batches_paginated(
        db: AsyncSession,
        drug_id: uuid.UUID,
        pagination: PaginationParams,
        branch_id: Optional[uuid.UUID] = None,
        branch_ids: Optional[List[uuid.UUID]] = None,
        include_expired: bool = False,
        include_empty: bool = False,
        expiring_within_days: Optional[int] = None,
    ) -> PaginatedResponse[DrugBatch]:
        """
        Paginated batch listing for a drug, ordered FEFO.

        ``expiring_within_days`` filters to batches expiring within N days
        from today (exclusive of already-expired).
        """
        from app.utils.pagination import Paginator

        query       = select(DrugBatch).where(DrugBatch.drug_id == drug_id)
        count_query = (
            select(func.count()).select_from(DrugBatch).where(DrugBatch.drug_id == drug_id)
        )

        if branch_id:
            query       = query.where(DrugBatch.branch_id == branch_id)
            count_query = count_query.where(DrugBatch.branch_id == branch_id)
        elif branch_ids:
            query       = query.where(DrugBatch.branch_id.in_(branch_ids))
            count_query = count_query.where(DrugBatch.branch_id.in_(branch_ids))

        if not include_expired:
            query       = query.where(DrugBatch.expiry_date > date.today())
            count_query = count_query.where(DrugBatch.expiry_date > date.today())

        if not include_empty:
            query       = query.where(DrugBatch.remaining_quantity > 0)
            count_query = count_query.where(DrugBatch.remaining_quantity > 0)

        if expiring_within_days is not None:
            expiry_threshold = date.today() + timedelta(days=expiring_within_days)
            window_cond = and_(
                DrugBatch.expiry_date > date.today(),
                DrugBatch.expiry_date <= expiry_threshold,
            )
            query       = query.where(window_cond)
            count_query = count_query.where(window_cond)

        query = query.order_by(DrugBatch.expiry_date, DrugBatch.created_at)

        paginator = Paginator(db)
        return await paginator.paginate_raw_query(
            query=query,
            count_query=count_query,
            params=pagination,
            schema=DrugBatchResponse,
        )

    # =========================================================================
    # REPORTS
    # =========================================================================

    @staticmethod
    async def get_low_stock_report(
        db: AsyncSession,
        organization_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        branch_ids: Optional[List[uuid.UUID]] = None,
    ) -> LowStockReport:
        """
        Generate a low-stock / out-of-stock report for an organisation.

        Includes every active drug whose ``BranchInventory.quantity`` is at
        or below its ``Drug.reorder_level``.
        """
        from app.models.pharmacy.pharmacy_model import Branch

        query = (
            select(
                Drug.id.label("drug_id"),
                Drug.name.label("drug_name"),
                Drug.sku,
                Drug.reorder_level,
                Drug.reorder_quantity,
                BranchInventory.branch_id,
                Branch.name.label("branch_name"),
                BranchInventory.quantity,
                BranchInventory.reserved_quantity,
            )
            .join(BranchInventory, Drug.id == BranchInventory.drug_id)
            .join(Branch, BranchInventory.branch_id == Branch.id)
            .where(
                Drug.organization_id == organization_id,
                Drug.is_active       == True,
                Drug.is_deleted      == False,
                (BranchInventory.quantity - BranchInventory.reserved_quantity) <= Drug.reorder_level,
            )
        )

        if branch_id:
            query = query.where(BranchInventory.branch_id == branch_id)
        elif branch_ids:
            query = query.where(BranchInventory.branch_id.in_(branch_ids))

        result = await db.execute(query)
        rows   = result.all()

        items             = []
        out_of_stock_count = 0
        low_stock_count    = 0

        for row in rows:
            available = row.quantity - (row.reserved_quantity or 0)
            item_status = "out_of_stock" if available == 0 else "low_stock"
            if item_status == "out_of_stock":
                out_of_stock_count += 1
            else:
                low_stock_count += 1

            items.append(
                LowStockItem(
                    drug_id=row.drug_id,
                    drug_name=row.drug_name,
                    sku=row.sku,
                    branch_id=row.branch_id,
                    branch_name=row.branch_name,
                    quantity=available,
                    reorder_level=row.reorder_level,
                    reorder_quantity=row.reorder_quantity,
                    status=item_status,
                    recommended_order_quantity=row.reorder_quantity,
                )
            )

        return LowStockReport(
            organization_id=organization_id,
            branch_id=branch_id,
            report_date=datetime.now(timezone.utc),
            items=items,
            total_items=len(items),
            out_of_stock_count=out_of_stock_count,
            low_stock_count=low_stock_count,
        )

    @staticmethod
    async def get_expiring_batches_report(
        db: AsyncSession,
        organization_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        days_threshold: int = 90,
    ) -> ExpiringBatchReport:
        """
        Return all non-empty batches expiring within ``days_threshold`` days.

        Uses ``>= date.today()`` (inclusive) so items expiring today appear
        in the report.  Ordered by earliest expiry first.
        """
        from app.models.pharmacy.pharmacy_model import Branch

        threshold_date = date.today() + timedelta(days=days_threshold)

        query = (
            select(
                DrugBatch.id.label("batch_id"),
                DrugBatch.batch_number,
                DrugBatch.remaining_quantity,
                DrugBatch.expiry_date,
                DrugBatch.cost_price,
                DrugBatch.selling_price,
                Drug.id.label("drug_id"),
                Drug.name.label("drug_name"),
                Branch.id.label("branch_id"),
                Branch.name.label("branch_name"),
            )
            .join(Drug,   DrugBatch.drug_id   == Drug.id)
            .join(Branch, DrugBatch.branch_id == Branch.id)
            .where(
                Drug.organization_id          == organization_id,
                DrugBatch.remaining_quantity  > 0,
                DrugBatch.expiry_date         >= date.today(),   # include today
                DrugBatch.expiry_date         <= threshold_date,
            )
        )

        if branch_id:
            query = query.where(DrugBatch.branch_id == branch_id)

        query = query.order_by(DrugBatch.expiry_date)

        result = await db.execute(query)
        rows   = result.all()

        items               = []
        total_quantity      = 0
        total_cost_value    = Decimal("0")
        total_selling_value = Decimal("0")

        for row in rows:
            days_until    = (row.expiry_date - date.today()).days
            cost_value    = (Decimal(str(row.cost_price))    if row.cost_price    else Decimal("0")) * row.remaining_quantity
            selling_value = (Decimal(str(row.selling_price)) if row.selling_price else Decimal("0")) * row.remaining_quantity

            items.append(
                ExpiringBatchItem(
                    batch_id=row.batch_id,
                    drug_id=row.drug_id,
                    drug_name=row.drug_name,
                    batch_number=row.batch_number,
                    branch_id=row.branch_id,
                    branch_name=row.branch_name,
                    remaining_quantity=row.remaining_quantity,
                    expiry_date=row.expiry_date,
                    days_until_expiry=days_until,
                    cost_value=cost_value,
                    selling_value=selling_value,
                )
            )

            total_quantity      += row.remaining_quantity
            total_cost_value    += cost_value
            total_selling_value += selling_value

        return ExpiringBatchReport(
            organization_id=organization_id,
            branch_id=branch_id,
            report_date=datetime.now(timezone.utc),
            days_threshold=days_threshold,
            items=items,
            total_items=len(items),
            total_quantity=total_quantity,
            total_cost_value=total_cost_value,
            total_selling_value=total_selling_value,
        )

    @staticmethod
    async def get_inventory_valuation(
        db: AsyncSession,
        branch_id: uuid.UUID,
    ) -> InventoryValuationResponse:
        """
        Calculate the cost and selling value of all stock at a branch.

        Uses ``Drug.cost_price`` and the branch-effective selling price as the
        per-unit values for the aggregate valuation. The effective selling
        price is ``BranchInventory.selling_price`` when set, otherwise the
        earliest valid batch ``DrugBatch.selling_price``, otherwise the
        catalogue ``Drug.unit_price``.

        Raises:
            HTTPException(404): Branch not found.
        """
        from app.models.pharmacy.pharmacy_model import Branch

        branch_res = await db.execute(
            select(Branch).where(Branch.id == branch_id)
        )
        branch = branch_res.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found.",
            )

        query = (
            select(
                Drug.id.label("drug_id"),
                Drug.name.label("drug_name"),
                Drug.sku,
                Drug.cost_price,
                Drug.unit_price,
                BranchInventory.selling_price.label("branch_selling_price"),
                BranchInventory.quantity,
            )
            .join(BranchInventory, Drug.id == BranchInventory.drug_id)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.quantity  > 0,
            )
        )

        result = await db.execute(query)
        rows   = result.all()
        batch_selling_prices = await InventoryService._get_fefo_batch_selling_prices(
            db=db,
            branch_id=branch_id,
            drug_ids=[row.drug_id for row in rows],
        )

        items               = []
        total_quantity      = 0
        total_cost_value    = Decimal("0")
        total_selling_value = Decimal("0")

        for row in rows:
            cost_price    = Decimal(str(row.cost_price))   if row.cost_price  else Decimal("0")
            branch_price = (
                Decimal(str(row.branch_selling_price))
                if row.branch_selling_price is not None
                else None
            )
            batch_price = batch_selling_prices.get(row.drug_id)
            catalog_price = Decimal(str(row.unit_price)) if row.unit_price else Decimal("0")
            selling_price = InventoryService._resolve_effective_selling_price(
                catalog_unit_price=catalog_price,
                branch_selling_price=branch_price,
                batch_selling_price=batch_price,
            )
            total_cost    = cost_price    * row.quantity
            total_selling = selling_price * row.quantity

            items.append(
                InventoryValuationItem(
                    drug_id=row.drug_id,
                    drug_name=row.drug_name,
                    sku=row.sku,
                    quantity=row.quantity,
                    cost_price=cost_price,
                    selling_price=selling_price,
                    branch_selling_price=branch_price,
                    total_cost_value=total_cost,
                    total_selling_value=total_selling,
                    potential_profit=total_selling - total_cost,
                )
            )

            total_quantity      += row.quantity
            total_cost_value    += total_cost
            total_selling_value += total_selling

        total_potential_profit = total_selling_value - total_cost_value
        profit_margin = (
            total_potential_profit / total_cost_value * 100
            if total_cost_value > 0
            else Decimal("0")
        )

        return InventoryValuationResponse(
            branch_id=branch_id,
            branch_name=branch.name,
            valuation_date=datetime.now(timezone.utc),
            items=items,
            total_items=len(items),
            total_quantity=total_quantity,
            total_cost_value=total_cost_value,
            total_selling_value=total_selling_value,
            total_potential_profit=total_potential_profit,
            profit_margin_percentage=profit_margin,
        )

    # =========================================================================
    # PRIVATE HELPERS
    # =========================================================================

    @staticmethod
    async def _resolve_inventory_alerts(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        current_quantity: int,
    ) -> None:
        """
        Resolve any unresolved low_stock or out_of_stock alerts if the
        current quantity has risen above the drug's reorder level.
        """
        from app.models.system_md.sys_models import SystemAlert

        # Fetch drug reorder level
        reorder_level = await db.scalar(
            select(Drug.reorder_level).where(Drug.id == drug_id)
        )
        if reorder_level is None:
            return

        if current_quantity > reorder_level:
            await db.execute(
                update(SystemAlert)
                .where(
                    SystemAlert.branch_id == branch_id,
                    SystemAlert.drug_id == drug_id,
                    SystemAlert.alert_type.in_(["low_stock", "out_of_stock"]),
                    SystemAlert.is_resolved == False,
                )
                .values(
                    is_resolved=True,
                    resolved_at=datetime.now(timezone.utc),
                    resolution_notes=f"Stock replenished to {current_quantity} (Reorder level: {reorder_level})"
                )
            )

    @staticmethod
    async def _recalculate_inventory_quantity(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
    ) -> int:
        """
        Recalculate the aggregate BranchInventory.quantity from the sum of all
        batches for this drug at this branch. Resolves drift between batch
        records and the inventory summary.
        """
        total_qty = await db.scalar(
            select(func.sum(DrugBatch.remaining_quantity)).where(
                DrugBatch.branch_id == branch_id,
                DrugBatch.drug_id == drug_id,
            )
        )
        new_qty = int(total_qty or 0)

        # Update the inventory record
        inv_res = await db.execute(
            select(BranchInventory).where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id == drug_id,
            ).with_for_update()
        )
        inventory = inv_res.scalar_one_or_none()
        if inventory:
            inventory.quantity = new_qty
            inventory.updated_at = datetime.now(timezone.utc)
            inventory.mark_as_pending_sync()

            # Resolve any existing alerts if the new quantity is healthy
            await InventoryService._resolve_inventory_alerts(db, branch_id, drug_id, new_qty)

        return new_qty

    @staticmethod
    async def _apply_adjustment(
        db: AsyncSession,
        branch_id: uuid.UUID,
        drug_id: uuid.UUID,
        quantity_change: int,
        adjustment_type: str,
        reason: str,
        adjusted_by: uuid.UUID,
        transfer_to_branch_id: Optional[uuid.UUID] = None,
    ) -> Tuple[StockAdjustment, BranchInventory]:
        """
        Internal write helper: apply a quantity delta and record the audit row.

        Intentionally does NOT commit.  All callers (``adjust_inventory``,
        ``transfer_stock``) manage their own transaction boundaries.

        Acquires ``SELECT ... FOR UPDATE`` on the inventory row to prevent
        concurrent lost-update races.  Raises ``HTTPException(404/400)`` on
        validation failure so the caller's savepoint rolls back cleanly.
        """
        result = await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id   == drug_id,
            )
            .with_for_update()
        )
        inventory = result.scalar_one_or_none()
        if not inventory:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inventory record not found for this drug at this branch.",
            )

        previous_inventory_quantity = inventory.quantity
        new_quantity = inventory.quantity + quantity_change
        if new_quantity < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Adjustment would result in negative stock. "
                    f"Current: {inventory.quantity}, "
                    f"Change: {quantity_change}, "
                    f"Result: {new_quantity}."
                ),
            )

        adjustment = StockAdjustment(
            id=uuid.uuid4(),
            branch_id=branch_id,
            drug_id=drug_id,
            adjustment_type=adjustment_type,
            quantity_change=quantity_change,
            previous_quantity=inventory.quantity,
            new_quantity=new_quantity,
            reason=reason,
            adjusted_by=adjusted_by,
            transfer_to_branch_id=transfer_to_branch_id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(adjustment)

        inventory.quantity   = new_quantity
        inventory.updated_at = datetime.now(timezone.utc)
        inventory.mark_as_pending_sync()

        # ── Update batches to maintain parity ──
        if quantity_change > 0:
            # For additions (return/correction), add to the earliest valid batch
            # if one exists, or create a dummy "System Adjustment" batch if not.
            batch_res = await db.execute(
                select(DrugBatch)
                .where(
                    DrugBatch.branch_id == branch_id,
                    DrugBatch.drug_id == drug_id,
                    DrugBatch.expiry_date > date.today(),
                )
                .order_by(DrugBatch.expiry_date.asc())
                .limit(1)
                .with_for_update()
            )
            target_batch = batch_res.scalar_one_or_none()
            if target_batch:
                previous_batch_quantity = target_batch.remaining_quantity
                target_batch.remaining_quantity += quantity_change
                # Prevent IntegrityError: ensure initial quantity tracks discovered stock
                if target_batch.remaining_quantity > target_batch.quantity:
                    target_batch.quantity = target_batch.remaining_quantity

                target_batch.updated_at = datetime.now(timezone.utc)
                target_batch.mark_as_pending_sync()
            else:
                # No active batch found - create a system adjustment batch to prevent data loss
                # during recalculation.
                previous_batch_quantity = 0
                target_batch = DrugBatch(
                    id=uuid.uuid4(),
                    branch_id=branch_id,
                    drug_id=drug_id,
                    batch_number=f"ADJ-{datetime.now().strftime('%Y%m%d')}",
                    quantity=quantity_change,
                    remaining_quantity=quantity_change,
                    expiry_date=date.today() + timedelta(days=365 * 10),  # 10 years
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                target_batch.mark_as_pending_sync()
                db.add(target_batch)

            movement_type = (
                "transfer_in"
                if adjustment_type == "transfer"
                else adjustment_type
            )
            await InventoryService._record_inventory_movement(
                db=db,
                branch_id=branch_id,
                drug_id=drug_id,
                movement_type=movement_type,
                quantity_change=quantity_change,
                quantity_before=previous_inventory_quantity,
                quantity_after=new_quantity,
                batch_id=target_batch.id,
                batch_quantity_before=previous_batch_quantity,
                batch_quantity_after=target_batch.remaining_quantity,
                unit_cost=(
                    Decimal(str(target_batch.cost_price))
                    if target_batch.cost_price is not None
                    else None
                ),
                unit_price=(
                    Decimal(str(target_batch.selling_price))
                    if target_batch.selling_price is not None
                    else None
                ),
                source_type="stock_adjustment",
                source_id=adjustment.id,
                reference_number=target_batch.batch_number,
                reason=reason,
                created_by=adjusted_by,
            )

        elif quantity_change < 0:
            # For deductions (damage/theft/correction), deduct FEFO
            # For expired write-offs, deduct from already-expired batches
            qty_to_deduct = abs(quantity_change)
            running_inventory_quantity = previous_inventory_quantity
            expiry_condition = (
                DrugBatch.expiry_date <= date.today()
                if adjustment_type == "expired"
                else DrugBatch.expiry_date > date.today()
            )
            batch_res = await db.execute(
                select(DrugBatch)
                .where(
                    DrugBatch.branch_id == branch_id,
                    DrugBatch.drug_id == drug_id,
                    DrugBatch.remaining_quantity > 0,
                    expiry_condition,
                )
                .order_by(DrugBatch.expiry_date.asc())
                .with_for_update()
            )
            for batch in batch_res.scalars().all():
                if qty_to_deduct <= 0:
                    break
                take = min(batch.remaining_quantity, qty_to_deduct)
                previous_batch_quantity = batch.remaining_quantity
                movement_quantity_before = running_inventory_quantity
                running_inventory_quantity -= take
                batch.remaining_quantity -= take
                batch.updated_at = datetime.now(timezone.utc)
                batch.mark_as_pending_sync()
                qty_to_deduct -= take

                movement_type = (
                    "transfer_out"
                    if adjustment_type == "transfer"
                    else adjustment_type
                )
                await InventoryService._record_inventory_movement(
                    db=db,
                    branch_id=branch_id,
                    drug_id=drug_id,
                    movement_type=movement_type,
                    quantity_change=-take,
                    quantity_before=movement_quantity_before,
                    quantity_after=running_inventory_quantity,
                    batch_id=batch.id,
                    batch_quantity_before=previous_batch_quantity,
                    batch_quantity_after=batch.remaining_quantity,
                    unit_cost=(
                        Decimal(str(batch.cost_price))
                        if batch.cost_price is not None
                        else None
                    ),
                    unit_price=(
                        Decimal(str(batch.selling_price))
                        if batch.selling_price is not None
                        else None
                    ),
                    source_type="stock_adjustment",
                    source_id=adjustment.id,
                    reference_number=batch.batch_number,
                    reason=reason,
                    created_by=adjusted_by,
                )

            if qty_to_deduct > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient eligible batch stock for adjustment. "
                        f"Unable to allocate {qty_to_deduct} unit(s) from batches."
                    ),
                )

        # ── Final parity check ──
        # Ensure aggregate inventory matches the new batch totals exactly
        await InventoryService._recalculate_inventory_quantity(db, branch_id, drug_id)

        await db.flush()
        return adjustment, inventory
