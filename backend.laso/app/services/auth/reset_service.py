from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException, status
import secrets
import hashlib
import logging

from app.models.user.user_model import User
from app.core.config import get_settings
from app.core.security import hash_password, SecurityUtils

logger = logging.getLogger(__name__)
settings = get_settings()

RESET_TOKEN_EXPIRE_MINUTES = 15


class ResetService:
    """Password reset service with token-based verification"""

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _generate_reset_token() -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(32)
        token_hash = ResetService._hash_token(raw_token)
        return raw_token, token_hash

    @staticmethod
    async def create_reset_token(db: AsyncSession, email: str) -> str | None:
        result = await db.execute(
            select(User).where(
                User.email == email.lower(),
                User.deleted_at.is_(None)
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            return None

        raw_token, token_hash = ResetService._generate_reset_token()
        user.reset_token_hash = token_hash
        user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=RESET_TOKEN_EXPIRE_MINUTES
        )
        await db.commit()

        return raw_token

    @staticmethod
    async def reset_password(
        db: AsyncSession,
        token: str,
        new_password: str
    ) -> None:
        token_hash = ResetService._hash_token(token)

        result = await db.execute(
            select(User).where(
                User.reset_token_hash == token_hash,
                User.deleted_at.is_(None)
            )
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token",
            )

        if not user.reset_token_expires_at or user.reset_token_expires_at < datetime.now(timezone.utc):
            user.reset_token_hash = None
            user.reset_token_expires_at = None
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset token has expired. Please request a new one.",
            )

        is_valid, error_msg = SecurityUtils.validate_password_strength(new_password)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg,
            )

        if user.verify_password(new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="New password must be different from your current password",
            )

        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        user.reset_token_hash = None
        user.reset_token_expires_at = None
        user.failed_login_attempts = 0
        user.account_locked_until = None

        from app.services.auth.auth_service import AuthService
        await AuthService._revoke_all_sessions(db, user)

        await db.commit()

    @staticmethod
    async def clear_expired_tokens(db: AsyncSession) -> int:
        result = await db.execute(
            select(User).where(
                User.reset_token_expires_at.isnot(None),
                User.reset_token_expires_at < datetime.now(timezone.utc)
            )
        )
        users = result.scalars().all()
        count = 0
        for user in users:
            user.reset_token_hash = None
            user.reset_token_expires_at = None
            count += 1
        if count:
            await db.commit()
        return count
