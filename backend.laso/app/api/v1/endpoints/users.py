from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import uuid

from app.core.deps import get_db, require_permission
from app.models.user.user_model import User, Role, Permission
from app.schemas.user_schema import UserCreate, UserUpdate, UserResponse
from app.services.auth.auth_service import AuthService
from app.utils.pagination import PaginatedResponse

router = APIRouter(prefix="/users")

def _branch_id_set(branches) -> set[str]:
    return {str(branch_id) for branch_id in (branches or [])}


def _shares_branch(user: User, branch_ids: set[str]) -> bool:
    return bool(_branch_id_set(user.assigned_branches) & branch_ids)


def _ensure_manager_can_manage_user(current_user: User, user: User) -> None:
    manager_branch_ids = _branch_id_set(current_user.assigned_branches)
    if user.is_super_admin or not _shares_branch(user, manager_branch_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")


def _ensure_manager_branch_assignment(current_user: User, assigned_branches) -> None:
    manager_branch_ids = _branch_id_set(current_user.assigned_branches)
    requested_branch_ids = _branch_id_set(assigned_branches)

    if not requested_branch_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assign the user to at least one of your branches",
        )

    if not requested_branch_ids.issubset(manager_branch_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managers can only assign users to their own branches",
        )


# ─────────────────────────────────────────────────────────────────────────────
# List users
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=PaginatedResponse[UserResponse])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=100),
    is_active: Optional[bool] = Query(None),
    branch_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """
    List users in the organization with optional filtering.
    """
    base_query = select(User).where(
        User.organization_id == current_user.organization_id,
        User.deleted_at.is_(None),
    ).options(selectinload(User.roles))

    manager_branch_ids = _branch_id_set(current_user.assigned_branches)
    branch_filter_id = str(branch_id) if branch_id else None

    # Simplified: If user has MANAGE_USERS but is limited to certain branches,
    # they only see users in those branches.
    if not current_user.is_super_admin and current_user.assigned_branches:
         pass # Filtering logic below handles this

    if search:
        term = f"%{search.strip()}%"
        base_query = base_query.where(
            or_(
                User.full_name.ilike(term),
                User.username.ilike(term),
                User.email.ilike(term),
                User.employee_id.ilike(term),
            )
        )

    if is_active is not None:
        base_query = base_query.where(User.is_active == is_active)

    result = await db.execute(base_query.order_by(User.full_name.asc()))
    filtered_users = list(result.scalars().all())

    # Apply branch filtering and manager-level restrictions
    if not current_user.is_super_admin and current_user.assigned_branches:
        filtered_users = [
            user for user in filtered_users
            if _shares_branch(user, manager_branch_ids)
        ]

    if branch_filter_id:
        filtered_users = [
            user for user in filtered_users
            if branch_filter_id in _branch_id_set(user.assigned_branches)
        ]

    total = len(filtered_users)

    # Paginate
    offset = (page - 1) * page_size
    users = filtered_users[offset:offset + page_size]

    total_pages = max(1, (total + page_size - 1) // page_size)

    return {
        "items": [UserResponse.model_validate(u) for u in users],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Get single user
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """
    Retrieve a single user by ID.
    """
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == current_user.organization_id,
            User.deleted_at.is_(None),
        ).options(selectinload(User.roles))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not current_user.is_super_admin and current_user.assigned_branches:
        _ensure_manager_can_manage_user(current_user, user)

    return UserResponse.model_validate(user)


# ─────────────────────────────────────────────────────────────────────────────
# Create user  (delegates to AuthService to reuse password validation / hashing)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """
    Create a new user in the organization.
    """
    if not user_data.organization_id:
        user_data = user_data.model_copy(
            update={"organization_id": current_user.organization_id}
        )

    if not current_user.is_super_admin and current_user.assigned_branches:
        _ensure_manager_branch_assignment(current_user, user_data.assigned_branches)

    user = await AuthService.create_user(db, user_data)
    # Reload with roles
    result = await db.execute(
        select(User).where(User.id == user.id).options(selectinload(User.roles))
    )
    user = result.scalar_one()
    return UserResponse.model_validate(user)


# ─────────────────────────────────────────────────────────────────────────────
# Update user
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    update_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """
    Update user fields.
    """
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == current_user.organization_id,
            User.deleted_at.is_(None),
        ).options(selectinload(User.roles))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use /auth/me or /auth/change-password to update your own account",
        )

    if not current_user.is_super_admin and current_user.assigned_branches:
        _ensure_manager_can_manage_user(current_user, user)
        if update_data.assigned_branches is not None:
            _ensure_manager_branch_assignment(current_user, update_data.assigned_branches)

    # Apply updates
    from datetime import datetime, timezone

    changes = update_data.model_dump(exclude_unset=True)

    if "role_ids" in changes:
        role_ids = changes.pop("role_ids")
        result = await db.execute(
            select(Role).where(
                Role.organization_id == current_user.organization_id,
                Role.id.in_(role_ids)
            )
        )
        user.roles = list(result.scalars().all())

    for field, value in changes.items():
        setattr(user, field, value)

    user.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


# ─────────────────────────────────────────────────────────────────────────────
# Activate / Deactivate
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{user_id}/activate", response_model=UserResponse)
async def activate_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """Re-activate a previously deactivated user."""
    return await _set_active(db, user_id, current_user, True)


@router.post("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """Deactivate a user account (non-destructive; preserves all data)."""
    return await _set_active(db, user_id, current_user, False)


async def _set_active(
    db: AsyncSession, user_id: uuid.UUID, current_user: User, active: bool
) -> UserResponse:
    from datetime import datetime, timezone
    from app.services.auth.auth_service import AuthService as AS

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == current_user.organization_id,
            User.deleted_at.is_(None),
        ).options(selectinload(User.roles))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own active status")

    if not current_user.is_super_admin and current_user.assigned_branches:
        _ensure_manager_can_manage_user(current_user, user)

    user.is_active = active
    user.updated_at = datetime.now(timezone.utc)

    # Revoke all sessions when deactivating
    if not active:
        await AS.logout_all_sessions(db, user)

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


# ─────────────────────────────────────────────────────────────────────────────
# Unlock account  (clear failed-login lockout)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{user_id}/unlock", response_model=UserResponse)
async def unlock_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """
    Manually clear an account lockout caused by too many failed login attempts.
    """
    from datetime import datetime, timezone

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == current_user.organization_id,
            User.deleted_at.is_(None),
        ).options(selectinload(User.roles))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not current_user.is_super_admin and current_user.assigned_branches:
        _ensure_manager_can_manage_user(current_user, user)

    user.account_locked_until = None
    user.failed_login_attempts = 0
    user.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


# ─────────────────────────────────────────────────────────────────────────────
# Soft delete
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """
    Soft-delete a user. The record is retained for audit purposes.
    """
    from datetime import datetime, timezone
    from app.services.auth.auth_service import AuthService as AS

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == current_user.organization_id,
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    if not current_user.is_super_admin and current_user.assigned_branches:
        _ensure_manager_can_manage_user(current_user, user)

    # Revoke all sessions first
    await AS.logout_all_sessions(db, user)

    user.deleted_at = datetime.now(timezone.utc)
    user.is_active = False
    user.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return None
