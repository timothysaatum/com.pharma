from datetime import datetime, timedelta, timezone
from typing import Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from fastapi import HTTPException, status
import uuid

from app.models.user.user_model import User, UserSession
from app.schemas.user_schema import (
    UserCreate, LoginRequest
)
from app.core.security import (
    hash_password, verify_password, create_access_token,
    create_refresh_token, decode_token, hash_token, SecurityUtils
)
from app.core.config import get_settings
settings = get_settings()


class AuthService:
    """Authentication service with comprehensive security features"""
    
    @staticmethod
    async def create_user(db: AsyncSession, user_data: UserCreate) -> User:
        """
        Create a new user with password hashing
        
        Args:
            db: Async database session
            user_data: User creation data
            
        Returns:
            Created user object
            
        Raises:
            HTTPException: If username or email already exists
        """
        # Check if username exists
        result = await db.execute(
            select(User).where(User.username == user_data.username)
        )
        existing_user = result.scalar_one_or_none()
        
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already registered"
            )
        
        # Check if email exists
        result = await db.execute(
            select(User).where(User.email == user_data.email.lower())
        )
        existing_email = result.scalar_one_or_none()
        
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Validate password strength
        is_valid, error_msg = SecurityUtils.validate_password_strength(user_data.password)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
        
        # Create user
        user = User(
            id=uuid.uuid4(),
            organization_id=user_data.organization_id,
            username=user_data.username,
            email=user_data.email.lower(),
            full_name=user_data.full_name,
            role=user_data.role,
            phone=user_data.phone,
            employee_id=user_data.employee_id,
            assigned_branches=user_data.assigned_branches or [],
            password_hash=hash_password(user_data.password),
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        
        db.add(user)
        await db.commit()
        await db.refresh(user)
        
        return user
    
    @staticmethod
    async def authenticate_user(
        db: AsyncSession,
        login_data: LoginRequest,
        ip_address: str,
        user_agent: str
    ) -> Tuple[User, str, str]:
        """
        Authenticate user and create session
        
        Args:
            db: Async database session
            login_data: Login credentials
            ip_address: Client IP address
            user_agent: Client user agent
            
        Returns:
            Tuple of (user, access_token, refresh_token)
            
        Raises:
            HTTPException: If authentication fails
        """
        # Find user
        result = await db.execute(
            select(User).where(
                User.username == login_data.username,
                User.deleted_at.is_(None)
            )
        )
        user = result.scalar_one_or_none()
        
        # Generic error message for security
        auth_error = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
        if not user:
            raise auth_error
        
        # Check if account is locked
        if user.account_locked_until:
            if user.account_locked_until > datetime.now(timezone.utc):
                remaining_minutes = int(
                    (user.account_locked_until - datetime.now(timezone.utc)).total_seconds() / 60
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Account locked. Try again in {remaining_minutes} minutes",
                )
            else:
                # Lock expired, clear it
                user.account_locked_until = None
                user.failed_login_attempts = 0
        
        # Verify password
        if not verify_password(login_data.password, user.password_hash):
            # Increment failed attempts
            user.failed_login_attempts += 1
            
            # Lock account if too many failures
            if user.failed_login_attempts >= settings.MAX_LOGIN_ATTEMPTS:
                user.account_locked_until = datetime.now(timezone.utc) + timedelta(
                    minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES
                )
                await db.commit()
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Account locked due to too many failed login attempts. "
                           f"Try again in {settings.ACCOUNT_LOCKOUT_DURATION_MINUTES} minutes",
                )
            
            await db.commit()
            raise auth_error
        
        # Check if user is active
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is inactive",
            )
        
        # Reset failed attempts on successful login
        user.failed_login_attempts = 0
        user.account_locked_until = None
        user.last_login = datetime.now(timezone.utc)
        
        # Create tokens
        access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username, "role": user.role}
        )
        refresh_token = create_refresh_token(
            data={"sub": str(user.id), "type": "refresh"}
        )
        
        # Clean up old sessions (keep only recent ones)
        await AuthService._cleanup_old_sessions(db, user.id)
        
        # Create session
        session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_token(access_token),
            refresh_token_hash=hash_token(refresh_token),
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=datetime.now(timezone.utc) + timedelta(
                minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
            ),
            is_revoked=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        
        db.add(session)
        await db.commit()
        await db.refresh(user)
        
        return user, access_token, refresh_token
    
    @staticmethod
    async def refresh_access_token(
        db: AsyncSession,
        refresh_token: str,
        ip_address: str,
        user_agent: str
    ) -> Tuple[str, str]:
        """
        Refresh access token using refresh token
        
        Args:
            db: Async database session
            refresh_token: Current refresh token
            ip_address: Client IP
            user_agent: Client user agent
            
        Returns:
            Tuple of (new_access_token, new_refresh_token)
            
        Raises:
            HTTPException: If refresh token is invalid
        """
        # Decode refresh token
        payload = decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )
        
        user_id = uuid.UUID(payload.get("sub"))
        
        # Verify session exists
        token_hash_value = hash_token(refresh_token)
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.refresh_token_hash == token_hash_value,
                UserSession.is_revoked == False
            )
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or invalid",
            )
        
        # Get user
        result = await db.execute(
            select(User).where(
                User.id == user_id,
                User.is_active == True,
                User.deleted_at.is_(None)
            )
        )
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        
        # Create new tokens
        new_access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username, "role": user.role}
        )
        new_refresh_token = create_refresh_token(
            data={"sub": str(user.id), "type": "refresh"}
        )
        
        # Update session
        session.token_hash = hash_token(new_access_token)
        session.refresh_token_hash = hash_token(new_refresh_token)
        session.expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
        session.ip_address = ip_address
        session.user_agent = user_agent
        session.updated_at = datetime.now(timezone.utc)
        
        await db.commit()
        
        return new_access_token, new_refresh_token
    
    @staticmethod
    async def logout(db: AsyncSession, user: User, access_token: str) -> None:
        """
        Logout user by revoking session
        
        Args:
            db: Async database session
            user: Current user
            access_token: Current access token
        """
        token_hash_value = hash_token(access_token)
        
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.token_hash == token_hash_value
            )
        )
        session = result.scalar_one_or_none()
        
        if session:
            session.is_revoked = True
            session.revoked_at = datetime.now(timezone.utc)
            await db.commit()
    
    @staticmethod
    async def logout_all_sessions(db: AsyncSession, user: User) -> int:
        """
        Logout user from all devices/sessions
        
        Args:
            db: Async database session
            user: Current user
            
        Returns:
            Number of sessions revoked
        """
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.is_revoked == False
            )
        )
        sessions = result.scalars().all()
        
        count = 0
        for session in sessions:
            session.is_revoked = True
            session.revoked_at = datetime.now(timezone.utc)
            count += 1
        
        await db.commit()
        return count
    
    @staticmethod
    async def change_password(
        db: AsyncSession,
        user: User,
        old_password: str,
        new_password: str
    ) -> None:
        """
        Change user password
        
        Args:
            db: Async database session
            user: Current user
            old_password: Current password
            new_password: New password
            
        Raises:
            HTTPException: If old password is incorrect or new password is weak
        """
        # Verify old password
        if not verify_password(old_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )
        
        # Validate new password strength
        is_valid, error_msg = SecurityUtils.validate_password_strength(new_password)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
        
        # Check new password is different
        if old_password == new_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="New password must be different from current password",
            )
        
        # Update password
        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)
        
        # Revoke all existing sessions for security
        await AuthService.logout_all_sessions(db, user)
        
        await db.commit()
    
    @staticmethod
    async def _cleanup_old_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
        """
        Remove expired and old sessions, keep only recent ones
        
        Args:
            db: Async database session
            user_id: User ID
        """
        # Remove expired sessions
        await db.execute(
            delete(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.expires_at < datetime.now(timezone.utc)
            )
        )
        
        # Remove revoked sessions older than cleanup hours
        cleanup_time = datetime.now(timezone.utc) - timedelta(
            hours=settings.SESSION_CLEANUP_HOURS
        )
        await db.execute(
            delete(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.is_revoked == True,
                UserSession.revoked_at < cleanup_time
            )
        )
        
        # Keep only max sessions per user
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.is_revoked == False,
                UserSession.expires_at > datetime.now(timezone.utc)
            ).order_by(UserSession.created_at.desc())
        )
        active_sessions = result.scalars().all()
        
        if len(active_sessions) > settings.MAX_SESSIONS_PER_USER:
            # Revoke oldest sessions
            for session in active_sessions[settings.MAX_SESSIONS_PER_USER:]:
                session.is_revoked = True
                session.revoked_at = datetime.now(timezone.utc)
        
        await db.commit()
    
    @staticmethod
    async def get_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
        """
        Get all active sessions for a user
        
        Args:
            db: Async database session
            user_id: User ID
            
        Returns:
            List of active sessions
        """
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.is_revoked == False,
                UserSession.expires_at > datetime.now(timezone.utc)
            ).order_by(UserSession.created_at.desc())
        )
        return list(result.scalars().all())