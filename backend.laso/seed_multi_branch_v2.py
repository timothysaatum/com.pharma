import asyncio
from sqlalchemy import select, delete
from app.db.base import Base
from app.db.session import engine, AsyncSessionLocal
from app.models.user.user_model import User
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.core.security import hash_password
import os
import uuid

async def seed():
    async with AsyncSessionLocal() as db:
        org_q = await db.execute(select(Organization).limit(1))
        org = org_q.scalar_one()

        # Create unique branches
        b1 = Branch(id=uuid.uuid4(), organization_id=org.id, name="North Clinic", code="NC001", is_active=True)
        b2 = Branch(id=uuid.uuid4(), organization_id=org.id, name="South Clinic", code="SC002", is_active=True)
        db.add_all([b1, b2])
        await db.flush()

        admin_password = os.getenv("TEST_ADMIN_PASSWORD", "password123")
        user = User(
            id=uuid.uuid4(),
            username="multi_admin",
            email="multi@example.com",
            full_name="Multi Branch Admin",
            password_hash=hash_password(admin_password),
            role="admin",
            is_active=True,
            organization_id=org.id,
            permissions={"additional": ["*"], "denied": []},
            assigned_branches=[b1.id, b2.id]
        )
        db.add(user)
        await db.commit()
        print("Multi-branch user seeded successfully.")

if __name__ == "__main__":
    asyncio.run(seed())
