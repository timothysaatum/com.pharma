import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.pharmacy.pharmacy_model import Branch
from app.models.inventory.inventory_model import Drug
from app.models.inventory.branch_inventory import BranchInventory
import uuid

async def seed():
    async with AsyncSessionLocal() as db:
        # Get North Clinic
        branch_q = await db.execute(select(Branch).where(Branch.name == "North Clinic"))
        branch = branch_q.scalar_one()

        # Get some drugs
        drugs_q = await db.execute(select(Drug).limit(5))
        drugs = drugs_q.scalars().all()

        for drug in drugs:
            inv = BranchInventory(
                id=uuid.uuid4(),
                branch_id=branch.id,
                drug_id=drug.id,
                quantity=100,
                reserved_quantity=0,
                is_active=True
            )
            db.add(inv)

        await db.commit()
        print(f"Inventory seeded for {branch.name}")

if __name__ == "__main__":
    asyncio.run(seed())
