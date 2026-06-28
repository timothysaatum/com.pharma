"""Create or reset the initial superadmin and a default organization."""
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend.laso"))

import dotenv
dotenv.load_dotenv(Path(__file__).parent / "backend.laso" / ".env")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text
from app.models.user.user_model import User
from app.models.pharmacy.pharmacy_model import Organization
from app.core.security import hash_password
from datetime import datetime, timezone

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://cassie1:SatumCassie25@localhost:5433/atlasdb")

async def create_admin():
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Check if admin exists
        result = await session.execute(
            select(User).where(User.username == "admin")
        )
        user = result.scalar_one_or_none()

        if user:
            user.set_password("adminPass!123")
            user.is_active = True
            user.is_super_admin = True
            user.failed_login_attempts = 0
            user.account_locked_until = None
            user.updated_at = datetime.now(timezone.utc)
            await session.commit()
            print(f"Superadmin 'admin' reset (password: adminPass!123)")
        else:
            # Get or create the default organization
            result = await session.execute(
                select(Organization).where(Organization.name == "Default Pharmacy")
            )
            org = result.scalar_one_or_none()
            if not org:
                org = Organization(
                    id=uuid.uuid4(),
                    name="Default Pharmacy",
                    type="pharmacy",
                    is_active=True,
                    subscription_tier="enterprise",
                    settings={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    sync_version=1,
                    sync_status="synced",
                )
                session.add(org)
                await session.flush()
                print(f"Created organization: {org.name} ({org.id})")

            user = User(
                username="admin",
                email="admin@pharmacy.com",
                full_name="System Administrator",
                organization_id=org.id,
                is_super_admin=True,
                is_active=True,
                assigned_branches=[],
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                sync_version=1,
                sync_status="synced",
            )
            user.set_password("adminPass!123")
            session.add(user)
            await session.commit()
            print(f"Superadmin 'admin' created (password: adminPass!123)")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(create_admin())
