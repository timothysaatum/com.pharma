from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json


class Settings(BaseSettings):
    # -------------------------
    # Core App Settings
    # -------------------------
    PROJECT_NAME: str = "Laso"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    SYSTEM_STATUS: str = "up"
    API_PREFIX: str = "/api/v1"

    # -------------------------
    # Security / Auth
    # -------------------------
    SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 150
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    MAX_LOGIN_ATTEMPTS: int = 5
    LOGIN_ATTEMPT_WINDOW_MINUTES: int = 15
    MAX_ACTIVE_SESSIONS: int = 5

    SUPER_ADMIN_USER_NAME: str = ""
    SUPER_ADMIN_PASSWORD_HASH: str = ""
    SUPER_ADMIN_TOKEN_EXPIRE_MINUTES: int = 60
    
    # Password Security
    MIN_PASSWORD_LENGTH: int = 8
    ACCOUNT_LOCKOUT_DURATION_MINUTES: int = 5
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    
    # Session Management
    MAX_SESSIONS_PER_USER: int = 5
    SESSION_CLEANUP_HOURS: int = 24
    
    # Audit Log
    AUDIT_LOG_ENABLED: bool = True
    AUDIT_LOG_RETENTION_DAYS: int = 90

    # -------------------------
    # Database
    # -------------------------
    DATABASE_URL: str = ""
    ALEMBIC_DB_URL: Optional[str] = None

    # -------------------------
    # Cache / Redis
    # -------------------------
    CACHE_ENABLED: bool = True
    CACHE_TYPE: str = "redis"
    CACHE_DEFAULT_TTL: int = 300
    CACHE_KEY_PREFIX: str = "Laso:"
    REDIS_URL: Optional[str] = None

    # -------------------------
    # CORS
    # -------------------------
    CORS_ORIGINS: List[str] = Field(default_factory=list)

    # -------------------------
    # Email (SMTP)
    # -------------------------
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    FROM_EMAIL: str = ""
    FROM_NAME: str = "Laso System"

    # -------------------------
    # Africa's Talking (SMS)
    # -------------------------
    ARKESEL_API_KEY: Optional[str] = None
    ARKESEL_BASE_URL: Optional[str] = None
    ARKESEL_CONTACTS_URL: Optional[str] = None
    ARKESEL_SENDER_ID: Optional[str] = None
    # -------------------------
    # Model config
    # -------------------------
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # -------------------------
    # Validators
    # -------------------------
    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """
        Allows:
        CORS_ORIGINS='["http://localhost:3000", "http://localhost:5173"]'
        or
        CORS_ORIGINS=http://a.com,http://b.com
        """
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [i.strip() for i in v.split(",")]
        return v

    @field_validator("ALEMBIC_DB_URL", mode="before")
    @classmethod
    def set_alembic_url(cls, v, info):
        if v:
            return v
        return info.data.get("DATABASE_URL")
    
    @property
    def is_production(self) -> bool:
        """Check if running in production"""
        return self.ENVIRONMENT.lower() == "production"
    
    @property
    def access_token_expire_seconds(self) -> int:
        """Get access token expiry in seconds"""
        return self.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    
    @property
    def refresh_token_expire_seconds(self) -> int:
        """Get refresh token expiry in seconds"""
        return self.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings instance for performance.
    Use: settings = get_settings()
    """
    return Settings()
