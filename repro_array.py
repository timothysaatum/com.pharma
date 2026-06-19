
import asyncio
import uuid
import json
from sqlalchemy import select, or_, insert
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, Mapped, mapped_column
from app.models.db_types import UUID, ARRAY

Base = declarative_base()

class TestModel(Base):
    __tablename__ = 'test_model'
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    items: Mapped[list] = mapped_column(ARRAY(UUID))
    roles: Mapped[list] = mapped_column(ARRAY)

async def run_test():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    item_id = uuid.uuid4()
    branch_id = uuid.uuid4()

    async with async_session() as session:
        # Insert a record
        stmt = insert(TestModel).values(
            id=item_id,
            items=[branch_id],
            roles=['admin', 'manager']
        )
        await session.execute(stmt)
        await session.commit()

        # Test 1: Exact match on ARRAY
        print("Testing exact match on ARRAY...")
        res = await session.execute(select(TestModel).where(TestModel.roles == ['admin', 'manager']))
        print(f"Match found: {res.scalar() is not None}")

        # Test 2: .contains on ARRAY (String)
        print("\nTesting .contains(['admin']) on ARRAY(String)...")
        try:
            res = await session.execute(select(TestModel).where(TestModel.roles.contains(['admin'])))
            print(f"Match found: {res.scalar() is not None}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 3: .contains on ARRAY(UUID)
        print("\nTesting .contains([branch_id]) on ARRAY(UUID)...")
        try:
            res = await session.execute(select(TestModel).where(TestModel.items.contains([branch_id])))
            print(f"Match found: {res.scalar() is not None}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 4: Loading and checking in Python
        print("\nTesting Python 'in' check...")
        res = await session.execute(select(TestModel).where(TestModel.id == item_id))
        record = res.scalar()
        print(f"Loaded roles: {record.roles} (type: {type(record.roles)})")
        print(f"Loaded items: {record.items} (type: {type(record.items)})")
        if record.items:
            print(f"First item type: {type(record.items[0])}")

        print(f"admin in record.roles: {'admin' in record.roles}")
        print(f"branch_id in record.items: {branch_id in record.items}")

if __name__ == "__main__":
    asyncio.run(run_test())
