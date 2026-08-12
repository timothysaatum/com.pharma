import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dependencies import get_db
from app.schemas.reset_schema import (
    ForgotPasswordRequest,
    ResetPasswordRequest,
    ResetTokenResponse,
    PasswordResetSuccess,
)
from app.services.auth.reset_service import ResetService
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/forgot-password",
    response_model=ResetTokenResponse,
    status_code=status.HTTP_200_OK,
)
async def forgot_password(
    request_data: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Request a password reset link.

    If the email exists in the system, a reset token is generated and
    would be sent via email.  For security, this endpoint always returns
    the same response regardless of whether the email exists, to prevent
    email enumeration.

    **Note:** Email sending is a placeholder.  In production, integrate
    with your SMTP provider.  The token is returned in the response for
    development convenience.
    """
    raw_token = await ResetService.create_reset_token(db, request_data.email)

    if settings.ENVIRONMENT.lower() != "production" and raw_token:
        logger.info(
            "Password reset token for %s: %s",
            request_data.email,
            raw_token,
        )

    if settings.SMTP_HOST and settings.SMTP_USER and raw_token:
        from app.utils.notifications import get_notification_manager
        try:
            notifier = get_notification_manager()
        except RuntimeError:
            notifier = None
        if notifier and notifier.email_notifier:
            reset_link = f"{settings.FRONTEND_URL or 'http://localhost:5173'}/reset-password?token={raw_token}"
            try:
                await notifier.email_notifier.send_email(
                    to=request_data.email,
                    subject="Password Reset Request",
                    html_body=f"""
                    <p>You requested a password reset.</p>
                    <p>Click <a href="{reset_link}">here</a> to reset your password.</p>
                    <p>This link expires in 15 minutes.</p>
                    <p>If you did not request this, please ignore this email.</p>
                    """,
                )
            except Exception as e:
                logger.error("Failed to send reset email: %s", str(e))

    return ResetTokenResponse()


@router.post(
    "/reset-password",
    response_model=PasswordResetSuccess,
    status_code=status.HTTP_200_OK,
)
async def reset_password(
    request_data: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Reset password using a valid reset token.

    Validates the token, checks expiry, enforces password strength,
    updates the password, revokes all active sessions, and clears
    any account lockout.
    """
    await ResetService.reset_password(db, request_data.token, request_data.new_password)

    return PasswordResetSuccess()
