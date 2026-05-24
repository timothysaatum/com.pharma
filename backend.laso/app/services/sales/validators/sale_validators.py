"""
Sale Validators
===============
All pre-sale validation logic that must pass before any inventory or financial
mutation takes place.

Validators
----------
check_customer_allergies     — SAFETY CRITICAL: block sale on allergy match
load_and_validate_contract   — load PriceContract and run all applicability
                               guards (date, branch, role, insurance)
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer.customer_model import Customer
from app.models.inventory.inventory_model import Drug
from app.models.pricing.pricing_model import PriceContract, PriceContractItem
from app.models.system_md.sys_models import SystemAlert
from app.models.user.user_model import User
from app.schemas.sales_schemas import SaleItemCreate

from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Allergy check
# ---------------------------------------------------------------------------


async def check_customer_allergies(
    db: AsyncSession,
    customer: Customer,
    drug_ids: List[uuid.UUID],
    drugs: Dict[uuid.UUID, Drug],
    branch_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> None:
    """
    SAFETY CRITICAL — must run before any inventory mutation.

    Blocks the sale and writes a critical ``SystemAlert`` if any drug's
    ``name``, ``generic_name``, or ``brand_name`` contains a substring that
    matches one of the customer's recorded allergies (case-insensitive).

    A ``SystemAlert`` is flushed to the session before the exception is raised
    so the alert is persisted even if the surrounding savepoint catches the
    HTTPException and rolls back other pending writes.

    Args:
        db:              Async SQLAlchemy session.
        customer:        Customer being served.
        drug_ids:        Ordered list of drug IDs from the sale request.
        drugs:           Drug lookup dict keyed by drug_id.
        branch_id:       Branch where the sale is being processed.
        organization_id: Organisation the sale belongs to.

    Raises:
        HTTPException(400): If any drug matches a recorded allergy.
    """
    if not customer.allergies:
        return

    for drug_id in drug_ids:
        drug = drugs[drug_id]
        name_fields = [
            (drug.name         or "").lower(),
            (drug.generic_name or "").lower(),
            (drug.brand_name   or "").lower(),
        ]

        for allergy in customer.allergies:
            allergy_lower = allergy.lower().strip()
            if not allergy_lower:
                continue

            if any(allergy_lower in field for field in name_fields):
                # Persist the alert before raising so it survives a rollback
                db.add(
                    SystemAlert(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        branch_id=branch_id,
                        alert_type="security",
                        severity="critical",
                        title=(
                            f"ALLERGY ALERT: "
                            f"{customer.first_name} {customer.last_name}"
                        ),
                        message=(
                            f"Attempted to dispense '{drug.name}' to customer "
                            f"allergic to '{allergy}'. Sale blocked. "
                            f"Customer ID: {customer.id}."
                        ),
                        drug_id=drug.id,
                        is_resolved=False,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db.flush()

                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"ALLERGY ALERT: {customer.first_name} {customer.last_name} "
                        f"is allergic to '{allergy}'. '{drug.name}' may contain "
                        f"'{allergy}'. Pharmacist override is required."
                    ),
                )


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


async def load_and_validate_contract(
    db: AsyncSession,
    contract_id: uuid.UUID,
    branch_id: uuid.UUID,
    drug_ids: List[uuid.UUID],
    user: User,
    insurance_verified: bool,
    customer_id: Optional[uuid.UUID],
) -> Tuple[PriceContract, Dict[uuid.UUID, PriceContractItem]]:
    """
    Load the PriceContract and run all applicability guards.

    Guards (in order)
    -----------------
    1. Contract exists, is active, status='active', belongs to user's org.
    2. Today is within the contract's effective date range.
    3. The branch is covered by the contract (or contract applies to all).
    4. The user's role is permitted to apply this contract.
    5. Insurance contracts require a registered customer + verified flag.

    After all guards pass, load per-drug ``PriceContractItem`` overrides for
    the drugs in this sale (single query, drug_id IN list).

    Args:
        db:                 Async SQLAlchemy session.
        contract_id:        ID of the contract selected by the cashier.
        branch_id:          Branch where the sale is being processed.
        drug_ids:           Drug IDs in the sale (for loading per-drug overrides).
        user:               User processing the sale.
        insurance_verified: Whether the customer's insurance has been verified.
        customer_id:        Customer ID (required for insurance contracts).

    Returns:
        Tuple of (PriceContract, dict of PriceContractItem keyed by drug_id).

    Raises:
        HTTPException(404): Contract not found or not active.
        HTTPException(400): Date range, branch, or insurance guard fails.
        HTTPException(403): User role not permitted for this contract.
    """
    today = date.today()

    result = await db.execute(
        select(PriceContract).where(
            PriceContract.id == contract_id,
            PriceContract.organization_id == user.organization_id,
            PriceContract.is_deleted == False,
            PriceContract.is_active == True,
            PriceContract.status == "active",
        )
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Price contract not found or not active.",
        )

    # Date range
    if today < contract.effective_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Contract '{contract.contract_name}' is not yet effective "
                f"(starts {contract.effective_from})."
            ),
        )
    if contract.effective_to and today > contract.effective_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Contract '{contract.contract_name}' expired on "
                f"{contract.effective_to}."
            ),
        )

    # Branch applicability
    if not contract.applies_to_all_branches:
        if branch_id not in (contract.applicable_branch_ids or []):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Contract '{contract.contract_name}' is not valid "
                    "at this branch."
                ),
            )

    # User role restriction
    if contract.allowed_user_roles and user.role not in contract.allowed_user_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your role '{user.role}' is not permitted to apply "
                f"contract '{contract.contract_name}'."
            ),
        )

    # Insurance-specific guards
    if contract.contract_type == "insurance":
        if not customer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insurance contracts require a registered customer.",
            )
        if not insurance_verified:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Insurance eligibility must be verified before "
                    "processing an insurance sale."
                ),
            )

    # Per-drug overrides
    overrides_res = await db.execute(
        select(PriceContractItem).where(
            PriceContractItem.contract_id == contract.id,
            PriceContractItem.drug_id.in_(drug_ids),
        )
    )
    contract_items: Dict[uuid.UUID, PriceContractItem] = {
        ci.drug_id: ci for ci in overrides_res.scalars().all()
    }

    return contract, contract_items