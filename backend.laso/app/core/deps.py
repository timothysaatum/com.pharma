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
        else:
            # Lock expired, clear it
            user.account_locked_until = None
            user.failed_login_attempts = 0
            await db.commit()
    
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
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Get current user and ensure they're active
    (Alias for clarity in endpoints)
    """
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


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """
    Get current user if authenticated, otherwise return None
    Useful for endpoints that work differently for authenticated users
    """
    if not credentials:
        return None
    
    try:
        return await get_current_user(credentials, db)
    except HTTPException:
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
    # Super admins and org admins can access any branch in their organization
    if current_user.is_super_admin or not current_user.assigned_branches:
        return branch_id
    
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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    
    # Check for real IP header
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    
    # Fall back to direct client
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    """Get user agent from request"""
    return request.headers.get("User-Agent", "unknown")
