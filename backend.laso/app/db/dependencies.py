from typing import AsyncGenerator

from app.db.session import AsyncSessionLocal



async def get_db() -> AsyncGenerator:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
