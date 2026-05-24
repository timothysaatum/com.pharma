"""
Price Contract Service
======================
Business logic for creating, reading, updating and managing the lifecycle of
price contracts (insurance, corporate, staff, senior-citizen, etc.).

Design principles
-----------------
* ``applicable_branch_ids`` is deduplicated before the count-equality check so
  a caller passing duplicate UUIDs never gets a spurious 400.
* ``update_contract`` protects workflow-controlled fields (``status``,
  ``approved_by``, ``approved_at``, ``created_by``) from being overwritten via
  the generic update path.  Those transitions go through the dedicated state
  methods (``approve_contract``, ``suspend_contract``, ``activate_contract``).
* ``effective_to`` date validation resolves the start date from the update
  payload first so it's correct when both dates change in the same call.
* ``applies_to_all_branches`` is tested with ``is False`` (not ``not ...``) to
  distinguish an explicit ``False`` from an unset ``None``.
* ``get_contract_with_details`` uses a Pydantic-friendly dict instead of
  spreading ``__dict__`` (which leaks ``_sa_instance_state``).
* ``get_available_contracts_for_pos`` pushes the role filter into SQL via an
  ARRAY-contains expression, avoiding a Python-side post-filter on large sets.
* ``suspend_contract`` persists the reason in the contract's ``notes`` field so
  the audit trail is complete.
* Creator lookup uses ``scalar_one_or_none()`` and falls back gracefully when
  the creator user has been deleted.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.pharmacy.pharmacy_model import Branch
from app.models.pricing.pricing_model import (
    InsuranceProvider,
    PriceContract,
    PriceContractItem,
)
from app.models.sales.sales_model import Sale
from app.models.user.user_model import User
from app.schemas.price_contract_schemas import (
    PriceContractCreate,
    PriceContractFilters,
    PriceContractUpdate,
)

# ---------------------------------------------------------------------------
# Fields that must not be overwritten via the generic update path —
# they are managed exclusively by dedicated state-transition methods.
# ---------------------------------------------------------------------------
_PROTECTED_UPDATE_FIELDS = frozenset(
    {"status", "approved_by", "approved_at", "created_by", "is_deleted",
     "deleted_at", "deleted_by"}
)


class PriceContractService:

    # =========================================================================
    # CREATE
    # =========================================================================

    @staticmethod
    async def create_contract(
        db: AsyncSession,
        contract_data: PriceContractCreate,
        user: User,
    ) -> PriceContract:
        """
        Create a new price contract.

        Validates:
        1. User role (admin / super_admin / manager only).
        2. contract_code unique within org.
        3. Only one default contract per org.
        4. Insurance contracts require a valid, active insurance_provider_id.
        5. Specific-branch contracts require valid branch IDs (deduplicated).
        """
        if user.role not in ("admin", "super_admin", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins and managers can create price contracts.",
            )

        # -- contract_code uniqueness ------------------------------------------
        existing = (await db.execute(
            select(PriceContract).where(
                PriceContract.organization_id == user.organization_id,
                PriceContract.contract_code   == contract_data.contract_code,
                PriceContract.is_deleted      == False,
            )
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Contract code '{contract_data.contract_code}' already exists.",
            )

        # -- single default contract per org -----------------------------------
        if contract_data.is_default_contract:
            existing_default = (await db.execute(
                select(PriceContract).where(
                    PriceContract.organization_id    == user.organization_id,
                    PriceContract.is_default_contract == True,
                    PriceContract.is_deleted         == False,
                )
            )).scalar_one_or_none()
            if existing_default:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Default contract already exists: "
                        f"'{existing_default.contract_name}'. "
                        "Remove default status from that contract first."
                    ),
                )

        # -- insurance provider validation ------------------------------------
        if contract_data.contract_type == "insurance":
            if not contract_data.insurance_provider_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="insurance_provider_id is required for insurance contracts.",
                )
            provider = (await db.execute(
                select(InsuranceProvider).where(
                    InsuranceProvider.id              == contract_data.insurance_provider_id,
                    InsuranceProvider.organization_id == user.organization_id,
                    InsuranceProvider.is_deleted      == False,
                    InsuranceProvider.is_active       == True,
                )
            )).scalar_one_or_none()
            if not provider:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Insurance provider not found or inactive.",
                )

        # -- branch validation -------------------------------------------------
        if not contract_data.applies_to_all_branches:
            if not contract_data.applicable_branch_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "applicable_branch_ids required when "
                        "applies_to_all_branches is False."
                    ),
                )
            # Deduplicate so count-equality check is not fooled by duplicates
            unique_branch_ids = list(dict.fromkeys(contract_data.applicable_branch_ids))
            branches = (await db.execute(
                select(Branch).where(
                    Branch.id.in_(unique_branch_ids),
                    Branch.organization_id == user.organization_id,
                    Branch.is_deleted      == False,
                )
            )).scalars().all()
            if len(branches) != len(unique_branch_ids):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Some branch IDs are invalid or don't belong to your organisation.",
                )
            contract_data.applicable_branch_ids = unique_branch_ids

        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=user.organization_id,
            contract_code=contract_data.contract_code,
            contract_name=contract_data.contract_name,
            description=contract_data.description,
            contract_type=contract_data.contract_type,
            is_default_contract=contract_data.is_default_contract,
            discount_type=contract_data.discount_type,
            discount_percentage=contract_data.discount_percentage,
            applies_to_prescription_only=contract_data.applies_to_prescription_only,
            applies_to_otc=contract_data.applies_to_otc,
            excluded_drug_categories=contract_data.excluded_drug_categories,
            excluded_drug_ids=contract_data.excluded_drug_ids,
            minimum_price_override=contract_data.minimum_price_override,
            maximum_discount_amount=contract_data.maximum_discount_amount,
            minimum_purchase_amount=contract_data.minimum_purchase_amount,
            maximum_purchase_amount=contract_data.maximum_purchase_amount,
            applies_to_all_branches=contract_data.applies_to_all_branches,
            applicable_branch_ids=contract_data.applicable_branch_ids,
            effective_from=contract_data.effective_from,
            effective_to=contract_data.effective_to,
            requires_verification=contract_data.requires_verification,
            allowed_user_roles=contract_data.allowed_user_roles,
            insurance_provider_id=contract_data.insurance_provider_id,
            copay_amount=contract_data.copay_amount,
            copay_percentage=contract_data.copay_percentage,
            status=contract_data.status,
            is_active=contract_data.is_active,
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        contract.mark_as_pending_sync()
        db.add(contract)
        await db.commit()
        await db.refresh(contract)
        return contract

    # =========================================================================
    # READ — list
    # =========================================================================

    @staticmethod
    async def get_contracts(
        db: AsyncSession,
        user: User,
        filters: Optional[PriceContractFilters] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[PriceContract], int]:
        """Return paginated contracts for the user's organisation."""
        query = select(PriceContract).where(
            PriceContract.organization_id == user.organization_id,
            PriceContract.is_deleted      == False,
        )

        if filters:
            if filters.contract_type:
                query = query.where(PriceContract.contract_type == filters.contract_type)
            if filters.status:
                query = query.where(PriceContract.status == filters.status)
            if filters.is_active is not None:
                query = query.where(PriceContract.is_active == filters.is_active)
            if filters.is_default is not None:
                query = query.where(PriceContract.is_default_contract == filters.is_default)
            if filters.insurance_provider_id:
                query = query.where(
                    PriceContract.insurance_provider_id == filters.insurance_provider_id
                )
            if filters.branch_id:
                query = query.where(
                    or_(
                        PriceContract.applies_to_all_branches == True,
                        PriceContract.applicable_branch_ids.contains([filters.branch_id]),
                    )
                )
            if filters.search:
                pattern = f"%{filters.search}%"
                query = query.where(
                    or_(
                        PriceContract.contract_code.ilike(pattern),
                        PriceContract.contract_name.ilike(pattern),
                    )
                )
            if filters.valid_on_date:
                query = query.where(
                    and_(
                        PriceContract.effective_from <= filters.valid_on_date,
                        or_(
                            PriceContract.effective_to.is_(None),
                            PriceContract.effective_to >= filters.valid_on_date,
                        ),
                    )
                )
            if filters.created_by:
                query = query.where(PriceContract.created_by == filters.created_by)

            # Sorting
            _sort_map = {
                "contract_name":      PriceContract.contract_name,
                "discount_percentage": PriceContract.discount_percentage,
                "usage_count":         PriceContract.total_transactions,
                "total_sales_amount":  PriceContract.total_discount_given,
                "effective_from":      PriceContract.effective_from,
                "last_used_at":        PriceContract.last_used_at,
            }
            _sort_by: str = getattr(filters, "sort_by", None) or ""
            sort_col = _sort_map.get(_sort_by, PriceContract.created_at)
            if getattr(filters, "sort_order", "desc") == "asc":
                query = query.order_by(sort_col.asc())
            else:
                query = query.order_by(sort_col.desc())
        else:
            query = query.order_by(PriceContract.created_at.desc())

        total = (
            await db.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0

        offset = (page - 1) * page_size
        contracts = list(
            (await db.execute(query.offset(offset).limit(page_size))).scalars().all()
        )
        return contracts, total

    # =========================================================================
    # READ — single
    # =========================================================================

    @staticmethod
    async def get_contract(
        db: AsyncSession,
        contract_id: uuid.UUID,
        user: User,
    ) -> PriceContract:
        """Return a single contract scoped to the user's organisation."""
        contract = (await db.execute(
            select(PriceContract).where(
                PriceContract.id              == contract_id,
                PriceContract.organization_id == user.organization_id,
                PriceContract.is_deleted      == False,
            )
        )).scalar_one_or_none()
        if not contract:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Price contract not found.",
            )
        return contract

    @staticmethod
    async def get_contract_with_details(
        db: AsyncSession,
        contract_id: uuid.UUID,
        user: User,
    ) -> Dict:
        """
        Return a contract with full related details.

        Builds the response dict explicitly rather than spreading
        ``contract.__dict__`` (which leaks ``_sa_instance_state`` and causes
        serialization errors in FastAPI).
        """
        contract = (await db.execute(
            select(PriceContract)
            .options(
                selectinload(PriceContract.insurance_provider),
                selectinload(PriceContract.contract_items),
            )
            .where(
                PriceContract.id              == contract_id,
                PriceContract.organization_id == user.organization_id,
                PriceContract.is_deleted      == False,
            )
        )).scalar_one_or_none()
        if not contract:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Price contract not found.",
            )

        # Creator — use scalar_one_or_none so deleted users don't crash
        creator = (await db.execute(
            select(User).where(User.id == contract.created_by)
        )).scalar_one_or_none()
        creator_name = creator.full_name if creator else "(deleted user)"

        # Approver
        approver_name: Optional[str] = None
        if contract.approved_by:
            approver = (await db.execute(
                select(User).where(User.id == contract.approved_by)
            )).scalar_one_or_none()
            if approver:
                approver_name = approver.full_name

        # Applicable branches
        applicable_branches = []
        if not contract.applies_to_all_branches and contract.applicable_branch_ids:
            branches = (await db.execute(
                select(Branch).where(
                    Branch.id.in_(contract.applicable_branch_ids),
                    Branch.is_deleted == False,
                )
            )).scalars().all()
            applicable_branches = [
                {
                    "id":       str(b.id),
                    "code":     b.code,
                    "name":     b.name,
                    "location": b.address,
                }
                for b in branches
            ]

        custom_items_count = (await db.execute(
            select(func.count())
            .select_from(PriceContractItem)
            .where(PriceContractItem.contract_id == contract_id)
        )).scalar() or 0

        # Build response dict explicitly — never spread __dict__
        return {
            "id":                       str(contract.id),
            "organization_id":          str(contract.organization_id),
            "contract_code":            contract.contract_code,
            "contract_name":            contract.contract_name,
            "description":              contract.description,
            "contract_type":            contract.contract_type,
            "is_default_contract":      contract.is_default_contract,
            "discount_type":            contract.discount_type,
            "discount_percentage":      float(contract.discount_percentage),
            "applies_to_prescription_only": contract.applies_to_prescription_only,
            "applies_to_otc":           contract.applies_to_otc,
            "excluded_drug_categories": [str(x) for x in (contract.excluded_drug_categories or [])],
            "excluded_drug_ids":        [str(x) for x in (contract.excluded_drug_ids or [])],
            "minimum_price_override":   contract.minimum_price_override,
            "maximum_discount_amount":  contract.maximum_discount_amount,
            "minimum_purchase_amount":  contract.minimum_purchase_amount,
            "maximum_purchase_amount":  contract.maximum_purchase_amount,
            "applies_to_all_branches":  contract.applies_to_all_branches,
            "applicable_branch_ids":    [str(x) for x in (contract.applicable_branch_ids or [])],
            "effective_from":           contract.effective_from.isoformat(),
            "effective_to":             contract.effective_to.isoformat() if contract.effective_to else None,
            "requires_verification":    contract.requires_verification,
            "allowed_user_roles":       contract.allowed_user_roles,
            "insurance_provider_id":    str(contract.insurance_provider_id) if contract.insurance_provider_id else None,
            "insurance_provider_name":  contract.insurance_provider.name if contract.insurance_provider else None,
            "insurance_provider_code":  contract.insurance_provider.code if contract.insurance_provider else None,
            "copay_amount":             contract.copay_amount,
            "copay_percentage":         contract.copay_percentage,
            "status":                   contract.status,
            "is_active":                contract.is_active,
            "total_transactions":       contract.total_transactions,
            "total_discount_given":     float(contract.total_discount_given),
            "last_used_at":             contract.last_used_at.isoformat() if contract.last_used_at else None,
            "created_by":               str(contract.created_by),
            "created_by_name":          creator_name,
            "approved_by":              str(contract.approved_by) if contract.approved_by else None,
            "approved_by_name":         approver_name,
            "approved_at":              contract.approved_at.isoformat() if contract.approved_at else None,
            "created_at":               contract.created_at.isoformat(),
            "updated_at":               contract.updated_at.isoformat(),
            "applicable_branches":      applicable_branches,
            "custom_pricing_items_count": custom_items_count,
            "is_valid_today":           contract.is_valid_for_date(),
            "days_until_expiry": (
                (contract.effective_to - date.today()).days
                if contract.effective_to else None
            ),
        }

    # =========================================================================
    # UPDATE
    # =========================================================================

    @staticmethod
    async def update_contract(
        db: AsyncSession,
        contract_id: uuid.UUID,
        update_data: PriceContractUpdate,
        user: User,
    ) -> PriceContract:
        """
        Partial update for a price contract.

        Protected fields (``status``, ``approved_by``, ``approved_at``,
        ``created_by``) cannot be changed via this method — use the dedicated
        state-transition methods instead.

        Validates:
        - Pricing fields cannot be changed if the contract has existing sales.
        - ``effective_to`` must be >= the resolved ``effective_from`` (using the
          new value if it's also being updated).
        - ``applicable_branch_ids`` check uses ``is False`` explicitly so that
          an unset field is not confused with ``False``.
        """
        if user.role not in ("admin", "super_admin", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins and managers can update price contracts.",
            )

        result = await db.execute(
            select(PriceContract)
            .where(
                PriceContract.id              == contract_id,
                PriceContract.organization_id == user.organization_id,
                PriceContract.is_deleted      == False,
            )
            .with_for_update()
        )
        contract = result.scalar_one_or_none()
        if not contract:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Price contract not found.",
            )

        update_dict = update_data.model_dump(exclude_unset=True)

        # -- Strip workflow-controlled fields ----------------------------------
        for field in _PROTECTED_UPDATE_FIELDS:
            update_dict.pop(field, None)

        # -- Pricing fields locked once sales exist ----------------------------
        _PRICING_FIELDS = frozenset(
            {"discount_percentage", "discount_type", "copay_amount", "copay_percentage"}
        )
        if contract.total_transactions > 0 and any(
            f in update_dict for f in _PRICING_FIELDS
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cannot modify pricing fields for a contract with existing "
                    "transactions. Create a new contract version instead."
                ),
            )

        # -- Date validation: resolve from against the new value if changing --
        new_effective_from = update_dict.get("effective_from", contract.effective_from)
        new_effective_to   = update_dict.get("effective_to",   contract.effective_to)
        if new_effective_to is not None and new_effective_to < new_effective_from:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="effective_to must be on or after effective_from.",
            )

        # -- Branch IDs: use `is False` to distinguish unset None from False --
        new_applies_to_all = update_dict.get(
            "applies_to_all_branches", contract.applies_to_all_branches
        )
        new_branch_ids = update_dict.get("applicable_branch_ids")

        if new_applies_to_all is False:
            resolved_ids = new_branch_ids or contract.applicable_branch_ids or []
            if not resolved_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "applicable_branch_ids required when "
                        "applies_to_all_branches is False."
                    ),
                )
            if new_branch_ids:
                unique_ids = list(dict.fromkeys(new_branch_ids))
                branches   = (await db.execute(
                    select(Branch).where(
                        Branch.id.in_(unique_ids),
                        Branch.organization_id == user.organization_id,
                        Branch.is_deleted      == False,
                    )
                )).scalars().all()
                if len(branches) != len(unique_ids):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Some branch IDs are invalid.",
                    )
                update_dict["applicable_branch_ids"] = unique_ids

        for field, value in update_dict.items():
            setattr(contract, field, value)

        contract.updated_at = datetime.now(timezone.utc)
        contract.mark_as_pending_sync()

        await db.commit()
        await db.refresh(contract)
        return contract

    # =========================================================================
    # DELETE (soft)
    # =========================================================================

    @staticmethod
    async def delete_contract(
        db: AsyncSession,
        contract_id: uuid.UUID,
        user: User,
    ) -> Dict:
        """
        Soft-delete a price contract.

        Guards:
        - Only admins / super_admins may delete.
        - Default contracts cannot be deleted.
        - Contracts used in the last 30 days must be suspended instead.
        """
        if user.role not in ("admin", "super_admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can delete price contracts.",
            )

        result = await db.execute(
            select(PriceContract)
            .where(
                PriceContract.id              == contract_id,
                PriceContract.organization_id == user.organization_id,
                PriceContract.is_deleted      == False,
            )
            .with_for_update()
        )
        contract = result.scalar_one_or_none()
        if not contract:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Price contract not found.",
            )

        if contract.is_default_contract:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the default contract. Set another contract as default first.",
            )

        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        recent_sales = (await db.execute(
            select(func.count())
            .select_from(Sale)
            .where(
                Sale.price_contract_id == contract_id,
                Sale.created_at        >= thirty_days_ago,
                Sale.status            == "completed",
            )
        )).scalar() or 0

        if recent_sales > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot delete a contract with {recent_sales} sales in "
                    "the last 30 days. Suspend it instead."
                ),
            )

        contract.is_deleted = True
        contract.deleted_at = datetime.now(timezone.utc)
        contract.deleted_by = user.id
        contract.is_active  = False
        contract.status     = "cancelled"
        contract.updated_at = datetime.now(timezone.utc)
        contract.mark_as_pending_sync()

        await db.commit()
        return {
            "success": True,
            "message": f"Contract '{contract.contract_name}' deleted successfully.",
        }

    # =========================================================================
    # STATE TRANSITIONS
    # =========================================================================

    @staticmethod
    async def approve_contract(
        db: AsyncSession,
        contract_id: uuid.UUID,
        user: User,
        notes: Optional[str] = None,
    ) -> PriceContract:
        """Transition a draft contract to active."""
        if user.role not in ("admin", "super_admin", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only managers and admins can approve contracts.",
            )

        contract = await PriceContractService.get_contract(db, contract_id, user)

        if contract.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only draft contracts can be approved. Current status: {contract.status}.",
            )

        contract.status      = "active"
        contract.approved_by = user.id
        contract.approved_at = datetime.now(timezone.utc)
        contract.is_active   = True
        contract.updated_at  = datetime.now(timezone.utc)
        contract.mark_as_pending_sync()

        await db.commit()
        await db.refresh(contract)
        return contract

    @staticmethod
    async def suspend_contract(
        db: AsyncSession,
        contract_id: uuid.UUID,
        user: User,
        reason: str,
    ) -> PriceContract:
        """
        Suspend an active contract.

        The reason is appended to the contract's description so there is a
        visible audit trail without adding a dedicated column.
        """
        if user.role not in ("admin", "super_admin", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only managers and admins can suspend contracts.",
            )

        contract = await PriceContractService.get_contract(db, contract_id, user)

        if contract.is_default_contract:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot suspend the default contract.",
            )

        if contract.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only active contracts can be suspended. Current status: {contract.status}.",
            )

        # Persist reason in description so it appears in audit trail
        suspension_note = (
            f"[Suspended {datetime.now(timezone.utc).date()} by {user.id}: {reason}]"
        )
        contract.description = (
            f"{contract.description or ''}\n{suspension_note}".strip()
        )
        contract.status     = "suspended"
        contract.is_active  = False
        contract.updated_at = datetime.now(timezone.utc)
        contract.mark_as_pending_sync()

        await db.commit()
        await db.refresh(contract)
        return contract

    @staticmethod
    async def activate_contract(
        db: AsyncSession,
        contract_id: uuid.UUID,
        user: User,
    ) -> PriceContract:
        """Re-activate a suspended contract after checking its date range."""
        if user.role not in ("admin", "super_admin", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only managers and admins can activate contracts.",
            )

        contract = await PriceContractService.get_contract(db, contract_id, user)

        if contract.status not in ("suspended", "draft"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot activate contract with status '{contract.status}'.",
            )

        if not contract.is_valid_for_date():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot activate a contract outside its valid date range.",
            )

        contract.status     = "active"
        contract.is_active  = True
        contract.updated_at = datetime.now(timezone.utc)
        contract.mark_as_pending_sync()

        await db.commit()
        await db.refresh(contract)
        return contract

    # =========================================================================
    # POS helpers
    # =========================================================================

    @staticmethod
    async def get_available_contracts_for_pos(
        db: AsyncSession,
        branch_id: uuid.UUID,
        user: User,
    ) -> List[Dict]:
        """
        Return contracts eligible at a specific branch for the current user.

        The role filter is pushed into SQL via an ARRAY-contains expression
        so only matching rows are returned rather than filtering in Python.
        """
        today = date.today()

        query = (
            select(PriceContract)
            .where(
                PriceContract.organization_id == user.organization_id,
                PriceContract.is_deleted      == False,
                PriceContract.is_active       == True,
                PriceContract.status          == "active",
                PriceContract.effective_from  <= today,
                or_(
                    PriceContract.effective_to.is_(None),
                    PriceContract.effective_to >= today,
                ),
                or_(
                    PriceContract.applies_to_all_branches == True,
                    PriceContract.applicable_branch_ids.contains([branch_id]),
                ),
                # Role filter in SQL: empty allowed_user_roles means any role
                or_(
                    PriceContract.allowed_user_roles == [],
                    PriceContract.allowed_user_roles.contains([user.role]),
                ),
            )
            .order_by(
                PriceContract.is_default_contract.desc(),
                PriceContract.contract_name.asc(),
            )
        )

        contracts = (await db.execute(query)).scalars().all()
        return [
            PriceContractService._format_contract_for_pos(c) for c in contracts
        ]

    @staticmethod
    def _format_contract_for_pos(contract: PriceContract) -> Dict:
        """Format a contract for the POS selection dropdown."""
        if contract.is_default_contract:
            display = f"{contract.contract_name} (Standard)"
        elif contract.contract_type == "insurance":
            display = f"{contract.contract_name} ({contract.discount_percentage}% + copay)"
        else:
            display = f"{contract.contract_name} ({contract.discount_percentage}% off)"

        return {
            "id":                   str(contract.id),
            "code":                 contract.contract_code,
            "name":                 contract.contract_name,
            "type":                 contract.contract_type,
            "discount_percentage":  float(contract.discount_percentage),
            "is_default":           contract.is_default_contract,
            "requires_verification": contract.requires_verification,
            "display":              display,
            "warning":              "Verify insurance card" if contract.requires_verification else None,
        }