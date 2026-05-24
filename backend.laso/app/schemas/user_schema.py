from app.schemas.base_schemas import BaseSchema, SyncSchema, TimestampSchema
from pydantic import (
    EmailStr, Field, field_validator, 
    model_validator, ConfigDict
)
from typing import Optional, List
from datetime import datetime
import uuid



class UserBase(BaseSchema):
    username: str = Field(
        ..., 
        min_length=3, 
        max_length=100,
        pattern="^[a-zA-Z0-9_-]+$",
        description="Username (alphanumeric, dash, underscore)"
    )
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=255)
    role: Optional[str] = Field(
        None,
        pattern="^(super_admin|admin|manager|pharmacist|cashier|viewer)$"
    )
    phone: Optional[str] = Field(None, max_length=20)
    employee_id: Optional[str] = Field(None, max_length=50)
    assigned_branches: Optional[List[uuid.UUID]] = Field(
        default_factory=list,
        description="Branch IDs this user can access"
    )


class UserCreate(UserBase):
    password: str = Field(
        ..., 
        min_length=8, 
        max_length=100,
        description="Password must be at least 8 characters"
    )
    organization_id: Optional[uuid.UUID]= None

    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Enforce password complexity"""
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
        
        # Check for common weak passwords
        weak_passwords = ['password', '12345678', 'qwerty', 'abc123']
        if v.lower() in weak_passwords:
            raise ValueError("Password is too common. Please choose a stronger password")
        
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "john_doe",
                "email": "john@medicare.com",
                "full_name": "John Doe",
                "password": "SecureP@ss123",
                "role": "cashier",
                "phone": "+1234567890",
                "employee_id": "EMP001",
                "organization_id": "123e4567-e89b-12d3-a456-426614174000",
                "assigned_branches": ["123e4567-e89b-12d3-a456-426614174001"]
            }
        }
    )

class UserUpdate(BaseSchema):
    full_name: Optional[str] = Field(None, min_length=2, max_length=255)
    phone: Optional[str] = None
    role: Optional[str] = Field(
        None,
        pattern="^(super_admin|admin|manager|pharmacist|cashier|viewer)$"
    )
    assigned_branches: Optional[List[uuid.UUID]] = None
    is_active: Optional[bool] = None


class PasswordChange(BaseSchema):
    old_password: str = Field(..., min_length=8)
    new_password: str = Field(..., min_length=8, max_length=100)
    
    @model_validator(mode='after')
    def validate_passwords(self) -> 'PasswordChange':
        """Ensure new password is different from old"""
        if self.old_password == self.new_password:
            raise ValueError("New password must be different from old password")
        return self


class UserResponse(UserBase, TimestampSchema, SyncSchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    last_login: Optional[datetime] = None
    two_factor_enabled: bool
    deleted_at: Optional[datetime] = None
    account_locked_until: Optional[datetime] = None
    # Security: Never expose password hash or 2FA secret
    model_config = ConfigDict(
        from_attributes=True
    )



class LoginRequest(BaseSchema):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    device_info: Optional[str] = Field(None, max_length=500)

    # Security: Don't include password in logs/examples
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "john_doe",
                "password": "********",
                "device_info": "Chrome/Windows 10"
            }
        }
    )


class TokenResponse(BaseSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token expiry in seconds")
    user: UserResponse


class RefreshTokenRequest(BaseSchema):
    refresh_token: str = Field(..., min_length=20)

