#!/usr/bin/env python3
import asyncio
import uuid
from decimal import Decimal
from datetime import datetime, timezone, date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User, Role, UserRole
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.core.config import get_settings
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.sales.sales_model import Supplier
from app.models.customer.customer_model import Customer

settings = get_settings()
DATABASE_URL = settings.DATABASE_URL

async def seed():
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as db:
        # 1. Organization
        org_res = await db.execute(select(Organization).where(Organization.name == "Demo Pharmacy Org"))
        org = org_res.scalar_one_or_none()
        if not org:
            org = Organization(
                id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                name="Demo Pharmacy Org",
                type="pharmacy",
                is_active=True,
                subscription_tier="enterprise",
            )
            db.add(org)
            await db.flush()
        
        # 2. Branch
        branch_res = await db.execute(select(Branch).where(Branch.organization_id == org.id))
        branch = branch_res.scalar_one_or_none()
        if not branch:
            branch = Branch(
                id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                organization_id=org.id,
                name="Downtown Main Branch",
                code="DT01",
                is_active=True,
            )
            db.add(branch)
            await db.flush()

        # 3. Roles
        role_res = await db.execute(select(Role).where(Role.organization_id == org.id, Role.name == "Admin"))
        admin_role = role_res.scalar_one_or_none()
        if not admin_role:
            admin_role = Role(
                id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
                organization_id=org.id,
                name="Admin",
                description="Administrator",
                level=30,
                permissions=["*"],
            )
            db.add(admin_role)
            await db.flush()

        # 4. Admin User
        user_res = await db.execute(select(User).where(User.username == "admin"))
        user = user_res.scalar_one_or_none()
        if not user:
            user = User(
                id=uuid.UUID("44444444-4444-4444-4444-444444444444"),
                organization_id=org.id,
                assigned_branches=[str(branch.id)],
                username="admin",
                email="admin@demopharmacy.com",
                password_hash=hash_password("Password123!"),
                full_name="Admin User",
                is_super_admin=True,
                is_active=True,
                must_change_password=False,
            )
            db.add(user)
            await db.flush()
            db.add(UserRole(user_id=user.id, role_id=admin_role.id))
            await db.flush()

        # 5. Drug Category
        cat_res = await db.execute(select(DrugCategory).where(DrugCategory.organization_id == org.id))
        cat = cat_res.scalar_one_or_none()
        if not cat:
            cat = DrugCategory(
                id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
                organization_id=org.id,
                name="Antibiotics",
                description="Antibacterial medications",
            )
            db.add(cat)
            await db.flush()

        # 6. Sample Drugs
        drug1_res = await db.execute(select(Drug).where(Drug.organization_id == org.id, Drug.sku == "AMOX-500"))
        drug1 = drug1_res.scalar_one_or_none()
        if not drug1:
            drug1 = Drug(
                id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
                organization_id=org.id,
                category_id=cat.id,
                name="Amoxicillin 500mg",
                generic_name="Amoxicillin",
                brand_name="Amoxil",
                sku="AMOX-500",
                barcode="8901234567890",
                drug_type="prescription",
                dosage_form="capsule",
                strength="500mg",
                unit_price=Decimal("15.00"),
                cost_price=Decimal("8.00"),
                reorder_level=20,
                is_active=True,
            )
            db.add(drug1)
            await db.flush()

            # Inventory for Drug 1
            inv1 = BranchInventory(
                id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
                branch_id=branch.id,
                drug_id=drug1.id,
                quantity=150,
                reserved_quantity=0,
                location="Aisle 1, Shelf B",
                selling_price=Decimal("15.00"),
            )
            db.add(inv1)
            
            # Batches for Drug 1
            b1 = DrugBatch(
                id=uuid.UUID("88888888-8888-8888-8888-888888888888"),
                branch_id=branch.id,
                drug_id=drug1.id,
                batch_number="BAT-2026-001",
                quantity=150,
                remaining_quantity=150,
                cost_price=Decimal("8.00"),
                selling_price=Decimal("15.00"),
                expiry_date=date(2027, 12, 31),
            )
            db.add(b1)

        drug2_res = await db.execute(select(Drug).where(Drug.organization_id == org.id, Drug.sku == "PARA-500"))
        drug2 = drug2_res.scalar_one_or_none()
        if not drug2:
            drug2 = Drug(
                id=uuid.UUID("66666666-6666-6666-6666-666666666667"),
                organization_id=org.id,
                category_id=cat.id,
                name="Paracetamol 500mg",
                generic_name="Acetaminophen",
                brand_name="Panadol",
                sku="PARA-500",
                barcode="8901234567891",
                drug_type="otc",
                dosage_form="tablet",
                strength="500mg",
                unit_price=Decimal("5.00"),
                cost_price=Decimal("2.50"),
                reorder_level=50,
                is_active=True,
            )
            db.add(drug2)
            await db.flush()

            inv2 = BranchInventory(
                id=uuid.UUID("77777777-7777-7777-7777-777777777778"),
                branch_id=branch.id,
                drug_id=drug2.id,
                quantity=200,
                reserved_quantity=0,
                location="Aisle 2, Shelf A",
                selling_price=Decimal("5.00"),
            )
            db.add(inv2)

            b2 = DrugBatch(
                id=uuid.UUID("88888888-8888-8888-8888-888888888889"),
                branch_id=branch.id,
                drug_id=drug2.id,
                batch_number="BAT-2026-002",
                quantity=200,
                remaining_quantity=200,
                cost_price=Decimal("2.50"),
                selling_price=Decimal("5.00"),
                expiry_date=date(2027, 6, 30),
            )
            db.add(b2)

        # 7. Customer
        cust_res = await db.execute(select(Customer).where(Customer.organization_id == org.id))
        cust = cust_res.scalar_one_or_none()
        if not cust:
            cust = Customer(
                id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
                organization_id=org.id,
                first_name="Jane",
                last_name="Doe",
                phone="0244123456",
                email="jane.doe@example.com",
                loyalty_points=50,
                loyalty_tier="silver",
            )
            db.add(cust)

        # 8. Supplier
        supp_res = await db.execute(select(Supplier).where(Supplier.organization_id == org.id))
        supp = supp_res.scalar_one_or_none()
        if not supp:
            supp = Supplier(
                id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                organization_id=org.id,
                name="Accra Medical Supplies",
                contact_person="Kwame Mensah",
                phone="0201234567",
                email="kwame@accramedical.com",
                is_active=True,
                is_deleted=False,
                total_orders=0,
                total_value=Decimal("0.00"),
            )
            db.add(supp)

        await db.commit()
        print("Seed completed successfully!")
        print(f"Org ID: {org.id}")
        print(f"Branch ID: {branch.id}")
        print(f"User: admin / Password123!")

if __name__ == "__main__":
    asyncio.run(seed())
