# Prescription Management - Visual Workflows

## 🔄 Complete Prescription Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                     PRESCRIPTION LIFECYCLE                          │
└─────────────────────────────────────────────────────────────────────┘

PHASE 1: DOCTOR ISSUES PRESCRIPTION
══════════════════════════════════════════════════════════════════════
    
    Healthcare Provider (Doctor)
           │
           └──> Issues Rx on Paper or Digital
                │
                ├─ prescription_number: "RX-2026-001234"
                ├─ patient (customer)
                ├─ medications list
                ├─ prescriber license
                ├─ issue date & expiry date
                └─ refills allowed


PHASE 2: ENTER INTO PHARMACY SYSTEM
══════════════════════════════════════════════════════════════════════
    
    Pharmacy Manager/Technician
           │
           └──> Uses POST /api/v1/prescriptions
                │
                ├─ Input all prescription details
                ├─ System validates
                └─ Returns: prescription.id ← SAVE THIS
                          prescription.status = "active"
                          prescription.refills_remaining = refills_allowed
                          prescription.created_at


PHASE 3: CUSTOMER CHECKOUT
══════════════════════════════════════════════════════════════════════
    
    Customer arrives with Rx drugs
           │
           ├──> Cashier scans items
           │
           └──> POS detects: drug.requires_prescription = true
                │
                ├──> Shows: "Prescription Required"
                │
                └──> Cashier clicks "Search Prescriptions"
                     │
                     GET /api/v1/prescriptions/customer/{customer_id}
                     │
                     ├─ Searches database
                     ├─ Filters: status="active" AND expiry_date >= today
                     └─ Returns: List of valid prescriptions
                        
                        ┌─────────────────────────────────────────┐
                        │ RX-2026-001234                          │
                        │ Dr. Jane Smith | Expires Jun 27         │
                        │ Medications: Amoxicillin, Ibuprofen    │
                        │ Refills: 2/2                            │
                        │ [SELECT] ← Cashier clicks here          │
                        └─────────────────────────────────────────┘


PHASE 4: VERIFY PRESCRIPTION
══════════════════════════════════════════════════════════════════════
    
    GET /api/v1/prescriptions/{id}  ← Pharmacist reviews
           │
           ├─ Full prescription details displayed
           ├─ Medications list shown
           ├─ Prescriber info verified
           └─ Pharmacist confirms: "OK to dispense"


PHASE 5: COMPLETE SALE
══════════════════════════════════════════════════════════════════════
    
    POST /api/v1/sales
    {
      "customer_id": "...",
      "items": [{"drug_id": "...", "quantity": 14}],
      "prescription_id": "990e8400...",  ← FROM PHASE 3
      "payment_method": "cash",
      "amount_paid": 450.00
    }
           │
           ├─ SalesService validates prescription:
           │  ├─ status = "active"? ✓
           │  ├─ NOT expired? ✓
           │  ├─ refills_remaining > 0? ✓
           │  ├─ User is pharmacist+? ✓
           │  └─ All checks pass ✓
           │
           ├─ Create sale with Rx tracking:
           │  ├─ sale.prescription_id = "990e8400..."
           │  ├─ sale.prescription_number = "RX-2026-001234"
           │  ├─ sale.prescriber_name = "Dr. Jane Smith"
           │  └─ sale.pharmacist_id = current_user.id
           │
           └─ Decrement prescription refills:
              │
              POST /api/v1/prescriptions/{id}/refill
              │
              ├─ refills_remaining: 2 → 1
              ├─ last_refill_date: 2026-05-27
              ├─ verified_by: pharmacist_id
              ├─ verified_at: now()
              │
              └─ If refills_remaining = 0:
                 └─ status: "active" → "filled"


PHASE 6: PRINT RECEIPT
══════════════════════════════════════════════════════════════════════
    
    ┌────────────────────────────────────────────────┐
    │          PHARMACY RECEIPT                      │
    ├────────────────────────────────────────────────┤
    │ Customer: John Patient                         │
    │ Date: 2026-05-27 14:32:15                    │
    │                                                │
    │ Prescription: RX-2026-001234                  │
    │ Prescriber: Dr. Jane Smith                    │
    │ Refills Used: 1 of 2                          │
    │                                                │
    │ Items:                                         │
    │ 1. Amoxicillin 500mg x14    KES 450.00      │
    │    [PRESCRIPTION] ✓ Verified                  │
    │                                                │
    │ Total:                      KES 450.00      │
    │ Payment: CASH               KES 450.00      │
    │ Change:                     KES 0.00        │
    │                                                │
    │ Pharmacist: Sarah Johnson                      │
    │ Verified: 2026-05-27 14:32:15                │
    └────────────────────────────────────────────────┘


PHASE 7: REFILL SCENARIO (LATER)
══════════════════════════════════════════════════════════════════════
    
    Customer returns week later
           │
           ├─ Same process as PHASE 3
           │  GET /api/v1/prescriptions/customer/{id}
           │  └─ Same prescription appears (still active)
           │
           ├─ Search shows:
           │  └─ Refills: 1/2 (was 2/2, now 1/2)
           │
           ├─ Complete sale PHASE 5
           │  └─ refills_remaining: 1 → 0
           │
           └─ After 2nd sale:
              status: "active" → "filled"
              Message: "No refills available - Get new Rx from doctor"


PHASE 8: OUT OF REFILLS
══════════════════════════════════════════════════════════════════════
    
    Customer returns 3rd time
           │
           ├─ GET /api/v1/prescriptions/customer/{id}
           │  └─ Prescription appears but status="filled"
           │
           ├─ Status badge shows: "FILLED"
           │
           └─ ✗ DISABLED FOR SELECTION
              Message: "No refills remaining"
              Action: "Patient must get new prescription from doctor"

```

---

## 📊 POS Checkout Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│              POS CHECKOUT WITH PRESCRIPTION                    │
└─────────────────────────────────────────────────────────────────┘


[1] INITIALIZE SALE
   ┌─────────────────────────────┐
   │ Cashier clicks "New Sale"   │
   │ Select customer             │
   │ Add items to cart           │
   └──────────────┬──────────────┘
                  │
                  ▼
[2] SCAN ITEMS
   ┌──────────────────────────────────┐
   │ Item 1: Amoxicillin             │
   │         requires_prescription: true
   │                                  │
   │ Item 2: Ibuprofen               │
   │         requires_prescription: false
   └──────────────┬───────────────────┘
                  │
                  ▼
[3] CHECK FOR Rx ITEMS
   ┌────────────────────────────────┐
   │ Scan items for requirements    │
   │                                 │
   │ Item 1: requires_prescription?  │
   │ ✗ YES → Rx Required             │
   │                                 │
   │ Item 2: requires_prescription?  │
   │ ✓ NO → OTC                      │
   └──────────────┬──────────────────┘
                  │
                  ▼ Rx items detected
   ┌──────────────────────────────────────────┐
   │  ALERT: Prescription required for this   │
   │         item(s)                          │
   │                                          │
   │  [SELECT PRESCRIPTION]                   │
   └──────────────┬───────────────────────────┘
                  │
                  ▼ Cashier clicks button
[4] SEARCH PRESCRIPTIONS
   ┌──────────────────────────────────────────────┐
   │ GET /api/v1/prescriptions/customer/{id}     │
   │                                              │
   │ System searches prescriptions for customer   │
   │ Filters:                                     │
   │   - status = "active"                        │
   │   - expiry_date >= today                    │
   │   - refills_remaining > 0                   │
   └──────────────┬───────────────────────────────┘
                  │
                  ▼
   ┌───────────────────────────────────────────────┐
   │ SEARCH RESULTS MODAL                          │
   ├───────────────────────────────────────────────┤
   │                                               │
   │ [✓] RX-2026-001234                            │
   │     Dr. Jane Smith                            │
   │     Expires: 2026-06-27 (30 days)             │
   │     Medications: 2 drugs                      │
   │     Refills: 2/2 ✓ Available                  │
   │     [SELECT] ← Cashier clicks                 │
   │                                               │
   │ [✓] RX-2026-001235                            │
   │     Dr. John Doe                              │
   │     Expires: 2026-07-15 (48 days)             │
   │     Medications: 1 drug                       │
   │     Refills: 0/3 ✗ No refills                │
   │     [SELECT - DISABLED]                       │
   │                                               │
   └───────────────────────────────────────────────┘
                  │
                  ▼ Cashier selects first one
   ┌────────────────────────────────────────────┐
   │ Prescription Selected: RX-2026-001234      │
   │ ✓ Status: active                            │
   │ ✓ Not expired                               │
   │ ✓ Refills available: 2                      │
   │ ✓ Click CONTINUE                            │
   └──────────────┬─────────────────────────────┘
                  │
                  ▼
[5] VERIFY WITH PHARMACIST
   ┌──────────────────────────────────────┐
   │ GET /api/v1/prescriptions/{id}       │
   │                                      │
   │ Pharmacist reviews full details:     │
   │ ├─ Prescriber: Dr. Jane Smith       │
   │ ├─ Medications: [...]               │
   │ ├─ Diagnosis: Infection              │
   │ ├─ Instructions: Do not crush        │
   │ └─ Approves ✓                        │
   └──────────────┬──────────────────────┘
                  │
                  ▼
[6] CONFIRM CHECKOUT
   ┌─────────────────────────────────┐
   │ Summary:                        │
   │                                 │
   │ Items:                          │
   │  1. Amoxicillin x14  KES 450   │
   │  2. Ibuprofen x10    KES 180   │
   │                                 │
   │ Rx: RX-2026-001234              │
   │ Refills: 2/2                    │
   │                                 │
   │ Payment: CASH                   │
   │ Amount: KES 450                 │
   │                                 │
   │ [PROCESS PAYMENT]               │
   └──────────────┬──────────────────┘
                  │
                  ▼ Pharmacist confirms
[7] PROCESS SALE
   ┌──────────────────────────────────────────┐
   │ POST /api/v1/sales                       │
   │ {                                        │
   │   customer_id: "...",                    │
   │   items: [...],                          │
   │   prescription_id: "990e8400...",  ◄─── KEY
   │   payment_method: "cash",                │
   │   amount_paid: 450.00                    │
   │ }                                        │
   │                                          │
   │ SalesService:                            │
   │   1. Validate prescription ✓             │
   │   2. Deduct inventory ✓                  │
   │   3. Create sale ✓                       │
   │   4. POST /prescriptions/{id}/refill ✓  │
   │   5. Decrement refills: 2 → 1 ✓         │
   │   6. Complete ✓                          │
   └──────────────┬───────────────────────────┘
                  │
                  ▼
[8] PRINT RECEIPT
   ┌──────────────────────────────────────────┐
   │ ┌────────────────────────────────────┐  │
   │ │      PHARMACY RECEIPT              │  │
   │ │ Customer: John Patient             │  │
   │ │ Date: 2026-05-27 14:32             │  │
   │ │                                    │  │
   │ │ Rx: RX-2026-001234                 │  │
   │ │ Dr. Jane Smith                     │  │
   │ │ Refills Used: 1 of 2               │  │
   │ │                                    │  │
   │ │ Items:                             │  │
   │ │ 1. Amoxicillin 500mg x14           │  │
   │ │    [PRESCRIPTION] ✓ Verified       │  │
   │ │ 2. Ibuprofen 400mg x10             │  │
   │ │    [OTC] No prescription needed    │  │
   │ │                                    │  │
   │ │ Total: KES 450                     │  │
   │ │ Pharmacist: Sarah Johnson          │  │
   │ └────────────────────────────────────┘  │
   └──────────────┬───────────────────────────┘
                  │
                  ▼
[9] SALE COMPLETE
   ✓ Customer has receipt
   ✓ Prescription refills decreased (2 → 1)
   ✓ Audit trail created
   ✓ Inventory updated
   ✓ Ready for next customer

```

---

## 🔄 Refill Tracking Example

```
CUSTOMER'S REFILL JOURNEY
═══════════════════════════════════════════════════════════════════════

DAY 1: Doctor Issues Prescription
  Doctor writes: "Amoxicillin 500mg, 2 refills"
  
DAY 2: Pharmacy Manager Enters in System
  POST /api/v1/prescriptions
  {
    prescription_number: "RX-2026-001234",
    customer_id: "550e8400...",
    refills_allowed: 2,
    ...
  }
  
  Response:
  {
    id: "990e8400...",
    status: "active",
    refills_remaining: 2,  ← Initialized to refills_allowed
    created_at: "2026-05-27"
  }

DAY 3: FIRST REFILL - Customer Buys
  ┌─────────────────────────────────┐
  │ Pharmacy State BEFORE Sale:     │
  │ ├─ status: "active"              │
  │ ├─ refills_remaining: 2          │
  │ ├─ last_refill_date: null        │
  │ └─ Can sell? YES ✓               │
  └─────────────────────────────────┘
           │
           ▼ Sale completes
  ┌──────────────────────────────────┐
  │ Pharmacy State AFTER Sale:       │
  │ ├─ status: "active"               │
  │ ├─ refills_remaining: 1           │ ◄─ DECREMENTED
  │ ├─ last_refill_date: 2026-05-27   │
  │ ├─ verified_by: pharmacist_id     │
  │ └─ verified_at: 2026-05-27 14:32  │
  └──────────────────────────────────┘
  
  Receipt: "Refills Used: 1 of 2"

DAY 10: SECOND REFILL - Customer Buys Again
  ┌─────────────────────────────────┐
  │ Pharmacy State BEFORE Sale:     │
  │ ├─ status: "active"              │
  │ ├─ refills_remaining: 1          │
  │ ├─ last_refill_date: 2026-05-27  │
  │ └─ Can sell? YES ✓               │
  └─────────────────────────────────┘
           │
           ▼ Sale completes
  ┌──────────────────────────────────┐
  │ Pharmacy State AFTER Sale:       │
  │ ├─ status: "filled"              │ ◄─ CHANGED (all refills used)
  │ ├─ refills_remaining: 0          │ ◄─ NOW ZERO
  │ ├─ last_refill_date: 2026-05-10  │
  │ ├─ verified_by: pharmacist_id    │
  │ └─ verified_at: 2026-05-10 10:15 │
  └──────────────────────────────────┘
  
  Receipt: "Refills Used: 2 of 2 [LAST REFILL]"
  Message: "No more refills. Contact your doctor."

DAY 11: CUSTOMER TRIES TO REFILL AGAIN
  GET /api/v1/prescriptions/customer/{customer_id}
  
  Search Result:
  ┌──────────────────────────────────┐
  │ RX-2026-001234                   │
  │ Dr. Jane Smith                   │
  │ Status: FILLED                   │ ◄─ DISABLED
  │ Refills: 0/2                     │
  │ [SELECT - DISABLED]              │
  └──────────────────────────────────┘
  
  Error Message: "No refills remaining on this prescription.
                 Contact your doctor for a new prescription."

DAY 15: Doctor Issues NEW Prescription
  Doctor writes: "Amoxicillin 500mg, 3 refills"
  
DAY 16: Pharmacy Manager Enters NEW Prescription
  POST /api/v1/prescriptions
  {
    prescription_number: "RX-2026-001235",  ◄─ DIFFERENT NUMBER
    customer_id: "550e8400...",
    refills_allowed: 3,
    ...
  }
  
  Now customer can buy again with new RX...

═══════════════════════════════════════════════════════════════════════
SUMMARY OF REFILL TRACKING

Initial State:     refills_allowed=2, refills_remaining=2, status=active
After 1st sale:    refills_remaining=1, status=active
After 2nd sale:    refills_remaining=0, status=filled ← AUTO-FILLED
3rd attempt:       ✗ ERROR - no refills
New prescription:  refills_allowed=3, refills_remaining=3, status=active

```

---

## 🎯 Decision Tree

```
SHOULD THIS DRUG REQUIRE A PRESCRIPTION?
════════════════════════════════════════════════════════════════════════

                    ┌─────────────────────────┐
                    │   Drug in POS System    │
                    │                         │
                    │ requires_prescription:? │
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                        │
                    ▼                        ▼
            ┌─────────────────┐      ┌──────────────┐
            │   TRUE (Rx)     │      │  FALSE (OTC) │
            └────────┬────────┘      └──────────────┘
                     │
        ┌────────────┴─────────────┐
        │                          │
        ▼                          ▼
   ┌────────────┐          ┌─────────────┐
   │  At POS    │          │  Checkout   │
   │            │          │             │
   │ Detect:    │          │ - No check  │
   │ "Rx Req"   │          │ - Buy now   │
   │            │          │             │
   │ Actions:   │          └─────────────┘
   │ 1. Alert   │
   │ 2. Search  │
   │ 3. Select  │
   │ 4. Verify  │
   │ 5. Approve │
   │ 6. Sell    │
   └────────────┘

PRESCRIPTION LOOKUP DURING CHECKOUT
════════════════════════════════════════════════════════════════════════

    Get Customer ID
         │
         ▼
    GET /prescriptions/customer/{id}
         │
         ▼
    Filter criteria:
    ├─ status = "active"? ──NO──> Exclude
    ├─ expiry_date >= today? ──NO──> Exclude
    ├─ refills_remaining > 0? ──NO──> Exclude
    └─ All pass? ──YES──> Include in results
         │
         ▼
    Display valid prescriptions:
    ├─ RX number
    ├─ Prescriber
    ├─ Medications
    ├─ Refills remaining
    └─ [SELECT] buttons
         │
         ▼
    Pharmacist selects one
         │
         ▼
    GET /prescriptions/{id} (optional - full review)
         │
         ▼
    Use prescription_id in sale:
    POST /api/v1/sales
    { prescription_id: "...", ... }

```

