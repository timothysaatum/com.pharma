from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import get_settings

settings = get_settings()


# Determine if we're using SQLite (which doesn't support pool settings)
is_sqlite = "sqlite" in settings.DATABASE_URL.lower()

# Build engine kwargs based on database type
engine_kwargs: dict[str, Any] = {
    "future": True,
    "echo": True,
}

# Only add pool settings for databases that support them (PostgreSQL, MySQL)
if not is_sqlite:
    engine_kwargs.update({
        "pool_size": 20,
        "max_overflow": 40,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "pool_timeout": 30,
    })

engine = create_async_engine(
    settings.DATABASE_URL,
    **engine_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)