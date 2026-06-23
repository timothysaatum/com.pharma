from app.services.auth.auth_service import AuthService
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import (
    get_db, get_current_user, get_current_active_user,
    get_client_ip, get_user_agent, require_permission
)
from app.core.config import get_settings
from app.middleware.rate_limit import rate_limit
from app.schemas.user_schema import (
    UserCreate, UserResponse, LoginRequest, TokenResponse,
    RefreshTokenRequest, PasswordChange
)
from app.models.user.user_model import User

settings = get_settings()


router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_user(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("manage_users"))
):
    """
    Register a new user (Admin only)
    
    - **Requires**: manage_users permission
    - **Validates**: username, email uniqueness and password strength
    - **Returns**: Created user information
    """
    user = await AuthService.create_user(db, user_data)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit(max_requests=5, window_seconds=60))]
)
async def login(
    request: Request,
    login_data: LoginRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Login and receive access tokens
    
    - **Validates**: username and password
    - **Returns**: Access token, refresh token, and user info
    - **Security**: Tracks failed attempts and locks account after max failures
    """
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)
    
    user, access_token, refresh_token = await AuthService.authenticate_user(
        db, login_data, ip_address, user_agent
    )
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_seconds,
        user=UserResponse.model_validate(user)
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: Request,
    refresh_data: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Refresh access token using refresh token
    
    - **Requires**: Valid refresh token
    - **Returns**: New access token and refresh token
    """
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)
    
    new_access_token, new_refresh_token = await AuthService.refresh_access_token(
        db, refresh_data.refresh_token, ip_address, user_agent
    )
    
    # Get user info from new token
    from app.core.security import decode_token
    import uuid
    
    payload = decode_token(new_access_token)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
        )
    
    user_id = uuid.UUID(payload["sub"])
    result = await db.execute(
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    
    return TokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_seconds,
        user=UserResponse.model_validate(user)
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Logout current session
    
    - **Requires**: Valid access token
    - **Action**: Revokes current session
    """
    # Extract token from Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing"
        )
    
    token = auth_header.replace("Bearer ", "")
    await AuthService.logout(db, current_user, token)
    
    return None


@router.post("/logout-all", status_code=status.HTTP_200_OK)
async def logout_all_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Logout from all devices/sessions
    
    - **Requires**: Valid access token
    - **Action**: Revokes all user sessions
    - **Returns**: Number of sessions revoked
    """
    count = await AuthService.logout_all_sessions(db, current_user)
    
    return {
        "message": f"Logged out from {count} session(s)",
        "sessions_revoked": count
    }


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user)
):
    """
    Get current user information
    
    - **Requires**: Valid access token
    - **Returns**: Current user details
    """
    return current_user


@router.post("/change-password", status_code=status.HTTP_401_UNAUTHORIZED)
async def change_password(
    password_data: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Change user password
    
    - **Requires**: Valid access token and current password
    - **Action**: Updates password and revokes all sessions
    - **Returns**: 401 with PASSWORD_CHANGED detail so the frontend
      forces re-login and clears stored tokens
    """
    await AuthService.change_password(
        db,
        current_user,
        password_data.old_password,
        password_data.new_password
    )
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="PASSWORD_CHANGED",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get("/sessions")
async def get_active_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all active sessions for current user
    
    - **Requires**: Valid access token
    - **Returns**: List of active sessions with device info
    """
    sessions = await AuthService.get_user_sessions(db, current_user.id)
    
    return {
        "total": len(sessions),
        "sessions": [
            {
                "id": str(session.id),
                "ip_address": session.ip_address,
                "user_agent": session.user_agent,
                "created_at": session.created_at,
                "expires_at": session.expires_at
            }
            for session in sessions
        ]
    }


@router.get("/verify", status_code=status.HTTP_200_OK)
async def verify_token(
    current_user: User = Depends(get_current_user)
):
    """
    Verify if current token is valid
    
    - **Requires**: Valid access token
    - **Returns**: Token validity status
    """
    return {
        "valid": True,
        "user_id": str(current_user.id),
        "username": current_user.username,
        "is_super_admin": current_user.is_super_admin
    }


@router.get("/permissions")
async def get_user_permissions(
    current_user: User = Depends(get_current_user)
):
    """
    Get current user's aggregated permissions (hierarchical)
    
    - **Requires**: Valid access token
    - **Returns**: Effective permissions after role hierarchy resolution.
      A role at level N inherits all permissions from roles at level < N.
    """
    from app.models.user.user_model import Permission
    
    if current_user.is_super_admin:
        return {
            "is_super_admin": True,
            "permissions": ["*"],
            "branches": current_user.assigned_branches,
            "max_role_level": 999,
        }
    
    effective = getattr(current_user, '_effective_permissions', None)
    if effective is None:
        effective = current_user.get_effective_permissions()
    
    max_level = max((r.level for r in current_user.roles), default=0)
    
    return {
        "is_super_admin": False,
        "permissions": sorted(effective),
        "branches": current_user.assigned_branches,
        "max_role_level": max_level,
    }