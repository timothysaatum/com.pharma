"""
Insurance Provider Service

Handles all insurance provider business logic including:
- CRUD operations
- Validation
- Deduplication checks
- Status management
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from fastapi import HTTPException, status
from datetime import datetime, timezone
import uuid

from app.models.pricing.pricing_model import InsuranceProvider
from app.models.user.user_model import User
from app.schemas.insurance_provider_schemas import (
    InsuranceProviderCreate,
    InsuranceProviderUpdate,
    InsuranceProviderResponse,
    InsuranceProviderSearchItem,
)


class InsuranceProviderService:
    """Service for managing insurance providers"""

    @staticmethod
    async def create(
        db: AsyncSession,
        data: InsuranceProviderCreate,
        user: User,
    ) -> InsuranceProviderResponse:
        """
        Create a new insurance provider.
        
        Validates:
        - Code is unique per organization
        - Email format if provided
        - Phone format if provided
        """
        # Check for duplicate code
        existing = await db.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.code == data.code.upper(),
                InsuranceProvider.organization_id == user.organization_id,
                InsuranceProvider.is_deleted == False,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Insurance provider with code '{data.code}' already exists",
            )

        provider = InsuranceProvider(
            id=uuid.uuid4(),
            organization_id=user.organization_id,
            name=data.name.strip(),
            code=data.code.upper(),
            logo_url=data.logo_url,
            phone=data.phone,
            email=data.email,
            website=data.website,
            address=data.address.model_dump(exclude_none=True) if data.address else None,
            primary_contact_name=data.primary_contact_name,
            primary_contact_phone=data.primary_contact_phone,
            primary_contact_email=data.primary_contact_email,
            billing_cycle=data.billing_cycle,
            payment_terms=data.payment_terms,
            requires_card_verification=data.requires_card_verification,
            requires_preauth=data.requires_preauth,
            verification_endpoint=data.verification_endpoint,
            is_active=data.is_active,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            sync_status="pending",
            sync_version=1,
        )

        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        
        return InsuranceProviderResponse.model_validate(provider)

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        provider_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> InsuranceProviderResponse:
        """Get insurance provider by ID"""
        result = await db.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.id == provider_id,
                InsuranceProvider.organization_id == organization_id,
                InsuranceProvider.is_deleted == False,
            )
        )
        provider = result.scalar_one_or_none()
        
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Insurance provider not found",
            )
        
        return InsuranceProviderResponse.model_validate(provider)

    @staticmethod
    async def list(
        db: AsyncSession,
        organization_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        active_only: bool = False,
    ) -> tuple[list[InsuranceProviderResponse], int]:
        """
        List insurance providers with pagination.
        
        Args:
            active_only: If True, only return active providers
        """
        query = select(InsuranceProvider).where(
            InsuranceProvider.organization_id == organization_id,
            InsuranceProvider.is_deleted == False,
        )
        
        if active_only:
            query = query.where(InsuranceProvider.is_active == True)
        
        # Get total count
        count_query = select(func.count(InsuranceProvider.id)).where(
            InsuranceProvider.organization_id == organization_id,
            InsuranceProvider.is_deleted == False,
        )
        if active_only:
            count_query = count_query.where(InsuranceProvider.is_active == True)
        
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0
        
        # Get paginated results
        query = query.order_by(InsuranceProvider.name).offset(skip).limit(limit)
        result = await db.execute(query)
        providers = result.scalars().all()
        
        return (
            [InsuranceProviderResponse.model_validate(p) for p in providers],
            total,
        )

    @staticmethod
    async def search(
        db: AsyncSession,
        organization_id: uuid.UUID,
        query: str = "",
        active_only: bool = True,
    ) -> list[InsuranceProviderSearchItem]:
        """
        Search insurance providers by name or code.
        Returns simplified response for dropdowns.
        """
        search_term = f"%{query.lower()}%"
        
        filters = [
            InsuranceProvider.organization_id == organization_id,
            InsuranceProvider.is_deleted == False,
        ]
        
        if active_only:
            filters.append(InsuranceProvider.is_active == True)
        
        filters.append(
            (
                func.lower(InsuranceProvider.name).ilike(search_term)
                | func.lower(InsuranceProvider.code).ilike(search_term)
            )
        )
        
        sql_query = select(InsuranceProvider).where(and_(*filters)).order_by(InsuranceProvider.name).limit(100)
        
        result = await db.execute(sql_query)
        providers = result.scalars().all()
        
        return [
            InsuranceProviderSearchItem(
                id=p.id,
                name=p.name,
                code=p.code,
                logo_url=p.logo_url,
                is_active=p.is_active,
            )
            for p in providers
        ]

    @staticmethod
    async def update(
        db: AsyncSession,
        provider_id: uuid.UUID,
        data: InsuranceProviderUpdate,
        organization_id: uuid.UUID,
    ) -> InsuranceProviderResponse:
        """Update insurance provider"""
        result = await db.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.id == provider_id,
                InsuranceProvider.organization_id == organization_id,
                InsuranceProvider.is_deleted == False,
            )
        )
        provider = result.scalar_one_or_none()
        
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Insurance provider not found",
            )
        
        # Update fields
        update_data = data.model_dump(exclude_none=True)
        
        if "address" in update_data and update_data["address"]:
            update_data["address"] = update_data["address"].model_dump(exclude_none=True)
        
        for field, value in update_data.items():
            if field == "code":
                # Don't allow code changes
                continue
            if field == "name" and value:
                value = value.strip()
            setattr(provider, field, value)
        
        provider.updated_at = datetime.now(timezone.utc)
        provider.sync_status = "pending"
        
        await db.commit()
        await db.refresh(provider)
        
        return InsuranceProviderResponse.model_validate(provider)

    @staticmethod
    async def deactivate(
        db: AsyncSession,
        provider_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> InsuranceProviderResponse:
        """Deactivate an insurance provider"""
        result = await db.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.id == provider_id,
                InsuranceProvider.organization_id == organization_id,
                InsuranceProvider.is_deleted == False,
            )
        )
        provider = result.scalar_one_or_none()
        
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Insurance provider not found",
            )
        
        # Check if provider is used in active contracts
        from app.models.pricing.pricing_model import PriceContract
        
        active_contracts = await db.execute(
            select(func.count(PriceContract.id)).where(
                PriceContract.insurance_provider_id == provider_id,
                PriceContract.status.in_(["draft", "active"]),
            )
        )
        contract_count = active_contracts.scalar() or 0
        
        if contract_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot deactivate: {contract_count} active contract(s) still use this provider",
            )
        
        provider.is_active = False
        provider.updated_at = datetime.now(timezone.utc)
        provider.sync_status = "pending"
        
        await db.commit()
        await db.refresh(provider)
        
        return InsuranceProviderResponse.model_validate(provider)

    @staticmethod
    async def delete(
        db: AsyncSession,
        provider_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> None:
        """Soft delete an insurance provider"""
        result = await db.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.id == provider_id,
                InsuranceProvider.organization_id == organization_id,
                InsuranceProvider.is_deleted == False,
            )
        )
        provider = result.scalar_one_or_none()
        
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Insurance provider not found",
            )
        
        # Check if provider is used in contracts
        from app.models.pricing.pricing_model import PriceContract
        
        contracts = await db.execute(
            select(func.count(PriceContract.id)).where(
                PriceContract.insurance_provider_id == provider_id,
            )
        )
        contract_count = contracts.scalar() or 0
        
        if contract_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete: {contract_count} contract(s) still reference this provider",
            )
        
        provider.is_deleted = True
        provider.deleted_at = datetime.now(timezone.utc)
        provider.updated_at = datetime.now(timezone.utc)
        
        await db.commit()
