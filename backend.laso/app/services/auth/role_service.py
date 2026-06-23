from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from fastapi import HTTPException, status
import uuid

from app.models.user.user_model import Role, Permission
from app.schemas.user_schema import RoleCreate, RoleUpdate


class RoleService:
    """Service for managing organization-specific roles"""

    @staticmethod
    async def create_role(
        db: AsyncSession,
        organization_id: uuid.UUID,
        role_data: RoleCreate
    ) -> Role:
        """Create a new role for an organization"""
        # Check if role name already exists in organization
        result = await db.execute(
            select(Role).where(
                and_(
                    Role.organization_id == organization_id,
                    Role.name == role_data.name
                )
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Role with name '{role_data.name}' already exists"
            )

        # Validate permissions
        valid_permissions = {p.value for p in Permission}
        for perm in role_data.permissions:
            if perm != "*" and perm not in valid_permissions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid permission: {perm}"
                )

        role = Role(
            id=uuid.uuid4(),
            organization_id=organization_id,
            name=role_data.name,
            description=role_data.description,
            permissions=role_data.permissions,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(role)
        await db.commit()
        await db.refresh(role)
        return role

    @staticmethod
    async def get_roles(
        db: AsyncSession,
        organization_id: uuid.UUID
    ) -> List[Role]:
        """Get all roles for an organization"""
        result = await db.execute(
            select(Role).where(Role.organization_id == organization_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_role(
        db: AsyncSession,
        role_id: uuid.UUID,
        organization_id: uuid.UUID
    ) -> Role:
        """Get a specific role"""
        result = await db.execute(
            select(Role).where(
                and_(
                    Role.id == role_id,
                    Role.organization_id == organization_id
                )
            )
        )
        role = result.scalar_one_or_none()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role not found"
            )
        return role

    @staticmethod
    async def update_role(
        db: AsyncSession,
        role_id: uuid.UUID,
        organization_id: uuid.UUID,
        role_data: RoleUpdate
    ) -> Role:
        """Update a role"""
        role = await RoleService.get_role(db, role_id, organization_id)

        if role_data.name is not None:
            # Check if name is taken by another role in the same org
            result = await db.execute(
                select(Role).where(
                    and_(
                        Role.organization_id == organization_id,
                        Role.name == role_data.name,
                        Role.id != role_id
                    )
                )
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Role with name '{role_data.name}' already exists"
                )
            role.name = role_data.name

        if role_data.description is not None:
            role.description = role_data.description

        if role_data.permissions is not None:
            # Validate permissions
            valid_permissions = {p.value for p in Permission}
            for perm in role_data.permissions:
                if perm != "*" and perm not in valid_permissions:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid permission: {perm}"
                    )
            role.permissions = role_data.permissions

        role.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(role)
        return role

    @staticmethod
    async def delete_role(
        db: AsyncSession,
        role_id: uuid.UUID,
        organization_id: uuid.UUID
    ) -> bool:
        """Delete a role"""
        role = await RoleService.get_role(db, role_id, organization_id)
        await db.delete(role)
        await db.commit()
        return True
