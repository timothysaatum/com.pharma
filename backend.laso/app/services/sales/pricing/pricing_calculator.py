"""
Pricing Calculator
==================
Pure pricing logic for the sales pipeline.

Responsibilities
----------------
* Resolve the correct selling unit price for each drug from its FEFO batch.
* Compute the full per-item pricing breakdown (subtotal, discount, tax, total)
  by applying PriceContract rules including exclusions, overrides, caps, floors,
  and insurance copay.

No database I/O happens here — all inputs are pre-loaded ORM objects.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional
import uuid

from app.models.inventory.branch_inventory import DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pricing.pricing_model import PriceContract, PriceContractItem
from app.schemas.sales_schemas import SaleItemCreate

# ---------------------------------------------------------------------------
# Shared decimal helpers — imported by other modules in this package
# ---------------------------------------------------------------------------

_TWO_PLACES = Decimal("0.01")


def d(value) -> Decimal:
    """Cast any numeric value to Decimal safely."""
    return Decimal(str(value))


def r2(value: Decimal) -> Decimal:
    """Round a Decimal to 2 decimal places (ROUND_HALF_UP)."""
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Unit price resolution
# ---------------------------------------------------------------------------


def resolve_unit_price(
    drug: Drug,
    batches: List[DrugBatch],
    branch_selling_price: Decimal | None = None,
) -> Decimal:
    """
    Resolve the selling unit price for a single drug at the point of sale.

    Priority
    --------
    1. BranchInventory.selling_price if this branch has a price override.
    2. ``DrugBatch.selling_price`` on the earliest-expiry (FEFO) batch.
       Each batch can carry its own selling price set at goods receipt, so the
       pharmacy can charge different amounts for different stock deliveries.
    2. ``Drug.unit_price`` — the catalog default price.

    ``DrugBatch.cost_price`` is the pharmacy's acquisition cost (what they paid
    the supplier) and is **never** used as a customer-facing price.

    Args:
        drug:    Drug ORM instance from the catalog.
        batches: FEFO-ordered DrugBatch list (earliest expiry first).
                 Pass an empty list when no valid batches exist — the function
                 falls through to the catalog price.

    Returns:
        Resolved selling price as ``Decimal``.
    """
    if branch_selling_price is not None:
        return d(branch_selling_price)

    if batches:
        primary = batches[0]  # earliest-expiry batch (FEFO primary)
        if primary.selling_price is not None:
            return d(primary.selling_price)

    # Fallback: catalog / default unit price
    return d(drug.unit_price)


# ---------------------------------------------------------------------------
# Per-item pricing
# ---------------------------------------------------------------------------


def compute_item_pricing(
    items: List[SaleItemCreate],
    drugs: Dict[uuid.UUID, Drug],
    contract: PriceContract,
    contract_items: Dict[uuid.UUID, PriceContractItem],
    resolved_prices: Dict[uuid.UUID, Decimal],
) -> List[Dict]:
    """
    Compute the complete pricing breakdown for every line item.

    Discount resolution order (per drug)
    -------------------------------------
    Exclusions (→ 0 % discount):
      1. PriceContractItem.is_excluded is True
      2. Drug ID in contract.excluded_drug_ids
      3. Drug's category_id in contract.excluded_drug_categories
      4. contract.applies_to_prescription_only=True  and drug is OTC
      5. contract.applies_to_otc=False               and drug is OTC

    Amount (if not excluded):
      6. PriceContractItem.fixed_price             → derive implied % and amount
      7. PriceContractItem.override_discount_percentage
      8. contract.discount_percentage              (contract default)

    Caps and floors (applied after base discount):
      - contract.maximum_discount_amount  → cap per-item discount amount
      - contract.minimum_price_override   → floor effective unit price

    Insurance copay (only when contract_type == 'insurance'):
      - copay_amount (fixed per unit) or copay_percentage (of discounted line)

    Tax is applied to (subtotal − discount_amount).

    Args:
        items:           Line items from the sale request.
        drugs:           Drug lookup dict keyed by drug_id.
        contract:        The validated PriceContract being applied.
        contract_items:  Per-drug PriceContractItem overrides keyed by drug_id.
        resolved_prices: Pre-resolved unit prices keyed by drug_id.

    Returns:
        List of dicts, one per item, with keys:
            item, drug, unit_price, item_subtotal, discount_percentage,
            discount_amount, tax_rate, tax_amount, item_total,
            insurance_covered, patient_copay, is_excluded
    """
    results: List[Dict] = []

    for item in items:
        drug          = drugs[item.drug_id]
        unit_price    = resolved_prices[item.drug_id]
        qty           = Decimal(str(item.quantity))
        item_subtotal = r2(qty * unit_price)
        tax_rate      = d(drug.tax_rate) if drug.tax_rate else Decimal("0")

        ci = contract_items.get(item.drug_id)  # PriceContractItem or None

        # ---- Exclusion checks ------------------------------------------------
        drug_is_otc = not drug.requires_prescription
        is_excluded = (
            (ci is not None and ci.is_excluded)
            or item.drug_id in (contract.excluded_drug_ids or [])
            or (
                drug.category_id is not None
                and drug.category_id in (contract.excluded_drug_categories or [])
            )
            or (contract.applies_to_prescription_only and drug_is_otc)
            or (not contract.applies_to_otc and drug_is_otc)
        )

        discount_pct      = Decimal("0")
        discount_amount   = Decimal("0")
        patient_copay: Optional[Decimal] = None
        insurance_covered = False

        if not is_excluded:
            if ci is not None and ci.fixed_price is not None:
                # Fixed-price override: back-calculate the implied discount
                fixed_unit         = d(ci.fixed_price)
                effective_subtotal = r2(fixed_unit * qty)
                discount_amount    = max(Decimal("0"), item_subtotal - effective_subtotal)
                discount_pct       = (
                    r2(discount_amount / item_subtotal * 100)
                    if item_subtotal > 0
                    else Decimal("0")
                )

            else:
                # Percentage discount: per-drug override takes priority over default
                effective_pct = (
                    d(ci.override_discount_percentage)
                    if ci is not None and ci.override_discount_percentage is not None
                    else d(contract.discount_percentage)
                )
                discount_pct  = effective_pct
                raw_discount  = r2(item_subtotal * effective_pct / 100)

                # Cap: maximum_discount_amount per item
                if contract.maximum_discount_amount is not None:
                    raw_discount = min(raw_discount, d(contract.maximum_discount_amount))

                discount_amount = raw_discount

            # Floor: minimum_price_override — never let the effective price drop below this
            if contract.minimum_price_override is not None:
                min_subtotal = d(contract.minimum_price_override) * qty
                if (item_subtotal - discount_amount) < min_subtotal:
                    discount_amount = max(Decimal("0"), item_subtotal - min_subtotal)
                    # Recalculate implied percentage after the floor clamps the discount
                    discount_pct = (
                        r2(discount_amount / item_subtotal * 100)
                        if item_subtotal > 0
                        else Decimal("0")
                    )

            # Insurance copay
            if contract.contract_type == "insurance":
                insurance_covered = True
                if contract.copay_amount is not None:
                    patient_copay = r2(d(contract.copay_amount) * qty)
                elif contract.copay_percentage is not None:
                    discounted    = item_subtotal - discount_amount
                    patient_copay = r2(discounted * d(contract.copay_percentage) / 100)

        discounted_subtotal = item_subtotal - discount_amount
        tax_amount          = r2(discounted_subtotal * tax_rate / 100) if tax_rate else Decimal("0")
        item_total          = r2(discounted_subtotal + tax_amount)

        results.append(
            {
                "item":                item,
                "drug":                drug,
                "unit_price":          unit_price,
                "item_subtotal":       item_subtotal,
                "discount_percentage": discount_pct,
                "discount_amount":     discount_amount,
                "tax_rate":            tax_rate,
                "tax_amount":          tax_amount,
                "item_total":          item_total,
                "insurance_covered":   insurance_covered,
                "patient_copay":       patient_copay,
                "is_excluded":         is_excluded,
            }
        )

    return results
