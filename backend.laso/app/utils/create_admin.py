import asyncio
import secrets
import string
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
    """Create or promote admin user to super admin"""
    
    async with AsyncSessionLocal() as db:
        try:
            # Check if organization exists
            result = await db.execute(select(Organization))
            org = result.scalars().first()
            
            if not org:
                print("Creating default organization...")
                org = Organization(
                    id=uuid.uuid4(),
                    name="Pharmacy System",
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
            
            # Check if admin user exists
            result = await db.execute(
                select(User).where(User.username == "admin")
            )
            user = result.scalars().first()
            
            if user:
                print(f"Admin user '{user.username}' found. Promoting to super admin...")
                user.is_super_admin = True
                user.is_active = True
                user.failed_login_attempts = 0
                user.account_locked_until = None
                await db.commit()
                print(f"User '{user.username}' is now a super admin.")
                return
            
            # Create admin user with a cryptographically random password
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
            admin_password = "".join(secrets.choice(alphabet) for _ in range(24))
            print("Creating admin user...")
            admin_data = UserCreate(
                username="admin",
                email="admin@pharmacy.com",
                password=admin_password,
                full_name="System Administrator",
                organization_id=org.id,
                assigned_branches=[],
                employee_id=None,
                phone=None
            )
            
            user = await AuthService.create_user(db, admin_data)
            user.is_super_admin = True
            await db.commit()
            
            print("\n" + "="*60)
            print("ADMIN USER CREATED SUCCESSFULLY!")
            print("="*60)
            print(f"Username:     {user.username}")
            print(f"Email:        {user.email}")
            print(f"Password:     {admin_password}")
            print(f"Super Admin:  Yes")
            print(f"Organization: {org.name}")
            print(f"User ID:      {user.id}")
            print("="*60)
            print("\n  !!! SAVE THIS PASSWORD - IT WILL NOT BE SHOWN AGAIN !!!")
            print("\n")
            
        except Exception as e:
            print(f"\n Error creating admin: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()


if __name__ == "__main__":
    print("\n LASO Pharmacy - Admin User Creation\n")
    asyncio.run(create_initial_admin())