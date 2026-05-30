"""
Sales Service
=============
Orchestrates the two public sale operations.  All domain logic lives in the
sub-modules imported below — this file is intentionally a thin coordinator.

Sub-modules
-----------
validators/sale_validators.py    — allergy check, contract validation
pricing/pricing_calculator.py    — unit price resolution, per-item pricing
inventory/inventory_deductor.py  — FEFO batch pre-loading
utils/sale_helpers.py            — loyalty tier, sale number, response builder,
                                   audit log writer

Transaction discipline
----------------------
* Every write is wrapped in a single ``db.begin_nested()`` savepoint.
* ``db.commit()`` is called exactly once, after the savepoint exits cleanly.
* ``create_audit_log`` (from utils) only flushes — it never commits — so a
  failed audit write cannot roll back a completed sale.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import (
    BranchInventory,
    DrugBatch,
    StockAdjustment,
)
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.precriptions.prescription_model import Prescription
from app.models.sales.sales_model import Sale, SaleItem
from app.models.system_md.sys_models import SystemAlert
from app.models.user.user_model import User
from app.schemas.sales_schemas import (
    ProcessSaleResponse,
    RefundSaleRequest,
    RefundSaleResponse,
    SaleCreate,
    SaleItemCreate,
)

# Sub-module imports
from app.services.sales.validators.sale_validators import (
    check_customer_allergies,
    load_and_validate_contract,
)
from app.services.sales.pricing.pricing_calculator import (
    d as _d,
    r2 as _r2,
    resolve_unit_price,
    compute_item_pricing,
)
from app.services.sales.inventory.inventory_deductor import load_fefo_batches
from app.services.sales.utils.sale_helpers import (
    resolve_loyalty_tier,
    generate_sale_number,
    build_sale_with_details,
    create_audit_log,
)


class SalesService:
    """
    Stateless service — every method is a static coroutine.

    Public surface
    --------------
    process_sale  — create a completed sale end-to-end
    refund_sale   — full or partial refund of a completed sale
    """

    # =========================================================================
    # PUBLIC: Process Sale
    # =========================================================================

    @staticmethod
    async def process_sale(
        db: AsyncSession,
        sale_data: SaleCreate,
        user: User,
    ) -> ProcessSaleResponse:
        """
        Process a customer sale end-to-end inside a single atomic savepoint.

        Steps
        -----
         1  Validate branch access (org-scoped).
         2  Load and validate customer (org-scoped, active only).
         3  Load and validate prescription (org-scoped, active + non-expired).
         4  Load and validate all drugs (org-scoped, active only).
         5  SAFETY: allergy check against customer profile.
         6  Gate: every prescription-only drug must have a valid prescription.
         7  Load and validate PriceContract; load per-drug overrides.
         8  Load FEFO batches; resolve unit prices (branch price → batch price → drug price).
         9  Reserve inventory with row-level locks (rollback-safe accumulator).
        10  Compute per-item and sale-level pricing.
        11  Validate contract purchase limits.
        12  Validate payment sufficiency.
        13  Persist Sale record (model-aligned fields only).
        14  Persist SaleItem records (model-aligned fields only).
        15  Deduct inventory via FEFO — multi-batch safe with FOR UPDATE locks.
        16  Write StockAdjustment (type='correction') + system alerts.
        17  Decrement prescription refills.
        18  Award loyalty points; recalculate tier.
        19  Commit.
        20  Append AuditLog (flush only — no commit).
        21  Return ProcessSaleResponse.
        """
        async with db.begin_nested():  # savepoint — full rollback on any exception

            # ------------------------------------------------------------------
            # 1. Branch access
            # ------------------------------------------------------------------
            if (
                str(sale_data.branch_id) not in {str(b) for b in (user.assigned_branches or [])}
                and user.role not in ("super_admin", "admin")
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not assigned to this branch.",
                )

            branch_res = await db.execute(
                select(Branch).where(
                    Branch.id == sale_data.branch_id,
                    Branch.organization_id == user.organization_id,
                    Branch.is_deleted == False,
                    Branch.is_active == True,
                )
            )
            branch = branch_res.scalar_one_or_none()
            if not branch:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Branch not found or inactive.",
                )

            org_res = await db.execute(
                select(Organization).where(
                    Organization.id == user.organization_id
                )
            )
            organization = org_res.scalar_one()

            # ------------------------------------------------------------------
            # 2. Customer
            # ------------------------------------------------------------------
            customer: Optional[Customer] = None
            if sale_data.customer_id:
                cust_res = await db.execute(
                    select(Customer).where(
                        Customer.id == sale_data.customer_id,
                        Customer.organization_id == user.organization_id,
                        Customer.is_deleted == False,
                        Customer.is_active == True,
                    )
                )
                customer = cust_res.scalar_one_or_none()
                if not customer:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Customer not found or inactive.",
                    )

            # ------------------------------------------------------------------
            # 3. Prescription
            # ------------------------------------------------------------------
            prescription: Optional[Prescription] = None
            pharmacist_id: Optional[uuid.UUID] = None

            if sale_data.prescription_id:
                if not sale_data.customer_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "A registered customer is required "
                            "when a prescription is provided."
                        ),
                    )

                rx_res = await db.execute(
                    select(Prescription).where(
                        Prescription.id == sale_data.prescription_id,
                        Prescription.organization_id == user.organization_id,
                        Prescription.customer_id == sale_data.customer_id,
                    )
                )
                prescription = rx_res.scalar_one_or_none()
                if not prescription:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Prescription not found for this customer.",
                    )

                if prescription.status != "active":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Prescription is '{prescription.status}'. "
                            "Only active prescriptions may be dispensed."
                        ),
                    )

                if prescription.refills_remaining <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No refills remaining on this prescription.",
                    )

                if date.today() > prescription.expiry_date:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Prescription expired on {prescription.expiry_date}. "
                            "A new prescription is required."
                        ),
                    )

                if user.role not in ("pharmacist", "admin", "super_admin", "manager"):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            "Only pharmacists and above may process "
                            "prescription sales."
                        ),
                    )
                pharmacist_id = user.id

            # ------------------------------------------------------------------
            # 4. Drugs — single query, org-scoped
            # ------------------------------------------------------------------
            drug_ids = [item.drug_id for item in sale_data.items]

            drugs_res = await db.execute(
                select(Drug).where(
                    Drug.id.in_(drug_ids),
                    Drug.organization_id == user.organization_id,
                    Drug.is_deleted == False,
                    Drug.is_active == True,
                )
            )
            drugs: Dict[uuid.UUID, Drug] = {
                d.id: d for d in drugs_res.scalars().all()
            }

            missing = set(drug_ids) - set(drugs.keys())
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Drugs not found or inactive: {missing}",
                )

            # ------------------------------------------------------------------
            # 5. Allergy check — runs before any inventory mutation
            # ------------------------------------------------------------------
            if customer:
                await check_customer_allergies(
                    db=db,
                    customer=customer,
                    drug_ids=drug_ids,
                    drugs=drugs,
                    branch_id=sale_data.branch_id,
                    organization_id=user.organization_id,
                )

            # ------------------------------------------------------------------
            # 6. Prescription gate — every Rx-only drug must have a valid Rx
            # ------------------------------------------------------------------
            for item in sale_data.items:
                drug = drugs[item.drug_id]
                if drug.requires_prescription and not prescription:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"'{drug.name}' requires a valid prescription. "
                            "Provide a prescription_id or remove this item."
                        ),
                    )

            # ------------------------------------------------------------------
            # 7. Contract — load, validate, fetch per-drug overrides
            # ------------------------------------------------------------------
            contract, contract_items = await load_and_validate_contract(
                db=db,
                contract_id=sale_data.price_contract_id,
                branch_id=sale_data.branch_id,
                drug_ids=drug_ids,
                user=user,
                insurance_verified=getattr(sale_data, "insurance_verified", False),
                customer_id=sale_data.customer_id,
            )

            # ------------------------------------------------------------------
            # 8. FEFO batches + unit price resolution
            #
            # Priority: BranchInventory.selling_price → DrugBatch.selling_price → Drug.unit_price
            # cost_price is NEVER used — it is the pharmacy's acquisition cost.
            # ------------------------------------------------------------------
            fefo_batches: Dict[uuid.UUID, List[DrugBatch]] = await load_fefo_batches(
                db=db,
                branch_id=sale_data.branch_id,
                drug_ids=drug_ids,
            )

            branch_price_rows = await db.execute(
                select(BranchInventory.drug_id, BranchInventory.selling_price).where(
                    BranchInventory.branch_id == sale_data.branch_id,
                    BranchInventory.drug_id.in_(drug_ids),
                )
            )
            branch_prices: Dict[uuid.UUID, Decimal] = {
                row.drug_id: Decimal(str(row.selling_price))
                for row in branch_price_rows
                if row.selling_price is not None
            }

            resolved_prices: Dict[uuid.UUID, Decimal] = {
                item.drug_id: resolve_unit_price(
                    drug=drugs[item.drug_id],
                    batches=fefo_batches.get(item.drug_id, []),
                    branch_selling_price=branch_prices.get(item.drug_id),
                )
                for item in sale_data.items
            }

            # ------------------------------------------------------------------
            # 9. Reserve inventory — row-level locks; rollback-safe accumulator
            # ------------------------------------------------------------------
            reservations: List[Tuple[BranchInventory, int]] = []
            try:
                for item in sale_data.items:
                    drug = drugs[item.drug_id]

                    inv_res = await db.execute(
                        select(BranchInventory)
                        .where(
                            BranchInventory.branch_id == sale_data.branch_id,
                            BranchInventory.drug_id   == item.drug_id,
                        )
                        .with_for_update()
                    )
                    inventory = inv_res.scalar_one_or_none()

                    if not inventory:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"No inventory record for '{drug.name}' at this "
                                "branch. Stock must be received first."
                            ),
                        )

                    available = inventory.quantity - inventory.reserved_quantity
                    if available < item.quantity:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Insufficient stock for '{drug.name}'. "
                                f"Available: {available}, "
                                f"Requested: {item.quantity}."
                            ),
                        )

                    # Cross-check: FEFO batch total covers the requested quantity
                    total_batch_qty = sum(
                        b.remaining_quantity
                        for b in fefo_batches.get(item.drug_id, [])
                    )
                    if total_batch_qty < item.quantity:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Insufficient non-expired batch stock for "
                                f"'{drug.name}'. Valid batch total: "
                                f"{total_batch_qty}, Requested: {item.quantity}."
                            ),
                        )

                    inventory.reserved_quantity += item.quantity
                    inventory.mark_as_synced()
                    reservations.append((inventory, item.quantity))

            except HTTPException:
                # Unwind any reservations already applied before re-raising
                for inv, qty in reservations:
                    inv.reserved_quantity -= qty
                raise

            # ------------------------------------------------------------------
            # 10. Per-item pricing
            # ------------------------------------------------------------------
            item_pricing = compute_item_pricing(
                items=sale_data.items,
                drugs=drugs,
                contract=contract,
                contract_items=contract_items,
                resolved_prices=resolved_prices,
            )

            subtotal       = _r2(sum((p["item_subtotal"] for p in item_pricing), Decimal("0")))
            total_discount = _r2(sum((p["discount_amount"] for p in item_pricing), Decimal("0")))
            total_tax      = _r2(sum((p["tax_amount"]      for p in item_pricing), Decimal("0")))
            total_amount   = _r2(subtotal - total_discount + total_tax)

            # ------------------------------------------------------------------
            # 11. Contract purchase limits
            # ------------------------------------------------------------------
            if contract.minimum_purchase_amount and total_amount < _d(contract.minimum_purchase_amount):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Minimum purchase of {contract.minimum_purchase_amount} "
                        f"required for '{contract.contract_name}'."
                    ),
                )

            if contract.maximum_purchase_amount and total_amount > _d(contract.maximum_purchase_amount):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Purchase total {total_amount} exceeds contract "
                        f"maximum of {contract.maximum_purchase_amount}."
                    ),
                )

            # Insurance totals
            patient_copay_amount: Optional[Decimal] = None
            insurance_covered_amount: Optional[Decimal] = None
            if contract.contract_type == "insurance":
                patient_copay_amount     = _r2(sum(
                    (p["patient_copay"] or Decimal("0") for p in item_pricing),
                    Decimal("0"),
                ))
                insurance_covered_amount = _r2(total_amount - patient_copay_amount)

            # ------------------------------------------------------------------
            # 12. Payment
            # ------------------------------------------------------------------
            amount_due = (
                patient_copay_amount
                if contract.contract_type == "insurance" and patient_copay_amount is not None
                else total_amount
            )
            amount_paid = _d(
                sale_data.amount_paid
                if sale_data.amount_paid is not None
                else amount_due
            )
            if amount_paid < amount_due:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient payment. "
                        f"Required: {amount_due}, Paid: {amount_paid}."
                    ),
                )
            change_amount = _r2(amount_paid - amount_due)

            # ------------------------------------------------------------------
            # 13. Persist Sale — only model-defined fields
            # ------------------------------------------------------------------
            sale_number = await generate_sale_number(db, branch.code)

            sale = Sale(
                id=uuid.uuid4(),
                organization_id=user.organization_id,
                branch_id=sale_data.branch_id,
                sale_number=sale_number,
                customer_id=sale_data.customer_id,
                customer_name=sale_data.customer_name,
                # Financials
                subtotal=float(subtotal),
                discount_amount=float(total_discount),
                tax_amount=float(total_tax),
                total_amount=float(total_amount),
                # Contract snapshot
                price_contract_id=contract.id,
                contract_name=contract.contract_name,
                contract_discount_percentage=float(contract.discount_percentage),
                # Payment
                payment_method=sale_data.payment_method,
                payment_status="completed",
                amount_paid=float(amount_paid),
                change_amount=float(change_amount),
                payment_reference=getattr(sale_data, "payment_reference", None),
                # Insurance
                insurance_claim_number=getattr(sale_data, "insurance_claim_number", None),
                patient_copay_amount=(
                    float(patient_copay_amount) if patient_copay_amount is not None else None
                ),
                insurance_covered_amount=(
                    float(insurance_covered_amount) if insurance_covered_amount is not None else None
                ),
                insurance_verified=getattr(sale_data, "insurance_verified", False),
                insurance_verified_at=(
                    datetime.now(timezone.utc)
                    if getattr(sale_data, "insurance_verified", False)
                    else None
                ),
                insurance_verified_by=(
                    user.id if getattr(sale_data, "insurance_verified", False) else None
                ),
                # Prescription snapshot
                prescription_id=sale_data.prescription_id,
                prescription_number=(
                    prescription.prescription_number if prescription else None
                ),
                prescriber_name=(
                    prescription.prescriber_name if prescription else None
                ),
                prescriber_license=(
                    prescription.prescriber_license if prescription else None
                ),
                # Staff
                cashier_id=user.id,
                pharmacist_id=pharmacist_id,
                # Meta
                notes=sale_data.notes,
                status="completed",
                receipt_printed=False,
                receipt_emailed=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            sale.mark_as_synced()
            db.add(sale)
            await db.flush()  # materialise sale.id for FK references below

            # ------------------------------------------------------------------
            # 14. Persist SaleItems — only model-defined fields
            # ------------------------------------------------------------------
            created_items: List[Tuple[SaleItem, SaleItemCreate]] = []

            for pricing in item_pricing:
                item_data: SaleItemCreate = pricing["item"]
                item_drug: Drug           = pricing["drug"]

                sale_item = SaleItem(
                    id=uuid.uuid4(),
                    sale_id=sale.id,
                    drug_id=item_data.drug_id,
                    drug_name=item_drug.name,
                    drug_sku=item_drug.sku,
                    # batch_id populated in step 15 after FEFO deduction
                    quantity=item_data.quantity,
                    unit_price=float(pricing["unit_price"]),
                    subtotal=float(pricing["item_subtotal"]),
                    discount_percentage=float(pricing["discount_percentage"]),
                    discount_amount=float(pricing["discount_amount"]),
                    tax_rate=float(pricing["tax_rate"]),
                    tax_amount=float(pricing["tax_amount"]),
                    total_price=float(pricing["item_total"]),
                    requires_prescription=item_drug.requires_prescription,
                    prescription_verified=bool(prescription),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.add(sale_item)
                created_items.append((sale_item, item_data))

            await db.flush()

            # ------------------------------------------------------------------
            # 15. FEFO inventory deduction — multi-batch safe
            #
            # Iterates FEFO-ordered batches under FOR UPDATE lock, spilling into
            # subsequent batches when the primary batch is exhausted.
            # The primary batch (first deducted) is stored as SaleItem.batch_id.
            # ------------------------------------------------------------------
            inventory_updated = 0
            batches_updated   = 0

            for sale_item, _ in created_items:
                item_drug     = drugs[sale_item.drug_id]
                qty_to_deduct = sale_item.quantity
                primary_batch_id: Optional[uuid.UUID] = None

                locked_res = await db.execute(
                    select(DrugBatch)
                    .where(
                        DrugBatch.branch_id        == sale_data.branch_id,
                        DrugBatch.drug_id          == sale_item.drug_id,
                        DrugBatch.remaining_quantity > 0,
                        DrugBatch.expiry_date      > date.today(),
                    )
                    .order_by(DrugBatch.expiry_date.asc())
                    .with_for_update()
                )
                available_batches = locked_res.scalars().all()

                if not available_batches:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"No valid (non-expired) batches available for "
                            f"'{item_drug.name}'. Stock may have been depleted "
                            "by a concurrent transaction."
                        ),
                    )

                for batch in available_batches:
                    if qty_to_deduct <= 0:
                        break

                    take = min(batch.remaining_quantity, qty_to_deduct)

                    if primary_batch_id is None:
                        primary_batch_id = batch.id

                    batch.remaining_quantity -= take
                    batch.updated_at          = datetime.now(timezone.utc)
                    batch.mark_as_synced()
                    qty_to_deduct  -= take
                    batches_updated += 1

                # Guard: concurrent depletion between reservation and deduction
                if qty_to_deduct > 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Concurrent stock depletion detected for "
                            f"'{item_drug.name}'. Please retry the transaction."
                        ),
                    )

                sale_item.batch_id = primary_batch_id  # tag with primary batch

                # Deduct from aggregate inventory and release the reservation
                inv_res = await db.execute(
                    select(BranchInventory)
                    .where(
                        BranchInventory.branch_id == sale_data.branch_id,
                        BranchInventory.drug_id   == sale_item.drug_id,
                    )
                    .with_for_update()
                )
                inventory     = inv_res.scalar_one()
                previous_qty  = inventory.quantity
                inventory.quantity          -= sale_item.quantity
                inventory.reserved_quantity -= sale_item.quantity  # release
                inventory.updated_at         = datetime.now(timezone.utc)
                inventory.mark_as_synced()
                inventory_updated += 1

                # ------------------------------------------------------------------
                # 16. StockAdjustment + system alerts
                #     'correction' is the model-valid type for sale-driven deductions
                # ------------------------------------------------------------------
                db.add(
                    StockAdjustment(
                        id=uuid.uuid4(),
                        branch_id=sale_data.branch_id,
                        drug_id=sale_item.drug_id,
                        adjustment_type="correction",
                        quantity_change=-sale_item.quantity,
                        previous_quantity=previous_qty,
                        new_quantity=inventory.quantity,
                        reason=f"Sale {sale_number}",
                        adjusted_by=user.id,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )

                # Low-stock / out-of-stock alert
                if inventory.quantity <= item_drug.reorder_level:
                    is_oos = inventory.quantity == 0
                    db.add(
                        SystemAlert(
                            id=uuid.uuid4(),
                            organization_id=user.organization_id,
                            branch_id=sale_data.branch_id,
                            alert_type="out_of_stock" if is_oos else "low_stock",
                            severity="critical" if is_oos else "high",
                            title=(
                                f"Out of Stock: {item_drug.name}"
                                if is_oos
                                else f"Low Stock: {item_drug.name}"
                            ),
                            message=(
                                f"{item_drug.name} is now out of stock. "
                                f"Suggested reorder: {item_drug.reorder_quantity} units."
                                if is_oos
                                else (
                                    f"{item_drug.name} is at {inventory.quantity} units "
                                    f"(reorder level: {item_drug.reorder_level}). "
                                    f"Suggested reorder: {item_drug.reorder_quantity} units."
                                )
                            ),
                            drug_id=item_drug.id,
                            is_resolved=False,
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )

                # Expiry warning — batches with ≤ 90 days remaining
                expiry_window = date.today() + timedelta(days=90)
                expiring_res  = await db.execute(
                    select(DrugBatch).where(
                        DrugBatch.branch_id        == sale_data.branch_id,
                        DrugBatch.drug_id          == sale_item.drug_id,
                        DrugBatch.remaining_quantity > 0,
                        DrugBatch.expiry_date      > date.today(),
                        DrugBatch.expiry_date      <= expiry_window,
                    )
                )
                for exp_batch in expiring_res.scalars().all():
                    days_left = (exp_batch.expiry_date - date.today()).days
                    db.add(
                        SystemAlert(
                            id=uuid.uuid4(),
                            organization_id=user.organization_id,
                            branch_id=sale_data.branch_id,
                            alert_type="expiry_warning",
                            severity="high" if days_left < 30 else "medium",
                            title=f"Expiring Soon: {item_drug.name}",
                            message=(
                                f"Batch {exp_batch.batch_number} of {item_drug.name} "
                                f"expires in {days_left} day(s) ({exp_batch.expiry_date}). "
                                f"Remaining: {exp_batch.remaining_quantity} units."
                            ),
                            drug_id=item_drug.id,
                            is_resolved=False,
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )

            # ------------------------------------------------------------------
            # 17. Prescription refill decrement
            # ------------------------------------------------------------------
            if prescription:
                prescription.refills_remaining -= 1
                prescription.last_refill_date   = date.today()
                prescription.verified_by        = user.id
                prescription.verified_at        = datetime.now(timezone.utc)
                prescription.status = (
                    "filled" if prescription.refills_remaining == 0 else "active"
                )
                prescription.updated_at = datetime.now(timezone.utc)
                prescription.mark_as_synced()

            # ------------------------------------------------------------------
            # 18. Loyalty points + tier recalculation
            #     Also increment denormalized purchase counters so
            #     CustomerService._build_with_details always has fresh data
            #     even if the live Sale aggregate query has a type issue.
            # ------------------------------------------------------------------
            points_earned = 0
            if customer:
                loyalty_cfg   = organization.settings.get("loyalty", {})
                points_rate   = _d(loyalty_cfg.get("points_per_unit", "1.0"))
                points_earned = int(total_amount * points_rate)

                # Increment denormalized purchase counters
                customer.total_orders = (customer.total_orders or 0) + 1
                customer.total_value  = float(_r2(_d(customer.total_value or 0) + total_amount))

                previous_tier         = customer.loyalty_tier
                customer.loyalty_points += points_earned

                tier_thresholds = loyalty_cfg.get(
                    "tier_thresholds",
                    {"silver": 100, "gold": 500, "platinum": 1000},
                )
                new_tier = resolve_loyalty_tier(customer.loyalty_points, tier_thresholds)

                if new_tier != customer.loyalty_tier:
                    customer.loyalty_tier = new_tier
                    db.add(
                        SystemAlert(
                            id=uuid.uuid4(),
                            organization_id=user.organization_id,
                            branch_id=sale_data.branch_id,
                            alert_type="system_info",
                            severity="low",
                            title=(
                                f"Loyalty Tier Upgrade: "
                                f"{customer.first_name} {customer.last_name}"
                            ),
                            message=(
                                f"Customer upgraded from '{previous_tier}' to "
                                f"'{customer.loyalty_tier}' tier "
                                f"({customer.loyalty_points} points)."
                            ),
                            is_resolved=False,
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )

                customer.updated_at = datetime.now(timezone.utc)
                customer.mark_as_synced()

        # ----------------------------------------------------------------------
        # 19. Commit — outside the savepoint context
        # ----------------------------------------------------------------------
        await db.commit()

        # ----------------------------------------------------------------------
        # 20. Audit log — flush only, never commit
        # ----------------------------------------------------------------------
        await create_audit_log(
            db=db,
            action="process_sale",
            entity_type="Sale",
            entity_id=sale.id,
            user_id=user.id,
            organization_id=sale.organization_id,
            changes={
                "sale_number":              sale_number,
                "customer_id":              str(sale.customer_id) if sale.customer_id else None,
                "total_amount":             float(total_amount),
                "discount_amount":          float(total_discount),
                "items_count":              len(created_items),
                "payment_method":           sale.payment_method,
                "prescription_id":          str(sale.prescription_id) if sale.prescription_id else None,
                "loyalty_points_awarded":   points_earned,
                "batches_deducted":         batches_updated,
            },
        )

        # ----------------------------------------------------------------------
        # 21. Build and return response
        # ----------------------------------------------------------------------
        await db.refresh(sale)
        sale_with_details = await build_sale_with_details(db, sale)

        return ProcessSaleResponse(
            sale=sale_with_details,
            loyalty_points_awarded=points_earned,
            contract_applied=contract.contract_name,
            contract_discount_given=total_discount,
            estimated_savings=total_discount,
            inventory_updated=inventory_updated,
            batches_updated=batches_updated,
            success=True,
            message="Sale processed successfully.",
        )

    # =========================================================================
    # PUBLIC: Refund Sale
    # =========================================================================

    @staticmethod
    async def refund_sale(
        db: AsyncSession,
        sale_id: uuid.UUID,
        refund_data: RefundSaleRequest,
        user: User,
    ) -> RefundSaleResponse:
        """
        Refund a sale (full or partial).

        Steps
        -----
         1  Permission check.
         2  Load Sale (with items) FOR UPDATE; validate it is refundable.
         3  Validate refund items and quantities against the original sale.
         4  Update Sale.status → 'refunded'.
         5  Restore BranchInventory + DrugBatch under FOR UPDATE locks.
         6  Write StockAdjustment (type='return').
         7  Reverse loyalty points; recalculate tier downward.
         8  Commit.
         9  Append AuditLog (flush only).
        10  Return RefundSaleResponse.
        """
        if not user.has_permission("process_refunds"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to process refunds.",
            )

        async with db.begin_nested():
            sale_res = await db.execute(
                select(Sale)
                .options(selectinload(Sale.items))
                .where(
                    Sale.id == sale_id,
                    Sale.organization_id == user.organization_id,
                )
                .with_for_update()
            )
            sale = sale_res.scalar_one_or_none()
            if not sale:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Sale not found.",
                )

            if sale.status != "completed":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Cannot refund a sale with status '{sale.status}'. "
                        "Only completed sales may be refunded."
                    ),
                )

            refund_amount = _d(refund_data.refund_amount)
            if refund_amount > _d(sale.total_amount):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Refund amount ({refund_amount}) cannot exceed "
                        f"sale total ({sale.total_amount})."
                    ),
                )

            sale_item_map: Dict[uuid.UUID, SaleItem] = {
                item.id: item for item in sale.items
            }
            refund_item_ids = {ri.sale_item_id for ri in refund_data.items_to_refund}
            invalid = refund_item_ids - set(sale_item_map.keys())
            if invalid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Refund items not found in the original sale: {invalid}",
                )

            # Update sale record
            sale.status        = "refunded"
            sale.refund_amount = float(refund_amount)
            sale.refunded_at   = datetime.now(timezone.utc)
            sale.notes         = (
                f"Refunded: {refund_data.reason}\n\n{sale.notes or ''}".strip()
            )
            sale.updated_at = datetime.now(timezone.utc)
            sale.mark_as_synced()

            inventory_restored = 0
            batches_restored   = 0

            for refund_item in refund_data.items_to_refund:
                sale_item = sale_item_map[refund_item.sale_item_id]

                if refund_item.quantity > sale_item.quantity:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Cannot refund {refund_item.quantity} units of "
                            f"'{sale_item.drug_name}'. "
                            f"Only {sale_item.quantity} were sold."
                        ),
                    )

                # Restore BranchInventory
                inv_res = await db.execute(
                    select(BranchInventory)
                    .where(
                        BranchInventory.branch_id == sale.branch_id,
                        BranchInventory.drug_id   == sale_item.drug_id,
                    )
                    .with_for_update()
                )
                inventory          = inv_res.scalar_one()
                previous_qty       = inventory.quantity
                inventory.quantity += refund_item.quantity
                inventory.updated_at = datetime.now(timezone.utc)
                inventory.mark_as_synced()
                inventory_restored += 1

                # Restore to the original batch
                if sale_item.batch_id:
                    batch_res = await db.execute(
                        select(DrugBatch)
                        .where(
                            DrugBatch.id        == sale_item.batch_id,
                            DrugBatch.branch_id == sale.branch_id,
                        )
                        .with_for_update()
                    )
                    batch = batch_res.scalar_one_or_none()
                    if batch:
                        batch.remaining_quantity += refund_item.quantity
                        batch.updated_at          = datetime.now(timezone.utc)
                        batch.mark_as_synced()
                        batches_restored += 1

                # 'return' is the correct model-valid type for customer returns
                db.add(
                    StockAdjustment(
                        id=uuid.uuid4(),
                        branch_id=sale.branch_id,
                        drug_id=sale_item.drug_id,
                        adjustment_type="return",
                        quantity_change=refund_item.quantity,
                        previous_quantity=previous_qty,
                        new_quantity=inventory.quantity,
                        reason=(
                            f"Refund for sale {sale.sale_number}: "
                            f"{refund_data.reason}"
                        ),
                        adjusted_by=user.id,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )

            # Reverse loyalty points + recalculate tier
            loyalty_points_deducted = 0
            if sale.customer_id:
                cust_res = await db.execute(
                    select(Customer)
                    .where(Customer.id == sale.customer_id)
                    .with_for_update()
                )
                customer = cust_res.scalar_one_or_none()

                if customer:
                    org_res = await db.execute(
                        select(Organization).where(
                            Organization.id == sale.organization_id
                        )
                    )
                    organization = org_res.scalar_one()

                    loyalty_cfg      = organization.settings.get("loyalty", {})
                    points_rate      = _d(loyalty_cfg.get("points_per_unit", "1.0"))
                    points_to_deduct = int(refund_amount * points_rate)

                    customer.loyalty_points = max(0, customer.loyalty_points - points_to_deduct)

                    tier_thresholds = loyalty_cfg.get(
                        "tier_thresholds",
                        {"silver": 100, "gold": 500, "platinum": 1000},
                    )
                    customer.loyalty_tier = resolve_loyalty_tier(
                        customer.loyalty_points, tier_thresholds
                    )
                    loyalty_points_deducted = points_to_deduct
                    customer.updated_at     = datetime.now(timezone.utc)
                    customer.mark_as_synced()

        # 8. Commit
        await db.commit()

        # 9. Audit log — flush only
        await create_audit_log(
            db=db,
            action="refund_sale",
            entity_type="Sale",
            entity_id=sale.id,
            user_id=user.id,
            organization_id=sale.organization_id,
            changes={
                "refund_amount":            float(refund_amount),
                "reason":                   refund_data.reason,
                "inventory_restored":       inventory_restored,
                "batches_restored":         batches_restored,
                "loyalty_points_deducted":  loyalty_points_deducted,
            },
        )

        # 10. Build and return response
        await db.refresh(sale)
        sale_with_details = await build_sale_with_details(db, sale)

        return RefundSaleResponse(
            sale=sale_with_details,
            refund_id=uuid.uuid4(),
            refund_amount=refund_amount,
            refund_method=refund_data.refund_method,
            inventory_restored=inventory_restored,
            batches_restored=batches_restored,
            loyalty_points_deducted=loyalty_points_deducted,
            success=True,
            message="Sale refunded successfully.",
        )


# Convenience re-export so callers that already import from this module
# don't need to change their import paths for the helper functions.
from sqlalchemy.orm import selectinload  # noqa: E402 (used above in refund_sale)
