"""
Pagination Utility
Reusable pagination helper for SQLAlchemy queries
"""
from typing import TypeVar, Generic, List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import Select, func, select
from pydantic import BaseModel, Field, computed_field
from math import ceil


T = TypeVar('T')


class PaginationParams(BaseModel):
    """Request parameters for pagination"""
    page: int = Field(default=1, ge=1, le=10000, description="Page number")
    page_size: int = Field(default=50, ge=1, le=500, description="Items per page")
    
    @computed_field
    @property
    def skip(self) -> int:
        """Calculate offset for database query"""
        return (self.page - 1) * self.page_size
    
    @computed_field
    @property
    def limit(self) -> int:
        """Get limit for database query"""
        return self.page_size


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response"""
    items: List[T] = Field(..., description="List of items for current page")
    total: int = Field(..., ge=0, description="Total number of items")
    page: int = Field(..., ge=1, description="Current page number")
    page_size: int = Field(..., ge=1, description="Items per page")
    total_pages: int = Field(..., ge=0, description="Total number of pages")
    has_next: bool = Field(..., description="Whether there's a next page")
    has_prev: bool = Field(..., description="Whether there's a previous page")
    
    model_config = {"from_attributes": True}


class Paginator:
    """
    Reusable paginator for SQLAlchemy queries
    
    Example usage:
        from app.utils.pagination import Paginator, PaginationParams
        
        # In your endpoint
        @router.get("/items")
        async def list_items(
            pagination: PaginationParams = Depends(),
            db: AsyncSession = Depends(get_db)
        ):
            # Create your base query
            query = select(Item).where(Item.is_active == True)
            
            # Use paginator
            paginator = Paginator(db)
            result = await paginator.paginate(
                query=query,
                params=pagination,
                schema=ItemResponse
            )
            
            return result
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def paginate(
        self,
        query: Select,
        params: PaginationParams,
        schema: Optional[type] = None
    ) -> PaginatedResponse:
        """
        Paginate a SQLAlchemy query
        
        Args:
            query: SQLAlchemy Select statement
            params: Pagination parameters (page, page_size)
            schema: Optional Pydantic schema to convert items to
        
        Returns:
            PaginatedResponse with items and pagination metadata
        """
        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        result = await self.db.execute(count_query)
        total = result.scalar() or 0
        
        # Calculate pagination metadata
        total_pages = ceil(total / params.page_size) if total > 0 else 0
        has_next = params.page < total_pages
        has_prev = params.page > 1
        
        # Apply pagination to query
        paginated_query = query.offset(params.skip).limit(params.limit)
        
        # Execute query
        result = await self.db.execute(paginated_query)
        items = result.scalars().all()
        
        # Convert to schema if provided
        if schema:
            items = [schema.model_validate(item) for item in items]
        
        return PaginatedResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=total_pages,
            has_next=has_next,
            has_prev=has_prev
        )
    
    async def paginate_raw_query(
        self,
        query: Select,
        count_query: Select,
        params: PaginationParams,
        schema: Optional[type] = None
    ) -> PaginatedResponse:
        """
        Paginate with custom count query (for complex queries with joins)
        
        Args:
            query: Main SQLAlchemy Select statement
            count_query: Separate count query (for performance with joins)
            params: Pagination parameters
            schema: Optional Pydantic schema
        
        Returns:
            PaginatedResponse with items and metadata
        """
        # Get total count
        result = await self.db.execute(count_query)
        total = result.scalar() or 0
        
        # Calculate pagination metadata
        total_pages = ceil(total / params.page_size) if total > 0 else 0
        has_next = params.page < total_pages
        has_prev = params.page > 1
        
        # Apply pagination
        paginated_query = query.offset(params.skip).limit(params.limit)
        
        # Execute query
        result = await self.db.execute(paginated_query)
        items = result.scalars().all()
        
        # Convert to schema if provided
        if schema:
            items = [schema.model_validate(item) for item in items]
        
        return PaginatedResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=total_pages,
            has_next=has_next,
            has_prev=has_prev
        )
    
    def paginate_list(
        self,
        items: List[Any],
        params: PaginationParams,
        schema: Optional[type] = None
    ) -> PaginatedResponse:
        """
        Paginate an in-memory list
        
        Args:
            items: List of items to paginate
            params: Pagination parameters
            schema: Optional Pydantic schema
        
        Returns:
            PaginatedResponse with paginated items
        """
        total = len(items)
        total_pages = ceil(total / params.page_size) if total > 0 else 0
        has_next = params.page < total_pages
        has_prev = params.page > 1
        
        # Slice the list
        start = params.skip
        end = start + params.page_size
        page_items = items[start:end]
        
        # Convert to schema if provided
        if schema:
            page_items = [schema.model_validate(item) for item in page_items]
        
        return PaginatedResponse(
            items=page_items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=total_pages,
            has_next=has_next,
            has_prev=has_prev
        )


# Helper function for quick pagination
async def paginate(
    db: AsyncSession,
    query: Select,
    page: int = 1,
    page_size: int = 50,
    schema: Optional[type] = None
) -> PaginatedResponse:
    """
    Quick pagination helper function
    
    Usage:
        from app.utils.pagination import paginate
        
        result = await paginate(
            db=db,
            query=select(User).where(User.is_active == True),
            page=1,
            page_size=20,
            schema=UserResponse
        )
    """
    params = PaginationParams(page=page, page_size=page_size)
    paginator = Paginator(db)
    return await paginator.paginate(query, params, schema)