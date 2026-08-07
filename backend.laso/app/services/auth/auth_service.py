from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status
import uuid

from app.models.user.user_model import User, UserSession
from app.schemas.user_schema import (
    UserCreate, LoginRequest
)
from app.core.security import (
    hash_password, verify_password, create_access_token,
    create_refresh_token, decode_token, hash_token, SecurityUtils,
    generate_totp_secret, get_totp_provisioning_uri, get_totp_qr_code_data_uri,
    verify_totp,
)
from app.core.config import get_settings
from app.core.encryption import decrypt_secret, encrypt_secret, SecretDecryptionError
from app.services.audit_service import AuditService
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
            phone=user_data.phone,
            employee_id=user_data.employee_id,
            assigned_branches=user_data.assigned_branches or [],
            password_hash=hash_password(user_data.password),
            is_active=True,
            must_change_password=True,
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
            select(User)
            .options(selectinload(User.roles))
            .where(
                User.username == login_data.username,
                User.deleted_at.is_(None)
            )
        )
        user = result.scalar_one_or_none()
        
        # Generic error message for security (prevents username enumeration)
        auth_error = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
        if not user:
            raise auth_error
        
        # Check if account is locked — use generic message to prevent enumeration
        if user.account_locked_until and user.account_locked_until > datetime.now(timezone.utc):
            raise auth_error
        
        if user.account_locked_until:
            user.account_locked_until = None
            user.failed_login_attempts = 0
        
        # Verify password
        if not verify_password(login_data.password, user.password_hash):
            # Check if the attempt window has expired — reset counter
            window = timedelta(minutes=settings.LOGIN_ATTEMPT_WINDOW_MINUTES)
            if user.last_login and (datetime.now(timezone.utc) - user.last_login) > window:
                user.failed_login_attempts = 0
            
            # Increment failed attempts
            user.failed_login_attempts += 1
            
            was_locked = False
            # Lock account if too many failures within the window
            if user.failed_login_attempts >= settings.MAX_LOGIN_ATTEMPTS:
                user.account_locked_until = datetime.now(timezone.utc) + timedelta(
                    minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES
                )
                was_locked = True
            
            # Audit: failed login attempt
            await AuditService.log(
                db, user.organization_id,
                action="login_failed",
                user_id=user.id,
                entity_type="user",
                entity_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                context_metadata={
                    "attempt": user.failed_login_attempts,
                    "locked": was_locked,
                },
            )
            await db.commit()
            
            raise auth_error
        
        # Check if user is active
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is inactive",
            )
        
        # Check MFA / TOTP before committing any state changes
        if user.two_factor_enabled:
            if not user.two_factor_secret:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="MFA is not configured correctly for this account. Contact your administrator.",
                )
            if not login_data.totp_code:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="MFA_REQUIRED",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                totp_secret = decrypt_secret(user.two_factor_secret)
            except SecretDecryptionError:
                totp_secret = user.two_factor_secret  # legacy plaintext TOTP secret, re-encrypted below after verification
            if not verify_totp(totp_secret, login_data.totp_code):
                now = datetime.now(timezone.utc)
                window = timedelta(minutes=settings.LOGIN_ATTEMPT_WINDOW_MINUTES)
                if user.last_login and (now - user.last_login) > window:
                    user.failed_login_attempts = 0

                user.failed_login_attempts += 1
                was_locked = False
                if user.failed_login_attempts >= settings.MAX_LOGIN_ATTEMPTS:
                    user.account_locked_until = now + timedelta(
                        minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES
                    )
                    was_locked = True

                await AuditService.log(
                    db, user.organization_id,
                    action="mfa_failed",
                    user_id=user.id,
                    entity_type="user",
                    entity_id=user.id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    context_metadata={
                        "method": "totp",
                        "attempt": user.failed_login_attempts,
                        "locked": was_locked,
                    },
                )
                await db.commit()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid verification code",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if user.two_factor_secret == totp_secret:
                user.two_factor_secret = encrypt_secret(totp_secret)

        # Reset failed attempts on successful login
        user.failed_login_attempts = 0
        user.account_locked_until = None
        user.last_login = datetime.now(timezone.utc)
        
        # Create tokens
        access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username}
        )
        refresh_token = create_refresh_token(
            data={"sub": str(user.id), "type": "refresh"}
        )
        
        # Create session
        session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_token(access_token),
            refresh_token_hash=hash_token(refresh_token),
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=datetime.now(timezone.utc) + timedelta(
                days=settings.REFRESH_TOKEN_EXPIRE_DAYS
            ),
            is_revoked=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        
        db.add(session)
        await db.flush()
        
        # Clean up old sessions after adding the new one so the count is accurate
        await AuthService._cleanup_old_sessions(db, user.id)

        # Audit: successful login
        await AuditService.log(
            db, user.organization_id,
            action="login",
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            context_metadata={"method": "password"},
        )

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
        Refresh access token using refresh token (with rotation)
        
        - Revokes the old session on every refresh (rotation)
        - Creates a brand-new session record
        - If a stolen refresh token is used, the legitimate session's
          token_hash no longer matches — detecting theft immediately
        
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
        
        try:
            user_id = uuid.UUID(payload.get("sub", ""))
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )
        
        # Verify session exists and is not expired
        token_hash_value = hash_token(refresh_token)
        result = await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.refresh_token_hash == token_hash_value,
                UserSession.is_revoked == False,
                UserSession.expires_at > datetime.now(timezone.utc),
            )
            .with_for_update()
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or invalid",
            )
        
        # Get user
        result = await db.execute(
            select(User)
            .options(selectinload(User.roles))
            .where(
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
            data={"sub": str(user.id), "username": user.username}
        )
        new_refresh_token = create_refresh_token(
            data={"sub": str(user.id), "type": "refresh"}
        )
        
        # Revoke old session (rotation)
        session.is_revoked = True
        session.revoked_at = datetime.now(timezone.utc)
        
        # Create new session record
        new_session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_token(new_access_token),
            refresh_token_hash=hash_token(new_refresh_token),
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=datetime.now(timezone.utc) + timedelta(
                days=settings.REFRESH_TOKEN_EXPIRE_DAYS
            ),
            is_revoked=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(new_session)
        
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
        count = await AuthService._revoke_all_sessions(db, user)
        await db.commit()
        return count

    @staticmethod
    async def _revoke_all_sessions(db: AsyncSession, user: User) -> int:
        """
        Revoke all user sessions without committing (for batching).
        
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
        user.must_change_password = False
        user.updated_at = datetime.now(timezone.utc)
        
        # Revoke all existing sessions for security
        count = await AuthService._revoke_all_sessions(db, user)
        
        await db.commit()
        
        # Audit: password changed (batch with a second commit for the audit entry)
        await AuditService.log(
            db, user.organization_id,
            action="password_change",
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            changes={"password_changed_at": str(user.password_changed_at)},
            context_metadata={"sessions_revoked": count},
        )
        await db.commit()
    
    @staticmethod
    async def force_change_password(
        db: AsyncSession,
        user: User,
        new_password: str
    ) -> None:
        """
        Force a password change without requiring the old password.
        Used for first-login password change.

        This method does NOT revoke sessions so the user stays logged in.
        """
        # Validate new password strength
        is_valid, error_msg = SecurityUtils.validate_password_strength(new_password)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )

        # Update password
        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        user.must_change_password = False
        user.updated_at = datetime.now(timezone.utc)

        await db.commit()

        await AuditService.log(
            db, user.organization_id,
            action="password_change",
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            changes={"password_changed_at": str(user.password_changed_at)},
            context_metadata={"forced": True},
        )
        await db.commit()

    @staticmethod
    async def admin_reset_password(
        db: AsyncSession,
        target_user: User,
        new_password: str,
        resetter: User
    ) -> None:
        """
        Admin/manager resets another user's password.
        The target user must change password on next login.

        Args:
            db: Async database session
            target_user: The user whose password is being reset
            new_password: The new password (will be validated)
            resetter: The admin/manager performing the reset
        """
        # Validate new password strength
        is_valid, error_msg = SecurityUtils.validate_password_strength(new_password)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )

        # Update password and force change
        target_user.password_hash = hash_password(new_password)
        target_user.password_changed_at = datetime.now(timezone.utc)
        target_user.must_change_password = True
        target_user.updated_at = datetime.now(timezone.utc)

        # Revoke all sessions so the user must log in again
        from app.services.auth.auth_service import AuthService as AS
        await AS._revoke_all_sessions(db, target_user)

        await db.commit()

        await AuditService.log(
            db, resetter.organization_id,
            action="password_reset",
            user_id=resetter.id,
            entity_type="user",
            entity_id=target_user.id,
            changes={"password_changed_at": str(target_user.password_changed_at)},
            context_metadata={"reset_by": str(resetter.id)},
        )
        await db.commit()

    @staticmethod
    async def _cleanup_old_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
        """
        Remove expired and old sessions, keep only recent ones

        Uses ``flush()`` instead of ``commit()`` so callers (e.g. login)
        can own the transaction boundary without a double-commit.

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

        await db.flush()
    
    # ── MFA / TOTP ──────────────────────────────────────────────────────

    @staticmethod
    async def setup_mfa(user: User) -> tuple[str, str, str]:
        """Generate a TOTP secret and provisioning URI for the user.

        Does NOT enable MFA yet — the user must verify with a code first.
        Returns (secret, provisioning_uri, qr_code_data_uri).
        """
        secret = generate_totp_secret()
        uri = get_totp_provisioning_uri(secret, user.email)
        qr_code_data_uri = get_totp_qr_code_data_uri(uri)
        user.two_factor_secret = encrypt_secret(secret)
        user.two_factor_enabled = False  # not active until verified
        return secret, uri, qr_code_data_uri

    @staticmethod
    async def verify_and_enable_mfa(db: AsyncSession, user: User, totp_code: str) -> None:
        """Verify a TOTP code and enable MFA for the user."""
        if not user.two_factor_secret:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="MFA has not been set up. Call setup first.",
            )
        try:
            secret = decrypt_secret(user.two_factor_secret)
        except SecretDecryptionError:
            secret = user.two_factor_secret  # legacy plaintext TOTP secret, re-encrypted below after verification
        if not verify_totp(secret, totp_code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid verification code",
            )
        user.two_factor_enabled = True
        # Transparently migrate legacy plaintext secrets after a valid proof.
        user.two_factor_secret = encrypt_secret(secret)
        user.updated_at = datetime.now(timezone.utc)
        await db.commit()

    @staticmethod
    async def disable_mfa(
        db: AsyncSession, user: User, password: str
    ) -> None:
        """Disable MFA after verifying the user's password."""
        if not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Incorrect password",
            )
        user.two_factor_enabled = False
        user.two_factor_secret = None
        user.updated_at = datetime.now(timezone.utc)
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
