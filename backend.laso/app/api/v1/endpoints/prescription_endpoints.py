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
from sqlalchemy import select, and_, or_
from typing import Optional, List
from datetime import date, datetime, timezone
import uuid

from app.core.deps import get_current_user, require_any_permission, require_permission
from app.db.dependencies import get_db
from app.models.user.user_model import User
from app.models.precriptions.prescription_model import Prescription
from app.models.customer.customer_model import Customer
from app.models.pharmacy.pharmacy_model import Organization
from app.schemas.sales_schemas import SaleCreate
from app.schemas.base_schemas import TimestampSchema, SyncSchema
from app.utils.pagination import PaginatedResponse, Paginator, PaginationParams
from pydantic import BaseModel, Field, ConfigDict, field_validator, field_serializer, model_validator
from decimal import Decimal

router = APIRouter(prefix="/prescriptions", tags=["Prescriptions"])


def _customer_display_name(customer: Optional[Customer]) -> Optional[str]:
    if not customer:
        return None
    full_name = " ".join(
        part for part in [customer.first_name, customer.last_name] if part
    ).strip()
    return full_name or customer.phone or customer.email


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
    """Update prescription details, medications, or status"""
    # Basic details
    prescriber_name: Optional[str] = None
    prescriber_license: Optional[str] = None
    prescriber_phone: Optional[str] = None
    prescriber_address: Optional[str] = None
    
    # Dates
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    
    # Medications
    medications: Optional[List[MedicationItem]] = None
    
    # Refills
    refills_allowed: Optional[int] = Field(None, ge=0, le=10)
    
    # Clinical info
    diagnosis: Optional[str] = None
    notes: Optional[str] = None
    special_instructions: Optional[str] = None
    
    # Status
    status: Optional[str] = Field(
        None,
        description="active, filled, expired, cancelled"
    )


class MedicationResponse(MedicationItem):
    """Response medication item"""
    pass


class PrescriptionResponse(TimestampSchema, SyncSchema):
    """Prescription response"""
    id: uuid.UUID
    organization_id: uuid.UUID
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
    
    model_config = ConfigDict(from_attributes=True)
    
    @model_validator(mode='before')
    @classmethod
    def map_last_synced_at(cls, data):
        """Map last_synced_at from DB model to synced_at"""
        if isinstance(data, dict) and 'last_synced_at' in data and 'synced_at' not in data:
            data['synced_at'] = data.pop('last_synced_at')
        return data


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
    dependencies=[Depends(require_any_permission("manage_prescriptions", "process_sales"))]
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
    await db.commit()
    await db.refresh(prescription)
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": _customer_display_name(customer),
        }
    )


@router.delete(
    "/{prescription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("manage_prescriptions"))]
)
async def delete_prescription(
    prescription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete a prescription.

    **Permissions:** manage_prescriptions
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

    await db.delete(prescription)
    await db.commit()
    return None


@router.get(
    "/",
    response_model=PaginatedResponse[PrescriptionResponse],
    dependencies=[Depends(require_any_permission("manage_prescriptions", "process_sales"))]
)
async def list_prescriptions(
    pagination: PaginationParams = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    customer_id: Optional[uuid.UUID] = Query(None),
    status_filter: Optional[str] = Query(None, description="Filter by status: active, filled, expired, cancelled"),
    include_expired: bool = Query(True),
    search: Optional[str] = Query(None, description="Search prescription number, prescriber, or customer name"),
):
    """
    List prescriptions for the organization.

    Used by the prescription management screen. POS still uses the
    customer-scoped search endpoint below.
    """
    conditions = [Prescription.organization_id == current_user.organization_id]

    if customer_id:
        conditions.append(Prescription.customer_id == customer_id)

    if status_filter:
        conditions.append(Prescription.status == status_filter)

    if not include_expired:
        conditions.append(Prescription.expiry_date >= date.today())

    query = select(Prescription).where(and_(*conditions))

    if search:
        term = f"%{search.strip()}%"
        query = (
            query
            .join(Customer, Customer.id == Prescription.customer_id)
            .where(
                or_(
                    Prescription.prescription_number.ilike(term),
                    Prescription.prescriber_name.ilike(term),
                    Customer.first_name.ilike(term),
                    Customer.last_name.ilike(term),
                    Customer.phone.ilike(term),
                )
            )
        )

    query = query.order_by(Prescription.issue_date.desc(), Prescription.created_at.desc())
    result = await Paginator(db).paginate(query, pagination)

    customer_ids = [p.customer_id for p in result.items]
    customers = {}
    if customer_ids:
        cust_res = await db.execute(
            select(Customer).where(Customer.id.in_(customer_ids))
        )
        customers = {c.id: c for c in cust_res.scalars().all()}

    items = [
        PrescriptionResponse(
            **{
                **p.__dict__,
                "is_expired": p.expiry_date < date.today(),
                "customer_name": _customer_display_name(customers.get(p.customer_id)),
            }
        )
        for p in result.items
    ]

    return PaginatedResponse(
        items=items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        total_pages=result.total_pages,
        has_next=result.has_next,
        has_prev=result.has_prev,
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
    
    result = await Paginator(db).paginate(
        select(Prescription)
        .where(and_(*conditions))
        .order_by(Prescription.issue_date.desc()),
        paginate,
    )
    prescriptions = result.items
    
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
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        total_pages=result.total_pages,
        has_next=result.has_next,
        has_prev=result.has_prev,
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
            "customer_name": _customer_display_name(customer),
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
    Update prescription details, medications, or status.
    
    **Permissions:** manage_prescriptions
    
    **Editable Fields:**
    - Prescriber details (name, license, phone, address)
    - Issue and expiry dates
    - Medications (full replacement)
    - Refills allowed
    - Clinical info (diagnosis, notes, instructions)
    - Status (active, filled, expired, cancelled)
    
    **Example - Edit medications:**
    ```json
    {
      "medications": [
        {
          "drug_id": "uuid",
          "drug_name": "Amoxicillin",
          "dosage": "500mg",
          "frequency": "twice daily",
          "duration": "7 days",
          "quantity": 14
        }
      ]
    }
    ```
    
    **Example - Change status:**
    ```json
    {
      "status": "filled",
      "notes": "Dispensed on 2026-05-27"
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
    
    # Validate dates if being updated
    issue_date = update_data.issue_date or prescription.issue_date
    expiry_date = update_data.expiry_date or prescription.expiry_date
    
    if issue_date > expiry_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Issue date cannot be after expiry date"
        )
    
    if expiry_date < date.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expiry date must be today or in the future"
        )
    
    # Update basic fields
    if update_data.prescriber_name is not None:
        prescription.prescriber_name = update_data.prescriber_name
    
    if update_data.prescriber_license is not None:
        prescription.prescriber_license = update_data.prescriber_license
    
    if update_data.prescriber_phone is not None:
        prescription.prescriber_phone = update_data.prescriber_phone
    
    if update_data.prescriber_address is not None:
        prescription.prescriber_address = update_data.prescriber_address
    
    if update_data.issue_date is not None:
        prescription.issue_date = update_data.issue_date
    
    if update_data.expiry_date is not None:
        prescription.expiry_date = update_data.expiry_date
    
    # Update medications if provided
    if update_data.medications is not None:
        if len(update_data.medications) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one medication is required"
            )
        medications_list = [m.model_dump() for m in update_data.medications]
        prescription.medications = medications_list
    
    # Update refills
    if update_data.refills_allowed is not None:
        prescription.refills_allowed = update_data.refills_allowed
        # If increasing refills, replenish remaining
        if update_data.refills_allowed > prescription.refills_remaining:
            prescription.refills_remaining = update_data.refills_allowed
    
    # Update clinical info
    if update_data.diagnosis is not None:
        prescription.diagnosis = update_data.diagnosis
    
    if update_data.notes is not None:
        prescription.notes = update_data.notes
    
    if update_data.special_instructions is not None:
        prescription.special_instructions = update_data.special_instructions
    
    # Update status
    if update_data.status is not None:
        prescription.status = update_data.status
    
    prescription.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(prescription)
    
    # Get customer name
    cust_res = await db.execute(
        select(Customer).where(Customer.id == prescription.customer_id)
    )
    customer = cust_res.scalar_one_or_none()
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": _customer_display_name(customer),
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
    
    await db.commit()
    await db.refresh(prescription)
    
    # Get customer name
    cust_res = await db.execute(
        select(Customer).where(Customer.id == prescription.customer_id)
    )
    customer = cust_res.scalar_one_or_none()
    
    return PrescriptionResponse(
        **{
            **prescription.__dict__,
            "is_expired": prescription.expiry_date < date.today(),
            "customer_name": _customer_display_name(customer),
        }
    )
