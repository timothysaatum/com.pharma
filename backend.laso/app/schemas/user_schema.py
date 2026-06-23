from app.schemas.base_schemas import BaseSchema, SyncSchema, TimestampSchema
from pydantic import (
    EmailStr, Field, field_validator, 
    model_validator, ConfigDict
)
from typing import Optional, List
from datetime import datetime
import uuid



class PermissionInfo(BaseSchema):
    name: str
    description: Optional[str] = None

class RoleBase(BaseSchema):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    permissions: List[str] = Field(default_factory=list)
    level: int = Field(
        default=0, ge=0, le=1000,
        description="Hierarchy level. Higher = more authority. "
                    "A role at level N inherits all permissions from roles at level < N."
    )

class RoleCreate(RoleBase):
    pass

class RoleUpdate(BaseSchema):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    permissions: Optional[List[str]] = None
    level: Optional[int] = Field(None, ge=0, le=1000)

class RoleResponse(RoleBase, SyncSchema, TimestampSchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    model_config = ConfigDict(from_attributes=True)


# Computed permission info returned alongside user data
class EffectivePermissionInfo(BaseSchema):
    """Effective permissions after role hierarchy resolution"""
    direct_role_permissions: List[str] = Field(
        default_factory=list,
        description="Permissions from the user's directly assigned roles"
    )
    inherited_permissions: List[str] = Field(
        default_factory=list,
        description="Permissions inherited from lower-level roles via hierarchy"
    )
    effective_permissions: List[str] = Field(
        default_factory=list,
        description="Union of direct and inherited permissions"
    )
    max_role_level: int = Field(0, description="Highest role level assigned to this user")


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
    role_ids: Optional[List[uuid.UUID]] = None
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
    is_super_admin: bool
    roles: List[RoleResponse] = Field(default_factory=list)
    effective_permissions: EffectivePermissionInfo = Field(
        default_factory=EffectivePermissionInfo,
        description="Computed permissions after role hierarchy resolution"
    )
    last_login: Optional[datetime] = None
    two_factor_enabled: bool
    deleted_at: Optional[datetime] = None
    account_locked_until: Optional[datetime] = None
    # Security: Never expose password hash or 2FA secret
    model_config = ConfigDict(
        from_attributes=True
    )

    @model_validator(mode='before')
    @classmethod
    def _populate_effective_permissions(cls, data):
        if isinstance(data, dict):
            return data
        user = data
        if hasattr(user, '_effective_permissions') and user._effective_permissions is not None:
            effective = user._effective_permissions
            max_level = 0
            if user.roles:
                max_level = max(r.level for r in user.roles)
            data.effective_permissions = EffectivePermissionInfo(
                direct_role_permissions=sorted({
                    p for role in user.roles for p in role.permissions
                }),
                inherited_permissions=sorted(effective - {
                    p for role in user.roles for p in role.permissions
                }),
                effective_permissions=sorted(effective),
                max_role_level=max_level,
            )
        return data



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

