from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jwt import InvalidTokenError
import hashlib
import secrets
import uuid

from app.core.config import get_settings
settings = get_settings()

class Argon2PasswordContext:
    """Small passlib-compatible adapter backed by maintained argon2-cffi."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            memory_cost=65_536,
            time_cost=3,
            parallelism=4,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, encoded_hash: str) -> bool:
        try:
            return self._hasher.verify(encoded_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_update(self, encoded_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except InvalidHashError:
            return True


# Single source of truth for password hashing. The compatibility methods keep
# existing model call sites stable while avoiding passlib's deprecated crypt
# module import on Python 3.12+.
pwd_context = Argon2PasswordContext()


class SecurityUtils:
    """Security utility functions"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using Argon2"""
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash"""
        return pwd_context.verify(plain_password, hashed_password)
    
    @staticmethod
    def hash_token(token: str) -> str:
        """Hash a token using SHA256 for storage"""
        return hashlib.sha256(token.encode()).hexdigest()
    
    @staticmethod
    def generate_token(length: int = 32) -> str:
        """Generate a random secure token"""
        return secrets.token_urlsafe(length)
    
    @staticmethod
    def create_access_token(
        data: Dict[str, Any],
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """Create a JWT access token"""
        to_encode = data.copy()
        now = datetime.now(timezone.utc)
        
        if expires_delta:
            expire = now + expires_delta
        else:
            expire = now + timedelta(
                minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
            )
        
        to_encode.update({
            "exp": expire,
            "iat": now,
            "jti": str(uuid.uuid4()),
            "type": "access",
        })
        
        encoded_jwt = jwt.encode(
            to_encode,
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM
        )
        
        return encoded_jwt
    
    @staticmethod
    def create_refresh_token(
        data: Dict[str, Any],
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """Create a JWT refresh token"""
        to_encode = data.copy()
        now = datetime.now(timezone.utc)
        
        if expires_delta:
            expire = now + expires_delta
        else:
            expire = now + timedelta(
                days=settings.REFRESH_TOKEN_EXPIRE_DAYS
            )
        
        to_encode.update({
            "exp": expire,
            "iat": now,
            "jti": str(uuid.uuid4()),
            "type": "refresh",
        })
        
        encoded_jwt = jwt.encode(
            to_encode,
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM
        )
        
        return encoded_jwt
    
    @staticmethod
    def decode_token(token: str) -> Optional[Dict[str, Any]]:
        """Decode and verify a JWT token"""
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM]
            )
            return payload
        except InvalidTokenError:
            return None
    
    @staticmethod
    def validate_password_strength(password: str) -> tuple[bool, str]:
        """
        Validate password strength
        Returns: (is_valid, error_message)
        """
        if len(password) < settings.MIN_PASSWORD_LENGTH:
            return False, f"Password must be at least {settings.MIN_PASSWORD_LENGTH} characters"
        
        if not any(c.isupper() for c in password):
            return False, "Password must contain at least one uppercase letter"
        
        if not any(c.islower() for c in password):
            return False, "Password must contain at least one lowercase letter"
        
        if not any(c.isdigit() for c in password):
            return False, "Password must contain at least one digit"
        
        special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
        if not any(c in special_chars for c in password):
            return False, "Password must contain at least one special character"
        
        # Check for common weak passwords
        weak_passwords = [
            'password', '12345678', 'qwerty', 'abc123',
            'password123', 'admin123', '11111111'
        ]
        if password.lower() in weak_passwords:
            return False, "Password is too common. Please choose a stronger password"
        
        return True, ""
    
    @staticmethod
    def generate_session_id() -> str:
        """Generate a unique session ID"""
        return str(uuid.uuid4())

    # ── TOTP / MFA ──────────────────────────────────────────────────────

    @staticmethod
    def generate_totp_secret() -> str:
        """Generate a random base32 secret for TOTP"""
        import pyotp
        return pyotp.random_base32()

    @staticmethod
    def get_totp_provisioning_uri(secret: str, email: str, issuer: str = "PharmaApp") -> str:
        """Get otpauth:// URI for QR code generation"""
        import pyotp
        return pyotp.TOTP(secret).provisioning_uri(email, issuer_name=issuer)

    @staticmethod
    def get_totp_qr_code_data_uri(provisioning_uri: str) -> str:
        """Render an otpauth:// provisioning URI as a PNG data URL."""
        import base64
        from io import BytesIO

        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        image = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def verify_totp(secret: str, code: str) -> bool:
        """Verify a TOTP code against the secret"""
        import pyotp
        return pyotp.TOTP(secret).verify(code)

    @staticmethod
    def is_admin_user(user) -> bool:
        """Check if a user has admin-level privileges (level >= 100 or MANAGE_ORGANIZATION)."""
        if user.is_super_admin:
            return True
        from app.models.user.user_model import Permission
        return user.has_permission(Permission.MANAGE_ORGANIZATION)


# Convenience functions
def hash_password(password: str) -> str:
    """Hash a password"""
    return SecurityUtils.hash_password(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password"""
    return SecurityUtils.verify_password(plain_password, hashed_password)


def create_access_token(data: Dict[str, Any]) -> str:
    """Create an access token"""
    return SecurityUtils.create_access_token(data)


def create_refresh_token(data: Dict[str, Any]) -> str:
    """Create a refresh token"""
    return SecurityUtils.create_refresh_token(data)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode a token"""
    return SecurityUtils.decode_token(token)


def hash_token(token: str) -> str:
    """Hash a token for storage"""
    return SecurityUtils.hash_token(token)


def generate_totp_secret() -> str:
    """Generate a random base32 secret for TOTP"""
    return SecurityUtils.generate_totp_secret()


def get_totp_provisioning_uri(secret: str, email: str, issuer: str = "PharmaApp") -> str:
    """Get otpauth:// URI for QR code generation"""
    return SecurityUtils.get_totp_provisioning_uri(secret, email, issuer)


def get_totp_qr_code_data_uri(provisioning_uri: str) -> str:
    """Render an otpauth:// provisioning URI as a PNG data URL."""
    return SecurityUtils.get_totp_qr_code_data_uri(provisioning_uri)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code against the secret"""
    return SecurityUtils.verify_totp(secret, code)
