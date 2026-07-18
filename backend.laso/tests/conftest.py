import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "sqlite+aiosqlite:///:memory:",
)
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-for-jwt-signing"
os.environ["ENCRYPTION_KEY"] = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
os.environ["ENVIRONMENT"] = "test"

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

DATABASE_URL_TEST = os.environ["DATABASE_URL"]

@pytest_asyncio.fixture(scope="function")
async def db():
    engine_kwargs = {}
    if DATABASE_URL_TEST.startswith("postgresql"):
        engine_kwargs["connect_args"] = {
            "server_settings": {
                "search_path": os.environ.get("TEST_DATABASE_SCHEMA", "public")
            }
        }

    engine = create_async_engine(DATABASE_URL_TEST, **engine_kwargs)
    postgres_only_indexes = []
    if DATABASE_URL_TEST.startswith("postgresql"):
        for table in Base.metadata.tables.values():
            for index in tuple(table.indexes):
                if index.name == "idx_drug_search":
                    table.indexes.remove(index)
                    postgres_only_indexes.append((table, index))

    try:
        async with engine.begin() as conn:
            if DATABASE_URL_TEST.startswith("postgresql"):
                await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    finally:
        for table, index in postgres_only_indexes:
            table.indexes.add(index)

    async_session_factory = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        yield session

    if DATABASE_URL_TEST.startswith("postgresql"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
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
    await db.flush()
    db.add(branch)
    db.add(user)
    db.add_all(drugs)
    db.add(customer)
    await db.commit()

    return org, branch, user, drugs, customer


@pytest_asyncio.fixture(scope="function")
async def admin_role(db: AsyncSession, setup_test_data):
    from app.models.user.user_model import Role
    org = setup_test_data[0]
    role = Role(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Admin",
        description="Administrator",
        level=30,
        permissions=["*"]
    )
    db.add(role)
    await db.commit()
    return role


@pytest_asyncio.fixture(scope="function")
async def pharmacist_role(db: AsyncSession, setup_test_data):
    from app.models.user.user_model import Role
    org = setup_test_data[0]
    role = Role(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Pharmacist",
        description="Pharmacist role",
        level=20,
        permissions=["manage_prescriptions", "view_drugs", "view_inventory", "process_sales"]
    )
    db.add(role)
    await db.commit()
    return role


@pytest_asyncio.fixture(scope="function")
async def cashier_role(db: AsyncSession, setup_test_data):
    from app.models.user.user_model import Role
    org = setup_test_data[0]
    role = Role(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Cashier",
        description="Cashier role",
        level=10,
        permissions=["process_sales", "view_drugs", "view_inventory"]
    )
    db.add(role)
    await db.commit()
    return role


@pytest_asyncio.fixture(scope="function")
async def admin_user(db: AsyncSession, setup_test_data, admin_role):
    from app.models.user.user_model import User, UserRole
    from app.core.security import hash_password
    org, branch = setup_test_data[0], setup_test_data[1]
    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username="admin_user",
        email="admin@pharmacy.com",
        password_hash=hash_password("AdminPass123!"),
        full_name="Admin User",
        is_super_admin=False,
        is_active=True,
        must_change_password=False,
        assigned_branches=[branch.id],
    )
    db.add(user)
    await db.flush()
    user_role = UserRole(user_id=user.id, role_id=admin_role.id)
    db.add(user_role)
    await db.commit()
    return user


@pytest_asyncio.fixture(scope="function")
async def pharmacist_user(db: AsyncSession, setup_test_data, pharmacist_role):
    from app.models.user.user_model import User, UserRole
    from app.core.security import hash_password
    org, branch = setup_test_data[0], setup_test_data[1]
    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username="pharmacist_user",
        email="pharmacist@pharmacy.com",
        password_hash=hash_password("PharmacistPass123!"),
        full_name="Pharmacist User",
        is_super_admin=False,
        is_active=True,
        must_change_password=False,
        assigned_branches=[branch.id],
    )
    db.add(user)
    await db.flush()
    user_role = UserRole(user_id=user.id, role_id=pharmacist_role.id)
    db.add(user_role)
    await db.commit()
    return user


@pytest_asyncio.fixture(scope="function")
async def cashier_user(db: AsyncSession, setup_test_data, cashier_role):
    from app.models.user.user_model import User, UserRole
    from app.core.security import hash_password
    org, branch = setup_test_data[0], setup_test_data[1]
    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username="cashier_user",
        email="cashier@pharmacy.com",
        password_hash=hash_password("CashierPass123!"),
        full_name="Cashier User",
        is_super_admin=False,
        is_active=True,
        must_change_password=False,
        assigned_branches=[branch.id],
    )
    db.add(user)
    await db.flush()
    user_role = UserRole(user_id=user.id, role_id=cashier_role.id)
    db.add(user_role)
    await db.commit()
    return user


@pytest_asyncio.fixture(scope="function")
async def auth_headers(db: AsyncSession):
    from app.core.security import create_access_token, hash_token
    from app.models.user.user_model import UserSession
    
    async def _helper(user) -> dict[str, str]:
        token = create_access_token(data={"sub": str(user.id), "username": user.username})
        session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_token(token),
            refresh_token_hash=hash_token("dummy-refresh-token"),
            ip_address="127.0.0.1",
            user_agent="pytest",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            is_revoked=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(session)
        await db.commit()
        return {"Authorization": f"Bearer {token}"}
        
    return _helper
