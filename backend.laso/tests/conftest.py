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
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User
from app.models.inventory.inventory_model import Drug
from app.models.customer.customer_model import Customer

DATABASE_URL_TEST = os.environ["DATABASE_URL"]


def _assert_not_production_database(url: str) -> None:
    """Refuse to run the suite against the live clinic database.

    The `db` fixture below runs `DROP SCHEMA IF EXISTS public CASCADE` on every
    PostgreSQL target. That is correct for a disposable cluster and catastrophic
    for production, and nothing in the URL distinguishes the two by itself.

    This has already happened: atlasdb carried 9 `prescription_created` events
    using fixture UUIDs (aggregate `aaaaaaaa-…`, author `44444444…`), which can
    only have come from the integration suite running against it.

    So the fence is explicit rather than clever: name the database, and refuse
    anything that is not obviously a throwaway.
    """
    if not url.startswith("postgresql"):
        return
    # The database name is the last path segment before any query string.
    db_name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
    forbidden = {"atlasdb", "postgres", "template0", "template1"}
    if db_name in forbidden:
        raise RuntimeError(
            f"REFUSING TO RUN: the test database is named {db_name!r}. "
            "tests/conftest.py drops the public schema on every PostgreSQL "
            "target, so this would destroy live data. Point "
            "TEST_DATABASE_URL at a disposable cluster, e.g. "
            "postgresql+asyncpg://postgres@/rx_impl?host=/tmp/... "
            "(see /tmp/pharmacare-investigation/pg.sh)."
        )


_assert_not_production_database(DATABASE_URL_TEST)


@pytest_asyncio.fixture(scope="function")
async def db():
    engine_kwargs = {}
    if DATABASE_URL_TEST.startswith("postgresql"):
        engine_kwargs["connect_args"] = {
            "server_settings": {
                "search_path": os.environ.get("TEST_DATABASE_SCHEMA", "public")
            }
        }

    engine = create_async_engine(DATABASE_URL_TEST, poolclass=NullPool, **engine_kwargs)
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
                from sqlalchemy import text
                await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
            await conn.run_sync(Base.metadata.create_all)
            if DATABASE_URL_TEST.startswith("postgresql"):
                # The event-sourced spine is owned by Alembic, not by an ORM model,
                # so Base.metadata.create_all never builds it. Any test that reaches
                # a stock write path now publishes an event inside the caller's
                # transaction, so the table has to exist for those tests to run.
                # Tests that need it with specific columns still drop and recreate
                # it themselves; IF NOT EXISTS keeps both paths working.
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS event_log (
                        event_id TEXT NOT NULL,
                        org_id UUID NOT NULL,
                        seq BIGINT NOT NULL,
                        aggregate_id UUID NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        schema_version SMALLINT NOT NULL DEFAULT 1,
                        payload JSONB NOT NULL,
                        dependencies TEXT[] NOT NULL DEFAULT '{}',
                        authored_at TIMESTAMPTZ NOT NULL,
                        authored_by UUID NOT NULL,
                        branch_id UUID NOT NULL,
                        hash_self TEXT NOT NULL,
                        hash_prev TEXT NOT NULL,
                        received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (org_id, event_id),
                        UNIQUE (org_id, seq)
                    )
                """))
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
