"""
Prescription Management API Endpoints
=====================================
Professional prescription handling for controlled substances and regulated pharmacy sales.

Features:
- Create and manage prescriptions from healthcare providers
- Track refills and expiration
- Search prescriptions by customer for POS checkout
- Verify prescriptions for sales processing
- Maintain audit trail for regulatory compliance
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import Optional, List
from datetime import date, datetime, timezone
import uuid

from app.core.deps import get_current_user, require_permission
from app.db.dependencies import get_db
from app.models.user.user_model import User
from app.models.precriptions.prescription_model import Prescription
from app.models.customer.customer_model import Customer
from app.models.pharmacy.pharmacy_model import Organization
from app.schemas.sales_schemas import SaleCreate
from app.utils.pagination import PaginatedResponse, Paginator, PaginationParams
from pydantic import BaseModel, Field
from decimal import Decimal

router = APIRouter(prefix="/prescriptions", tags=["Prescriptions"])


# ============================================
# Schemas for Prescription Management
# ============================================

class MedicationItem(BaseModel):
    """Single medication entry in prescription"""
    drug_id: uuid.UUID
    drug_name: str
    dosage: str = Field(..., description="e.g., '500mg', '10ml'")
    frequency: str = Field(..., description="e.g., 'twice daily', 'every 8 hours'")
    duration: str = Field(..., description="e.g., '7 days', '2 weeks'")
    quantity: int = Field(..., gt=0, description="Total quantity prescribed")
    instructions: Optional[str] = None


class PrescriptionCreate(BaseModel):
    """Create a new prescription"""
    prescription_number: str = Field(
        ...,
        description="Unique prescription number from prescriber"
    )
    customer_id: uuid.UUID = Field(..., description="Patient/customer UUID")
    
    # Prescriber information
    prescriber_name: str = Field(..., description="Doctor/healthcare provider name")
    prescriber_license: str = Field(..., description="Medical license number")
    prescriber_phone: Optional[str] = Field(None, description="Contact phone")
    prescriber_address: Optional[str] = Field(None, description="Practice address")
    
    # Prescription timing
    issue_date: date = Field(..., description="Date prescription was issued")
    expiry_date: date = Field(..., description="Date prescription expires")
    
    # Medications
    medications: List[MedicationItem] = Field(
        ...,
        description="Array of prescribed medications"
    )
    
    # Refills
    refills_allowed: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Number of refills allowed (0-10)"
    )
    
    # Clinical info
    diagnosis: Optional[str] = None
    notes: Optional[str] = None
    special_instructions: Optional[str] = Field(
        None,
        description="e.g., 'Do not crush', 'Take with food'"
    )


class PrescriptionUpdate(BaseModel):
    """Update prescription status or refills"""
    status: Optional[str] = Field(
        None,
        description="active, filled, expired, cancelled"
    )
    notes: Optional[str] = None


class MedicationResponse(MedicationItem):
    """Response medication item"""
    pass


class PrescriptionResponse(BaseModel):
    """Prescription response"""
    id: uuid.UUID
    prescription_number: str
    customer_id: uuid.UUID
    customer_name: Optional[str] = None
    
    prescriber_name: str
    prescriber_license: str
    prescriber_phone: Optional[str] = None
    prescriber_address: Optional[str] = None
    
    issue_date: date
    expiry_date: date
    is_expired: bool = Field(..., description="True if expiry_date < today")
    
    medications: List[dict]
    diagnosis: Optional[str] = None
    notes: Optional[str] = None
    special_instructions: Optional[str] = None
    
    refills_allowed: int
    refills_remaining: int
    last_refill_date: Optional[date] = None
    
    status: str = Field(..., description="active, filled, expired, cancelled")
    
    verified_by: Optional[uuid.UUID] = None
    verified_at: Optional[datetime] = None
    
    created_at: datetime
    updated_at: datetime


class PrescriptionSearchResponse(BaseModel):
    """Quick response for checkout search"""
    id: uuid.UUID
    prescription_number: str
    prescriber_name: str
    medications_count: int = Field(..., description="Number of drugs prescribed")
    issue_date: date
    expiry_date: date
    is_expired: bool
    status: str
    refills_remaining: int
    refills_allowed: int


# ============================================
# Endpoints
# ============================================

@router.post(
    "/",
    response_model=PrescriptionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("manage_prescriptions"))]
)
async def create_prescription(
    prescription_data: PrescriptionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new prescription
    
    **Permissions:** manage_prescriptions
    
    **Requirements:**
    - Prescription number must be unique
    - Customer must exist and be active
    - Expiry date must be in future (or today)
    - Issue date <= expiry date
    - At least one medication required
    
    **Example:**
    ```json
    {
      "prescription_number": "RX-2026-001234",
      "customer_id": "uuid",
      "prescriber_name": "Dr. Jane Smith",
      "prescriber_license": "MD123456",
      "prescriber_phone": "+1-555-0100",
      "issue_date": "2026-05-27",
      "expiry_date": "2026-06-27",
      "medications": [
        {
          "drug_id": "uuid",
          "drug_name": "Amoxicillin",
          "dosage": "500mg",
          "frequency": "twice daily",
          "duration": "7 days",
          "quantity": 14,
          "instructions": "Take with or without food"
        }
      ],
      "refills_allowed": 2,
      "diagnosis": "Bacterial infection",
      "special_instructions": "Do not crush tablets"
    }
    ```
    """
    # Validate dates
    if prescription_data.issue_date > prescription_data.expiry_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Issue date cannot be after expiry date"
        )
    
    if prescription_data.expiry_date < date.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expiry date must be today or in the future"
        )
    
    # Verify customer exists
    cust_res = await db.execute(
        select(Customer).where(
            Customer.id == prescription_data.customer_id,
            Customer.organization_id == current_user.organization_id,
            Customer.is_deleted == False,
            Customer.is_active == True,
        )
    )
    customer = cust_res.scalar_one_or_none()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found or inactive"
        )
    
    # Check duplicate prescription number
    dup_res = await db.execute(
        select(Prescription).where(
            Prescription.prescription_number == prescription_data.prescription_number,
            Prescription.organization_id == current_user.organization_id,
        )
    )
    if dup_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Prescription number '{prescription_data.prescription_number}' already exists"
        )
    
    # Create prescription
    medications_list = [m.model_dump() for m in prescription_data.medications]
    
    prescription = Prescription(
        id=uuid.uuid4(),
        organization_id=current_user.organization_id,
        prescription_number=prescription_data.prescription_number,
        customer_id=prescription_data.customer_id,
        prescriber_name=prescription_data.prescriber_name,
        prescriber_license=prescription_data.prescriber_license,
        prescriber_phone=prescription_data.prescriber_phone,
        prescriber_address=prescription_data.prescriber_address,
        issue_date=prescription_data.issue_date,
        expiry_date=prescription_data.expiry_date,
        medications=medications_list,
        refills_allowed=prescription_data.refills_allowed,
        refills_remaining=prescription_data.refills_allowed,
        diagnosis=prescription_data.diagnosis,
        notes=prescription_data.notes,
        special_instructions=prescription_data.special_instructions,
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    
    db.add(prescription)
    await db.flush()
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": customer.name,
        }
    )


@router.get(
    "/customer/{customer_id}",
    response_model=PaginatedResponse[PrescriptionSearchResponse],
    dependencies=[Depends(require_permission("process_sales"))]
)
async def search_customer_prescriptions(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_filter: Optional[str] = Query(None, description="Filter by status: active, filled, expired, cancelled"),
    include_expired: bool = Query(False, description="Include expired prescriptions"),
    paginate: PaginationParams = Depends(),
):
    """
    Search prescriptions for a customer (for checkout)
    
    **Permissions:** process_sales
    
    **Use Case:** During POS checkout, search for active prescriptions to use for Rx drugs
    
    **Query Parameters:**
    - `status_filter`: Filter by status (optional)
    - `include_expired`: Include expired prescriptions (default: false)
    
    **Returns:** List of prescriptions with quick info for selection
    
    **Example Response:**
    ```json
    {
      "items": [
        {
          "id": "uuid",
          "prescription_number": "RX-2026-001234",
          "prescriber_name": "Dr. Jane Smith",
          "medications_count": 2,
          "issue_date": "2026-05-27",
          "expiry_date": "2026-06-27",
          "is_expired": false,
          "status": "active",
          "refills_remaining": 2,
          "refills_allowed": 2
        }
      ],
      "total": 1,
      "page": 1,
      "size": 10
    }
    ```
    """
    # Verify customer exists
    cust_res = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.organization_id == current_user.organization_id,
            Customer.is_deleted == False,
        )
    )
    customer = cust_res.scalar_one_or_none()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found"
        )
    
    # Build query
    conditions = [
        Prescription.customer_id == customer_id,
        Prescription.organization_id == current_user.organization_id,
    ]
    
    if not include_expired:
        conditions.append(Prescription.expiry_date >= date.today())
    
    if status_filter:
        conditions.append(Prescription.status == status_filter)
    
    # Get total count
    count_res = await db.execute(
        select(Prescription).where(and_(*conditions))
    )
    total = len(count_res.scalars().all())
    
    # Get paginated results
    paginator = Paginator(paginate.page, paginate.size, total)
    res = await db.execute(
        select(Prescription)
        .where(and_(*conditions))
        .order_by(Prescription.issue_date.desc())
        .limit(paginator.limit)
        .offset(paginator.offset)
    )
    prescriptions = res.scalars().all()
    
    items = [
        PrescriptionSearchResponse(
            id=p.id,
            prescription_number=p.prescription_number,
            prescriber_name=p.prescriber_name,
            medications_count=len(p.medications),
            issue_date=p.issue_date,
            expiry_date=p.expiry_date,
            is_expired=p.expiry_date < date.today(),
            status=p.status,
            refills_remaining=p.refills_remaining,
            refills_allowed=p.refills_allowed,
        )
        for p in prescriptions
    ]
    
    return PaginatedResponse(
        items=items,
        total=total,
        page=paginator.page,
        size=paginator.size
    )


@router.get(
    "/{prescription_id}",
    response_model=PrescriptionResponse,
)
async def get_prescription(
    prescription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get prescription details
    
    **Returns:** Full prescription with all medications and details
    """
    res = await db.execute(
        select(Prescription).where(
            Prescription.id == prescription_id,
            Prescription.organization_id == current_user.organization_id,
        )
    )
    prescription = res.scalar_one_or_none()
    if not prescription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found"
        )
    
    # Get customer name
    cust_res = await db.execute(
        select(Customer).where(Customer.id == prescription.customer_id)
    )
    customer = cust_res.scalar_one_or_none()
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": customer.name if customer else None,
        }
    )


@router.patch(
    "/{prescription_id}",
    response_model=PrescriptionResponse,
    dependencies=[Depends(require_permission("manage_prescriptions"))]
)
async def update_prescription(
    prescription_id: uuid.UUID,
    update_data: PrescriptionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update prescription status
    
    **Permissions:** manage_prescriptions
    
    **Allowed Status Changes:**
    - active → filled (after dispensing)
    - active → cancelled (before expiry)
    - filled → cancelled (withdraw fill)
    
    **Example:**
    ```json
    {
      "status": "filled",
      "notes": "Dispensed 14 tablets on 2026-05-27"
    }
    ```
    """
    res = await db.execute(
        select(Prescription).where(
            Prescription.id == prescription_id,
            Prescription.organization_id == current_user.organization_id,
        )
    )
    prescription = res.scalar_one_or_none()
    if not prescription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found"
        )
    
    if update_data.status:
        prescription.status = update_data.status
    
    if update_data.notes:
        prescription.notes = update_data.notes
    
    prescription.updated_at = datetime.now(timezone.utc)
    await db.flush()
    
    # Get customer name
    cust_res = await db.execute(
        select(Customer).where(Customer.id == prescription.customer_id)
    )
    customer = cust_res.scalar_one_or_none()
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": customer.name if customer else None,
        }
    )


@router.post(
    "/{prescription_id}/refill",
    response_model=PrescriptionResponse,
    dependencies=[Depends(require_permission("process_sales"))]
)
async def use_prescription_refill(
    prescription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Decrement refills when prescription is used in a sale
    
    **Automatically called by:** SalesService.process_sale()
    
    **Validation:**
    - Status must be 'active'
    - Expiry date must be in future
    - refills_remaining > 0
    
    **Effect:** Decrements refills_remaining by 1
    
    **Returns:** Updated prescription
    """
    res = await db.execute(
        select(Prescription).where(
            Prescription.id == prescription_id,
            Prescription.organization_id == current_user.organization_id,
        )
    )
    prescription = res.scalar_one_or_none()
    if not prescription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found"
        )
    
    # Validation
    if prescription.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Prescription is {prescription.status}. Only active prescriptions can be used."
        )
    
    if date.today() > prescription.expiry_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Prescription expired on {prescription.expiry_date}"
        )
    
    if prescription.refills_remaining <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No refills remaining on this prescription"
        )
    
    # Decrement refills
    prescription.refills_remaining -= 1
    prescription.last_refill_date = date.today()
    
    # Auto-update status if all refills used
    if prescription.refills_remaining == 0:
        prescription.status = "filled"
    
    prescription.verified_by = current_user.id
    prescription.verified_at = datetime.now(timezone.utc)
    prescription.updated_at = datetime.now(timezone.utc)
    
    await db.flush()
    
    # Get customer name
    cust_res = await db.execute(
        select(Customer).where(Customer.id == prescription.customer_id)
    )
    customer = cust_res.scalar_one_or_none()
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": customer.name if customer else None,
        }
    )
