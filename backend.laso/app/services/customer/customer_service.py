"""
Customer Service
================
All customer business logic: CRUD, search, loyalty program management,
insurance/contract association, quick POS lookup.

Conventions (matching existing services):
  - All public methods are @classmethod coroutines.
  - Organization isolation: every query filters on organization_id.
  - Soft-delete: set deleted_at / is_deleted, never hard-delete from this service.
  - Loyalty tier recalculation happens in _recalculate_tier(), called after every
    points change.
  - All SELECT statements use lazy="raise" safe patterns (explicit joinedload/
    selectinload or separate queries — never implicit lazy access).
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, asc, desc

from fastapi import HTTPException, status

from app.models.customer.customer_model import Customer
from app.models.pricing.pricing_model import InsuranceProvider, PriceContract
from app.models.sales.sales_model import Sale
from app.schemas.customer_schemas import (
    CustomerCreate,
    CustomerUpdate,
    CustomerWithDetails,
    CustomerQuickLookup,
    CustomerSearchResult,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# LOYALTY TIER THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

LOYALTY_TIERS = [
    ("platinum", 5000),
    ("gold",     2000),
    ("silver",   500),
    ("bronze",   0),
]


def _tier_for_points(points: int) -> str:
    """Return the tier name for a given points total."""
    for tier_name, threshold in LOYALTY_TIERS:
        if points >= threshold:
            return tier_name
    return "bronze"


# ─────────────────────────────────────────────────────────────────────────────
# SORT COLUMN MAPPING
# ─────────────────────────────────────────────────────────────────────────────

_SORT_COLUMNS = {
    "created_at":     Customer.created_at,
    "first_name":     Customer.first_name,
    "last_name":      Customer.last_name,
    "loyalty_points": Customer.loyalty_points,
}


class CustomerService:
    """Customer business logic."""

    # =========================================================================
    # CREATE
    # =========================================================================

    @classmethod
    async def create_customer(
        cls,
        db: AsyncSession,
        customer_data: CustomerCreate,
        created_by_user_id: uuid.UUID,
    ) -> CustomerWithDetails:
        """
        Create a new customer.

        Validates:
          - Phone uniqueness within organisation (for non-walk_in types).
          - Email uniqueness within organisation (if provided).
          - Insurance provider exists (for insurance type).
          - Preferred contract exists (for corporate type).

        Returns CustomerWithDetails with resolved relationship names.
        """
        # ── Uniqueness checks ────────────────────────────────────────────────
        if customer_data.customer_type != "walk_in":
            if customer_data.phone:
                existing = await cls._find_by_phone(
                    db, customer_data.phone, customer_data.organization_id
                )
                if existing:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"A customer with phone {customer_data.phone} already exists in this organisation",
                    )

            if customer_data.email:
                existing = await cls._find_by_email(
                    db, str(customer_data.email), customer_data.organization_id
                )
                if existing:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"A customer with email {customer_data.email} already exists in this organisation",
                    )

        # ── Validate insurance provider exists ───────────────────────────────
        if customer_data.insurance_provider_id:
            provider = await db.get(InsuranceProvider, customer_data.insurance_provider_id)
            if not provider or provider.is_deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Insurance provider not found",
                )

        # ── Validate preferred contract exists ───────────────────────────────
        if customer_data.preferred_contract_id:
            contract = await db.get(PriceContract, customer_data.preferred_contract_id)
            if not contract or contract.is_deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Preferred contract not found",
                )

        # ── Create the record ────────────────────────────────────────────────
        customer = Customer(
            organization_id=customer_data.organization_id,
            customer_type=customer_data.customer_type,
            first_name=customer_data.first_name,
            last_name=customer_data.last_name,
            phone=customer_data.phone,
            email=str(customer_data.email) if customer_data.email else None,
            date_of_birth=customer_data.date_of_birth,
            address=customer_data.address,
            insurance_provider_id=customer_data.insurance_provider_id,
            insurance_member_id=customer_data.insurance_member_id,
            insurance_card_image_url=customer_data.insurance_card_image_url,
            preferred_contract_id=customer_data.preferred_contract_id,
            preferred_contact_method=customer_data.preferred_contact_method,
            marketing_consent=customer_data.marketing_consent,
            loyalty_points=0,
            loyalty_tier="bronze",
            is_active=True,
        )

        db.add(customer)
        await db.commit()
        await db.refresh(customer)

        logger.info(
            "Customer created: id=%s type=%s by user=%s",
            customer.id,
            customer.customer_type,
            created_by_user_id,
        )

        return await cls._build_with_details(db, customer)

    # =========================================================================
    # READ — list
    # =========================================================================

    @classmethod
    async def list_customers(
        cls,
        db: AsyncSession,
        organization_id: uuid.UUID,
        search: Optional[str] = None,
        customer_type: Optional[str] = None,
        loyalty_tier: Optional[str] = None,
        insurance_provider_id: Optional[uuid.UUID] = None,
        preferred_contract_id: Optional[uuid.UUID] = None,
        is_active: Optional[bool] = None,
        min_loyalty_points: Optional[int] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 25,
    ) -> Tuple[List[Customer], int]:
        """
        Return a filtered, paginated list of customers plus the total count.
        Caller is responsible for converting to the response schema.
        """
        stmt = (
            select(Customer)
            .where(
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )

        # ── Filters ───────────────────────────────────────────────────────────
        if search:
            term = f"%{search.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Customer.first_name).like(term),
                    func.lower(Customer.last_name).like(term),
                    func.lower(Customer.phone).like(term),
                    func.lower(Customer.email).like(term),
                    func.lower(Customer.insurance_member_id).like(term),
                )
            )

        if customer_type:
            stmt = stmt.where(Customer.customer_type == customer_type)

        if loyalty_tier:
            stmt = stmt.where(Customer.loyalty_tier == loyalty_tier)

        if insurance_provider_id:
            stmt = stmt.where(Customer.insurance_provider_id == insurance_provider_id)

        if preferred_contract_id:
            stmt = stmt.where(Customer.preferred_contract_id == preferred_contract_id)

        if is_active is not None:
            stmt = stmt.where(Customer.is_active == is_active)

        if min_loyalty_points is not None:
            stmt = stmt.where(Customer.loyalty_points >= min_loyalty_points)

        # ── Sorting ───────────────────────────────────────────────────────────
        sort_col = _SORT_COLUMNS.get(sort_by, Customer.created_at)
        stmt = stmt.order_by(desc(sort_col) if sort_order == "desc" else asc(sort_col))

        # ── Count ─────────────────────────────────────────────────────────────
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total: int = (await db.execute(count_stmt)).scalar_one()

        # ── Paginate ──────────────────────────────────────────────────────────
        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await db.execute(stmt)
        customers = result.scalars().all()

        return list(customers), total

    # =========================================================================
    # READ — single
    # =========================================================================

    @classmethod
    async def get_customer_by_id(
        cls,
        db: AsyncSession,
        customer_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> CustomerWithDetails:
        """
        Fetch a single customer with full relationship details.
        Raises 404 if not found or belongs to another organisation.
        """
        result = await db.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )
        customer = result.scalar_one_or_none()

        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found",
            )

        return await cls._build_with_details(db, customer)

    # =========================================================================
    # READ — quick search (POS)
    # =========================================================================

    @classmethod
    async def search_customers_quick(
        cls,
        db: AsyncSession,
        organization_id: uuid.UUID,
        query: str,
        limit: int = 10,
    ) -> CustomerSearchResult:
        """
        Fast typeahead search for the POS.
        Searches name, phone, email, and insurance_member_id.
        Returns minimal CustomerQuickLookup shape — no join-heavy stats.
        """
        if not query or len(query.strip()) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Search query must be at least 2 characters",
            )

        term = f"%{query.strip().lower()}%"

        result = await db.execute(
            select(Customer)
            .where(
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
                Customer.is_active == True,
                or_(
                    func.lower(Customer.first_name).like(term),
                    func.lower(Customer.last_name).like(term),
                    func.lower(Customer.phone).like(term),
                    func.lower(Customer.email).like(term),
                    func.lower(Customer.insurance_member_id).like(term),
                    # Full name concatenated search
                    func.lower(
                        func.concat(
                            func.coalesce(Customer.first_name, ""),
                            " ",
                            func.coalesce(Customer.last_name, ""),
                        )
                    ).like(term),
                ),
            )
            .order_by(Customer.first_name.asc())
            .limit(limit)
        )
        customers = result.scalars().all()

        # ── Resolve insurance provider names in one query ─────────────────────
        provider_ids = {
            c.insurance_provider_id
            for c in customers
            if c.insurance_provider_id
        }
        provider_map: dict[uuid.UUID, InsuranceProvider] = {}
        if provider_ids:
            prov_result = await db.execute(
                select(InsuranceProvider).where(
                    InsuranceProvider.id.in_(provider_ids)
                )
            )
            for p in prov_result.scalars().all():
                provider_map[p.id] = p

        # ── Resolve contract names ─────────────────────────────────────────────
        contract_ids = {
            c.preferred_contract_id
            for c in customers
            if c.preferred_contract_id
        }
        contract_map: dict[uuid.UUID, PriceContract] = {}
        if contract_ids:
            con_result = await db.execute(
                select(PriceContract).where(
                    PriceContract.id.in_(contract_ids)
                )
            )
            for con in con_result.scalars().all():
                contract_map[con.id] = con

        matches: list[CustomerQuickLookup] = []
        for c in customers:
            provider = provider_map.get(c.insurance_provider_id) if c.insurance_provider_id else None
            contract = contract_map.get(c.preferred_contract_id) if c.preferred_contract_id else None

            # Age calculation for senior citizen flag
            eligible_senior = False
            if c.date_of_birth:
                from datetime import date
                today = date.today()
                age = (today - c.date_of_birth).days // 365
                eligible_senior = age >= 60

            full_name = None
            if c.first_name or c.last_name:
                full_name = " ".join(filter(None, [c.first_name, c.last_name]))

            matches.append(
                CustomerQuickLookup(
                    id=c.id,
                    full_name=full_name,
                    phone=c.phone,
                    email=c.email,
                    customer_type=c.customer_type,
                    loyalty_points=c.loyalty_points,
                    has_insurance=c.insurance_provider_id is not None,
                    insurance_provider_name=provider.name if provider else None,
                    preferred_contract_name=contract.contract_name if contract else None,
                    eligible_for_senior_discount=eligible_senior,
                )
            )

        return CustomerSearchResult(
            matches=matches,
            total=len(matches),
            search_term=query.strip(),
        )

    # =========================================================================
    # UPDATE
    # =========================================================================

    @classmethod
    async def update_customer(
        cls,
        db: AsyncSession,
        customer_id: uuid.UUID,
        organization_id: uuid.UUID,
        update_data: CustomerUpdate,
        updated_by_user_id: uuid.UUID,
    ) -> CustomerWithDetails:
        """
        Partial update. Only fields explicitly provided in update_data are applied.
        Re-validates uniqueness for phone / email if they are changing.
        """
        result = await db.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )
        customer = result.scalar_one_or_none()
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found",
            )

        # Only iterate fields that were explicitly set (not None by default)
        payload = update_data.model_dump(exclude_unset=True)

        # ── Uniqueness checks for changing contact fields ─────────────────────
        new_phone = payload.get("phone")
        if new_phone and new_phone != customer.phone:
            existing = await cls._find_by_phone(db, new_phone, organization_id)
            if existing and existing.id != customer_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Phone {new_phone} is already registered to another customer",
                )

        new_email = payload.get("email")
        if new_email and str(new_email) != customer.email:
            existing = await cls._find_by_email(db, str(new_email), organization_id)
            if existing and existing.id != customer_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Email {new_email} is already registered to another customer",
                )

        # ── Validate new insurance provider ───────────────────────────────────
        new_provider_id = payload.get("insurance_provider_id")
        if new_provider_id:
            provider = await db.get(InsuranceProvider, new_provider_id)
            if not provider or provider.is_deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Insurance provider not found",
                )

        # ── Validate new preferred contract ───────────────────────────────────
        new_contract_id = payload.get("preferred_contract_id")
        if new_contract_id:
            contract = await db.get(PriceContract, new_contract_id)
            if not contract or contract.is_deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Preferred contract not found",
                )

        # ── Apply changes ─────────────────────────────────────────────────────
        for field, value in payload.items():
            # email comes as EmailStr object — store as string
            if field == "email" and value is not None:
                value = str(value)
            setattr(customer, field, value)

        customer.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(customer)

        logger.info("Customer updated: id=%s by user=%s", customer_id, updated_by_user_id)

        return await cls._build_with_details(db, customer)

    # =========================================================================
    # SOFT-DELETE
    # =========================================================================

    @classmethod
    async def delete_customer(
        cls,
        db: AsyncSession,
        customer_id: uuid.UUID,
        organization_id: uuid.UUID,
        deleted_by_user_id: uuid.UUID,
    ) -> dict:
        """
        Soft-delete a customer. Sets is_deleted=True and deleted_at.
        Raises 400 if the customer has sales in the last 90 days
        (preserving financial data integrity).
        """
        result = await db.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )
        customer = result.scalar_one_or_none()
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found",
            )

        # Guard: prevent deletion if recent sales exist
        from datetime import timedelta
        ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)
        sale_count_result = await db.execute(
            select(func.count(Sale.id)).where(
                Sale.customer_id == customer_id,
                Sale.created_at >= ninety_days_ago,
                Sale.status.in_(["completed", "refunded"]),
            )
        )
        recent_sales = sale_count_result.scalar_one()
        if recent_sales > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot delete customer: {recent_sales} sale(s) in the last 90 days. "
                    "Deactivate instead to preserve financial records."
                ),
            )

        customer.is_deleted = True
        customer.deleted_at = datetime.now(timezone.utc)
        customer.is_active = False
        customer.updated_at = datetime.now(timezone.utc)

        await db.commit()

        logger.info("Customer soft-deleted: id=%s by user=%s", customer_id, deleted_by_user_id)

        return {"message": "Customer deleted successfully", "id": str(customer_id)}

    # =========================================================================
    # LOYALTY — award
    # =========================================================================

    @classmethod
    async def award_loyalty_points(
        cls,
        db: AsyncSession,
        customer_id: uuid.UUID,
        organization_id: uuid.UUID,
        points: int,
        reason: str,
        awarded_by_user_id: uuid.UUID,
    ) -> CustomerWithDetails:
        """
        Manually award loyalty points to a customer.
        Recalculates tier after award.
        Only manager, admin, super_admin may call this endpoint.
        """
        customer = await cls._get_active_customer(db, customer_id, organization_id)

        if customer.customer_type == "walk_in":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot award loyalty points to walk-in customers. Register the customer first.",
            )

        customer.loyalty_points += points
        customer.loyalty_tier = _tier_for_points(customer.loyalty_points)
        customer.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(customer)

        logger.info(
            "Loyalty points awarded: customer=%s points=%d reason=%r by user=%s",
            customer_id, points, reason, awarded_by_user_id,
        )

        return await cls._build_with_details(db, customer)

    # =========================================================================
    # LOYALTY — deduct
    # =========================================================================

    @classmethod
    async def deduct_loyalty_points(
        cls,
        db: AsyncSession,
        customer_id: uuid.UUID,
        organization_id: uuid.UUID,
        points: int,
        reason: str,
        deducted_by_user_id: uuid.UUID,
    ) -> CustomerWithDetails:
        """
        Manually deduct loyalty points.
        Points cannot go below 0.
        Recalculates tier after deduction.
        """
        customer = await cls._get_active_customer(db, customer_id, organization_id)

        if customer.loyalty_points < points:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot deduct {points} points: customer only has "
                    f"{customer.loyalty_points} points."
                ),
            )

        customer.loyalty_points -= points
        customer.loyalty_tier = _tier_for_points(customer.loyalty_points)
        customer.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(customer)

        logger.info(
            "Loyalty points deducted: customer=%s points=%d reason=%r by user=%s",
            customer_id, points, reason, deducted_by_user_id,
        )

        return await cls._build_with_details(db, customer)

    # =========================================================================
    # PRIVATE HELPERS
    # =========================================================================

    @classmethod
    async def _get_active_customer(
        cls,
        db: AsyncSession,
        customer_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> Customer:
        """Fetch customer, raise 404 if missing or inactive."""
        result = await db.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )
        customer = result.scalar_one_or_none()
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found",
            )
        if not customer.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Customer account is inactive",
            )
        return customer

    @classmethod
    async def _find_by_phone(
        cls,
        db: AsyncSession,
        phone: str,
        organization_id: uuid.UUID,
    ) -> Optional[Customer]:
        result = await db.execute(
            select(Customer).where(
                Customer.phone == phone,
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )
        return result.scalar_one_or_none()

    @classmethod
    async def _find_by_email(
        cls,
        db: AsyncSession,
        email: str,
        organization_id: uuid.UUID,
    ) -> Optional[Customer]:
        result = await db.execute(
            select(Customer).where(
                func.lower(Customer.email) == email.lower(),
                Customer.organization_id == organization_id,
                Customer.is_deleted == False,
            )
        )
        return result.scalar_one_or_none()

    @classmethod
    async def _build_with_details(
        cls,
        db: AsyncSession,
        customer: Customer,
    ) -> CustomerWithDetails:
        """
        Enrich a Customer ORM object with related names and purchase stats.
        Uses separate targeted queries — avoids N+1 via explicit lookups.
        """
        # ── Insurance provider name ───────────────────────────────────────────
        insurance_provider_name: Optional[str] = None
        insurance_provider_code: Optional[str] = None
        if customer.insurance_provider_id:
            provider = await db.get(InsuranceProvider, customer.insurance_provider_id)
            if provider:
                insurance_provider_name = provider.name
                insurance_provider_code = provider.code

        # ── Preferred contract name / discount ────────────────────────────────
        preferred_contract_name: Optional[str] = None
        preferred_contract_discount: Optional[float] = None
        if customer.preferred_contract_id:
            contract = await db.get(PriceContract, customer.preferred_contract_id)
            if contract:
                preferred_contract_name = contract.contract_name
                preferred_contract_discount = float(contract.discount_percentage)

        # ── Purchase statistics (aggregated, no individual row loading) ────────
        # Note: coalesce uses 0.0 (float) not 0 (int) to avoid Decimal/int
        # type mismatch in async SQLAlchemy. organization_id filter added for
        # safety to prevent cross-org data leakage.
        stats_result = await db.execute(
            select(
                func.count(Sale.id).label("total_purchases"),
                func.coalesce(func.sum(Sale.total_amount), 0.0).label("total_spent"),
                func.max(Sale.created_at).label("last_purchase_date"),
            ).where(
                Sale.customer_id == customer.id,
                Sale.organization_id == customer.organization_id,
                Sale.status.in_(["completed", "refunded"]),
            )
        )
        stats = stats_result.one()

        return CustomerWithDetails(
            # Base fields
            id=customer.id,
            organization_id=customer.organization_id,
            customer_type=customer.customer_type,
            first_name=customer.first_name,
            last_name=customer.last_name,
            phone=customer.phone,
            email=customer.email,
            date_of_birth=customer.date_of_birth,
            address=customer.address,
            insurance_provider_id=customer.insurance_provider_id,
            insurance_member_id=customer.insurance_member_id,
            insurance_card_image_url=customer.insurance_card_image_url,
            preferred_contract_id=customer.preferred_contract_id,
            preferred_contact_method=customer.preferred_contact_method,
            marketing_consent=customer.marketing_consent,
            loyalty_points=customer.loyalty_points,
            loyalty_tier=customer.loyalty_tier,
            is_active=customer.is_active,
            deleted_at=customer.deleted_at,
            created_at=customer.created_at,
            updated_at=customer.updated_at,
            # SyncSchema fields
            sync_status=customer.sync_status,
            sync_version=customer.sync_version,
            synced_at=customer.last_synced_at,
            # Resolved names
            insurance_provider_name=insurance_provider_name,
            insurance_provider_code=insurance_provider_code,
            preferred_contract_name=preferred_contract_name,
            preferred_contract_discount=preferred_contract_discount,
            # Purchase stats
            total_purchases=stats.total_purchases,
            total_spent=float(stats.total_spent),
            last_purchase_date=stats.last_purchase_date,
        )