from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from app.db.dependencies import get_db
from app.core.deps import get_current_user, get_organization_id, require_permission
from app.models.user.user_model import User, Permission
from app.schemas.user_schema import RoleCreate, RoleUpdate, RoleResponse, PermissionInfo
from app.services.auth.role_service import RoleService

router = APIRouter()

@router.get("/permissions", response_model=List[PermissionInfo])
async def get_available_permissions(
    current_user: User = Depends(get_current_user)
):
    """Get all available system permissions"""
    return [
        PermissionInfo(name=p.value, description=p.name.replace("_", " ").title())
        for p in Permission
    ]

@router.post("/", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    role_data: RoleCreate,
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
    current_user: User = Depends(require_permission(Permission.MANAGE_ORGANIZATION))
):
    """Create a new role for the organization"""
    return await RoleService.create_role(db, organization_id, role_data, current_user)

@router.get("/", response_model=List[RoleResponse])
async def get_roles(
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
    current_user: User = Depends(get_current_user)
):
    """Get all roles for the organization"""
    return await RoleService.get_roles(db, organization_id)

@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
    current_user: User = Depends(get_current_user)
):
    """Get a specific role by ID"""
    return await RoleService.get_role(db, role_id, organization_id)

@router.put("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: uuid.UUID,
    role_data: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
    current_user: User = Depends(require_permission(Permission.MANAGE_ORGANIZATION))
):
    """Update a role"""
    return await RoleService.update_role(db, role_id, organization_id, role_data, current_user)

@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
    current_user: User = Depends(require_permission(Permission.MANAGE_ORGANIZATION))
):
    """Delete a role"""
    await RoleService.delete_role(db, role_id, organization_id)
    return None
