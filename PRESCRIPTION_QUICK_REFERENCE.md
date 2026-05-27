# Prescription Management - Quick Reference

## How to Get a Prescription ID (For Checkout)

### The Complete Flow in 4 Steps:

#### Step 1: Create Prescription (Admin/Manager)
```bash
POST /api/v1/prescriptions
{
  "prescription_number": "RX-2026-001234",
  "customer_id": "550e8400-e29b-41d4-a716-446655440000",
  "prescriber_name": "Dr. Jane Smith",
  "prescriber_license": "MD123456",
  "issue_date": "2026-05-27",
  "expiry_date": "2026-06-27",
  "medications": [
    {
      "drug_id": "660e8400-e29b-41d4-a716-446655440001",
      "drug_name": "Amoxicillin",
      "dosage": "500mg",
      "frequency": "twice daily",
      "duration": "7 days",
      "quantity": 14
    }
  ],
  "refills_allowed": 2
}
```

**Response includes:**
```json
{
  "id": "990e8400-e29b-41d4-a716-446655440003",  ← THIS IS YOUR PRESCRIPTION ID
  "prescription_number": "RX-2026-001234",
  "status": "active",
  "refills_remaining": 2
}
```

---

#### Step 2: Find Prescription at Checkout
```bash
GET /api/v1/prescriptions/customer/550e8400-e29b-41d4-a716-446655440000
```

**Response:**
```json
{
  "items": [
    {
      "id": "990e8400-e29b-41d4-a716-446655440003",  ← PRESCRIPTION ID
      "prescription_number": "RX-2026-001234",
      "prescriber_name": "Dr. Jane Smith",
      "medications_count": 1,
      "issue_date": "2026-05-27",
      "expiry_date": "2026-06-27",
      "is_expired": false,
      "status": "active",
      "refills_remaining": 2,
      "refills_allowed": 2
    }
  ]
}
```

---

#### Step 3: Select & Verify Prescription
```bash
GET /api/v1/prescriptions/990e8400-e29b-41d4-a716-446655440003
```

Shows full details for pharmacist review before checkout.

---

#### Step 4: Use in Checkout
```bash
POST /api/v1/sales
{
  "branch_id": "uuid",
  "customer_id": "550e8400-e29b-41d4-a716-446655440000",
  "items": [
    {
      "drug_id": "660e8400-e29b-41d4-a716-446655440001",
      "quantity": 14
    }
  ],
  "prescription_id": "990e8400-e29b-41d4-a716-446655440003",  ← USE HERE
  "payment_method": "cash",
  "amount_paid": 450.00
}
```

**After sale completes:**
- ✓ refills_remaining: 2 → 1
- ✓ sale.prescription_id = prescription_id
- ✓ sale.pharmacist_id = current user
- ✓ Patient can refill 1 more time

---

## Key Concepts

| Concept | Meaning |
|---------|---------|
| **prescription_id** | UUID of the prescription record in your system |
| **prescription_number** | Human-readable ID from doctor (e.g., "RX-2026-001234") |
| **refills_remaining** | How many more times prescription can be used |
| **status** | "active" = can use, "filled" = out of refills, "expired" = past expiry date |
| **is_expired** | True if today > expiry_date |

---

## Refill Tracking Example

```
Initial Prescription Created:
├─ refills_allowed: 3
└─ refills_remaining: 3

After Sale 1:
└─ refills_remaining: 2 (status = "active")

After Sale 2:
└─ refills_remaining: 1 (status = "active")

After Sale 3:
└─ refills_remaining: 0 (status = "filled" ← NEEDS NEW RX FROM DOCTOR)

Sale 4 Attempt:
└─ ✗ ERROR: "No refills remaining"
     → Customer must visit doctor for new prescription
```

---

## Common Issues & Solutions

| Problem | Cause | Solution |
|---------|-------|----------|
| "Prescription not found" | Wrong customer_id | Verify customer before checkout |
| "No refills remaining" | All refills used | Patient needs new RX from doctor |
| "Prescription expired" | Today > expiry_date | Patient needs new RX from doctor |
| "Only pharmacists may process" | User is cashier | Have pharmacist complete sale |
| "Prescription number already exists" | Duplicate entry | Check if already in system |

---

## API Summary

| Operation | Endpoint | Method | Permission |
|-----------|----------|--------|-----------|
| Create | POST /api/v1/prescriptions | POST | manage_prescriptions |
| Search for checkout | GET /api/v1/prescriptions/customer/{id} | GET | process_sales |
| View details | GET /api/v1/prescriptions/{id} | GET | (any user) |
| Update status | PATCH /api/v1/prescriptions/{id} | PATCH | manage_prescriptions |
| Use in sale | POST /api/v1/sales with prescription_id | POST | process_sales |

---

## Real-World Workflow

**Day 1: Patient visits doctor**
- Doctor issues prescription on paper or digitally
- Prescription: "RX-2026-001234" for Amoxicillin x14, 2 refills

**Day 2: Patient arrives at pharmacy**
- Pharmacist enters prescription:
  ```bash
  POST /api/v1/prescriptions {prescription_number: "RX-2026-001234", ...}
  ```
- System responds with: `id: "990e8400..."`

**Day 2: Checkout**
- Cashier rings up Amoxicillin
- POS detects: `requires_prescription=true`
- Cashier clicks "Search Prescriptions"
  ```bash
  GET /api/v1/prescriptions/customer/{customer_uuid}
  ```
- Shows: `RX-2026-001234, Refills: 2/2, Expires: Jun 27`
- Pharmacist approves → Selects it
- Checkout completes with `prescription_id: "990e8400..."`
- refills_remaining: 2 → 1

**Week 1: Patient refills**
- Comes back to buy more
- Same prescription found (refills_remaining = 1)
- After checkout: refills_remaining: 1 → 0, status: "filled"
- Message: "No more refills on this prescription"

**Week 2: Patient tries again**
- Comes back for another refill
- Search shows: status = "filled", refills_remaining = 0
- ✗ Cannot complete sale
- Pharmacist: "You'll need a new prescription from your doctor"
- Customer contacts doctor, gets new RX, repeats process

---

## For Frontend Implementation

### POS Checkout Flow

```javascript
// When user adds Rx drug to cart
if (drug.requires_prescription) {
  // Show prescription search modal
  
  // Search endpoint
  const response = await fetch(
    `/api/v1/prescriptions/customer/${customerId}`
  );
  
  // User selects one from list
  const selectedPrescription = searchResults[0]; // User clicks
  const prescriptionId = selectedPrescription.id;
  
  // Use in checkout
  const sale = {
    customer_id: customerId,
    items: [...],
    prescription_id: prescriptionId,  // KEY FIELD
    payment_method: "cash",
    amount_paid: 450
  };
  
  // Complete sale
  const response = await fetch('/api/v1/sales', {
    method: 'POST',
    body: JSON.stringify(sale)
  });
}
```

---

## Permission Assignment

In user settings, assign permissions:

- **Pharmacist Role:**
  - ✓ process_sales
  - ✓ manage_prescriptions
  - ✓ view_reports

- **Manager Role:**
  - ✓ process_sales
  - ✓ manage_prescriptions
  - ✓ manage_inventory
  - ✓ approve_purchase_orders

- **Cashier Role:**
  - ✓ process_sales
  - ✗ manage_prescriptions
  - ✗ view_reports

---

## Testing Checklist

- [ ] Create prescription with valid data
- [ ] Try duplicate prescription_number → Error
- [ ] Search for customer with no prescriptions → Empty list
- [ ] Search returns only non-expired, active prescriptions
- [ ] Use prescription in sale → refills decremented
- [ ] Use all refills → status changes to "filled"
- [ ] Try to use expired prescription → Error
- [ ] Update prescription status to "cancelled" → No more sales
- [ ] Verify audit trail (verified_by, verified_at)
- [ ] Check receipt includes prescription details

