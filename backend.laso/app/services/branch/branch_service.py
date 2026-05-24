"""
Branch Service
Business logic for branch/location management
"""
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
import uuid

from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.user.user_model import User
from app.schemas.branch_schemas import BranchCreate, BranchUpdate


class BranchService:
    """Service class for branch operations"""

    @staticmethod
    async def create_branch(
        db: AsyncSession,
        branch_data: BranchCreate,
        created_by_user_id: uuid.UUID
    ) -> Branch:
        """
        Create a new branch with validation.
        
        After successful creation, all super_admin and admin users in the
        organisation are automatically assigned to the new branch so they
        retain full visibility without a manual step.
        
        Args:
            db: Database session
            branch_data: Branch creation data
            created_by_user_id: ID of user creating the branch
            
        Returns:
            Created Branch object
            
        Raises:
            HTTPException: If validation fails
        """
        # Check if organization exists
        result = await db.execute(
            select(Organization).where(Organization.id == branch_data.organization_id)
        )
        organization = result.scalar_one_or_none()
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )
        
        # Check branch code uniqueness within organization
        result = await db.execute(
            select(Branch).where(
                Branch.organization_id == branch_data.organization_id,
                Branch.code == branch_data.code,
                Branch.is_deleted == False
            )
        )
        existing_branch = result.scalar_one_or_none()
        if existing_branch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Branch with code '{branch_data.code}' already exists in this organization"
            )
        
        # Validate manager exists if provided
        if branch_data.manager_id:
            result = await db.execute(
                select(User).where(
                    User.id == branch_data.manager_id,
                    User.organization_id == branch_data.organization_id,
                    User.is_active == True,
                    User.is_deleted == False
                )
            )
            manager = result.scalar_one_or_none()
            if not manager:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Manager not found or not active in this organization"
                )
            
            # Check if manager role is appropriate
            if manager.role not in ['admin', 'manager', 'super_admin']:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"User with role '{manager.role}' cannot be a branch manager"
                )
        
        # Convert Pydantic model to dict, excluding unset fields
        branch_dict = branch_data.model_dump(exclude_unset=True)
        
        # Properly serialize nested Pydantic models to dicts for JSONB
        # Address field
        if 'address' in branch_dict and branch_dict['address'] is not None:
            if hasattr(branch_dict['address'], 'model_dump'):
                branch_dict['address'] = branch_dict['address'].model_dump()
            branch_dict['address'] = dict(branch_dict['address'])
        
        # Operating hours field - more complex nested structure
        if 'operating_hours' in branch_dict and branch_dict['operating_hours'] is not None:
            operating_hours_dict = {}
            operating_hours = branch_dict['operating_hours']
            
            if hasattr(operating_hours, 'model_dump'):
                operating_hours = operating_hours.model_dump()
            
            for day, hours in operating_hours.items():
                if hours is not None:
                    if hasattr(hours, 'model_dump'):
                        operating_hours_dict[day] = hours.model_dump()
                    else:
                        operating_hours_dict[day] = dict(hours) if hours else None
                else:
                    operating_hours_dict[day] = None
            
            branch_dict['operating_hours'] = operating_hours_dict
        
        # Create branch with proper fields
        try:
            branch = Branch(
                id=uuid.uuid4(),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                sync_status='pending',
                sync_version=1,
                is_deleted=False,
                **branch_dict
            )
            
            db.add(branch)
            await db.flush()  # flush so branch.id is available before we reference it below

            # ── Auto-assign new branch to all super_admin and admin users ──────
            # These roles have org-wide scope (super_admin has '*', admin has
            # manage_branches) so they must always see every branch.
            # managers/pharmacists/cashiers are assigned to branches explicitly.
            result = await db.execute(
                select(User).where(
                    User.organization_id == branch_data.organization_id,
                    User.role.in_(['super_admin', 'admin']),
                    User.is_active == True,
                    User.is_deleted == False,
                )
            )
            admin_users = result.scalars().all()

            for admin_user in admin_users:
                current = list(admin_user.assigned_branches or [])
                if branch.id not in current:
                    # Reassign the whole list — SQLAlchemy ARRAY mutation
                    # requires a new list object to detect the change.
                    admin_user.assigned_branches = current + [branch.id]
                    admin_user.updated_at = datetime.now(timezone.utc)

            await db.commit()
            await db.refresh(branch)
            
            return branch
            
        except Exception as e:
            await db.rollback()
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error creating branch: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create branch: {str(e)}"
            )

    @staticmethod
    async def get_branch_by_id(
        db: AsyncSession,
        branch_id: uuid.UUID,
        organization_id: uuid.UUID
    ) -> Optional[Branch]:
        """Get branch by ID"""
        result = await db.execute(
            select(Branch).where(
                Branch.id == branch_id,
                Branch.organization_id == organization_id,
                Branch.is_deleted == False
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_branch_by_code(
        db: AsyncSession,
        code: str,
        organization_id: uuid.UUID
    ) -> Optional[Branch]:
        """Get branch by unique code"""
        result = await db.execute(
            select(Branch).where(
                Branch.code == code.upper(),
                Branch.organization_id == organization_id,
                Branch.is_deleted == False
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def search_branches(
        db: AsyncSession,
        organization_id: uuid.UUID,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        manager_id: Optional[uuid.UUID] = None,
        state: Optional[str] = None,
        city: Optional[str] = None
    ) -> List[Branch]:
        """Search branches with filters"""
        query = select(Branch).where(
            Branch.organization_id == organization_id,
            Branch.is_deleted == False
        )
        
        if is_active is not None:
            query = query.where(Branch.is_active == is_active)
        
        if manager_id:
            query = query.where(Branch.manager_id == manager_id)
        
        if search:
            search_term = f"%{search.lower()}%"
            query = query.where(
                or_(
                    func.lower(Branch.name).like(search_term),
                    func.lower(Branch.code).like(search_term),
                    Branch.address['city'].astext.ilike(search_term)
                )
            )
        
        if state:
            query = query.where(Branch.address['state'].astext.ilike(f"%{state}%"))
        
        if city:
            query = query.where(Branch.address['city'].astext.ilike(f"%{city}%"))
        
        query = query.order_by(Branch.name)
        
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def update_branch(
        db: AsyncSession,
        branch_id: uuid.UUID,
        branch_data: BranchUpdate,
        organization_id: uuid.UUID,
        updated_by_user_id: uuid.UUID
    ) -> Branch:
        """Update branch with validation"""
        branch = await BranchService.get_branch_by_id(db, branch_id, organization_id)
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found"
            )
        
        update_data = branch_data.model_dump(exclude_unset=True)
        
        if 'code' in update_data and update_data['code'] != branch.code:
            result = await db.execute(
                select(Branch).where(
                    Branch.organization_id == organization_id,
                    Branch.code == update_data['code'],
                    Branch.id != branch_id,
                    Branch.is_deleted == False
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Branch with code '{update_data['code']}' already exists"
                )
        
        if 'manager_id' in update_data and update_data['manager_id']:
            result = await db.execute(
                select(User).where(
                    User.id == update_data['manager_id'],
                    User.organization_id == organization_id,
                    User.is_active == True,
                    User.is_deleted == False
                )
            )
            manager = result.scalar_one_or_none()
            if not manager:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Manager not found"
                )
            if manager.role not in ['admin', 'manager', 'super_admin']:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"User with role '{manager.role}' cannot be a branch manager"
                )
        
        if 'address' in update_data and update_data['address'] is not None:
            if hasattr(update_data['address'], 'model_dump'):
                update_data['address'] = update_data['address'].model_dump()
            update_data['address'] = dict(update_data['address'])
        
        if 'operating_hours' in update_data and update_data['operating_hours'] is not None:
            operating_hours_dict = {}
            operating_hours = update_data['operating_hours']
            
            if hasattr(operating_hours, 'model_dump'):
                operating_hours = operating_hours.model_dump()
            
            for day, hours in operating_hours.items():
                if hours is not None:
                    if hasattr(hours, 'model_dump'):
                        operating_hours_dict[day] = hours.model_dump()
                    else:
                        operating_hours_dict[day] = dict(hours) if hours else None
                else:
                    operating_hours_dict[day] = None
            
            update_data['operating_hours'] = operating_hours_dict
        
        for key, value in update_data.items():
            setattr(branch, key, value)
        
        branch.updated_at = datetime.now(timezone.utc)
        branch.sync_version += 1
        
        try:
            await db.commit()
            await db.refresh(branch)
            return branch
        except Exception as e:
            await db.rollback()
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error updating branch: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update branch: {str(e)}"
            )

    @staticmethod
    async def delete_branch(
        db: AsyncSession,
        branch_id: uuid.UUID,
        organization_id: uuid.UUID,
        deleted_by_user_id: uuid.UUID,
        hard_delete: bool = False
    ) -> None:
        """Delete branch (soft or hard)"""
        branch = await BranchService.get_branch_by_id(db, branch_id, organization_id)
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found"
            )
        
        from app.models.inventory.branch_inventory import BranchInventory
        result = await db.execute(
            select(func.count(BranchInventory.id)).where(
                BranchInventory.branch_id == branch_id
            )
        )
        inventory_count = result.scalar() or 0
        if inventory_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete branch with {inventory_count} inventory items"
            )
        
        if hard_delete:
            await db.delete(branch)
        else:
            branch.is_deleted = True
            branch.updated_at = datetime.now(timezone.utc)
        
        await db.commit()

    @staticmethod
    async def assign_user_to_branches(
        db: AsyncSession,
        user_id: uuid.UUID,
        branch_ids: List[uuid.UUID],
        organization_id: uuid.UUID
    ) -> User:
        """Assign user to multiple branches"""
        result = await db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.is_deleted == False
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        result = await db.execute(
            select(Branch).where(
                Branch.id.in_(branch_ids),
                Branch.organization_id == organization_id,
                Branch.is_deleted == False
            )
        )
        branches = result.scalars().all()
        if len(branches) != len(branch_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more branches not found"
            )
        
        user.assigned_branches = branch_ids
        user.updated_at = datetime.now(timezone.utc)
        
        await db.commit()
        await db.refresh(user)
        
        return user
    
    @staticmethod
    async def get_branch_with_stats(
        db: AsyncSession,
        branch_id: uuid.UUID,
        organization_id: uuid.UUID
    ) -> Dict[str, Any]:
        """Get branch with comprehensive statistics"""
        branch = await BranchService.get_branch_by_id(db, branch_id, organization_id)
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found"
            )
        
        stats = {
            'branch': branch,
            'total_inventory_items': 0,
            'total_inventory_value': 0.0,
            'low_stock_count': 0,
            'total_sales_today': 0.0,
            'total_sales_month': 0.0,
            'active_users_count': 0
        }
        
        return stats