from pydantic import Field, field_validator, model_validator
from typing import Optional
import uuid

from app.schemas.base_schemas import BaseSchema


class ForgotPasswordRequest(BaseSchema):
    email: str = Field(
        ...,
        max_length=255,
        description="Registered email address for the account"
    )


class ResetPasswordRequest(BaseSchema):
    token: str = Field(
        ...,
        min_length=20,
        description="Password reset token received via email"
    )
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=100,
        description="New password meeting strength requirements"
    )

    @field_validator('new_password')
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")

        checks = [
            (any(c.isupper() for c in v), "Password must contain at least one uppercase letter"),
            (any(c.islower() for c in v), "Password must contain at least one lowercase letter"),
            (any(c.isdigit() for c in v), "Password must contain at least one digit"),
            (any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v),
             "Password must contain at least one special character")
        ]

        for check, error in checks:
            if not check:
                raise ValueError(error)

        weak_passwords = ['password', '12345678', 'qwerty', 'abc123',
                          'password123', 'admin123', '11111111']
        if v.lower() in weak_passwords:
            raise ValueError("Password is too common. Please choose a stronger password")

        return v


class ResetTokenResponse(BaseSchema):
    message: str = Field(default="If the email exists, a reset link has been sent")


class PasswordResetSuccess(BaseSchema):
    message: str = Field(default="Password has been reset successfully. Please log in with your new password.")
