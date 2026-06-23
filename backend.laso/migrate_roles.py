import asyncio
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal as async_session_maker
from app.models.user.user_model import User, Role, UserRole, Permission
from app.models.pharmacy.pharmacy_model import Organization
from app.models.pricing.pricing_model import PriceContract

# Map legacy roles to new permissions
ROLE_PERMISSIONS = {
    'admin': [
        Permission.MANAGE_USERS,
        Permission.MANAGE_BRANCHES,
        Permission.MANAGE_DRUGS,
        Permission.MANAGE_SUPPLIERS,
        Permission.APPROVE_PURCHASE_ORDERS,
        Permission.MANAGE_INVENTORY,
        Permission.PROCESS_SALES,
        Permission.VIEW_REPORTS,
        Permission.EXPORT_DATA,
        Permission.MANAGE_CUSTOMERS,
        Permission.MANAGE_ORGANIZATION,
        Permission.MANAGE_PRICING,
        Permission.MANAGE_PRESCRIPTIONS,
        Permission.VIEW_AUDIT_LOGS,
        Permission.VIEW_DRUGS,
        Permission.VIEW_INVENTORY
    ],
    'manager': [
        Permission.MANAGE_DRUGS,
        Permission.MANAGE_INVENTORY,
        Permission.PROCESS_SALES,
        Permission.APPROVE_PURCHASE_ORDERS,
        Permission.VIEW_REPORTS,
        Permission.EXPORT_DATA,
        Permission.MANAGE_SUPPLIERS,
        Permission.MANAGE_CUSTOMERS,
        Permission.MANAGE_PRICING,
        Permission.MANAGE_PRESCRIPTIONS,
        Permission.VIEW_DRUGS,
        Permission.VIEW_INVENTORY,
        Permission.VIEW_AUDIT_LOGS
    ],
    'pharmacist': [
        Permission.VIEW_DRUGS,
        Permission.PROCESS_SALES,
        Permission.VIEW_INVENTORY,
        Permission.APPROVE_PURCHASE_ORDERS,
        Permission.MANAGE_PRESCRIPTIONS,
        Permission.MANAGE_SUPPLIERS,
        Permission.MANAGE_CUSTOMERS
    ],
    'cashier': [
        Permission.VIEW_DRUGS,
        Permission.PROCESS_SALES,
        Permission.VIEW_INVENTORY,
        Permission.MANAGE_CUSTOMERS
    ],
    'viewer': [
        Permission.VIEW_DRUGS,
        Permission.VIEW_INVENTORY,
        Permission.VIEW_REPORTS
    ]
}

async def migrate_roles():
    print("Starting role migration...")
    async with async_session_maker() as db:
        # 1. Get all organizations
        org_result = await db.execute(select(Organization))
        organizations = org_result.scalars().all()

        for org in organizations:
            print(f"Migrating organization: {org.name} ({org.id})")

            # Create standard roles for this organization if they don't exist
            roles_map = {}
            for role_name, perms in ROLE_PERMISSIONS.items():
                # Check if role exists
                role_result = await db.execute(
                    select(Role).where(
                        Role.organization_id == org.id,
                        Role.name == role_name.capitalize()
                    )
                )
                role = role_result.scalar_one_or_none()

                if not role:
                    print(f"  Creating role: {role_name.capitalize()}")
                    role = Role(
                        id=uuid.uuid4(),
                        organization_id=org.id,
                        name=role_name.capitalize(),
                        description=f"Standard {role_name} role",
                        permissions=[p.value for p in perms],
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc)
                    )
                    db.add(role)

                roles_map[role_name] = role

            await db.flush()

            # 2. Get all users for this organization
            # Note: We need to use a raw query or a separate table check
            # if the 'role' column was already removed from the model definition.
            # Since I updated the model, 'user.role' is no longer in the Mapped class.
            user_result = await db.execute(
                text(f"SELECT id, role FROM users WHERE organization_id = '{org.id}'")
            )
            users_data = user_result.fetchall()

            for user_id, legacy_role in users_data:
                if not legacy_role:
                    continue

                if legacy_role == 'super_admin':
                    print(f"  Setting user {user_id} as super_admin")
                    await db.execute(
                        text("UPDATE users SET is_super_admin = 1 WHERE id = :uid"),
                        {"uid": str(user_id)}
                    )
                    continue

                new_role = roles_map.get(legacy_role)
                if new_role:
                    # Check if assignment already exists
                    assoc_result = await db.execute(
                        select(UserRole).where(
                            UserRole.user_id == user_id,
                            UserRole.role_id == new_role.id
                        )
                    )
                    if not assoc_result.scalar_one_or_none():
                        print(f"  Assigning role {legacy_role} to user {user_id}")
                        user_role = UserRole(
                            user_id=user_id,
                            role_id=new_role.id,
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc)
                        )
                        db.add(user_role)

            # 3. Migrate PriceContracts for this organization
            contract_result = await db.execute(
                select(PriceContract).where(PriceContract.organization_id == org.id)
            )
            contracts = contract_result.scalars().all()

            for contract in contracts:
                if not contract.allowed_user_roles:
                    continue

                new_allowed_roles = []
                changed = False
                for role_val in contract.allowed_user_roles:
                    # If it's already a UUID, keep it
                    try:
                        uuid.UUID(role_val)
                        new_allowed_roles.append(role_val)
                    except ValueError:
                        # It's a legacy role name, map it
                        legacy_name = role_val.lower()
                        if legacy_name in roles_map:
                            new_allowed_roles.append(str(roles_map[legacy_name].id))
                            changed = True
                        else:
                            # Keep it if we can't map it (e.g. it was already something else)
                            new_allowed_roles.append(role_val)

                if changed:
                    print(f"  Updating PriceContract {contract.id} allowed_user_roles: {contract.allowed_user_roles} -> {new_allowed_roles}")
                    contract.allowed_user_roles = new_allowed_roles

        await db.commit()
    print("Migration complete!")

if __name__ == "__main__":
    asyncio.run(migrate_roles())
