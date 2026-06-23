import pytest
import pytest_asyncio
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User
from app.models.inventory.inventory_model import Drug
from app.models.customer.customer_model import Customer

DATABASE_URL_TEST = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture(scope="function")
async def db():
    engine = create_async_engine(DATABASE_URL_TEST)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session_factory = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        yield session

    await engine.dispose()

@pytest_asyncio.fixture(scope="function")
async def setup_test_data(db: AsyncSession):
    """Create test data: organization, branch, user, drugs, customer."""
    org = Organization(
        id=uuid.uuid4(),
        name="Test Pharmacy",
        type="pharmacy",
        tax_id="123456789",
        settings={"loyalty": {"points_per_unit": "1.0", "tier_thresholds": {"silver": 100, "gold": 500, "platinum": 1000}}}
    )

    branch = Branch(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Test Branch",
        code="TB001",
        is_active=True,
        is_deleted=False,
    )

    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username="test_user",
        email="test@pharmacy.com",
        password_hash="hashed_pwd",
        full_name="Test User",
        is_super_admin=True,
        is_active=True,
        assigned_branches=[branch.id],
    )

    drugs = [
        Drug(
            id=uuid.uuid4(),
            organization_id=org.id,
            name=f"Drug {i}",
            sku=f"SKU{i:03d}",
            unit_price=Decimal("50.00"),
            reorder_level=10,
            is_active=True,
            is_deleted=False,
            tax_rate=Decimal("0.00")
        )
        for i in range(3)
    ]

    customer = Customer(
        id=uuid.uuid4(),
        organization_id=org.id,
        first_name="Test",
        last_name="Customer",
        phone="0501234567",
        loyalty_tier="bronze",
        loyalty_points=0,
    )

    db.add(org)
    db.add(branch)
    db.add(user)
    db.add_all(drugs)
    db.add(customer)
    await db.commit()

    return org, branch, user, drugs, customer
