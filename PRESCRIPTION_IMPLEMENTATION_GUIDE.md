"""
PROFESSIONAL PRESCRIPTION MANAGEMENT & POS CHECKOUT GUIDE
=========================================================

This guide shows how to integrate prescriptions into your pharmacy POS system.

## PART 1: UNDERSTANDING THE SYSTEM
==================================

### Prescription Workflow

1. HEALTHCARE PROVIDER (Doctor)
   └─> Creates prescription in external system or manually entered
       • prescription_number (unique ID: "RX-2026-001234")
       • patient (customer) info
       • medications list
       • prescriber license & contact
       • issue date & expiry date
       • refills allowed (0-10)

2. PHARMACY STAFF - INPUT PRESCRIPTION
   └─> Uses POST /api/v1/prescriptions
       • Enters all prescription details from paper/digital RX
       • System generates UUID internally
       • prescription.id is what you use in checkout

3. DURING POS CHECKOUT - FIND PRESCRIPTION
   └─> Customer buys Rx-only drug
       • Click "Search Prescriptions" → Search for customer
       • GET /api/v1/prescriptions/customer/{customer_id}
       • Shows list of valid (non-expired, active) prescriptions
       • Pharmacist selects one → gets prescription.id

4. COMPLETE SALE WITH PRESCRIPTION
   └─> Use prescription_id in sale request
       • POST /api/v1/sales with prescription_id
       • System validates & decrements refills
       • Creates audit trail for compliance

5. REFILL TRACKING
   └─> Each use decrements refills_remaining
       • When refills_remaining = 0 → status changes to "filled"
       • Customer needs new RX from doctor for more refills


### Key Data Flow

┌─────────────────────────────────────────────────────┐
│         PRESCRIPTION MANAGEMENT FLOW                 │
├─────────────────────────────────────────────────────┤
│                                                     │
│  [Healthcare Provider Issues RX]                    │
│         ↓                                           │
│  [Pharmacy Staff Enters in System]                  │
│         ↓                                           │
│  POST /api/v1/prescriptions                         │
│  ├─ prescription_number: "RX-2026-001234"          │
│  ├─ customer_id: uuid                               │
│  ├─ medications: [{drug_id, dosage, qty}]          │
│  ├─ refills_allowed: 3                              │
│  └─ expiry_date: 2026-06-27                         │
│         ↓                                           │
│  ✓ Prescription created (id: uuid)                  │
│  ✓ refills_remaining initialized to refills_allowed│
│  ✓ status = "active"                                │
│         ↓                                           │
│  [Customer Comes to Checkout]                       │
│         ↓                                           │
│  POS searches for prescriptions                     │
│  GET /api/v1/prescriptions/customer/{customer_id}  │
│  └─ Returns list of active, non-expired RXes       │
│         ↓                                           │
│  [Cashier/Pharmacist Selects Prescription]          │
│         ↓                                           │
│  POST /api/v1/sales                                │
│  ├─ customer_id: uuid                              │
│  ├─ items: [{drug_id, quantity}]                   │
│  ├─ prescription_id: uuid ← FROM SEARCH ABOVE      │
│  └─ payment info...                                │
│         ↓                                           │
│  SalesService validates:                            │
│  ├─ prescription.status == "active"                 │
│  ├─ NOT expired                                     │
│  ├─ refills_remaining > 0                           │
│  └─ user has pharmacist+ role                       │
│         ↓                                           │
│  ✓ Sale created                                     │
│  ✓ refills_remaining decremented                    │
│  ✓ verified_by = pharmacist_id                      │
│  ✓ verified_at = now()                              │
│         ↓                                           │
│  [Sale Complete - Print Receipt]                    │
│         ↓                                           │
│  If refills_remaining = 0:                         │
│     status = "filled" (customer needs new RX)       │
│  Else:                                              │
│     status = "active" (can use again)               │
│                                                     │
└─────────────────────────────────────────────────────┘


## PART 2: API ENDPOINTS REFERENCE
===================================

### 1. CREATE PRESCRIPTION (Backend/Admin)
   Endpoint: POST /api/v1/prescriptions
   Permission: manage_prescriptions
   
   Request:
   {
     "prescription_number": "RX-2026-001234",
     "customer_id": "550e8400-e29b-41d4-a716-446655440000",
     "prescriber_name": "Dr. Jane Smith",
     "prescriber_license": "MD123456",
     "prescriber_phone": "+1-555-0100",
     "prescriber_address": "123 Medical Center, City, State",
     "issue_date": "2026-05-27",
     "expiry_date": "2026-06-27",
     "medications": [
       {
         "drug_id": "660e8400-e29b-41d4-a716-446655440001",
         "drug_name": "Amoxicillin",
         "dosage": "500mg",
         "frequency": "twice daily",
         "duration": "7 days",
         "quantity": 14,
         "instructions": "Take with food"
       },
       {
         "drug_id": "770e8400-e29b-41d4-a716-446655440002",
         "drug_name": "Ibuprofen",
         "dosage": "400mg",
         "frequency": "as needed",
         "duration": "pain relief",
         "quantity": 10
       }
     ],
     "refills_allowed": 2,
     "diagnosis": "Bacterial respiratory infection",
     "notes": "Standard post-infection follow-up",
     "special_instructions": "Do not crush tablets. Do not take with dairy."
   }
   
   Response: PrescriptionResponse with id, created_at, etc.


### 2. SEARCH PRESCRIPTIONS (POS Checkout)
   Endpoint: GET /api/v1/prescriptions/customer/{customer_id}
   Permission: process_sales
   Query Parameters:
     - status_filter: Optional[str] (active, filled, expired, cancelled)
     - include_expired: bool (default: false)
     - page: int (default: 1)
     - size: int (default: 10)
   
   Example: GET /api/v1/prescriptions/customer/550e8400-e29b-41d4-a716-446655440000?include_expired=false&page=1&size=20
   
   Response:
   {
     "items": [
       {
         "id": "990e8400-e29b-41d4-a716-446655440003",
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


### 3. GET PRESCRIPTION DETAILS
   Endpoint: GET /api/v1/prescriptions/{prescription_id}
   Permission: (none - readable by any authenticated user)
   
   Response: Full PrescriptionResponse with all medications


### 4. UPDATE PRESCRIPTION STATUS
   Endpoint: PATCH /api/v1/prescriptions/{prescription_id}
   Permission: manage_prescriptions
   
   Request:
   {
     "status": "filled",  // or "cancelled"
     "notes": "Dispensed 14 tablets on 2026-05-27. Customer counseled."
   }


### 5. USE REFILL (Auto-called by Sales Service)
   Endpoint: POST /api/v1/prescriptions/{prescription_id}/refill
   Permission: process_sales
   
   Effect:
   - Decrements refills_remaining by 1
   - Sets last_refill_date = today
   - Sets verified_by = current_user.id
   - If refills_remaining becomes 0, status = "filled"
   
   Called automatically by: SalesService.process_sale()


## PART 3: POS CHECKOUT IMPLEMENTATION
=====================================

### Step-by-Step Checkout Flow

1. CUSTOMER ARRIVES AT CHECKOUT
   ├─ Click "New Sale"
   ├─ Select or create customer
   └─ Add items to cart


2. SCANNING RX DRUGS
   When scanning drug with requires_prescription=true:
   
   ✓ POS highlights: "This drug requires a prescription"
   ├─ Cashier clicks "Select Prescription"
   ├─ Frontend calls: GET /api/v1/prescriptions/customer/{customer_id}
   └─ Shows popup with list of prescriptions


3. PRESCRIPTION SEARCH RESULTS
   Display:
   ┌─────────────────────────────────────────────────┐
   │ AVAILABLE PRESCRIPTIONS                         │
   ├─────────────────────────────────────────────────┤
   │ [✓] RX-2026-001234                              │
   │     Dr. Jane Smith | Expires: Jun 27            │
   │     Medications: Amoxicillin, Ibuprofen        │
   │     Refills Remaining: 2/2                      │
   │     [SELECT]                                    │
   ├─────────────────────────────────────────────────┤
   │ [✓] RX-2026-001235                              │
   │     Dr. John Doe | Expires: Jul 15              │
   │     Medications: Metformin                      │
   │     Refills Remaining: 0/3 [EXPIRED REFILLS]   │
   │     [SELECT]                                    │
   └─────────────────────────────────────────────────┘


4. PHARMACIST REVIEW
   After prescription selected:
   ├─ POS calls GET /api/v1/prescriptions/{id}
   ├─ Shows full details for review
   ├─ Pharmacist verifies medications match patient needs
   └─ Approves or returns to search


5. COMPLETE SALE
   Frontend constructs sale request:
   
   POST /api/v1/sales
   {
     "branch_id": "uuid",
     "customer_id": "550e8400-e29b-41d4-a716-446655440000",
     "items": [
       {
         "drug_id": "660e8400-e29b-41d4-a716-446655440001",
         "quantity": 14,
         "batch_id": null  // Let system auto-select
       },
       {
         "drug_id": "770e8400-e29b-41d4-a716-446655440002",
         "quantity": 10
       }
     ],
     "prescription_id": "990e8400-e29b-41d4-a716-446655440003",  ← KEY FIELD
     "payment_method": "cash",
     "amount_paid": 450.00,
     "notes": "Patient allergic to Penicillin"
   }


6. BACKEND PROCESSING
   SalesService.process_sale() performs:
   
   a) Validate prescription:
      ├─ Load prescription (org-scoped)
      ├─ Check status == "active"
      ├─ Check NOT expired
      ├─ Check refills_remaining > 0
      ├─ Check user is pharmacist or above
      └─ ✓ Validation passed
   
   b) Validate against all Rx drugs in sale:
      └─ For each item where drug.requires_prescription = true:
         └─ Ensure prescription_id was provided
   
   c) Create sale with prescription tracking:
      ├─ sale.prescription_id = prescription_id
      ├─ sale.prescription_number = "RX-2026-001234"
      ├─ sale.prescriber_name = "Dr. Jane Smith"
      ├─ sale.pharmacist_id = current_user.id
      └─ sale_item.prescription_verified = true
   
   d) Decrement prescription refills:
      ├─ Call POST /api/v1/prescriptions/{id}/refill
      ├─ refills_remaining: 2 → 1
      ├─ last_refill_date = 2026-05-27
      ├─ verified_at = now()
      └─ If refills_remaining becomes 0:
         └─ status: "active" → "filled" (needs new RX)
   
   e) Continue with normal sale processing:
      ├─ Deduct inventory (FEFO batches)
      ├─ Apply discounts
      ├─ Calculate tax
      ├─ Process payment
      └─ Print receipt


7. RECEIPT OUTPUT
   Printed/Digital Receipt includes:
   ┌─────────────────────────────────────────────────┐
   │ PHARMACY RECEIPT                                │
   │ [Logo]                                          │
   ├─────────────────────────────────────────────────┤
   │ Customer: John Patient                          │
   │ Date: 2026-05-27 14:32:15                      │
   │ Receipt #: 20260527-001234                      │
   ├─────────────────────────────────────────────────┤
   │ Prescription: RX-2026-001234                    │
   │ Prescriber: Dr. Jane Smith (MD123456)           │
   │ Refills Used: 1 of 2                            │
   ├─────────────────────────────────────────────────┤
   │ Items:                                          │
   │ 1. Amoxicillin 500mg x14        KES 450.00    │
   │    [PRESCRIPTION REQUIRED] ✓ Verified           │
   │ 2. Ibuprofen 400mg x10          KES 180.00    │
   │    [OTC] No prescription needed                │
   ├─────────────────────────────────────────────────┤
   │ Subtotal:                       KES 630.00    │
   │ Insurance Discount:             KES 50.00     │
   │ Tax (16%):                      KES 92.80     │
   │ Total:                          KES 672.80    │
   ├─────────────────────────────────────────────────┤
   │ Payment: CASH                   KES 700.00    │
   │ Change:                         KES 27.20     │
   ├─────────────────────────────────────────────────┤
   │ Pharmacist: Sarah Johnson                       │
   │ Verified: 2026-05-27 14:32:15                  │
   │                                                │
   │ *** PATIENT COUNSELING PROVIDED ***            │
   │ *** KEEP FOR YOUR RECORDS ***                  │
   └─────────────────────────────────────────────────┘


## PART 4: COMMON SCENARIOS
===========================

### Scenario 1: Patient with 3 refills, buys 3 times
   
   Initial: refills_allowed=3, refills_remaining=3
   
   Sale 1: refills_remaining: 3 → 2, status="active"
   Sale 2: refills_remaining: 2 → 1, status="active"
   Sale 3: refills_remaining: 1 → 0, status="filled" ← Need new RX!
   
   Sale 4: ✗ ERROR - "No refills remaining on this prescription"
           → Patient must get new RX from doctor


### Scenario 2: Prescription expires during month
   
   Today: 2026-05-27
   expiry_date: 2026-05-31 (4 days remaining)
   
   Sale 1 (May 28): ✓ Works (not expired)
   Sale 2 (May 31): ✓ Works (expires today, still valid)
   Sale 3 (Jun 1):  ✗ ERROR - "Prescription expired on 2026-05-31"
                    → Patient needs new RX


### Scenario 3: Doctor cancels prescription
   
   Initial: status="active"
   Doctor calls pharmacy: "Cancel RX-2026-001234"
   
   Pharmacist:
   PATCH /api/v1/prescriptions/{id}
   {
     "status": "cancelled",
     "notes": "Doctor requested cancellation per phone call"
   }
   
   Result: ✗ All future sales with this RX fail


### Scenario 4: Walk-in customer (no existing RX)
   
   Customer: "I have a prescription from home"
   Cashier: "We'll need to enter it into the system first"
   
   Pharmacy Manager (if available):
   POST /api/v1/prescriptions
   { ... enter all details from paper RX ... }
   
   → Then proceeds with normal checkout


### Scenario 5: Insurance claim with prescription
   
   Same as normal, but add to sale:
   
   POST /api/v1/sales
   {
     ... all normal fields ...
     "prescription_id": "990e8400...",
     "insurance_verified": true,
     "insurance_claim_number": "CLAIM-2026-98765",
     "patient_copay_amount": 50.00,
     "insurance_covered_amount": 622.80
   }
   
   Result: Sale includes insurance tracking for claim submission


## PART 5: PERMISSION REQUIREMENTS
==================================

MANAGE PRESCRIPTIONS (manage_prescriptions):
├─ Create new prescription
├─ Update prescription status
└─ Cancel prescription

PROCESS SALES (process_sales):
├─ Search prescriptions for customer
├─ View prescription details
├─ Use prescription in sale
└─ Automatically decrements refills

ROLE-BASED:
├─ Pharmacist: Can process Rx sales (required)
├─ Manager: Can process Rx sales
├─ Admin: Can do everything
├─ Cashier: Cannot process Rx sales
└─ Viewer: Cannot process Rx sales


## TROUBLESHOOTING
==================

Problem: "Prescription not found for this customer"
├─ Cause: Prescription created for wrong customer
├─ Fix: Verify prescription customer_id matches sale customer_id
└─ Check: GET /api/v1/prescriptions/customer/{customer_id}

Problem: "No refills remaining on this prescription"
├─ Cause: All refills used up
├─ Fix: Patient needs new RX from doctor
└─ Check: GET /api/v1/prescriptions/{id} → refills_remaining field

Problem: "Prescription expired. A new prescription is required"
├─ Cause: expiry_date < today()
├─ Fix: Doctor must issue new prescription
└─ Check: Compare expiry_date with today's date

Problem: "Only pharmacists may process prescription sales"
├─ Cause: Logged-in user is cashier or viewer
├─ Fix: Pharmacist or manager must complete the sale
└─ Check: User role in system

Problem: Cannot create prescription - "Prescription number already exists"
├─ Cause: This prescription_number was already entered
├─ Fix: Check if RX was already input, or use different number
└─ Check: Ensure unique prescription_number across organization
"""
