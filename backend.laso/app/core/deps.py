from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone
import uuid

from app.db.dependencies import get_db
from app.core.security import decode_token
from app.models.user.user_model import User, UserSession
from app.core.config import get_settings

settings = get_settings()

# HTTP Bearer token scheme
security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Get the current authenticated user from JWT token
    
    Validates:
    - Token presence
    - Token validity
    - User existence
    - User active status
    - Session validity
    - Account lock status
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = credentials.credentials
    
    # Decode token
    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check token type
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user ID from token
    user_id_str: str = payload.get("sub") or ""
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID format",
        )
    
    # Get user from database
    result = await db.execute(
        select(User)
        .options(selectinload(User.roles))
        .where(
            User.id == user_id,
            User.deleted_at.is_(None)
        )
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    
    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )
    
    # Check account lock
    if user.account_locked_until:
        if user.account_locked_until > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is temporarily locked due to too many failed login attempts",
            )
    
    # Verify session exists and is valid
    from app.core.security import hash_token
    token_hash = hash_token(token)
    
    result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.token_hash == token_hash,
            UserSession.is_revoked == False,
            UserSession.expires_at > datetime.now(timezone.utc)
        )
    )
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Compute effective permissions using role hierarchy
    if user.roles:
        max_level = max(r.level for r in user.roles)
        # Load all roles at or below the user's highest level to pick up inherited permissions
        from app.models.user.user_model import Role as RoleModel
        all_result = await db.execute(
            select(RoleModel).where(
                RoleModel.organization_id == user.organization_id,
                RoleModel.level <= max_level,
            )
        )
        all_roles = all_result.scalars().all()
        effective: set[str] = set()
        for r in all_roles:
            effective.update(r.permissions)
        user._effective_permissions = effective
    else:
        user._effective_permissions = set()
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Get current user and ensure they're active and allowed to access the app.

    Blocks users who must change their password first.
    The /auth/force-change-password endpoint uses get_current_user directly
    to allow through users in this state.
    """
    if current_user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PASSWORD_CHANGE_REQUIRED",
        )
    return current_user


def require_role(*allowed_roles: str):
    """
    [DEPRECATED] Use require_permission instead.
    Hardcoded roles are being replaced by dynamic roles and fixed permissions.
    For now, this maps old roles to Permission.MANAGE_ORGANIZATION as a proxy.
    """
    from app.models.user.user_model import Permission
    return require_permission(Permission.MANAGE_ORGANIZATION)

def require_any_role(allowed_roles: list[str]):
    """[DEPRECATED] Use require_permission instead."""
    from app.models.user.user_model import Permission
    return require_permission(Permission.MANAGE_ORGANIZATION)

def require_permission(permission: str):
    """
    Dependency factory for permission-based access control
    
    Usage:
        @router.post("/drugs", dependencies=[Depends(require_permission("manage_drugs"))])
    """
    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required permission: {permission}",
            )
        return current_user
    
    return permission_checker


def require_any_permission(*permissions: str):
    """
    Dependency factory for endpoints that accept one of several permissions.
    """
    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not any(current_user.has_permission(permission) for permission in permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required one of: {', '.join(permissions)}",
            )
        return current_user

    return permission_checker


async def require_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require a platform-level super administrator."""
    if not current_user.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform super administrator access required",
        )
    return current_user


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """
    Get current user if authenticated, otherwise return None
    Useful for endpoints that work differently for authenticated users
    
    Only swallows 401 (not authenticated) — re-raises 403 (inactive/locked)
    so callers can distinguish "not logged in" from "logged in but blocked".
    """
    if not credentials:
        return None
    
    try:
        return await get_current_user(credentials, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise
        return None


def get_organization_id(
    current_user: User = Depends(get_current_user)
) -> uuid.UUID:
    """
    Get the organization ID of the current user
    Useful for data filtering by organization
    """
    return current_user.organization_id


def get_user_branches(
    current_user: User = Depends(get_current_user)
) -> list[uuid.UUID]:
    """
    Get list of branches the current user can access
    """
    return current_user.assigned_branches


async def verify_branch_access(
    branch_id: uuid.UUID,
    current_user: User = Depends(get_current_user)
) -> uuid.UUID:
    """
    Verify user has access to specific branch
    
    Super admins and admins have access to all branches
    Other users only access assigned branches
    """
    # Only super admins can access any branch in their organization
    if current_user.is_super_admin:
        return branch_id
    
    # Users with no assigned branches cannot access any branch
    if not current_user.assigned_branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No branches assigned. Contact an administrator.",
        )
    
    # Other users must have branch in their assigned list
    if branch_id not in current_user.assigned_branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this branch",
        )
    
    return branch_id


def get_client_ip(request: Request) -> str:
    """
    Get client IP address from request
    Handles proxy headers
    """
    # Check for forwarded IP (when behind proxy)
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
    
    # Fall back to direct client
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    """Get user agent from request"""
    return request.headers.get("User-Agent", "unknown")


async def check_subscription_expiry(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Dependency that checks whether the user's organization subscription has expired.
    Raise 403 if expired; pass-through if still valid.

    Compose into any route that should be blocked for expired subscriptions:
        @router.get("/sales", dependencies=[Depends(check_subscription_expiry)])
    """
    from app.models.pharmacy.pharmacy_model import Organization

    result = await db.execute(
        select(Organization).where(Organization.id == current_user.organization_id)
    )
    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    if org.subscription_expires_at and org.subscription_expires_at.astimezone(timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization subscription has expired. Please renew to continue.",
        )

    return current_user
