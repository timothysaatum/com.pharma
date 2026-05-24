"""
Sale Helpers
============
Stateless utility functions used by ``SalesService`` that do not belong to
the pricing, inventory, or validation domains.

Functions
---------
resolve_loyalty_tier      — pure tier calculation from a point balance
generate_sale_number      — async DB query to produce a unique sale number
build_sale_with_details   — async response DTO assembler
create_audit_log          — async AuditLog row writer (flush only, no commit)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional
import uuid

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.sales.sales_model import Sale, SaleItem
from app.models.system_md.sys_models import AuditLog
from app.models.user.user_model import User
from app.schemas.sales_schemas import SaleItemWithDetails, SaleWithDetails


# ---------------------------------------------------------------------------
# Loyalty tier
# ---------------------------------------------------------------------------


def resolve_loyalty_tier(points: int, thresholds: Dict) -> str:
    """
    Return the correct loyalty tier for a given point balance.

    Evaluates from highest tier to lowest so the result is always accurate
    after both point earnings (process_sale) and deductions (refund_sale).

    Args:
        points:     Current total loyalty points.
        thresholds: Dict with keys 'silver', 'gold', 'platinum' and int values.
                    Falls back to { silver: 100, gold: 500, platinum: 1000 }
                    for any missing key.

    Returns:
        One of 'bronze', 'silver', 'gold', 'platinum'.
    """
    if points >= thresholds.get("platinum", 1000):
        return "platinum"
    if points >= thresholds.get("gold", 500):
        return "gold"
    if points >= thresholds.get("silver", 100):
        return "silver"
    return "bronze"


# ---------------------------------------------------------------------------
# Sale number generator
# ---------------------------------------------------------------------------


async def generate_sale_number(db: AsyncSession, branch_code: str) -> str:
    """
    Generate a unique, human-readable sale number for a new sale.

    Format : {BRANCH_CODE}-{YYYYMMDD}-{SEQUENCE:04d}
    Example: BR001-20260321-0001

    The sequence resets each day per branch.  The counter is derived from a
    COUNT query on existing sale numbers with the same prefix — this is safe
    under concurrent load because ``process_sale`` holds a savepoint and the
    ``sale_number`` column has a UNIQUE constraint, so a duplicate will cause
    a constraint error that triggers a retry rather than silent corruption.

    Args:
        db:          Async SQLAlchemy session.
        branch_code: Unique branch code (e.g. 'BR001').

    Returns:
        Formatted sale number string.
    """
    from datetime import date

    today_str = date.today().strftime("%Y%m%d")
    prefix    = f"{branch_code}-{today_str}"

    result = await db.execute(
        select(func.count(Sale.id)).where(
            Sale.sale_number.like(f"{prefix}%")
        )
    )
    count = result.scalar() or 0
    return f"{prefix}-{str(count + 1).zfill(4)}"


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------


async def build_sale_with_details(
    db: AsyncSession,
    sale: Sale,
) -> SaleWithDetails:
    """
    Assemble a ``SaleWithDetails`` response DTO from a committed ``Sale`` row.

    Performs individual lookups for branch, organisation, cashier, customer,
    and all sale items (with their drug and batch details).  These are
    post-commit reads so no locks are needed.

    Args:
        db:   Async SQLAlchemy session.
        sale: Committed Sale ORM instance.

    Returns:
        Fully populated SaleWithDetails DTO.
    """
    branch_res = await db.execute(
        select(Branch).where(Branch.id == sale.branch_id)
    )
    branch = branch_res.scalar_one()

    cashier_res = await db.execute(
        select(User).where(User.id == sale.cashier_id)
    )
    cashier = cashier_res.scalar_one()

    org_res = await db.execute(
        select(Organization).where(Organization.id == sale.organization_id)
    )
    organization = org_res.scalar_one()

    (
        customer_full_name,
        customer_phone,
        customer_email,
        customer_loyalty_points,
        customer_loyalty_tier,
    ) = (None, None, None, None, None)

    if sale.customer_id:
        cust_res = await db.execute(
            select(Customer).where(Customer.id == sale.customer_id)
        )
        customer = cust_res.scalar_one_or_none()
        if customer:
            customer_full_name      = (
                f"{customer.first_name or ''} "
                f"{customer.last_name or ''}".strip()
            )
            customer_phone          = customer.phone
            customer_email          = customer.email
            customer_loyalty_points = customer.loyalty_points
            customer_loyalty_tier   = customer.loyalty_tier

    items_res = await db.execute(
        select(SaleItem).where(SaleItem.sale_id == sale.id)
    )
    items = items_res.scalars().all()

    items_with_details: List[SaleItemWithDetails] = []
    for item in items:
        drug_res = await db.execute(
            select(Drug).where(Drug.id == item.drug_id)
        )
        drug = drug_res.scalar_one()

        # Resolve the human-readable batch_number for display purposes
        batch_number: Optional[str] = None
        if item.batch_id:
            bn_res = await db.execute(
                select(DrugBatch.batch_number).where(
                    DrugBatch.id == item.batch_id
                )
            )
            batch_number = bn_res.scalar_one_or_none()

        item_dict = {
            k: v
            for k, v in item.__dict__.items()
            if not k.startswith("_") and k != "batch_id"
        }
        items_with_details.append(
            SaleItemWithDetails(
                **item_dict,
                drug_generic_name=drug.generic_name,
                drug_manufacturer=drug.manufacturer,
                batch_number=batch_number,
            )
        )

    sale_dict = {
        k: v
        for k, v in sale.__dict__.items()
        if not k.startswith("_")
        and k not in ("items", "items_count")  # exclude ORM relationship + computed_field name
    }
    return SaleWithDetails(
        **sale_dict,
        items=items_with_details,
        branch_name=branch.name,
        branch_address=branch.address,
        organization_name=organization.name,
        organization_tax_id=organization.tax_id,
        cashier_name=cashier.full_name,
        customer_full_name=customer_full_name,
        customer_phone=customer_phone,
        customer_email=customer_email,
        customer_loyalty_tier=customer_loyalty_tier,
    )


# ---------------------------------------------------------------------------
# Audit log writer
# ---------------------------------------------------------------------------


async def create_audit_log(
    db: AsyncSession,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    changes: Optional[Dict] = None,
) -> None:
    """
    Append an immutable ``AuditLog`` row and flush it to the session.

    This function intentionally **never** calls ``db.commit()``.  Callers own
    the transaction boundary.  Placing the audit write after the main commit
    means a transient audit failure cannot roll back a completed sale.

    Args:
        db:              Async SQLAlchemy session.
        action:          Human-readable action name, e.g. 'process_sale'.
        entity_type:     Table / model affected, e.g. 'Sale'.
        entity_id:       Primary key of the affected record.
        user_id:         ID of the user who performed the action.
        organization_id: Organisation scope.
        changes:         Optional dict of before/after state or summary data.
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
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()