from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from fastapi import HTTPException, status
import uuid

from app.models.user.user_model import Role, Permission, User
from app.schemas.user_schema import RoleCreate, RoleUpdate


class RoleService:
    """Service for managing organization-specific roles"""

    @staticmethod
    def _validate_permissions(permissions: list[str], is_super_admin: bool) -> None:
        valid_permissions = {p.value for p in Permission}
        for perm in permissions:
            if perm == "*":
                if not is_super_admin:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Wildcard permission '*' is reserved for super admins",
                    )
                continue
            if perm not in valid_permissions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid permission: {perm}"
                )

    @staticmethod
    async def _validate_level_not_duplicate(
        db: AsyncSession,
        organization_id: uuid.UUID,
        level: int,
        exclude_role_id: uuid.UUID | None = None,
    ) -> None:
        """Warn but don't block duplicate levels — multiple roles can share a level"""
        query = select(Role).where(
            and_(
                Role.organization_id == organization_id,
                Role.level == level,
            )
        )
        if exclude_role_id:
            query = query.where(Role.id != exclude_role_id)
        result = await db.execute(query)
        existing = result.scalar_one_or_none()
        if existing:
            import logging
            logging.getLogger(__name__).warning(
                "Duplicate role level %d — role '%s' already has this level. "
                "Multiple roles can share the same level; their permissions are merged.",
                level, existing.name,
            )

    @staticmethod
    async def create_role(
        db: AsyncSession,
        organization_id: uuid.UUID,
        role_data: RoleCreate,
        current_user: User | None = None,
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

        # Validate permissions (restrict wildcard to super admins)
        is_super = current_user.is_super_admin if current_user else False
        RoleService._validate_permissions(role_data.permissions, is_super)

        await RoleService._validate_level_not_duplicate(db, organization_id, role_data.level)

        role = Role(
            id=uuid.uuid4(),
            organization_id=organization_id,
            name=role_data.name,
            description=role_data.description,
            level=role_data.level,
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
        """Get all roles for an organization, ordered by level ascending"""
        result = await db.execute(
            select(Role)
            .where(Role.organization_id == organization_id)
            .order_by(Role.level.asc(), Role.name.asc())
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
        role_data: RoleUpdate,
        current_user: User | None = None,
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
            # Validate permissions (restrict wildcard to super admins)
            is_super = current_user.is_super_admin if current_user else False
            RoleService._validate_permissions(role_data.permissions, is_super)
            role.permissions = role_data.permissions

        if role_data.level is not None:
            await RoleService._validate_level_not_duplicate(
                db, organization_id, role_data.level, exclude_role_id=role_id
            )
            role.level = role_data.level

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
