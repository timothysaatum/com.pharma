from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from passlib.context import CryptContext
import hashlib
import secrets
import uuid

from app.core.config import get_settings
settings = get_settings()

# Password hashing context (Argon2)
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=65536,  # 64 MB
    argon2__time_cost=3,
    argon2__parallelism=4
)


class SecurityUtils:
    """Security utility functions"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using Argon2"""
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash"""
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except Exception:
            return False
    
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
        
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
            )
        
        to_encode.update({
            "exp": expire,
            "iat": datetime.now(timezone.utc),
            "type": "access"
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
        
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                days=settings.REFRESH_TOKEN_EXPIRE_DAYS
            )
        
        to_encode.update({
            "exp": expire,
            "iat": datetime.now(timezone.utc),
            "type": "refresh"
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
        except JWTError:
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