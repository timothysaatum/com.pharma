"""Seed test data into the development SQLite database."""
import asyncio, uuid, os, sys
from datetime import datetime, timedelta, date
import json

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./laso_dev.sqlite3"
os.environ["SECRET_KEY"] = "dev-secret-key-that-is-at-least-32-characters"
os.environ["ENCRYPTION_KEY"] = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
os.environ["ENVIRONMENT"] = "development"

sys.path.insert(0, os.path.dirname(__file__))

from app.db.session import AsyncSessionLocal, engine
from app.db.base import Base
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.customer.customer_model import Customer
from app.models.system_md.sys_models import AuditLog
from app.core.security import hash_password
from sqlalchemy import select

async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Organization).limit(1))
        if result.scalar_one_or_none():
            print("Data already seeded, skipping.")
            return

        org_id = uuid.uuid4()
        org = Organization(id=org_id, name="Test Pharmacy", type="pharmacy",
                          tax_id="123456789", settings={},
                          created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(org)

        branch_id = uuid.uuid4()
        branch = Branch(id=branch_id, organization_id=org_id, name="Main Branch",
                       code="MB001", is_active=True, is_deleted=False,
                       created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(branch)

        user_id = uuid.uuid4()
        password_hash = hash_password("admin123")
        user = User(id=user_id, organization_id=org_id, username="admin",
                   email="admin@pharmacy.com", password_hash=password_hash,
                   full_name="Admin User", is_super_admin=True,
                   is_active=True, assigned_branches=[branch_id],
                   created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(user)

        await db.flush()

        cat_id = uuid.uuid4()
        cat = DrugCategory(id=cat_id, organization_id=org_id, name="General",
                          created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(cat)

        cat2_id = uuid.uuid4()
        cat2 = DrugCategory(id=cat2_id, organization_id=org_id, name="Antibiotics",
                           created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(cat2)

        await db.flush()

        now = datetime.utcnow()
        drug1 = Drug(id=uuid.uuid4(), organization_id=org_id, name="Paracetamol 500mg",
                    sku="PARA001", drug_type="otc", category_id=cat_id,
                    unit_price=10.0, cost_price=5.0, reorder_level=10,
                    reorder_quantity=50, requires_prescription=False,
                    is_active=True, is_deleted=False,
                    sync_version=1, sync_status="synced",
                    created_at=now, updated_at=now)
        drug2 = Drug(id=uuid.uuid4(), organization_id=org_id, name="Amoxicillin 250mg",
                    sku="AMOX001", drug_type="prescription", category_id=cat2_id,
                    unit_price=25.0, cost_price=12.0, reorder_level=5,
                    reorder_quantity=30, requires_prescription=True,
                    is_active=True, is_deleted=False,
                    sync_version=1, sync_status="synced",
                    created_at=now, updated_at=now)
        drug3 = Drug(id=uuid.uuid4(), organization_id=org_id, name="Vitamin C 1000mg",
                    sku="VITC001", drug_type="otc", category_id=cat_id,
                    unit_price=15.0, cost_price=8.0, reorder_level=20,
                    reorder_quantity=100, requires_prescription=False,
                    is_active=True, is_deleted=False,
                    sync_version=1, sync_status="synced",
                    created_at=now, updated_at=now)
        db.add_all([drug1, drug2, drug3])

        await db.flush()

        cust1 = Customer(
            id=uuid.uuid4(), organization_id=org_id,
            customer_type="walk_in", first_name="John",
            last_name="Doe", phone="233501234567",
            email="john@example.com", is_active=True, is_deleted=False,
            sync_version=1, sync_status="synced",
            created_at=now, updated_at=now
        )
        cust2 = Customer(
            id=uuid.uuid4(), organization_id=org_id,
            customer_type="walk_in", first_name="Jane",
            last_name="Smith", phone="233507654321",
            email="jane@example.com", is_active=True, is_deleted=False,
            sync_version=1, sync_status="synced",
            created_at=now, updated_at=now
        )
        cust3 = Customer(
            id=uuid.uuid4(), organization_id=org_id,
            customer_type="walk_in", first_name="Bob",
            last_name="Johnson", phone="233509999999",
            email="bob@example.com", is_active=True, is_deleted=False,
            sync_version=1, sync_status="synced",
            created_at=now, updated_at=now
        )
        db.add_all([cust1, cust2, cust3])

        await db.flush()

        for i in range(5):
            audit = AuditLog(
                id=uuid.uuid4(), organization_id=org_id, user_id=user_id,
                action=f"test_action_{i}", entity_type="sale",
                entity_id=uuid.uuid4(),
                changes={"test": f"change_{i}"},
                ip_address=None, user_agent=None, context_metadata=None,
                created_at=now, updated_at=now,
                sync_version=1, sync_status="synced",
            )
            db.add(audit)

        await db.commit()
        print(f"Seeded successfully!")
        print(f"  Org ID: {org_id}")
        print(f"  Branch ID: {branch_id}")
        print(f"  User ID: {user_id}")
        print(f"  Login: admin / admin123")
        print(f"  3 drugs, 3 customers, 5 audit logs")

asyncio.run(seed())
