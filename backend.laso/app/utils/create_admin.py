import asyncio
import uuid
import sys
from pathlib import Path

from app.services.auth.auth_service import AuthService

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import all models first to register relationships
import app.models

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.schemas.user_schema import UserCreate
from app.models.pharmacy.pharmacy_model import Organization
from app.models.user.user_model import User


async def create_initial_admin():
    """Create initial admin user and organization"""
    
    async with AsyncSessionLocal() as db:
        try:
            # Check if any users exist
            result = await db.execute(select(User))
            existing_user = result.scalar_one_or_none()
            
            if existing_user:
                print("Admin user already exists. Skipping creation.")
                return
            
            # Check if organization exists
            result = await db.execute(select(Organization))
            org = result.scalar_one_or_none()
            
            if not org:
                # Create a default organization
                print("Creating default organization...")
                org = Organization(
                    id=uuid.uuid4(),
                    name="LASO Pharmacy System",
                    type="pharmacy",
                    is_active=True,
                    subscription_tier="basic",
                    settings={}
                )
                db.add(org)
                await db.commit()
                await db.refresh(org)
                print(f"Organization created: {org.name} (ID: {org.id})")
            else:
                print(f"Using existing organization: {org.name} (ID: {org.id})")
            
            # Create admin user
            print("Creating admin user...")
            admin_data = UserCreate(
                username="admin",
                email="admin@laso.com",
                password="Admin@123456",
                full_name="System Administrator",
                role="super_admin",
                organization_id=org.id,
                assigned_branches=[],
                employee_id=None,
                phone=None
            )
            
            user = await AuthService.create_user(db, admin_data)
            
            print("\n" + "="*60)
            print("ADMIN USER CREATED SUCCESSFULLY!")
            print("="*60)
            print(f"Username:     {user.username}")
            print(f"Email:        {user.email}")
            print(f"Password:     Admin@123456")
            print(f"Role:         {user.role}")
            print(f"Organization: {org.name}")
            print(f"User ID:      {user.id}")
            print("="*60)
            print("\n  IMPORTANT: Change the default password after first login!")
            print("\n")
            
        except Exception as e:
            print(f"\n Error creating admin: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()


if __name__ == "__main__":
    print("\n LASO Pharmacy - Admin User Creation\n")
    asyncio.run(create_initial_admin())