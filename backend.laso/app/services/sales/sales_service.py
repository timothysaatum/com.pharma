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
import logging
from typing import Any, Dict, List, Optional, Tuple
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
from app.models.prescriptions.prescription_model import Prescription
from app.models.sales.sales_model import Sale, SaleItem, SaleItemBatchAllocation
from app.models.system_md.sys_models import SystemAlert
from app.models.user.user_model import Permission, User
from app.services.inventory.inventory_service import InventoryService
from app.schemas.sales_schemas import (
    ProcessSaleResponse,
    RefundSaleRequest,
    RefundSaleResponse,
    SaleCreate,
    SaleItemCreate,
)

logger = logging.getLogger(__name__)


def _coerce_uuid(value) -> Optional[uuid.UUID]:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_positive_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _prescription_drug_limits(
    prescription: Prescription,
) -> Dict[uuid.UUID, Optional[int]]:
    """
    Extract prescribed drug quantities from the prescription JSON payload.

    Quantity is optional because historical records may only store the drug.
    When present, it is enforced as the maximum dispense quantity for the sale.
    """
    limits: Dict[uuid.UUID, Optional[int]] = {}
    quantity_keys = (
        "quantity",
        "qty",
        "dispense_quantity",
        "dispenseQuantity",
        "quantity_prescribed",
        "quantityPrescribed",
    )

    for medication in prescription.medications or []:
        if not isinstance(medication, dict):
            continue
        drug_id = _coerce_uuid(
            medication.get("drug_id")
            or medication.get("drugId")
            or medication.get("id")
        )
        if not drug_id:
            continue

        quantity = None
        for key in quantity_keys:
            quantity = _coerce_positive_int(medication.get(key))
            if quantity is not None:
                break

        existing = limits.get(drug_id)
        if existing is None or quantity is None:
            limits[drug_id] = quantity
        else:
            limits[drug_id] = max(existing, quantity)

    return limits


def _validate_stock_and_prescription_items(
    *,
    sale_items: List[SaleItemCreate],
    drugs: Dict[uuid.UUID, Drug],
    prescription: Optional[Prescription],
    inventories: Dict[uuid.UUID, BranchInventory],
    batch_available_by_drug: Dict[uuid.UUID, int],
) -> None:
    """
    Combined stock + prescription validation for ALL items.

    Runs stock checks (inventory exists, qty <= available, qty <= batch
    available) and prescription checks (Rx-only drugs have valid Rx) for
    every item, collecting ALL errors before raising.

    Both checks run per-item so the user gets a single response listing every
    failing item (e.g. ``Drug A: insufficient stock; Drug B: prescription
    required``).
    """
    rx_limits = _prescription_drug_limits(prescription) if prescription else {}
    errors: List[Dict[str, Any]] = []

    for item in sale_items:
        drug = drugs[item.drug_id]

        # --- Stock validation ---
        inventory = inventories.get(item.drug_id)
        if not inventory:
            errors.append({
                "drug_id": str(item.drug_id),
                "drug_name": drug.name,
                "error": (
                    f"No inventory record for '{drug.name}' at this branch. "
                    "Stock must be received first."
                ),
                "error_type": "no_inventory",
            })
        else:
            available = inventory.quantity - inventory.reserved_quantity
            if available < item.quantity:
                errors.append({
                    "drug_id": str(item.drug_id),
                    "drug_name": drug.name,
                    "error": (
                        f"Insufficient stock for '{drug.name}'. "
                        f"Available: {available}, Requested: {item.quantity}."
                    ),
                    "error_type": "insufficient_stock",
                })

            batch_available = batch_available_by_drug.get(item.drug_id, 0)
            if batch_available < item.quantity:
                errors.append({
                    "drug_id": str(item.drug_id),
                    "drug_name": drug.name,
                    "error": (
                        f"Insufficient non-expired batch stock for "
                        f"'{drug.name}'. Available in batches: "
                        f"{batch_available}, Requested: {item.quantity}."
                    ),
                    "error_type": "insufficient_batch_stock",
                })

        # --- Prescription validation ---
        if drug.requires_prescription:
            if not prescription:
                errors.append({
                    "drug_id": str(item.drug_id),
                    "drug_name": drug.name,
                    "error": (
                        f"'{drug.name}' requires a valid prescription. "
                        "Provide a prescription_id or remove this item."
                    ),
                    "error_type": "prescription_required",
                })
            elif item.drug_id not in rx_limits:
                errors.append({
                    "drug_id": str(item.drug_id),
                    "drug_name": drug.name,
                    "error": (
                        f"Prescription {prescription.prescription_number} does "
                        f"not include '{drug.name}'."
                    ),
                    "error_type": "prescription_drug_mismatch",
                })
            else:
                allowed_quantity = rx_limits[item.drug_id]
                if allowed_quantity is not None and item.quantity > allowed_quantity:
                    errors.append({
                        "drug_id": str(item.drug_id),
                        "drug_name": drug.name,
                        "error": (
                            f"Requested quantity for '{drug.name}' exceeds the "
                            f"prescribed quantity ({allowed_quantity})."
                        ),
                        "error_type": "prescription_quantity_exceeded",
                    })

    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"Sale validation failed for {len(errors)} item(s).",
                "errors": errors,
            },
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
    DEFAULT_LOYALTY_THRESHOLDS,
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
 6  Pre-load inventory + FEFO batches (row-level locks held from here).
 7  Combined stock + prescription validation (collects ALL errors per-item).
 8  Load and validate PriceContract; load per-drug overrides.
 9  Resolve unit prices from already-loaded data.
10  Reserve inventory with row-level locks (rollback-safe accumulator).
11  Compute per-item and sale-level pricing.
12  Validate contract purchase limits.
13  Validate payment sufficiency.
14  Persist Sale record (model-aligned fields only).
15  Persist SaleItem records (model-aligned fields only).
16  Deduct inventory via FEFO — multi-batch safe with FOR UPDATE locks.
17  Write StockAdjustment (type='correction') + system alerts.
18  Decrement prescription refills.
19  Award loyalty points; recalculate tier.
20  Commit.
21  Append AuditLog (flush only — no commit).
22  Return ProcessSaleResponse.
        """
        async with db.begin_nested():  # savepoint — full rollback on any exception

            # ------------------------------------------------------------------
            # 1. Branch access
            # ------------------------------------------------------------------
            if (
                str(sale_data.branch_id) not in {str(b) for b in (user.assigned_branches or [])}
                and not user.is_super_admin
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not assigned to this branch.",
                )

            branch_res = await db.execute(
                select(Branch)
                .where(
                    Branch.id == sale_data.branch_id,
                    Branch.organization_id == user.organization_id,
                    Branch.is_deleted == False,
                    Branch.is_active == True,
                )
                .with_for_update()
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

                # Verify prescription belongs to the sale's branch
                if str(prescription.branch_id) != str(sale_data.branch_id):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Prescription belongs to a different branch.",
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

                if not (user.is_super_admin or user.has_permission("manage_prescriptions")):
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
            # 6. Pre-load inventory + FEFO batches (row-level locks held from here)
            # ------------------------------------------------------------------
            fefo_batches: Dict[uuid.UUID, List[DrugBatch]] = await load_fefo_batches(
                db=db,
                branch_id=sale_data.branch_id,
                drug_ids=drug_ids,
            )

            inventory_res = await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == sale_data.branch_id,
                    BranchInventory.drug_id.in_(drug_ids),
                )
                .with_for_update()
            )
            inventories: Dict[uuid.UUID, BranchInventory] = {
                inventory.drug_id: inventory
                for inventory in inventory_res.scalars().all()
            }
            branch_prices: Dict[uuid.UUID, Decimal] = {
                drug_id: Decimal(str(inventory.selling_price))
                for drug_id, inventory in inventories.items()
                if inventory.selling_price is not None
            }
            batch_available_by_drug: Dict[uuid.UUID, int] = {
                drug_id: sum(batch.remaining_quantity for batch in batches)
                for drug_id, batches in fefo_batches.items()
            }

            # ------------------------------------------------------------------
            # 7. Combined stock + prescription validation (collects ALL errors)
            # ------------------------------------------------------------------
            _validate_stock_and_prescription_items(
                sale_items=sale_data.items,
                drugs=drugs,
                prescription=prescription,
                inventories=inventories,
                batch_available_by_drug=batch_available_by_drug,
            )

            # ------------------------------------------------------------------
            # 8. Contract — load, validate, fetch per-drug overrides
            # ------------------------------------------------------------------
            contract, contract_items, verification_confirmed = await load_and_validate_contract(
                db=db,
                contract_id=sale_data.price_contract_id,
                branch_id=sale_data.branch_id,
                drug_ids=drug_ids,
                user=user,
                insurance_verified=getattr(sale_data, "insurance_verified", False),
                contract_verification_token=getattr(
                    sale_data, "contract_verification_token", None
                ),
                insurance_preauth_number=getattr(
                    sale_data, "insurance_preauth_number", None
                ),
                customer_id=sale_data.customer_id,
            )

            # ------------------------------------------------------------------
            # 9. Unit price resolution (inventory + batches already loaded)
            #
            # Priority: BranchInventory.selling_price → DrugBatch.selling_price → Drug.unit_price
            # cost_price is NEVER used — it is the pharmacy's acquisition cost.
            # ------------------------------------------------------------------
            resolved_prices: Dict[uuid.UUID, Decimal] = {
                item.drug_id: resolve_unit_price(
                    drug=drugs[item.drug_id],
                    batches=fefo_batches.get(item.drug_id, []),
                    branch_selling_price=branch_prices.get(item.drug_id),
                )
                for item in sale_data.items
            }

            # ------------------------------------------------------------------
            # 10. Reserve inventory — row-level locks; rollback-safe accumulator
            # ------------------------------------------------------------------
            reservations: List[Tuple[BranchInventory, int]] = []
            try:
                for item in sale_data.items:
                    drug = drugs[item.drug_id]
                    inventory = inventories.get(item.drug_id)

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

                    batch_available = batch_available_by_drug.get(item.drug_id, 0)
                    if batch_available < item.quantity:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Insufficient non-expired batch stock for "
                                f"'{drug.name}'. Available in batches: "
                                f"{batch_available}, Requested: {item.quantity}."
                            ),
                        )

                    inventory.reserved_quantity += item.quantity
                    inventory.mark_as_pending_sync()
                    reservations.append((inventory, item.quantity))

            except HTTPException:
                # Unwind any reservations already applied before re-raising
                for inv, qty in reservations:
                    inv.reserved_quantity -= qty
                raise

            # ------------------------------------------------------------------
            # 11. Per-item pricing
            # ------------------------------------------------------------------
            tax_inclusive = bool(organization.settings.get("tax_inclusive", False))

            item_pricing = compute_item_pricing(
                items=sale_data.items,
                drugs=drugs,
                contract=contract,
                contract_items=contract_items,
                resolved_prices=resolved_prices,
                tax_inclusive=tax_inclusive,
            )

            subtotal       = _r2(sum((p["item_subtotal"] for p in item_pricing), Decimal("0")))
            total_discount = _r2(sum((p["discount_amount"] for p in item_pricing), Decimal("0")))
            total_tax      = _r2(sum((p["tax_amount"]      for p in item_pricing), Decimal("0")))
            if tax_inclusive:
                total_amount = _r2(subtotal - total_discount)
            else:
                total_amount = _r2(subtotal - total_discount + total_tax)

            # Track ProcessSaleResponse fields
            alert_count_low_stock  = 0
            alert_count_expiry     = 0
            loyalty_tier_upgraded  = False
            new_loyalty_tier_value = None

            # ------------------------------------------------------------------
            # 12. Contract purchase limits
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
            # 13. Payment
            # ------------------------------------------------------------------
            amount_due = (
                patient_copay_amount
                if contract.contract_type == "insurance" and patient_copay_amount is not None
                else total_amount
            )
            if sale_data.payment_method == "split":
                if not sale_data.split_payment_details:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="split_payment_details are required for split payments.",
                    )
                split_total = _r2(
                    sum(
                        (_d(amount) for amount in sale_data.split_payment_details.values()),
                        Decimal("0"),
                    )
                )
                if split_total != amount_due:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Split payment total ({split_total}) must equal "
                            f"amount due ({amount_due})."
                        ),
                    )
                if (
                    sale_data.amount_paid is not None
                    and _r2(_d(sale_data.amount_paid)) != split_total
                ):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "amount_paid must match split_payment_details total "
                            "for split payments."
                        ),
                    )
                amount_paid = split_total
            else:
                if sale_data.split_payment_details:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="split_payment_details are only allowed when payment_method is 'split'.",
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
            # 14. Persist Sale — only model-defined fields
            # ------------------------------------------------------------------
            sale_number = await generate_sale_number(db, branch.code)

            sale = Sale(
                id=uuid.uuid4(),
                organization_id=user.organization_id,
                branch_id=sale_data.branch_id,
                sale_number=sale_number,
                customer_id=sale_data.customer_id,
                customer_name=sale_data.customer_name,
                # Financials — cast via str to avoid float→Decimal precision traps
                subtotal=float(str(subtotal)),
                discount_amount=float(str(total_discount)),
                tax_amount=float(str(total_tax)),
                total_amount=float(str(total_amount)),
                # Contract snapshot
                price_contract_id=contract.id,
                contract_name=contract.contract_name,
                contract_discount_percentage=float(contract.discount_percentage),
                contract_type=contract.contract_type,
                # Payment
                payment_method=sale_data.payment_method,
                payment_status="completed",
                amount_paid=float(amount_paid),
                change_amount=float(change_amount),
                payment_reference=getattr(sale_data, "payment_reference", None),
                split_payment_details=getattr(sale_data, "split_payment_details", None),
                # Insurance
                insurance_claim_number=getattr(sale_data, "insurance_claim_number", None),
                insurance_preauth_number=getattr(sale_data, "insurance_preauth_number", None),
                patient_copay_amount=(
                    float(patient_copay_amount) if patient_copay_amount is not None else None
                ),
                insurance_covered_amount=(
                    float(insurance_covered_amount) if insurance_covered_amount is not None else None
                ),
                insurance_verified=(
                    verification_confirmed and contract.contract_type == "insurance"
                ),
                insurance_verified_at=(
                    datetime.now(timezone.utc)
                    if verification_confirmed and contract.contract_type == "insurance"
                    else None
                ),
                insurance_verified_by=(
                    user.id
                    if verification_confirmed and contract.contract_type == "insurance"
                    else None
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
            # 15. Persist SaleItems — only model-defined fields
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
                    refunded_quantity=0,
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
            # 16. FEFO inventory deduction — multi-batch safe
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
                inventory = inventories[sale_item.drug_id]
                previous_qty = inventory.quantity
                running_inventory_qty = previous_qty

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
                    previous_batch_qty = batch.remaining_quantity

                    if primary_batch_id is None:
                        primary_batch_id = batch.id

                    batch.remaining_quantity -= take
                    batch.updated_at          = datetime.now(timezone.utc)
                    batch.mark_as_pending_sync()
                    qty_to_deduct  -= take
                    batches_updated += 1

                    db.add(
                        SaleItemBatchAllocation(
                            id=uuid.uuid4(),
                            sale_item_id=sale_item.id,
                            branch_id=sale_data.branch_id,
                            drug_id=sale_item.drug_id,
                            batch_id=batch.id,
                            batch_number=batch.batch_number,
                            batch_expiry_date=batch.expiry_date,
                            quantity=take,
                            refunded_quantity=0,
                            unit_cost_at_sale=batch.cost_price,
                            unit_price_at_sale=sale_item.unit_price,
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )

                    movement_before = running_inventory_qty
                    running_inventory_qty -= take
                    await InventoryService._record_inventory_movement(
                        db=db,
                        organization_id=user.organization_id,
                        branch_id=sale_data.branch_id,
                        drug_id=sale_item.drug_id,
                        movement_type="sale",
                        quantity_change=-take,
                        quantity_before=movement_before,
                        quantity_after=running_inventory_qty,
                        batch_id=batch.id,
                        batch_quantity_before=previous_batch_qty,
                        batch_quantity_after=batch.remaining_quantity,
                        unit_cost=(
                            Decimal(str(batch.cost_price))
                            if batch.cost_price is not None
                            else None
                        ),
                        unit_price=Decimal(str(sale_item.unit_price)),
                        source_type="sale",
                        source_id=sale.id,
                        source_line_id=sale_item.id,
                        reference_number=sale_number,
                        reason=f"Sale {sale_number}",
                        created_by=user.id,
                    )

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
                inventory.quantity           = running_inventory_qty
                inventory.reserved_quantity -= sale_item.quantity  # release
                inventory.updated_at         = datetime.now(timezone.utc)
                inventory.mark_as_pending_sync()
                inventory_updated += 1

                # ------------------------------------------------------------------
                # 17. StockAdjustment + system alerts
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
                    alert_count_low_stock += 1
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
                    alert_count_expiry += 1
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
            # 18. Prescription refill decrement
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
                prescription.mark_as_pending_sync()

            # ------------------------------------------------------------------
            # 19. Loyalty points + tier recalculation
            #     Only award points when the loyalty program is enabled.
            #     Also increment denormalized purchase counters so
            #     CustomerService._build_with_details always has fresh data
            #     even if the live Sale aggregate query has a type issue.
            # ------------------------------------------------------------------
            points_earned = 0
            loyalty_enabled = bool(organization.settings.get("enable_loyalty_program", False))
            if customer and loyalty_enabled:
                loyalty_cfg   = organization.settings.get("loyalty", {})
                points_rate   = _d(loyalty_cfg.get("points_per_unit", "1.0"))
                points_earned = int(total_amount * points_rate)

                # Increment denormalized purchase counters
                customer.total_orders = (customer.total_orders or 0) + 1
                customer.total_value  = _r2(_d(customer.total_value or 0) + total_amount)

                previous_tier         = customer.loyalty_tier
                customer.loyalty_points += points_earned

                tier_thresholds = loyalty_cfg.get(
                    "tier_thresholds",
                    DEFAULT_LOYALTY_THRESHOLDS,
                )
                new_tier = resolve_loyalty_tier(customer.loyalty_points, tier_thresholds)

                if new_tier != previous_tier:
                    customer.loyalty_tier  = new_tier
                    loyalty_tier_upgraded   = True
                    new_loyalty_tier_value  = new_tier
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
                customer.mark_as_pending_sync()

            # Always increment purchase counters even when loyalty is off
            elif customer:
                customer.total_orders = (customer.total_orders or 0) + 1
                customer.total_value  = _r2(_d(customer.total_value or 0) + total_amount)
                customer.updated_at = datetime.now(timezone.utc)
                customer.mark_as_pending_sync()

            contract.total_transactions = (contract.total_transactions or 0) + 1
            contract.total_discount_given = _r2(
                _d(contract.total_discount_given or 0) + total_discount
            )
            contract.last_used_at = datetime.now(timezone.utc)
            contract.updated_at = datetime.now(timezone.utc)
            contract.mark_as_pending_sync()

        # ----------------------------------------------------------------------
        # 20. Commit — outside the savepoint context
        # ----------------------------------------------------------------------
        await db.commit()

        # ----------------------------------------------------------------------
        # 21. Audit log — persisted separately so it cannot roll back the sale
        # ----------------------------------------------------------------------
        try:
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
            await db.commit()
        except Exception:
            logger.exception("Failed to write process_sale audit log for sale %s", sale.id)
            await db.rollback()

        # ----------------------------------------------------------------------
        # 22. Build and return response
        # ----------------------------------------------------------------------
        await db.refresh(sale)
        sale_with_details = await build_sale_with_details(db, sale)

        return ProcessSaleResponse(
            sale=sale_with_details,
            loyalty_points_awarded=points_earned,
            loyalty_tier_upgraded=loyalty_tier_upgraded,
            new_loyalty_tier=new_loyalty_tier_value,
            contract_applied=contract.contract_name,
            contract_discount_given=total_discount,
            estimated_savings=total_discount,
            inventory_updated=inventory_updated,
            batches_updated=batches_updated,
            low_stock_alerts_created=alert_count_low_stock,
            expiry_alerts_created=alert_count_expiry,
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
         4  Update cumulative refund state.
         5  Restore BranchInventory + DrugBatch under FOR UPDATE locks.
         6  Write StockAdjustment (type='return').
         7  Reverse loyalty points; recalculate tier downward.
         8  Commit.
         9  Append AuditLog (flush only).
        10  Return RefundSaleResponse.
        """

        if not user.has_permission(Permission.PROCESS_REFUNDS):
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

            # Branch access check
            assigned = [str(b) for b in (user.assigned_branches or [])]
            if str(sale.branch_id) not in assigned and not user.is_super_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have access to this sale's branch",
                )

            if sale.status not in ("completed", "partially_refunded"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Cannot refund a sale with status '{sale.status}'. "
                        "Only completed or partially refunded sales may be refunded."
                    ),
                )

            refund_amount = _r2(_d(refund_data.refund_amount))
            existing_refund_amount = _r2(_d(sale.refund_amount or 0))
            sale_total = _r2(_d(sale.total_amount))
            new_refund_total = _r2(existing_refund_amount + refund_amount)
            if new_refund_total > sale_total:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Refund total ({new_refund_total}) cannot exceed "
                        f"sale total ({sale_total}). Already refunded: "
                        f"{existing_refund_amount}."
                    ),
                )

            # Manager approval check
            if refund_data.manager_approval_user_id == user.id:
                approver = user
            else:
                approver_res = await db.execute(
                    select(User).where(
                        User.id == refund_data.manager_approval_user_id,
                        User.organization_id == user.organization_id,
                    ).options(selectinload(User.roles))
                )
                approver = approver_res.scalar_one_or_none()
                if not approver:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Approving manager not found.",
                    )

            if not (
                approver.is_super_admin
                or approver.has_permission(Permission.PROCESS_REFUNDS)
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Approving manager does not have required permissions.",
                )

            # Lock SaleItem rows to prevent concurrent refund races
            sale_item_res = await db.execute(
                select(SaleItem)
                .where(SaleItem.sale_id == sale.id)
                .with_for_update()
            )
            locked_items = list(sale_item_res.scalars().all())

            sale_item_map: Dict[uuid.UUID, SaleItem] = {
                item.id: item for item in locked_items
            }
            refund_item_ids = {ri.sale_item_id for ri in refund_data.items_to_refund}
            invalid = refund_item_ids - set(sale_item_map.keys())
            if invalid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Refund items not found in the original sale: {invalid}",
                )

            for refund_item in refund_data.items_to_refund:
                sale_item = sale_item_map[refund_item.sale_item_id]
                already_refunded = int(sale_item.refunded_quantity or 0)
                remaining_quantity = sale_item.quantity - already_refunded
                if refund_item.quantity > remaining_quantity:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Cannot refund {refund_item.quantity} units of "
                            f"'{sale_item.drug_name}'. Remaining refundable "
                            f"quantity: {remaining_quantity}."
                        ),
                    )

            expected_refund = _r2(
                sum(
                    (
                        _d(sale_item_map[item.sale_item_id].total_price)
                        * Decimal(item.quantity)
                        / Decimal(sale_item_map[item.sale_item_id].quantity)
                    )
                    for item in refund_data.items_to_refund
                )
            )
            if abs(refund_amount - expected_refund) > Decimal('0.005'):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Refund amount ({refund_amount}) must equal the selected item value "
                        f"({expected_refund})."
                    ),
                )

            # Update sale record with refund tracking.
            is_full_refund = new_refund_total == sale_total
            sale.status        = "refunded" if is_full_refund else "partially_refunded"
            sale.payment_status = "refunded" if is_full_refund else "partial"
            sale.refund_amount = new_refund_total
            sale.refunded_at   = datetime.now(timezone.utc)
            sale.refunded_by   = user.id
            sale.refund_reason = refund_data.reason
            sale.refund_reference = f"REF-{sale.sale_number}-{str(uuid.uuid4())[:8]}"
            sale.notes         = (
                f"Refunded: {refund_data.reason}\n\n{sale.notes or ''}".strip()
            )
            sale.updated_at = datetime.now(timezone.utc)
            sale.mark_as_pending_sync()

            # Restore prescription refill if this sale was prescription-linked
            if sale.prescription_id:
                presc_res = await db.execute(
                    select(Prescription)
                    .where(
                        Prescription.id == sale.prescription_id,
                        Prescription.organization_id == sale.organization_id,
                    )
                    .with_for_update()
                )
                prescription = presc_res.scalar_one_or_none()
                if prescription and prescription.refills_remaining < prescription.refills_allowed:
                    prescription.refills_remaining += 1
                    prescription.status = "active"
                    prescription.updated_at = datetime.now(timezone.utc)
                    prescription.mark_as_pending_sync()

            inventory_restored = 0
            batches_restored   = 0

            for refund_item in refund_data.items_to_refund:
                sale_item = sale_item_map[refund_item.sale_item_id]
                sale_item.refunded_quantity = int(
                    sale_item.refunded_quantity or 0
                ) + refund_item.quantity
                sale_item.updated_at = datetime.now(timezone.utc)

                allocations_res = await db.execute(
                    select(SaleItemBatchAllocation)
                    .where(SaleItemBatchAllocation.sale_item_id == sale_item.id)
                    .order_by(SaleItemBatchAllocation.created_at.asc())
                    .with_for_update()
                )
                allocations = list(allocations_res.scalars().all())

                restore_targets = []
                qty_to_allocate = refund_item.quantity
                for allocation in allocations:
                    if qty_to_allocate <= 0:
                        break
                    allocation_remaining = (
                        allocation.quantity - int(allocation.refunded_quantity or 0)
                    )
                    if allocation_remaining <= 0:
                        continue
                    consume_qty = min(allocation_remaining, qty_to_allocate)
                    allocation.refunded_quantity = int(
                        allocation.refunded_quantity or 0
                    ) + consume_qty
                    allocation.updated_at = datetime.now(timezone.utc)
                    restore_targets.append(
                        {
                            "batch_id": allocation.batch_id,
                            "batch_number": allocation.batch_number,
                            "batch_expiry_date": allocation.batch_expiry_date,
                            "quantity": consume_qty,
                        }
                    )
                    qty_to_allocate -= consume_qty

                if allocations and qty_to_allocate > 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Refund quantity exceeds remaining sale batch allocations.",
                    )

                if not allocations:
                    restore_targets = [
                        {
                            "batch_id": sale_item.batch_id,
                            "batch_number": None,
                            "batch_expiry_date": None,
                            "quantity": refund_item.quantity,
                        }
                    ]

                if not refund_item.restock:
                    continue

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
                running_inventory_qty = previous_qty
                inventory_restored += 1

                qty_to_restore = refund_item.quantity
                for target in restore_targets:
                    if qty_to_restore <= 0:
                        break

                    restore_qty = min(int(target["quantity"]), qty_to_restore)
                    batch = None
                    if target["batch_id"]:
                        batch_res = await db.execute(
                            select(DrugBatch)
                            .where(
                                DrugBatch.id == target["batch_id"],
                                DrugBatch.branch_id == sale.branch_id,
                            )
                            .with_for_update()
                        )
                        batch = batch_res.scalar_one_or_none()

                    if batch is None:
                        batch = DrugBatch(
                            id=uuid.uuid4(),
                            branch_id=sale.branch_id,
                            drug_id=sale_item.drug_id,
                            batch_number=(
                                target["batch_number"]
                                or f"RETURN-{sale.sale_number}-{str(sale_item.id)[:8]}"
                            ),
                            quantity=restore_qty,
                            remaining_quantity=restore_qty,
                            expiry_date=(
                                target["batch_expiry_date"]
                                or date.today() + timedelta(days=365 * 10)
                            ),
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                        db.add(batch)
                        await db.flush()

                    previous_batch_qty = batch.remaining_quantity
                    batch.remaining_quantity += restore_qty
                    if batch.remaining_quantity > batch.quantity:
                        batch.quantity = batch.remaining_quantity
                    batch.updated_at = datetime.now(timezone.utc)
                    batch.mark_as_pending_sync()
                    batches_restored += 1

                    movement_before = running_inventory_qty
                    running_inventory_qty += restore_qty
                    await InventoryService._record_inventory_movement(
                        db=db,
                        organization_id=sale.organization_id,
                        branch_id=sale.branch_id,
                        drug_id=sale_item.drug_id,
                        movement_type="refund",
                        quantity_change=restore_qty,
                        quantity_before=movement_before,
                        quantity_after=running_inventory_qty,
                        batch_id=batch.id,
                        batch_quantity_before=previous_batch_qty,
                        batch_quantity_after=batch.remaining_quantity,
                        unit_cost=(
                            Decimal(str(batch.cost_price))
                            if batch.cost_price is not None
                            else None
                        ),
                        unit_price=Decimal(str(sale_item.unit_price)),
                        source_type="sale",
                        source_id=sale.id,
                        source_line_id=sale_item.id,
                        reference_number=sale.sale_number,
                        reason=f"Refund for sale {sale.sale_number}: {refund_data.reason}",
                        created_by=user.id,
                    )
                    qty_to_restore -= restore_qty

                if qty_to_restore > 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Refund restock quantity could not be allocated to sale batches.",
                    )

                inventory.quantity = running_inventory_qty
                inventory.updated_at = datetime.now(timezone.utc)
                inventory.mark_as_pending_sync()

                # 'return' is the correct model-valid type for customer returns
                db.add(
                    StockAdjustment(
                        id=uuid.uuid4(),
                        branch_id=sale.branch_id,
                        drug_id=sale_item.drug_id,
                        adjustment_type="return",
                        quantity_change=refund_item.quantity,
                        previous_quantity=previous_qty,
                        new_quantity=running_inventory_qty,
                        reason=(
                            f"Refund for sale {sale.sale_number}: "
                            f"{refund_data.reason}"
                        ),
                        adjusted_by=user.id,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )

                # Recalculate and resolve alerts
                await InventoryService._recalculate_inventory_quantity(
                    db=db,
                    branch_id=sale.branch_id,
                    drug_id=sale_item.drug_id,
                )

            # Reverse loyalty points + recalculate tier
            loyalty_points_deducted = 0
            if sale.customer_id:
                cust_res = await db.execute(
                    select(Customer)
                    .where(
                        Customer.id == sale.customer_id,
                        Customer.organization_id == sale.organization_id,
                    )
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

                    loyalty_enabled = bool(organization.settings.get("enable_loyalty_program", False))
                    if loyalty_enabled:
                        loyalty_cfg      = organization.settings.get("loyalty", {})
                        points_rate      = _d(loyalty_cfg.get("points_per_unit", "1.0"))
                        points_to_deduct = int(refund_amount * points_rate)

                        customer.loyalty_points = max(0, customer.loyalty_points - points_to_deduct)

                        tier_thresholds = loyalty_cfg.get(
                            "tier_thresholds",
                            DEFAULT_LOYALTY_THRESHOLDS,
                        )
                        customer.loyalty_tier = resolve_loyalty_tier(
                            customer.loyalty_points, tier_thresholds
                        )
                        loyalty_points_deducted = points_to_deduct
                    customer.updated_at     = datetime.now(timezone.utc)
                    customer.mark_as_pending_sync()

        # 8. Commit
        await db.commit()

        # 9. Audit log — persisted separately so it cannot roll back the refund
        try:
            await create_audit_log(
                db=db,
                action="refund_sale",
                entity_type="Sale",
                entity_id=sale.id,
                user_id=user.id,
                organization_id=sale.organization_id,
                changes={
                    "previous_refund_amount":   float(existing_refund_amount),
                    "refund_amount":            float(refund_amount),
                    "new_refund_total":         float(new_refund_total),
                    "is_full_refund":           is_full_refund,
                    "reason":                   refund_data.reason,
                    "inventory_restored":       inventory_restored,
                    "batches_restored":         batches_restored,
                    "loyalty_points_deducted":  loyalty_points_deducted,
                },
            )
            await db.commit()
        except Exception:
            logger.exception("Failed to write refund_sale audit log for sale %s", sale.id)
            await db.rollback()

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
