import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from app.models.user.user_model import User
from app.core.security import hash_password
import os

# Adjust to absolute path for test.db
DATABASE_URL = f"sqlite+aiosqlite:///{os.getcwd()}/backend.laso/test.db"

async def reset_admin():
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.username == "admin"))
        user = result.scalar_one_or_none()
        if user:
            user.password_hash = hash_password("admin123")
            user.is_active = True
            user.failed_login_attempts = 0
            user.account_locked_until = None
            await session.commit()
            print("Admin password reset to admin123")
        else:
            print("Admin user not found")

if __name__ == "__main__":
    import sys
    sys.path.append(os.path.join(os.getcwd(), "backend.laso"))
    asyncio.run(reset_admin())
