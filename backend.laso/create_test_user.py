
import asyncio
from sqlalchemy import select, delete
from app.db.base import Base
from app.db.session import engine, AsyncSessionLocal
from app.models.user.user_model import User
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.sales.sales_model import Sale, SaleItem
from app.core.security import hash_password
import os
import uuid
from datetime import datetime, timedelta
import random

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Clear existing data to avoid conflicts
        await db.execute(delete(SaleItem))
        await db.execute(delete(Sale))
        await db.execute(delete(Drug))
        await db.execute(delete(DrugCategory))
        await db.execute(delete(User))
        await db.execute(delete(Branch))
        await db.execute(delete(Organization))
        await db.commit()

    async with AsyncSessionLocal() as db:
        print("Creating organization...")
        org = Organization(
            id=uuid.uuid4(),
            name="Test Organization",
            type="pharmacy",
            is_active=True
        )
        db.add(org)
        await db.flush()

        print("Creating branch...")
        branch = Branch(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Main Branch",
            code="MB001",
            is_active=True
        )
        db.add(branch)
        await db.flush()

        print("Creating admin user...")
        admin_password = os.getenv("TEST_ADMIN_PASSWORD", "TemporaryPassword123!")
        user = User(
            id=uuid.uuid4(),
            username="admin",
            email="admin@example.com",
            full_name="Admin User",
            password_hash=hash_password(admin_password),
            role="admin",
            is_active=True,
            organization_id=org.id,
            permissions={"additional": ["*"], "denied": []},
            assigned_branches=[branch.id]
        )
        db.add(user)
        await db.flush()

        print("Creating drug category...")
        category = DrugCategory(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="General",
            description="General drugs"
        )
        db.add(category)
        await db.flush()

        print("Creating 30 sample drugs...")
        drugs = []
        for i in range(30):
            drug = Drug(
                id=uuid.uuid4(),
                organization_id=org.id,
                name=f"Drug {i+1}",
                generic_name=f"Generic {i+1}",
                sku=f"SKU-{i+1:04d}",
                barcode=f"BAR-{i+1:04d}",
                category_id=category.id,
                unit_price=10.0 + i,
                cost_price=5.0 + i,
                is_active=True,
                drug_type="otc"
            )
            db.add(drug)
            drugs.append(drug)
        await db.flush()

        print("Creating 120 sample sales...")
        for i in range(120):
            sale_date = datetime.now() - timedelta(days=i // 2) # 2 sales per day for 60 days
            sale = Sale(
                id=uuid.uuid4(),
                organization_id=org.id,
                branch_id=branch.id,
                sale_number=f"SALE-{i:06d}",
                subtotal=0,
                discount_amount=0,
                tax_amount=0,
                total_amount=0,
                payment_method="cash",
                payment_status="completed",
                status="completed",
                cashier_id=user.id,
                created_at=sale_date
            )
            db.add(sale)

            # Add items to sale
            num_items = random.randint(1, 3)
            sale_subtotal = 0
            for _ in range(num_items):
                drug = random.choice(drugs)
                qty = random.randint(1, 5)
                item_subtotal = qty * float(drug.unit_price)
                item = SaleItem(
                    id=uuid.uuid4(),
                    sale_id=sale.id,
                    drug_id=drug.id,
                    drug_name=drug.name,
                    drug_sku=drug.sku,
                    quantity=qty,
                    unit_price=float(drug.unit_price),
                    subtotal=item_subtotal,
                    discount_percentage=0,
                    discount_amount=0,
                    tax_rate=0,
                    tax_amount=0,
                    total_price=item_subtotal
                )
                db.add(item)
                sale_subtotal += item_subtotal

            sale.subtotal = sale_subtotal
            sale.total_amount = sale_subtotal

        await db.commit()
        print("Sample data created successfully.")

if __name__ == "__main__":
    asyncio.run(init_db())
