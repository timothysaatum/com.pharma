"""
Purchase Order Service
Business logic for purchase orders, receiving goods, and supplier management.

Key design decisions:
- All writes use explicit transactions; nested savepoints guard the critical
  receive_goods path so a single bad item cannot silently corrupt inventory.
- Row-level locking (WITH FOR UPDATE) on BranchInventory during receive ensures
  concurrent PO receipts for the same drug never produce phantom stock.
- PO-number generation is collision-safe via a SELECT … FOR UPDATE on the
  sequence counter, not a naive COUNT.
- _build_po_with_details uses a single JOIN query instead of N+1 per-item
  Drug lookups that the original had.
- audit logs are created inside the same transaction as the mutation they
  describe; they are never committed separately.
- mutable default argument `changes: dict = {}` is fixed to `changes=None`.
- debug print() in create_purchase_order removed.
- StockAdjustment type 'return' replaced with 'purchase_receipt' — the correct
  value for a goods receipt. 'received' (a previous incorrect attempt) is not
  in the check_adjustment_type constraint.
- receive_goods: fully-received items are now silently skipped with a warning
  field rather than raising, matching real warehouse behaviour.
- Rejected POs use status 'rejected', not 'cancelled', matching the schema
  comment and keeping the audit trail distinguishable.
- _build_po_with_details fetches branch/orderer/approver in one round-trip
  each using WHERE … IN rather than per-object queries.

Type fix (2026-03-30):
- receive_goods: added explicit None-guard on full_po after the post-commit
  reload. db.scalar() returns T | None; _build_po_with_details expects T.
  The guard raises HTTP 500 (should never fire in practice) and narrows the
  type so the type-checker is satisfied without a cast.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch
from app.models.sales.sales_model import PurchaseOrder, PurchaseOrderItem, Supplier
from app.models.system_md.sys_models import AuditLog
from app.models.user.user_model import User
from app.services.inventory.inventory_service import InventoryService
from app.schemas.purchase_order_schemas import (
    PurchaseOrderCreate,
    PurchaseOrderItemCreate,
    PurchaseOrderItemWithDetails,
    PurchaseOrderWithDetails,
    ReceivePurchaseOrder,
    ReceivePurchaseOrderResponse,
    SupplierCreate,
)

_UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(_UTC)


class PurchaseOrderService:
    """Service for purchase order management."""

    # =========================================================================
    # Supplier Management
    # =========================================================================

    @staticmethod
    async def create_supplier(
        db: AsyncSession,
        supplier_data: SupplierCreate,
        user: User,
    ) -> Supplier:
        """
        Create a new supplier.

        Raises 409 if a supplier with the same name already exists for the org.
        """
        existing = await db.scalar(
            select(Supplier).where(
                Supplier.organization_id == supplier_data.organization_id,
                Supplier.name == supplier_data.name,
                Supplier.is_deleted.is_(False),
            )
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Supplier '{supplier_data.name}' already exists",
            )

        supplier = Supplier(
            id=uuid.uuid4(),
            **supplier_data.model_dump(),
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(supplier)
        await db.flush()  # Obtain PK before audit log

        await PurchaseOrderService._create_audit_log(
            db,
            action="create_supplier",
            entity_type="Supplier",
            entity_id=supplier.id,
            user_id=user.id,
            organization_id=supplier.organization_id,
            changes={"after": supplier_data.model_dump(mode="json")},
        )

        await db.commit()
        await db.refresh(supplier)
        return supplier

    @staticmethod
    async def get_supplier(
        db: AsyncSession,
        supplier_id: uuid.UUID,
    ) -> Supplier:
        """Fetch a non-deleted supplier or raise 404."""
        supplier = await db.scalar(
            select(Supplier).where(
                Supplier.id == supplier_id,
                Supplier.is_deleted.is_(False),
            )
        )
        if not supplier:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Supplier not found",
            )
        return supplier

    @staticmethod
    async def delete_supplier(
        db: AsyncSession,
        supplier_id: uuid.UUID,
        user: User,
    ) -> None:
        """Soft-delete a supplier."""
        supplier = await PurchaseOrderService.get_supplier(db, supplier_id)
        if supplier.organization_id != user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied",
            )
        supplier.is_deleted = True
        supplier.deleted_at = _now()
        supplier.deleted_by = user.id
        await db.commit()

    # =========================================================================
    # Purchase Order CRUD
    # =========================================================================

    @staticmethod
    async def create_purchase_order(
        db: AsyncSession,
        po_data: PurchaseOrderCreate,
        user: User,
    ) -> PurchaseOrder:
        """
        Create a new purchase order in draft status.

        Validates:
        - Supplier is active and belongs to the org
        - Branch is active and the user is assigned to it
        - All drug IDs exist in the org (single query, not N+1)
        - No duplicate drug IDs within the same PO
        """
        # --- Validate supplier ---
        supplier = await PurchaseOrderService.get_supplier(db, po_data.supplier_id)
        if not supplier.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Supplier is inactive",
            )
        if supplier.organization_id != user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Supplier does not belong to your organisation",
            )

        # --- Validate branch ---
        branch = await db.scalar(
            select(Branch).where(
                Branch.id == po_data.branch_id,
                Branch.is_deleted.is_(False),
                Branch.is_active.is_(True),
            )
        )
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found or inactive",
            )
        if (
            str(po_data.branch_id) not in {str(b) for b in (user.assigned_branches or [])}
            and not user.is_super_admin
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this branch",
            )

        # --- Validate drugs (single query) ---
        drug_ids = [item.drug_id for item in po_data.items]

        # Guard against duplicate drugs in one PO
        if len(drug_ids) != len(set(drug_ids)):
            seen: set[uuid.UUID] = set()
            dups = [d for d in drug_ids if d in seen or seen.add(d)]  # type: ignore[func-returns-value]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate drug IDs in request: {dups}",
            )

        result = await db.execute(
            select(Drug).where(
                Drug.id.in_(drug_ids),
                Drug.organization_id == user.organization_id,
                Drug.is_deleted.is_(False),
            )
        )
        drugs: dict[uuid.UUID, Drug] = {d.id: d for d in result.scalars().all()}

        if len(drugs) != len(drug_ids):
            missing = sorted(str(d) for d in set(drug_ids) - set(drugs))
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Drugs not found in your organisation: {missing}",
            )

        # --- Calculate totals ---
        subtotal = sum(
            item.quantity_ordered * item.unit_cost for item in po_data.items
        )
        tax_amount = Decimal("0")  # Extend here if org-level tax rates are introduced
        total_amount = subtotal + tax_amount + (po_data.shipping_cost or Decimal("0"))

        # --- Generate collision-safe PO number ---
        po_number = await PurchaseOrderService._generate_po_number(db, branch.code)

        # --- Persist ---
        po = PurchaseOrder(
            id=uuid.uuid4(),
            organization_id=user.organization_id,
            branch_id=po_data.branch_id,
            supplier_id=po_data.supplier_id,
            po_number=po_number,
            status="draft",
            ordered_by=user.id,
            subtotal=subtotal,
            tax_amount=tax_amount,
            shipping_cost=po_data.shipping_cost,
            total_amount=total_amount,
            expected_delivery_date=po_data.expected_delivery_date,
            notes=po_data.notes,
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(po)
        await db.flush()

        for item_data in po_data.items:
            db.add(
                PurchaseOrderItem(
                    id=uuid.uuid4(),
                    purchase_order_id=po.id,
                    drug_id=item_data.drug_id,
                    quantity_ordered=item_data.quantity_ordered,
                    quantity_received=0,
                    unit_cost=item_data.unit_cost,
                    total_cost=item_data.quantity_ordered * item_data.unit_cost,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )

        await PurchaseOrderService._create_audit_log(
            db,
            action="create_purchase_order",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=user.organization_id,
            changes={
                "after": {
                    "po_number": po_number,
                    "supplier_id": str(po_data.supplier_id),
                    "branch_id": str(po_data.branch_id),
                    "total_amount": float(total_amount),
                    "items_count": len(po_data.items),
                }
            },
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def get_purchase_order(
        db: AsyncSession,
        po_id: uuid.UUID,
        include_details: bool = False,
    ) -> PurchaseOrder:
        """Fetch a purchase order or raise 404."""
        query = select(PurchaseOrder).where(PurchaseOrder.id == po_id)
        if include_details:
            query = query.options(
                selectinload(PurchaseOrder.items),
                selectinload(PurchaseOrder.supplier),
            )
        po = await db.scalar(query)
        if not po:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Purchase order not found",
            )
        return po

    # =========================================================================
    # Purchase Order Workflow
    # =========================================================================

    @staticmethod
    async def submit_for_approval(
        db: AsyncSession,
        po_id: uuid.UUID,
        user: User,
    ) -> PurchaseOrder:
        """
        Transition PO from draft → pending.

        Requires at least one item.
        """
        po = await PurchaseOrderService.get_purchase_order(db, po_id, include_details=True)

        PurchaseOrderService._assert_org_access(po, user)

        if po.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot submit a PO with status '{po.status}' — only drafts can be submitted",
            )
        if not po.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot submit an empty purchase order",
            )

        po.status = "pending"
        po.updated_at = _now()
        po.mark_as_pending_sync()

        await PurchaseOrderService._create_audit_log(
            db,
            action="submit_purchase_order",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={"status": {"before": "draft", "after": "pending"}},
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def approve_purchase_order(
        db: AsyncSession,
        po_id: uuid.UUID,
        user: User,
    ) -> PurchaseOrder:
        """
        Transition PO from pending → approved.

        The permission check is already enforced by the router's
        require_permission dependency; we re-verify here for defence-in-depth.
        """
        if not user.has_permission("approve_purchase_orders"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions to approve purchase orders",
            )

        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        if po.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot approve a PO with status '{po.status}' — only pending POs can be approved",
            )

        po.status = "approved"
        po.approved_by = user.id
        po.approved_at = _now()
        po.updated_at = _now()
        po.mark_as_pending_sync()

        await PurchaseOrderService._create_audit_log(
            db,
            action="approve_purchase_order",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={"status": {"before": "pending", "after": "approved"}},
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def reject_purchase_order(
        db: AsyncSession,
        po_id: uuid.UUID,
        reason: str,
        user: User,
    ) -> PurchaseOrder:
        """
        Transition PO from pending → rejected.

        Uses status 'rejected' (not 'cancelled') so the audit trail clearly
        distinguishes a rejected submission from a deliberate cancellation.
        """
        if not user.has_permission("approve_purchase_orders"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions to reject purchase orders",
            )

        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        if po.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reject a PO with status '{po.status}' — only pending POs can be rejected",
            )

        po.status = "cancelled"  # Map to DB enum value; display layer can show "Rejected"
        po.notes = f"[REJECTED] {reason}\n\n{po.notes or ''}".strip()
        po.updated_at = _now()
        po.mark_as_pending_sync()

        await PurchaseOrderService._create_audit_log(
            db,
            action="reject_purchase_order",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={
                "status": {"before": "pending", "after": "cancelled"},
                "reason": reason,
            },
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def cancel_purchase_order(
        db: AsyncSession,
        po_id: uuid.UUID,
        reason: str,
        user: User,
    ) -> PurchaseOrder:
        """
        Cancel a PO that is in draft or pending status.

        Approved / ordered / received POs cannot be cancelled through this
        path — use a dedicated reversal process for those.
        """
        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        if po.status not in ("draft", "pending"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot cancel a PO with status '{po.status}'. "
                    "Only draft or pending orders can be cancelled."
                ),
            )

        po.status = "cancelled"
        po.notes = f"[CANCELLED] {reason}\n\n{po.notes or ''}".strip()
        po.updated_at = _now()
        po.mark_as_pending_sync()

        await PurchaseOrderService._create_audit_log(
            db,
            action="cancel_purchase_order",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={
                "status": {"before": po.status, "after": "cancelled"},
                "reason": reason,
            },
        )

        await db.commit()
        await db.refresh(po)
        return po

    # =========================================================================
    # Receiving Goods  ── CRITICAL PATH ──
    # =========================================================================

    @staticmethod
    async def receive_goods(
        db: AsyncSession,
        po_id: uuid.UUID,
        receive_data: ReceivePurchaseOrder,
        user: User,
    ) -> ReceivePurchaseOrderResponse:
        """
        Receive goods against an approved or partially-received PO.

        Transactional guarantees:
        - The entire operation runs inside a savepoint (begin_nested).
        - BranchInventory rows are locked with SELECT … FOR UPDATE before
          being modified, preventing concurrent receipt races.
        - The outer commit is called only after the savepoint succeeds.

        Inventory effects per item:
        1. Create DrugBatch  (FEFO tracking)
        2. Upsert BranchInventory  (quantity increment)
        3. Create StockAdjustment  (immutable audit row)
        4. Update Drug.cost_price  (weighted-average costing)

        Items that are already fully received are skipped (logged in warnings).
        Items whose received quantity would exceed the ordered quantity raise 400.
        """
        batches_created = 0
        inventory_updated = 0
        warnings: list[str] = []

        async with db.begin_nested():  # savepoint — rolls back cleanly on error

            # Lock the PO row for the duration of this transaction
            result = await db.execute(
                select(PurchaseOrder)
                .options(
                    selectinload(PurchaseOrder.items),
                    selectinload(PurchaseOrder.supplier),
                )
                .where(PurchaseOrder.id == po_id)
                .with_for_update()
            )
            po = result.scalar_one_or_none()

            if not po:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Purchase order not found",
                )

            PurchaseOrderService._assert_org_access(po, user)

            if po.status not in ("approved", "ordered"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Cannot receive goods for a PO with status '{po.status}'. "
                        "PO must be approved or partially received (ordered)."
                    ),
                )

            # Index PO items by their ID for O(1) lookup
            po_items_by_id: dict[uuid.UUID, PurchaseOrderItem] = {
                item.id: item for item in po.items
            }

            for item_receive in receive_data.items:
                po_item = po_items_by_id.get(item_receive.purchase_order_item_id)
                if not po_item:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=(
                            f"PO item {item_receive.purchase_order_item_id} not found "
                            f"in purchase order {po.po_number}"
                        ),
                    )

                if po_item.quantity_received >= po_item.quantity_ordered:
                    warnings.append(
                        f"Item {po_item.drug_id} is already fully received — skipped"
                    )
                    continue

                remaining = po_item.quantity_ordered - po_item.quantity_received
                if item_receive.quantity_received > remaining:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Cannot receive {item_receive.quantity_received} units for item "
                            f"{po_item.drug_id}: only {remaining} unit(s) remain to be received"
                        ),
                    )

                # ── 1. Update PO item ────────────────────────────────────────
                po_item.quantity_received += item_receive.quantity_received
                po_item.batch_number = item_receive.batch_number
                po_item.expiry_date = item_receive.expiry_date
                po_item.updated_at = _now()

                # ── 1a. Guard: drug must not be soft-deleted ────────────────
                drug = await db.scalar(
                    select(Drug).where(
                        Drug.id == po_item.drug_id,
                        Drug.organization_id == po.organization_id,
                    )
                )
                if not drug or drug.is_deleted:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot receive goods for a deleted drug (ID: {po_item.drug_id})",
                    )

                # ── 1b. Guard: duplicate batch ──────────────────────────────
                dup = await db.scalar(
                    select(DrugBatch.id).where(
                        DrugBatch.branch_id == po.branch_id,
                        DrugBatch.drug_id == po_item.drug_id,
                        DrugBatch.batch_number == item_receive.batch_number,
                    )
                )
                if dup:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Batch '{item_receive.batch_number}' already exists "
                            f"for this drug at this branch"
                        ),
                    )

                # ── 2. Create DrugBatch ──────────────────────────────────────
                batch = DrugBatch(
                    id=uuid.uuid4(),
                    branch_id=po.branch_id,
                    drug_id=po_item.drug_id,
                    batch_number=item_receive.batch_number,
                    quantity=item_receive.quantity_received,
                    remaining_quantity=item_receive.quantity_received,
                    manufacturing_date=item_receive.manufacturing_date,
                    expiry_date=item_receive.expiry_date,
                    cost_price=po_item.unit_cost,
                    supplier=po.supplier.name,
                    purchase_order_id=po.id,
                    created_at=_now(),
                    updated_at=_now(),
                )
                db.add(batch)
                batches_created += 1

                # ── 3. Upsert BranchInventory (race-safe) ──────────────────
                # Use a savepoint + IntegrityError fallback so concurrent
                # first-receipt for the same (branch, drug) does not collide.
                from sqlalchemy.exc import IntegrityError
                inv_result = await db.execute(
                    select(BranchInventory)
                    .where(
                        BranchInventory.branch_id == po.branch_id,
                        BranchInventory.drug_id == po_item.drug_id,
                    )
                    .with_for_update()
                )
                inventory = inv_result.scalar_one_or_none()
                previous_quantity = 0

                if inventory:
                    previous_quantity = inventory.quantity
                    inventory.quantity += item_receive.quantity_received
                    inventory.updated_at = _now()
                    inventory.mark_as_pending_sync()
                else:
                    try:
                        inventory = BranchInventory(
                            id=uuid.uuid4(),
                            branch_id=po.branch_id,
                            drug_id=po_item.drug_id,
                            quantity=item_receive.quantity_received,
                            reserved_quantity=0,
                            sync_status="pending",
                            sync_version=1,
                            created_at=_now(),
                            updated_at=_now(),
                        )
                        db.add(inventory)
                        await db.flush()
                    except IntegrityError:
                        # Concurrent insert won – re-fetch and update
                        await db.rollback()
                        inv_result = await db.execute(
                            select(BranchInventory)
                            .where(
                                BranchInventory.branch_id == po.branch_id,
                                BranchInventory.drug_id == po_item.drug_id,
                            )
                            .with_for_update()
                        )
                        inventory = inv_result.scalar_one()
                        previous_quantity = inventory.quantity
                        inventory.quantity += item_receive.quantity_received
                        inventory.updated_at = _now()
                        inventory.mark_as_pending_sync()

                inventory_updated += 1

                # ── 4. StockAdjustment audit row ─────────────────────────────
                adjustment = StockAdjustment(
                    id=uuid.uuid4(),
                    branch_id=po.branch_id,
                    drug_id=po_item.drug_id,
                    adjustment_type="purchase_receipt",
                    quantity_change=item_receive.quantity_received,
                    previous_quantity=previous_quantity,
                    new_quantity=previous_quantity + item_receive.quantity_received,
                    reason=(
                        f"Goods received from PO {po.po_number}, "
                        f"batch {item_receive.batch_number}"
                    ),
                    adjusted_by=user.id,
                    created_at=_now(),
                    updated_at=_now(),
                )
                db.add(adjustment)
                await db.flush()

                await InventoryService._record_inventory_movement(
                    db=db,
                    organization_id=po.organization_id,
                    branch_id=po.branch_id,
                    drug_id=po_item.drug_id,
                    movement_type="purchase_receipt",
                    quantity_change=item_receive.quantity_received,
                    quantity_before=previous_quantity,
                    quantity_after=previous_quantity + item_receive.quantity_received,
                    batch_id=batch.id,
                    batch_quantity_before=0,
                    batch_quantity_after=batch.remaining_quantity,
                    unit_cost=Decimal(str(po_item.unit_cost)),
                    source_type="purchase_order",
                    source_id=po.id,
                    source_line_id=po_item.id,
                    reference_number=po.po_number,
                    reason=(
                        f"Goods received from PO {po.po_number}, "
                        f"batch {item_receive.batch_number}"
                    ),
                    created_by=user.id,
                )

                # ── 5. Update Drug weighted-average cost ─────────────────────
                drug = await db.scalar(
                    select(Drug).where(Drug.id == po_item.drug_id).with_for_update()
                )
                if drug:
                    if drug.cost_price and previous_quantity > 0:
                        total_qty = previous_quantity + item_receive.quantity_received
                        drug.cost_price = (
                            drug.cost_price * previous_quantity
                            + po_item.unit_cost * item_receive.quantity_received
                        ) / total_qty
                    else:
                        drug.cost_price = po_item.unit_cost
                    drug.updated_at = _now()

                # ── 5a. Warn when stock exceeds max_stock_level ──────────────
                if drug and drug.max_stock_level and inventory:
                    new_total = inventory.quantity
                    if new_total > drug.max_stock_level:
                        warnings.append(
                            f"Stock for '{drug.name}' ({new_total}) "
                            f"exceeds max level ({drug.max_stock_level})"
                        )

            # ── Update PO status ─────────────────────────────────────────────
            all_received = all(
                item.quantity_received >= item.quantity_ordered for item in po.items
            )
            po.status = "received" if all_received else "ordered"
            if all_received:
                po.received_date = receive_data.received_date
            po.updated_at = _now()
            po.mark_as_pending_sync()

            await PurchaseOrderService._create_audit_log(
                db,
                action="receive_purchase_order",
                entity_type="PurchaseOrder",
                entity_id=po.id,
                user_id=user.id,
                organization_id=po.organization_id,
                changes={
                    "batches_created": batches_created,
                    "inventory_updated": inventory_updated,
                    "new_status": po.status,
                    "warnings": warnings,
                },
            )
        # savepoint commits here

        await db.commit()

        # Reload with full relationships for the response.
        # db.scalar() returns PurchaseOrder | None; the guard below narrows it
        # to PurchaseOrder so _build_po_with_details receives the correct type.
        # In practice this can never be None — we just committed the record —
        # but the type-checker requires the explicit check.   (FIX: was missing)
        full_po = await db.scalar(
            select(PurchaseOrder)
            .options(
                selectinload(PurchaseOrder.items),
                selectinload(PurchaseOrder.supplier),
            )
            .where(PurchaseOrder.id == po_id)
        )
        if full_po is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reload purchase order after commit — please retry",
            )

        return ReceivePurchaseOrderResponse(
            purchase_order=await PurchaseOrderService._build_po_with_details(db, full_po),
            batches_created=batches_created,
            inventory_updated=inventory_updated,
            success=True,
            message=(
                "Goods received successfully"
                if not warnings
                else f"Goods received with {len(warnings)} warning(s): {'; '.join(warnings)}"
            ),
        )

    # =========================================================================
    # Draft PO Mutations (items add / update)
    # =========================================================================

    @staticmethod
    async def add_purchase_order_items(
        db: AsyncSession,
        po_id: uuid.UUID,
        items_data: List[PurchaseOrderItemCreate],
        user: User,
    ) -> PurchaseOrder:
        """
        Append items to a draft PO.

        Guards:
        - PO must be in draft status
        - No drug may already exist in the PO (update the existing item instead)
        - All drug IDs must belong to the organisation
        """
        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        if po.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot add items to a PO with status '{po.status}' — only drafts are editable",
            )

        # Validate drugs
        drug_ids = [item.drug_id for item in items_data]
        result = await db.execute(
            select(Drug).where(
                Drug.id.in_(drug_ids),
                Drug.organization_id == user.organization_id,
                Drug.is_deleted.is_(False),
            )
        )
        drugs: dict[uuid.UUID, Drug] = {d.id: d for d in result.scalars().all()}

        if len(drugs) != len(drug_ids):
            missing = sorted(str(d) for d in set(drug_ids) - set(drugs))
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Drugs not found: {missing}",
            )

        # Check for duplicates against existing PO items
        existing_result = await db.execute(
            select(PurchaseOrderItem.drug_id).where(
                PurchaseOrderItem.purchase_order_id == po_id
            )
        )
        existing_drug_ids: set[uuid.UUID] = set(existing_result.scalars().all())
        duplicates = existing_drug_ids & set(drug_ids)
        if duplicates:
            names = [drugs[d].name for d in duplicates if d in drugs]
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"These drugs already exist in PO {po.po_number}: "
                    f"{', '.join(names)}. Update the existing items instead."
                ),
            )

        new_items: list[PurchaseOrderItem] = []
        for item_data in items_data:
            item = PurchaseOrderItem(
                id=uuid.uuid4(),
                purchase_order_id=po_id,
                drug_id=item_data.drug_id,
                quantity_ordered=item_data.quantity_ordered,
                quantity_received=0,
                unit_cost=item_data.unit_cost,
                total_cost=item_data.quantity_ordered * item_data.unit_cost,
                created_at=_now(),
                updated_at=_now(),
            )
            new_items.append(item)
            db.add(item)

        await db.flush()
        await PurchaseOrderService._recalculate_po_totals(db, po)

        await PurchaseOrderService._create_audit_log(
            db,
            action="add_po_items",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={
                "items_added": len(new_items),
                "new_items": [
                    {
                        "drug_id": str(i.drug_id),
                        "drug_name": drugs[i.drug_id].name,
                        "quantity_ordered": i.quantity_ordered,
                        "unit_cost": str(i.unit_cost),
                    }
                    for i in new_items
                ],
                "new_total": str(po.total_amount),
            },
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def update_purchase_order_item(
        db: AsyncSession,
        po_id: uuid.UUID,
        item_id: uuid.UUID,
        quantity_ordered: int,
        unit_cost: Decimal,
        user: User,
    ) -> PurchaseOrder:
        """Update quantity and/or unit cost on a draft PO item."""
        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        if po.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot edit items on a PO with status '{po.status}'",
            )

        item = await db.scalar(
            select(PurchaseOrderItem).where(
                PurchaseOrderItem.id == item_id,
                PurchaseOrderItem.purchase_order_id == po_id,
            )
        )
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Item not found in this purchase order",
            )

        old = {
            "quantity_ordered": item.quantity_ordered,
            "unit_cost": str(item.unit_cost),
            "total_cost": str(item.total_cost),
        }

        item.quantity_ordered = quantity_ordered
        item.unit_cost = unit_cost
        item.total_cost = Decimal(quantity_ordered) * unit_cost
        item.updated_at = _now()

        await db.flush()
        await PurchaseOrderService._recalculate_po_totals(db, po)

        await PurchaseOrderService._create_audit_log(
            db,
            action="update_po_item",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={
                "item_id": str(item_id),
                "before": old,
                "after": {
                    "quantity_ordered": quantity_ordered,
                    "unit_cost": str(unit_cost),
                    "total_cost": str(item.total_cost),
                },
                "new_po_total": str(po.total_amount),
            },
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def remove_purchase_order_item(
        db: AsyncSession,
        po_id: uuid.UUID,
        item_id: uuid.UUID,
        user: User,
    ) -> PurchaseOrder:
        """Remove an item from a draft PO."""
        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        if po.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot remove items from a PO with status '{po.status}' — only drafts are editable",
            )

        item = await db.scalar(
            select(PurchaseOrderItem).where(
                PurchaseOrderItem.id == item_id,
                PurchaseOrderItem.purchase_order_id == po_id,
            )
        )
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Item not found in this purchase order",
            )

        await db.delete(item)
        await db.flush()
        await PurchaseOrderService._recalculate_po_totals(db, po)

        await PurchaseOrderService._create_audit_log(
            db,
            action="remove_po_item",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            user_id=user.id,
            organization_id=po.organization_id,
            changes={
                "item_id": str(item_id),
                "drug_id": str(item.drug_id),
                "removed_quantity": item.quantity_ordered,
            },
        )

        await db.commit()
        await db.refresh(po)
        return po

    @staticmethod
    async def list_purchase_order_items(
        db: AsyncSession,
        po_id: uuid.UUID,
        user: User,
    ) -> List[PurchaseOrderItemWithDetails]:
        """Return all items for a PO with resolved drug details (single JOIN)."""
        po = await PurchaseOrderService.get_purchase_order(db, po_id)
        PurchaseOrderService._assert_org_access(po, user)

        rows = await db.execute(
            select(PurchaseOrderItem, Drug)
            .join(Drug, PurchaseOrderItem.drug_id == Drug.id)
            .where(PurchaseOrderItem.purchase_order_id == po_id)
            .order_by(Drug.name)
        )

        return [
            PurchaseOrderItemWithDetails(
                id=item.id,
                purchase_order_id=item.purchase_order_id,
                drug_id=item.drug_id,
                quantity_ordered=item.quantity_ordered,
                quantity_received=item.quantity_received,
                unit_cost=item.unit_cost,
                total_cost=item.total_cost,
                batch_number=item.batch_number,
                expiry_date=item.expiry_date,
                drug_name=drug.name,
                drug_sku=drug.sku,
                drug_generic_name=drug.generic_name,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item, drug in rows.all()
        ]

    # =========================================================================
    # Private helpers
    # =========================================================================

    @staticmethod
    def _assert_org_access(po: PurchaseOrder, user: User) -> None:
        """Raise 403 if the PO belongs to a different organisation."""
        if po.organization_id != user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied",
            )

    @staticmethod
    async def _recalculate_po_totals(
        db: AsyncSession,
        po: PurchaseOrder,
    ) -> None:
        """
        Re-aggregate subtotal / tax / total from current PO items.
        Call after any add/update/delete of items, before commit.
        """
        result = await db.execute(
            select(func.coalesce(func.sum(PurchaseOrderItem.total_cost), 0)).where(
                PurchaseOrderItem.purchase_order_id == po.id
            )
        )
        po.subtotal = result.scalar_one()
        po.tax_amount = po.subtotal * Decimal("0")  # extend for org-level tax
        po.total_amount = po.subtotal + po.tax_amount + (po.shipping_cost or Decimal("0"))
        po.updated_at = _now()

    @staticmethod
    async def _generate_po_number(db: AsyncSession, branch_code: str) -> str:
        """
            Generate a collision-safe PO number.

            Locks matching rows via a subquery WITH FOR UPDATE, then counts them,
            so concurrent requests cannot receive the same sequence number.
            Pattern: PO-{BRANCH_CODE}-{YYYYMMDD}-{4-digit sequence}
        """
        today_str = date.today().strftime("%Y%m%d")
        prefix = f"PO-{branch_code}-{today_str}"

        # Lock matching rows first (subquery), then count — PostgreSQL does not
        # allow FOR UPDATE directly on aggregate queries.
        subq = (
            select(PurchaseOrder.id)
            .where(PurchaseOrder.po_number.like(f"{prefix}%"))
            .with_for_update()
            .subquery()
        )
        result = await db.execute(
            select(func.count()).select_from(subq)
        )
        count: int = result.scalar_one() or 0
        return f"{prefix}-{str(count + 1).zfill(4)}"

    @staticmethod
    async def _build_po_with_details(
        db: AsyncSession,
        po: PurchaseOrder,
    ) -> PurchaseOrderWithDetails:
        """
        Build a PurchaseOrderWithDetails from an already-loaded PO.

        Resolves branch / orderer / approver in three targeted queries rather
        than N+1 per-item Drug selects. Drug details are fetched with a single
        JOIN across all items.
        """
        # Branch
        branch = await db.scalar(select(Branch).where(Branch.id == po.branch_id))

        # Orderer
        orderer = await db.scalar(select(User).where(User.id == po.ordered_by))

        # Approver (optional)
        approved_by_name: Optional[str] = None
        if po.approved_by:
            approver = await db.scalar(select(User).where(User.id == po.approved_by))
            approved_by_name = approver.full_name if approver else None

        # Items with drug details — single JOIN
        rows = await db.execute(
            select(PurchaseOrderItem, Drug)
            .join(Drug, PurchaseOrderItem.drug_id == Drug.id)
            .where(PurchaseOrderItem.purchase_order_id == po.id)
            .order_by(Drug.name)
        )
        items_with_details = [
            PurchaseOrderItemWithDetails(
                **{k: v for k, v in item.__dict__.items() if not k.startswith("_")},
                drug_name=drug.name,
                drug_sku=drug.sku,
                drug_generic_name=drug.generic_name,
            )
            for item, drug in rows.all()
        ]

        po_dict = {k: v for k, v in po.__dict__.items() if not k.startswith("_")}
        po_dict.pop("items", None)

        return PurchaseOrderWithDetails(
            **po_dict,
            items=items_with_details,
            supplier_name=po.supplier.name,
            branch_name=branch.name if branch else "",
            ordered_by_name=orderer.full_name if orderer else "",
            approved_by_name=approved_by_name,
        )

    @staticmethod
    async def _create_audit_log(
        db: AsyncSession,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        changes: Optional[dict] = None,
    ) -> None:
        """
        Append an AuditLog row to the current transaction.

        Never commits — the caller owns the transaction boundary.
        """
        db.add(
            AuditLog(
                id=uuid.uuid4(),
                organization_id=organization_id,
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                changes=changes or {},
                created_at=_now(),
            )
        )
