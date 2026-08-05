# Pharmacy Audit Plan

**System:** Laso Pharmacy Management System
**Target:** Full-stack validation (API, Database, Audit, UI, Offline Sync)
**Approach:** Dependency-ordered workflow execution

---

## Contents

1. [Audit Conventions](#1-audit-conventions)
2. [Phase 1: Organization & Branch Setup](#2-phase-1-organization--branch-setup)
3. [Phase 2: User & Role Management](#3-phase-2-user--role-management)
4. [Phase 3: Drug Catalog & Inventory](#4-phase-3-drug-catalog--inventory)
5. [Phase 4: Purchase Orders & Stock Receiving](#5-phase-4-purchase-orders--stock-receiving)
6. [Phase 5: Sales & POS](#6-phase-5-sales--pos)
7. [Phase 6: Refunds](#7-phase-6-refunds)
8. [Phase 7: Prescriptions](#8-phase-7-prescriptions)
9. [Phase 8: Customer & Loyalty](#9-phase-8-customer--loyalty)
10. [Phase 9: Pricing Contracts](#10-phase-9-pricing-contracts)
11. [Phase 10: Stock Transfers & Adjustments](#11-phase-10-stock-transfers--adjustments)
12. [Phase 11: Reporting & Valuation](#12-phase-11-reporting--valuation)
13. [Phase 12: Authentication & Security](#13-phase-12-authentication--security)
14. [Phase 13: Offline Sync](#14-phase-13-offline-sync)
15. [Phase 14: Concurrent Operations & Race Conditions](#15-phase-14-concurrent-operations--race-conditions)
16. [Bug Investigation Matrix](#16-bug-investigation-matrix)
17. [Completion Criteria](#17-completion-criteria)
18. [Database Verification Queries](#18-database-verification-queries)

---

## 1. Audit Conventions

### Data Naming Convention
All test entities use the prefix `AUDIT_` for easy identification and cleanup:
- Org: `Audit Test Pharmacy {YYYY}`
- Branches: `AUDIT-{CITY}`
- Drugs: `AUDIT-{NAME}`
- Customers: `audit_{name}`
- Users: `audit_{role}_{name}`
- Suppliers: `Audit Supplier {name}`
- POs: `AUDIT-PO-{NNN}`

### Teardown Notes
- Do NOT delete test data between phases — later phases build on earlier ones.
- If a bug prevents proceeding, document it and mark the scenario BLOCKED.
- After audit completion, clean up by soft-deleting all `AUDIT_` prefixed records.

### Variable Storage Convention
Store IDs in environment variables or a shared context file for reuse across scenarios.

### Reporting Convention
Every scenario records:
- **Result**: PASS / FAIL / BLOCKED
- **Bug IDs**: References to bug investigation matrix
- **Notes**: Observations, unexpected behavior, edge cases

---

## 2. Phase 1: Organization & Branch Setup

### 2.1 Create Organization via Onboarding

| Field | Value |
|---|---|
| **Scenario** | Onboard a new pharmacy organization |
| **Preconditions** | Super admin exists (username: `admin`, password: `adminPass!123`) |
| **User Role** | `hemolyc_admin` / super_admin |
| **API Endpoint** | `POST /api/v1/organizations/onboard` |

**Steps:**
1. Login as super_admin → get JWT token
2. POST `/api/v1/organizations/onboard` with:
   ```json
   {
     "name": "Audit Test Pharmacy {YYYY}",
     "type": "pharmacy",
     "license_number": "PHM-AUDIT-{YYYY}-001",
     "phone": "+233501234567",
     "email": "audit{yyyy}@testpharmacy.com",
     "address": {"street": "123 Audit Road", "city": "Accra"},
     "subscription_tier": "professional",
     "currency": "GHS",
     "timezone": "Africa/Accra",
     "admin": {
       "username": "audit_admin_{yyyy}",
       "email": "audit_admin{yyyy}@testpharmacy.com",
       "full_name": "Audit Admin",
       "password": "AuditPass123!",
       "phone": "+233501234568"
     },
     "branches": [
       {
         "name": "Audit Accra Central",
         "code": "AUDIT-ACCRA",
         "phone": "+233509876543",
         "email": "accra@auditpharmacy.com",
         "address": {"street": "45 Independence Ave", "city": "Accra"},
         "operating_hours": {
           "monday": {"open_time": "08:00", "close_time": "20:00"},
           "tuesday": {"open_time": "08:00", "close_time": "20:00"},
           "wednesday": {"open_time": "08:00", "close_time": "20:00"},
           "thursday": {"open_time": "08:00", "close_time": "20:00"},
           "friday": {"open_time": "08:00", "close_time": "20:00"},
           "saturday": {"open_time": "09:00", "close_time": "18:00"},
           "sunday": {"is_closed": true}
         }
       },
       {
         "name": "Audit Kumasi Branch",
         "code": "AUDIT-KUMASI",
         "phone": "+233509876544",
         "email": "kumasi@auditpharmacy.com",
         "address": {"street": "10 Garden Road", "city": "Kumasi"},
         "operating_hours": {
           "monday": {"open_time": "08:00", "close_time": "19:00"},
           "tuesday": {"open_time": "08:00", "close_time": "19:00"},
           "wednesday": {"open_time": "08:00", "close_time": "19:00"},
           "thursday": {"open_time": "08:00", "close_time": "19:00"},
           "friday": {"open_time": "08:00", "close_time": "19:00"},
           "saturday": {"open_time": "09:00", "close_time": "17:00"},
           "sunday": {"is_closed": true}
         }
       }
     ]
   }
   ```

**Expected API Results:**
- HTTP 201 Created
- Response body contains:
  - `organization` with `id`, `name`, `type`, `subscription_tier`, `is_active=true`
  - `admin_user` with `id`, `username`, `password_change_required=true`
  - `branches` array with exactly 2 entries, each with `id`, `name`, `code`

**Expected Database Changes:**
| Table | Change |
|---|---|
| `organizations` | 1 row: name=`Audit Test Pharmacy {YYYY}`, type=`pharmacy`, subscription_tier=`professional`, is_active=true, settings contains currency/timezone |
| `users` | 1 row: username=`audit_admin_{yyyy}`, is_super_admin=false, must_change_password=true, assigned_branches=[both branch IDs] |
| `branches` | 2 rows: codes `AUDIT-ACCRA` and `AUDIT-KUMASI`, organization_id set, operating_hours stored as JSONB |
| `roles` | 4 rows: Admin(level=100, 17 permissions), Manager(50), Pharmacist(30), Cashier(10) |
| `user_roles` | 1 row: admin user → Admin role |
| `audit_logs` | 1 row: action=`organization_created`, entity_type=`organization` |

**Expected UI Results:**
- Organization appears in organization list
- Admin user can login
- Both branches visible in branch management

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Branch codes auto-generated instead of using provided code | Medium |
| Operating hours silently discarded | Medium |
| Admin must_change_password=False | High |
| Response missing expected fields (id, organization, etc.) | High |
| Duplicate name allowed | Medium |
| Duplicate license_number allowed | Medium |
| Rollback on failure — partial data should not persist | Critical |

**Bug Investigation:**
- Check that branch codes match input exactly (case-sensitive)
- Verify operating_hours stored as proper JSONB, not truncated
- Verify admin_user has `user_roles` entry
- Verify `onboarding_idempotency_key` prevents duplicate submission

---

### 2.2 Create Additional Branch (Post-Onboarding)

| Field | Value |
|---|---|
| **Scenario** | Create a new branch after org creation |
| **Preconditions** | Organization exists with ID from 2.1 |
| **User Role** | Organization admin (`audit_admin_{yyyy}`) |

**Steps:**
1. Login as `audit_admin_{yyyy}`
2. If `must_change_password=true`, first call `POST /api/v1/auth/force-change-password`
3. POST `/api/v1/branches`:
   ```json
   {
     "name": "Audit Cape Coast Branch",
     "code": "AUDIT-CAPECOAST",
     "organization_id": "{org_id}",
     "phone": "+233509876545",
     "email": "capecoast@auditpharmacy.com",
     "address": {"street": "22 Beach Road", "city": "Cape Coast"},
     "operating_hours": {
       "monday": {"open_time": "08:00", "close_time": "18:00"},
       "saturday": {"open_time": "09:00", "close_time": "15:00"},
       "sunday": {"is_closed": true}
     }
   }
   ```

**Expected API Results:**
- HTTP 201 Created
- Branch object with `id`, `name`, `code=AUDIT-CAPECOAST`

**Expected Database Changes:**
| Table | Change |
|---|---|
| `branches` | 1 new row |
| `users` | Admin's `assigned_branches` updated to include new branch ID |

**Expected Audit Records:**
- 1 audit entry: action=`branch_created`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate code within org accepted | High |
| Branch created but admin not auto-assigned | Medium |
| Missing org_id validation (cross-org creation) | High |
| API raises 500 instead of validation error | Medium |

---

## 3. Phase 2: User & Role Management

### 3.1 List Default Roles

| Field | Value |
|---|---|
| **Scenario** | Verify the 4 default roles created during onboarding |
| **Preconditions** | Org exists with roles |
| **User Role** | Organization admin |
| **API Endpoint** | `GET /api/v1/roles/` |

**Steps:**
1. Login as `audit_admin_{yyyy}`
2. GET `/api/v1/roles/`

**Expected API Results:**
- HTTP 200
- Array of exactly 4 roles:
  - `Admin` (level=100) with all 17 permissions
  - `Manager` (level=50)
  - `Pharmacist` (level=30)
  - `Cashier` (level=10)

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Missing default roles | High |
| Wrong permission set per role | Medium |
| Role level hierarchy incorrect (e.g., Admin < Manager) | High |

---

### 3.2 Create Custom Role

| Field | Value |
|---|---|
| **Scenario** | Create a custom role with specific permissions |
| **Preconditions** | Org admin authenticated |
| **User Role** | Organization admin (requires `manage_organization` permission) |

**Steps:**
1. POST `/api/v1/roles/`:
   ```json
   {
     "name": "AUDIT-Inventory Manager",
     "description": "Manages inventory only",
     "permissions": ["view_inventory", "manage_inventory", "view_reports"],
     "level": 45
   }
   ```

**Expected API Results:**
- HTTP 201 Created
- Role object with `id`, `name`, `level=45`, `permissions` matching input

**Expected Database Changes:**
| Table | Change |
|---|---|
| `roles` | 1 new row, org-scoped, with specified permissions and level |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate role name allowed | Medium |
| Level out of range accepted | Low |
| Invalid permission string accepted | Medium |
| Cross-org role creation possible | High |

---

### 3.3 Update Role

| Field | Value |
|---|---|
| **Scenario** | Modify an existing role |
| **Preconditions** | Role exists from 3.2 |
| **User Role** | Organization admin |

**Steps:**
1. PUT `/api/v1/roles/{role_id}`:
   ```json
   {
     "name": "AUDIT-Senior Inventory Manager",
     "description": "Manages inventory and can export data",
     "permissions": ["view_inventory", "manage_inventory", "view_reports", "export_data"],
     "level": 55
   }
   ```

**Expected API Results:**
- HTTP 200
- Updated role object with new name, level, permissions

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Update applies to wrong org's role (IDOR) | Critical |
| Role level conflict with existing hierarchy not caught | Medium |

---

### 3.4 Create User with Role Assignment

| Field | Value |
|---|---|
| **Scenario** | Create a user and assign a role |
| **Preconditions** | Org admin authenticated, Pharmacist role ID known |
| **User Role** | Organization admin (requires `manage_users` permission) |

**Steps:**
1. POST `/api/v1/users`:
   ```json
   {
     "username": "audit_pharmacist_01",
     "email": "pharmacist01@auditpharmacy.com",
     "full_name": "Audit Pharmacist One",
     "password": "PharmPass123!",
     "phone": "+233501234571",
     "assigned_branches": ["{AUDIT-ACCRA_ID}"],
     "role_ids": ["{PHARMACIST_ROLE_ID}"]
   }
   ```

**Expected API Results:**
- HTTP 201 Created
- User object with `username`, `must_change_password=true`, `roles` array containing Pharmacist

**Expected Database Changes:**
| Table | Change |
|---|---|
| `users` | 1 new row, must_change_password=true, assigned_branches set |
| `user_roles` | 1 row linking user to Pharmacist role |
| `audit_logs` | 1 entry: action=`user_created` |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| must_change_password=false for new user | High |
| Role not assigned (greenlet error) | High |
| Duplicate username silently overwrites | Critical |
| User created in wrong org | Critical |
| Password strength not enforced | Medium |

---

### 3.5 Create Additional Users for Later Tests

Create these users for use in downstream workflows:

| Username | Role | Branches | Purpose |
|---|---|---|---|
| `audit_cashier_01` | Cashier | AUDIT-ACCRA | Process sales |
| `audit_manager_01` | Manager | All branches | Approve POs, manage inventory, view reports |
| `audit_viewer_01` | (no role) | AUDIT-ACCRA | Test permission denial |

**Verify:**
- Each user can login
- Each user's permissions match expected role permissions
- `must_change_password=true` for all new users

---

### 3.6 User Update (Branch Assignment)

| Field | Value |
|---|---|
| **Scenario** | Update a user's branch assignments |
| **Preconditions** | User exists with limited branch access |
| **User Role** | Organization admin |

**Steps:**
1. PATCH `/api/v1/users/{user_id}`:
   ```json
   {
     "assigned_branches": ["{AUDIT-ACCRA_ID}", "{AUDIT-KUMASI_ID}"]
   }
   ```

**Expected API Results:**
- HTTP 200
- User object with updated `assigned_branches`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| User can be assigned to branches in different org | Critical |
| Update without MANAGE_USERS permission succeeds | High |
| assigned_branches set to empty removes all access (should still allow) | Medium |

---

## 4. Phase 3: Drug Catalog & Inventory

### 4.1 Create Drug Category

| Field | Value |
|---|---|
| **Scenario** | Create a drug category for catalog organization |
| **Preconditions** | Org admin authenticated |
| **User Role** | Org admin / Pharmacist (requires `manage_drugs`) |

**Steps:**
1. POST `/api/v1/drug-categories`:
   ```json
   {
     "name": "AUDIT Antihypertensives",
     "description": "Blood pressure medications"
   }
   ```

**Expected API Results:**
- HTTP 201
- Category object with `id`, `name`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate category name allowed per org | Low |
| Cross-org category creation possible | Medium |

---

### 4.2 Create Drugs

| Field | Value |
|---|---|
| **Scenario** | Create drugs across different types for inventory testing |
| **Preconditions** | Category exists from 4.1 |
| **User Role** | Org admin / Pharmacist |

**Steps:**
1. POST `/api/v1/drugs` (repeat for each drug below):
   ```json
   {
     "name": "AUDIT Amlodipine 10mg",
     "generic_name": "Amlodipine Besylate",
     "sku": "AUD-AML-10",
     "barcode": "200000000001",
     "category_id": "{category_id}",
     "unit_price": 15.00,
     "cost_price": 8.00,
     "markup_percentage": 87.5,
     "tax_rate": 3.0,
     "drug_type": "prescription",
     "requires_prescription": true,
     "reorder_level": 20,
     "is_active": true
   }
   ```

**Test Drug Matrix (create all):**

| Drug Name | SKU | Barcode | Type | Requires Rx | Unit Price | Cost Price | Reorder |
|---|---|---|---|---|---|---|---|
| AUDIT Amlodipine 10mg | AUD-AML-10 | 200000000001 | prescription | yes | 15.00 | 8.00 | 20 |
| AUDIT Paracetamol 500mg | AUD-PCM-500 | 200000000002 | otc | no | 5.00 | 2.00 | 50 |
| AUDIT Amoxicillin 250mg | AUD-AMX-250 | 200000000003 | prescription | yes | 12.00 | 6.00 | 30 |
| AUDIT Metformin 500mg | AUD-MET-500 | 200000000004 | prescription | yes | 8.00 | 4.00 | 40 |
| AUDIT Vitamin C 1000mg | AUD-VITC-1 | 200000000005 | otc | no | 10.00 | 5.00 | 25 |
| AUDIT Ibuprofen 400mg | AUD-IBU-400 | 200000000006 | otc | no | 6.00 | 2.50 | 30 |
| AUDIT Controlled Substance X | AUD-CS-X01 | 200000000007 | controlled | yes | 25.00 | 15.00 | 10 |

**Expected API Results:**
- HTTP 201 for each
- Drug object with `id`, `sku`, `barcode`, markup auto-calculated

**Expected Database Changes:**
| Table | Change |
|---|---|
| `drugs` | 7 new rows, each with search_vector populated |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate SKU accepted (even with soft-deleted) | High |
| Duplicate barcode accepted | High |
| markup_percentage not auto-calculated when omitted | Medium |
| search_vector not populated (full-text search broken) | Medium |
| Negative price accepted (Pydantic should block) | Medium |

**Bug Investigation:**
- Verify `search_vector` is populated with trigram-compatible content
- Test SKU/barcode uniqueness with soft-deleted drugs (should allow reuse? or block?)
- Verify `drug_type='controlled'` has special handling

---

### 4.3 Drug Search

| Field | Value |
|---|---|
| **Scenario** | Search drugs by SKU, barcode, name |
| **Preconditions** | Drugs exist from 4.2 |
| **User Role** | Any authenticated user |

**Steps:**
1. GET `/api/v1/drugs/search?q=AUD-AML-10`
2. GET `/api/v1/drugs/search?q=200000000001`
3. GET `/api/v1/drugs/search?q=Amlodipine`
4. GET `/api/v1/drugs/search?q=blood+pressure` (trigram search)

**Expected API Results:**
- HTTP 200
- Matching drugs returned with relevance order

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Search returns no results for existing drugs | High |
| Cross-org search returns other org's drugs | Critical |
| Trigram search broken (no results for partial matches) | Medium |

---

### 4.4 Drug Update (Pricing)

| Field | Value |
|---|---|
| **Scenario** | Update drug pricing and verify recalculation |
| **Preconditions** | Drug exists |
| **User Role** | Org admin / Pharmacist |

**Steps:**
1. PATCH `/api/v1/drugs/{drug_id}`:
   ```json
   {
     "unit_price": 18.00,
     "cost_price": 9.00,
     "markup_percentage": 100.0
   }
   ```

**Expected API Results:**
- HTTP 200
- Updated drug with new pricing

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Markup not matching (unit_price - cost_price) / cost_price * 100 | Medium |
| Update without MANAGE_DRUGS permission | High |

---

## 5. Phase 4: Purchase Orders & Stock Receiving

### 5.1 Create Supplier

| Field | Value |
|---|---|
| **Scenario** | Create a supplier for purchase orders |
| **Preconditions** | Org admin authenticated |
| **User Role** | Org admin / Pharmacist (requires `manage_suppliers`) |

**Steps:**
1. POST `/api/v1/suppliers/`:
   ```json
   {
     "name": "Audit Supplier PharmaCo",
     "code": "AUDIT-SUP-01",
     "contact_person": "John Supplier",
     "phone": "+233501234580",
     "email": "supplier@auditpharma.com",
     "payment_terms": "net30",
     "is_active": true
   }
   ```

**Expected API Results:**
- HTTP 201
- Supplier object with `id`

---

### 5.2 Create Purchase Order

| Field | Value |
|---|---|
| **Scenario** | Create a purchase order in draft status |
| **Preconditions** | Supplier exists, drugs exist |
| **User Role** | Pharmacist / Manager (requires `manage_inventory`) |

**Steps:**
1. POST `/api/v1/purchase-orders/`:
   ```json
   {
     "supplier_id": "{supplier_id}",
     "branch_id": "{AUDIT-ACCRA_ID}",
     "items": [
       {"drug_id": "{AUDIT-AML-10_ID}", "quantity_ordered": 100, "unit_cost": 7.50},
       {"drug_id": "{AUDIT-PCM-500_ID}", "quantity_ordered": 200, "unit_cost": 1.80},
       {"drug_id": "{AUDIT-AMX-250_ID}", "quantity_ordered": 150, "unit_cost": 5.50},
       {"drug_id": "{AUDIT-MET-500_ID}", "quantity_ordered": 120, "unit_cost": 3.60}
     ]
   }
   ```

**Expected API Results:**
- HTTP 201
- PO object with status=`draft`, `po_number` auto-generated, items array

**Expected Database Changes:**
| Table | Change |
|---|---|
| `purchase_orders` | 1 row, status=`draft` |
| `purchase_order_items` | 4 rows, each with `quantity_received=0` |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| PO created without items (should be blocked) | Medium |
| Duplicate po_number allowed | Medium |
| Drug from different org accepted | Critical |

---

### 5.3 Submit Purchase Order

| Field | Value |
|---|---|
| **Scenario** | Submit draft PO for approval |
| **Preconditions** | PO in `draft` status |
| **User Role** | Pharmacist / Manager |

**Steps:**
1. POST `/api/v1/purchase-orders/{po_id}/submit`

**Expected API Results:**
- HTTP 200
- PO status=`pending` (or `submitted`)

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Submit without items allowed | Medium |
| Submit already submitted PO allowed | Medium |
| Non-draft PO submit changes status unexpectedly | Medium |

---

### 5.4 Approve Purchase Order

| Field | Value |
|---|---|
| **Scenario** | Approve submitted PO |
| **Preconditions** | PO in `pending` status |
| **User Role** | Manager (requires `approve_purchase_orders`) |

**Steps:**
1. POST `/api/v1/purchase-orders/{po_id}/approve`

**Expected API Results:**
- HTTP 200
- PO status=`approved`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Approve without permission returns 200 | Critical |
| Approve draft (not submitted) PO allowed | Medium |
| Cashier can approve PO | Critical |

---

### 5.5 Receive Goods (Full Receipt)

| Field | Value |
|---|---|
| **Scenario** | Receive all items from approved PO |
| **Preconditions** | PO in `approved` status |
| **User Role** | Pharmacist / Manager |

**Steps:**
1. POST `/api/v1/purchase-orders/{po_id}/receive`:
   ```json
   {
     "items": [
       {"purchase_order_item_id": "{item1_id}", "quantity_received": 100, "batch_number": "AUDIT-BATCH-AML-001", "expiry_date": "2027-06-01"},
       {"purchase_order_item_id": "{item2_id}", "quantity_received": 200, "batch_number": "AUDIT-BATCH-PCM-001", "expiry_date": "2027-12-01"},
       {"purchase_order_item_id": "{item3_id}", "quantity_received": 150, "batch_number": "AUDIT-BATCH-AMX-001", "expiry_date": "2026-12-01"},
       {"purchase_order_item_id": "{item4_id}", "quantity_received": 120, "batch_number": "AUDIT-BATCH-MET-001", "expiry_date": "2028-03-01"}
     ]
   }
   ```

**Expected API Results:**
- HTTP 200
- PO status=`received`
- Each item `quantity_received` updated

**Expected Database Changes:**
| Table | Change | Count |
|---|---|---|
| `purchase_orders` | status=`received` | 1 row |
| `purchase_order_items` | quantity_received set | 4 rows |
| `drug_batches` | INSERT new batch per item | 4 rows |
| `branch_inventory` | UPSERT quantity increased | 4 rows |
| `stock_adjustments` | type=`purchase_receipt` | 4 rows |
| `inventory_movements` | movement_type=`purchase_receipt` | 4 rows |
| `drugs` | cost_price updated (weighted avg) | 4 rows |
| `audit_logs` | action=goods_received | 1 row |

**Expected Inventory State (AUDIT-ACCRA):**

| Drug | Quantity | Batches |
|---|---|---|
| AUDIT Amlodipine 10mg | 100 | AUDIT-BATCH-AML-001 (100, exp 2027-06) |
| AUDIT Paracetamol 500mg | 200 | AUDIT-BATCH-PCM-001 (200, exp 2027-12) |
| AUDIT Amoxicillin 250mg | 150 | AUDIT-BATCH-AMX-001 (150, exp 2026-12) |
| AUDIT Metformin 500mg | 120 | AUDIT-BATCH-MET-001 (120, exp 2028-03) |

**Expected Audit Records:**
- 1 entry: `goods_received` for PO
- 4 entries: `inventory_movement` for each drug

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Over-receipt allowed (qty > ordered) | High |
| Partial receipt marks PO as received (not partially_received) | Medium |
| No batch created (inventory increases without batch tracking) | Critical |
| Cost price not updated as weighted average | Medium |
| Receive for unapproved PO allowed | High |

**Bug Investigation:**
- Verify `cost_price` weighted average calculation: `((old_cost * old_qty) + (new_cost * received_qty)) / (old_qty + received_qty)`
- Verify duplicate batch_number is allowed (with warning)
- Check `FOR UPDATE` lock is acquired on `branch_inventory` row

---

### 5.6 Partial Receipt Edge Case

| Field | Value |
|---|---|
| **Scenario** | Receive partial quantity, then receive remainder |
| **Preconditions** | Second PO exists in approved status |
| **User Role** | Pharmacist / Manager |

**Steps:**
1. Create a PO for 50 units of AUDIT-VITC-1, submit, approve
2. Receive 30 units (batch `AUDIT-BATCH-VITC-001`, exp 2027-06)
3. PO should remain in `ordered`/`partially_received` status
4. Receive remaining 20 units (same batch, same expiry)
5. PO should transition to `received` status
6. Inventory should total 50

**Expected Behavior:**
- `purchase_order_items.quantity_received` increments on partial receive
- PO stays open until all items fully received
- Second receipt of same batch appends to existing batch (increases `remaining_quantity`)

**Failure Conditions:**
| Condition | Severity |
|---|---|
| PO closes after first partial receipt | High |
| Second partial receipt creates duplicate batch instead of appending | Medium |

---

## 6. Phase 5: Sales & POS

### 6.1 Walk-in Sale (Cash) — Smoke Test

| Field | Value |
|---|---|
| **Scenario** | Basic walk-in sale with cash payment |
| **Preconditions** | AUDIT-ACCRA has stock from PO receipt (5.5). Cashier user exists. |
| **User Role** | Cashier (`audit_cashier_01` — requires `process_sales`) |

**Steps:**
1. Login as `audit_cashier_01` (force password change first if needed)
2. POST `/api/v1/sales/`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "items": [
       {"drug_id": "{AUDIT-PCM-500_ID}", "quantity": 10, "unit_price": 5.00},
       {"drug_id": "{AUDIT-IBU-400_ID}", "quantity": 5, "unit_price": 6.00}
     ],
     "payment_method": "cash",
     "amount_paid": 100.00
   }
   ```

**Expected API Results:**
- HTTP 201
- Sale object with `id`, `sale_number`, `status=completed`
- `subtotal`: 80.00 = (10×5.00) + (5×6.00)
- `tax_amount`: calculated at each drug's tax_rate
- `total_amount`: subtotal + tax - discount
- `inventory_stats` showing deduction summary

**Expected Database Changes:**
| Table | Change |
|---|---|
| `sales` | 1 row, status=`completed`, cash payment |
| `sale_items` | 2 rows with correct prices |
| `sale_item_batch_allocations` | 2 rows (one per batch consumed) |
| `drug_batches` | PCM batch: 200→190, IBU batch: remaining created |
| `branch_inventory` | Paracetamol: 200→190, Ibuprofen: 30→25 |
| `inventory_movements` | 2 rows, movement_type=`sale` |
| `audit_logs` | 1 entry: action=`sale_completed` |

**Expected Inventory Deduction (FEFO):**
- PCM-500: oldest batch consumed first (should be AUDIT-BATCH-PCM-001, 200→190)
- IBU-400: created batch should be consumed

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Stock not deducted | Critical |
| Wrong batch consumed (not FEFO) | High |
| Tax calculation wrong | High |
| Sale number not generated | Medium |
| Total_amount != subtotal - discount + tax | Critical |
| Inventory goes negative | Critical |
| Cashier without permission can process sale | Critical |

**Bug Investigation:**
- Verify FEFO order: batches sorted by `expiry_date ASC`
- Verify `sale_item_batch_allocations` matches inventory deduction exactly
- Verify no customer record modified (walk-in = no customer_id)
- Check `loyalty_info` is null for walk-in

---

### 6.2 Customer Sale with Loyalty

| Field | Value |
|---|---|
| **Scenario** | Sale for registered customer that earns loyalty points |
| **Preconditions** | Customer exists (created in Phase 8), stock available |
| **User Role** | Cashier |

**Steps:**
1. POST `/api/v1/sales/`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "customer_id": "{customer_id}",
     "items": [
       {"drug_id": "{AUDIT-AML-10_ID}", "quantity": 5, "unit_price": 15.00},
       {"drug_id": "{AUDIT-MET-500_ID}", "quantity": 10, "unit_price": 8.00}
     ],
     "payment_method": "mobile_money",
     "amount_paid": 200.00
   }
   ```

**Expected API Results:**
- `loyalty_info.points_awarded > 0`
- `loyalty_info.tier` updated
- Customer `total_orders` incremented
- Customer `total_value` incremented by `total_amount`

**Expected Database Changes:**
| Table | Change |
|---|---|
| `customers` | loyalty_points increased, total_orders+1, total_value increased |
| `sales` | customer_id set |
| `drug_batches` | AML: 100→95, MET: 120→110 |
| `branch_inventory` | Both decremented |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Loyalty points not awarded | Medium |
| Customer total_orders / total_value not updated | Medium |
| Customer from different org can be used in sale | Critical |

---

### 6.3 Insufficient Stock Rejection

| Field | Value |
|---|---|
| **Scenario** | Attempt sale exceeding available stock |
| **Preconditions** | Drug has limited stock (e.g., AUDIT-AML-10 has 95 remaining) |
| **User Role** | Cashier |

**Steps:**
1. POST `/api/v1/sales/` with quantity=200 for AUDIT-AML-10

**Expected API Results:**
- HTTP 400
- Error message indicating insufficient stock

**Expected Database Changes:**
- No changes — entire transaction rolled back

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Partial inventory deduction on failure | Critical |
| Sale created with quantity > available stock | Critical |
| Error message unclear or missing stock quantity | Low |

---

### 6.4 Sale with Multiple Payment Methods

| Field | Value |
|---|---|
| **Scenario** | Split payment across cash + mobile money |
| **Preconditions** | Stock available |
| **User Role** | Cashier |

**Steps:**
1. POST `/api/v1/sales/`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "items": [{"drug_id": "{AUDIT-VITC-1_ID}", "quantity": 5, "unit_price": 10.00}],
     "payment_method": "split",
     "split_payments": [
       {"method": "cash", "amount": 25.00},
       {"method": "mobile_money", "amount": 27.50}
     ]
   }
   ```

**Expected API Results:**
- HTTP 201
- Payment status reflects split payment details

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Split payment not supported | High |
| Split amounts != total (should validate) | Medium |
| Payment method not recorded per split | Low |

---

### 6.5 Prescription-Only Drug Blocked Without Rx

| Field | Value |
|---|---|
| **Scenario** | Attempt sale of prescription-only drug without linking a prescription |
| **Preconditions** | AUDIT-AML-10 requires_prescription=true |
| **User Role** | Cashier |

**Steps:**
1. POST `/api/v1/sales/` with AUDIT-AML-10, no `prescription_id`

**Expected API Results:**
- HTTP 400
- Error: prescription required for this drug

**Expected Database Changes:**
- None — transaction rolled back

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Prescription-only drug sold without Rx | Critical |
| Error mentions all-or-nothing (should identify which drug) | Medium |

---

### 6.6 Sale with Price Contract Discount

(Detailed in Phase 9: Pricing Contracts)

---

## 7. Phase 6: Refunds

### 7.1 Full Refund — Complete Sale Reversal

| Field | Value |
|---|---|
| **Scenario** | Full refund of a completed cash sale |
| **Preconditions** | Completed sale exists (use sale from 6.1). Customer is walk-in (no loyalty). |
| **User Role** | Manager / Pharmacist (requires `process_refunds`) |

**Steps:**
1. GET `/api/v1/sales/{sale_id}` to verify original state
2. POST `/api/v1/sales/{sale_id}/refund`:
   ```json
   {
     "reason": "AUDIT TEST — Full refund verification",
     "items": [
       {"sale_item_id": "{item1_id}", "quantity": 10},
       {"sale_item_id": "{item2_id}", "quantity": 5}
     ]
   }
   ```

**Expected API Results:**
- HTTP 200
- Sale status=`refunded`
- `refund_amount` equal to original `total_amount`
- Inventory restoration details included

**Expected Database Changes:**
| Table | Change |
|---|---|
| `sales` | status=`refunded`, refund_amount=total, refund_reason set |
| `sale_items` | refunded_quantity set to original quantity for both items |
| `sale_item_batch_allocations` | refunded_quantity set on each allocation |
| `drug_batches` | PCM batch: 190→200 (restored), IBU batch: remaining increased |
| `branch_inventory` | Paracetamol: 190→200, Ibuprofen: 25→30 |
| `stock_adjustments` | 1+ rows type=`correction` |
| `inventory_movements` | 2+ rows movement_type=`refund` |
| `audit_logs` | 1 entry: action=`sale_refunded` |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Inventory not restored | Critical |
| Wrong batch restored (different cost/price from original) | High |
| Inventory restored to original batch OR new batch inconsistently | High |
| Duplicate refund on same item allowed | Critical |
| Refund without PROCESS_REFUNDS permission allowed | Critical |
| No audit log created for refund | Medium |
| Drug batch prices at refund time used instead of sale-time prices | High |

**Bug Investigation:**
- Verify `sale_item_batch_allocations.refunded_quantity` tracking prevents double-refund
- Check if original batch still exists: verify restoration goes to same batch
- If original batch fully consumed (remaining=0): verify new batch created with original prices from allocation record
- Verify `unit_cost_at_sale` and `unit_price_at_sale` from allocation record are used

---

### 7.2 Partial Refund

| Field | Value |
|---|---|
| **Scenario** | Partial refund of a single line item |
| **Preconditions** | Completed sale with multiple units of one item |
| **User Role** | Manager / Pharmacist |

**Steps:**
1. Create a sale of 20 units of AUDIT-PCM-500 (or use an existing sale with > 1 unit)
2. POST `/api/v1/sales/{sale_id}/refund`:
   ```json
   {
     "reason": "AUDIT TEST — Partial refund",
     "items": [{"sale_item_id": "{item_id}", "quantity": 5}]
   }
   ```

**Expected API Results:**
- HTTP 200
- Sale status=`partially_refunded`
- `refund_amount` = proportional (5/20 of original item price + tax)

**Expected Database Changes:**
| Table | Change |
|---|---|
| `sales` | status=`partially_refunded`, refund_amount updated |
| `sale_items` | refunded_quantity=5 for refunded item |
| `branch_inventory` | quantity increased by 5 |
| `drug_batches` | remaining_quantity increased by 5 |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Sale status changes to `refunded` instead of `partially_refunded` | High |
| Refund amount incorrectly calculated | Medium |
| Cannot partial-refund (only full refund) | High |

---

### 7.3 Refund With Loyalty Deduction

| Field | Value |
|---|---|
| **Scenario** | Refund a sale that earned loyalty points — verify points deducted |
| **Preconditions** | Customer sale exists (from 6.2) with awarded loyalty points |
| **User Role** | Manager / Pharmacist |

**Steps:**
1. Record customer's current `loyalty_points` before refund
2. Refund the sale from 6.2
3. Check customer's loyalty points decreased

**Expected Database Changes:**
| Table | Change |
|---|---|
| `customers` | loyalty_points decreased by amount awarded during that sale |
| `customers` | loyalty_tier may change if points drop below threshold |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Loyalty points not deducted on refund | Medium |
| Loyalty points deducted but inventory not restored (partial failure) | Critical |
| Loyalty tier not recalculated after deduction | Low |

---

### 7.4 Refund With Prescription Refill Restoration

(Detailed in Phase 7: Prescriptions — 7.5)

---

### 7.5 Duplicate Refund Prevention

| Field | Value |
|---|---|
| **Scenario** | Attempt to refund an already-fully-refunded item |
| **Preconditions** | Item from 7.1 is already fully refunded |
| **User Role** | Manager / Pharmacist |

**Steps:**
1. POST `/api/v1/sales/{sale_id}/refund` with the same items as 7.1

**Expected API Results:**
- HTTP 400
- Error: item already fully refunded

**Expected Database Changes:**
- None

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Double refund allowed (inventory double-counted) | Critical |
| Partial refund on fully-refunded item allowed | Critical |

---

## 8. Phase 7: Prescriptions

### 8.1 Create Prescription

| Field | Value |
|---|---|
| **Scenario** | Create a valid prescription for a customer |
| **Preconditions** | Customer exists, prescriber information available |
| **User Role** | Pharmacist (requires `manage_prescriptions`) |

**Steps:**
1. POST `/api/v1/prescriptions/`:
   ```json
   {
     "customer_id": "{customer_id}",
     "prescriber_name": "Dr. Audit Physician",
     "prescriber_license": "AUDIT-MDC-001",
     "prescriber_phone": "+233501234590",
     "medications": [
       {"drug_id": "{AUDIT-AML-10_ID}", "quantity": 30, "dosage": "10mg daily"},
       {"drug_id": "{AUDIT-AMX-250_ID}", "quantity": 21, "dosage": "250mg three times daily"}
     ],
     "issue_date": "{today}",
     "expiry_date": "{today+30}",
     "refills_allowed": 2,
     "notes": "AUDIT TEST prescription"
   }
   ```

**Expected API Results:**
- HTTP 201
- Prescription object with `id`, `prescription_number`, `status=active`, `refills_remaining=2`

**Expected Database Changes:**
| Table | Change |
|---|---|
| `prescriptions` | 1 row, status=`active`, refills_remaining=2, medications JSONB |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Prescription created for non-existent customer | Medium |
| expiry_date before issue_date accepted | Medium |
| refills_allowed without refills_remaining = refills_allowed | Medium |
| medications stored as raw string instead of JSONB | High |
| Prescription number not auto-generated | Medium |

---

### 8.2 Fill Prescription via Sale

| Field | Value |
|---|---|
| **Scenario** | Fill a prescription as part of a sale |
| **Preconditions** | Active prescription exists from 8.1, stock available |
| **User Role** | Cashier / Pharmacist |

**Steps:**
1. POST `/api/v1/sales/`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "prescription_id": "{prescription_id}",
     "customer_id": "{customer_id}",
     "items": [
       {"drug_id": "{AUDIT-AML-10_ID}", "quantity": 5, "unit_price": 15.00},
       {"drug_id": "{AUDIT-AMX-250_ID}", "quantity": 7, "unit_price": 12.00}
     ],
     "payment_method": "cash",
     "amount_paid": 200.00
   }
   ```

**Expected API Results:**
- HTTP 201
- Sale linked to prescription

**Expected Database Changes:**
| Table | Change |
|---|---|
| `prescriptions` | refills_remaining: 2→1 |
| `sales` | prescription_id set |
| `sale_items` | prescription_item_flag=true for Rx drugs |
| `drug_batches` | AML: 95→90, AMX: 150→143 |
| `branch_inventory` | Both decremented |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Refills not decremented after sale | High |
| Refills decremented but sale fails (rollback) | Critical |
| Non-prescription drug can be linked to prescription | Low |
| Prescription from different customer can be used | Critical |

---

### 8.3 Prescription Refill (API)

| Field | Value |
|---|---|
| **Scenario** | Use the refill API to decrement refills |
| **Preconditions** | Prescription exists with refills_remaining > 0 |
| **User Role** | Pharmacist |

**Steps:**
1. POST `/api/v1/prescriptions/{prescription_id}/refill`

**Expected API Results:**
- HTTP 200
- Prescription refills_remaining decremented by 1

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Refill with no refills remaining succeeds | High |
| Refill on expired prescription succeeds | High |
| Refill on cancelled prescription succeeds | High |

---

### 8.4 Expired Prescription Blocked At Sale

| Field | Value |
|---|---|
| **Scenario** | Attempt sale with expired prescription |
| **Preconditions** | Prescription with expiry_date in the past |
| **User Role** | Cashier |

**Steps:**
1. Create a prescription with issue_date=30 days ago and expiry_date=yesterday
2. POST `/api/v1/sales/` using that prescription

**Expected API Results:**
- HTTP 400
- Error: prescription has expired

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Sale proceeds with expired prescription | Critical |
| Expired prescription status not checked | High |

---

### 8.5 Refill Exhausted Blocked At Sale

| Field | Value |
|---|---|
| **Scenario** | Attempt sale when prescription has no refills remaining |
| **Preconditions** | Prescription with refills_remaining=0 |
| **User Role** | Cashier |

**Steps:**
1. Use the prescription from 8.2 (which now has refills_remaining=1 after first fill)
2. Fill it via sale → refills_remaining goes to 0
3. Attempt another fill

**Expected API Results:**
- Third attempt: HTTP 400
- Error: no refills remaining

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Sale proceeds with 0 refills remaining | Critical |
| refills_remaining goes negative | Critical |

---

### 8.6 Cancel Prescription

| Field | Value |
|---|---|
| **Scenario** | Cancel an active prescription |
| **Preconditions** | Active prescription exists |
| **User Role** | Pharmacist |

**Steps:**
1. PATCH `/api/v1/prescriptions/{prescription_id}/cancel`

**Expected API Results:**
- HTTP 200
- Prescription status=`cancelled`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Cancelled prescription can be used in sale | Critical |
| Already-cancelled prescription can be cancelled again (no-op) | Low |

---

### 8.7 Soft-Delete Prescription

| Field | Value |
|---|---|
| **Scenario** | Soft-delete a prescription |
| **Preconditions** | Prescription exists (not used in any sale) |
| **User Role** | Pharmacist |

**Steps:**
1. DELETE `/api/v1/prescriptions/{prescription_id}`

**Expected API Results:**
- HTTP 204
- Prescription.is_deleted=true

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Hard delete instead of soft delete | High |
| Prescription used in sale can be deleted (FK integrity) | High |

---

### 8.8 Refund Restores Refills

| Field | Value |
|---|---|
| **Scenario** | Refunding a prescription sale restores refills |
| **Preconditions** | Sale exists from 8.2 (refills decremented from 2→1) |
| **User Role** | Manager / Pharmacist |

**Steps:**
1. Refund the sale from 8.2
2. Check prescription refills_remaining restored to 2

**Expected Database Changes:**
| Table | Change |
|---|---|
| `prescriptions` | refills_remaining: 1→2 |
| `sales` | status=`refunded` |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Refills not restored on refund | High |
| Refills restored even if prescription expired since sale | High (risk: expired prescription now usable) |

---

## 9. Phase 8: Customer & Loyalty

### 9.1 Register Customer

| Field | Value |
|---|---|
| **Scenario** | Register a new customer |
| **Preconditions** | Org admin / Pharmacist authenticated |
| **User Role** | Pharmacist (requires `manage_customers`) |

**Steps:**
1. POST `/api/v1/customers/`:
   ```json
   {
     "first_name": "Audit",
     "last_name": "Patient",
     "phone": "+233501234591",
     "email": "patient@audit.com",
     "date_of_birth": "1990-01-15",
     "customer_type": "registered",
     "loyalty_tier": "bronze"
   }
   ```

**Expected API Results:**
- HTTP 201
- Customer object with `id`, `loyalty_points=0`, `loyalty_tier=bronze`, `is_active=true`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate phone allowed | Medium |
| Duplicate email allowed | Medium |
| Walk-in type can register (should be limited) | Low |
| Customer created in wrong org | Critical |

---

### 9.2 Register Additional Customers

Create additional customers for diversity testing:

| First | Last | Phone | Email | Type |
|---|---|---|---|---|
| Audit | Senior | +233501234592 | senior@audit.com | registered (senior_citizen=true) |
| Audit | Insurance | +233501234593 | insured@audit.com | registered (insurance) |
| Audit | Walkin | +233501234594 | walkin@audit.com | walk_in |

---

### 9.3 Customer Search

| Field | Value |
|---|---|
| **Scenario** | Search customers by phone, name, email |
| **Preconditions** | Customers exist |
| **User Role** | Any authenticated user |

**Steps:**
1. GET `/api/v1/customers/search?q=+233501234591`
2. GET `/api/v1/customers/search?q=Audit`
3. GET `/api/v1/customers/search?q=patient@audit.com`

**Expected API Results:**
- Matching customers returned with insurance provider, preferred contract, senior_citizen flag

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Search returns no results for exact phone match | High |
| Cross-org search leaks customers | Critical |

---

### 9.4 Loyalty Points Award (Manual)

| Field | Value |
|---|---|
| **Scenario** | Manually award loyalty points |
| **Preconditions** | Registered customer exists |
| **User Role** | Pharmacist |

**Steps:**
1. POST `/api/v1/customers/{customer_id}/loyalty/award`:
   ```json
   {"points": 100, "reason": "AUDIT TEST — Manual award"}
   ```

**Expected API Results:**
- HTTP 200
- Customer with increased `loyalty_points`

**Expected Database Changes:**
| Table | Change |
|---|---|
| `customers` | loyalty_points increased by 100, tier potentially recalculated |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Award to walk-in customer succeeds | Medium |
| Tier not recalculated after points cross threshold | Low |
| Negative points accepted | Medium |

---

### 9.5 Loyalty Points Deduct (Manual)

| Field | Value |
|---|---|
| **Scenario** | Manually deduct loyalty points |
| **Preconditions** | Customer has points |
| **User Role** | Pharmacist |

**Steps:**
1. POST `/api/v1/customers/{customer_id}/loyalty/deduct`:
   ```json
   {"points": 50, "reason": "AUDIT TEST — Manual deduction"}
   ```

**Expected API Results:**
- HTTP 200
- Customer with decreased points

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Deduct below 0 (negative points) | High |
| Deduct from customer with 0 points succeeds (goes negative) | High |

---

### 9.6 Customer Delete Blocked With Recent Sales

| Field | Value |
|---|---|
| **Scenario** | Attempt to delete a customer who has sales |
| **Preconditions** | Customer from Phase 6 has completed sales |
| **User Role** | Pharmacist |

**Steps:**
1. DELETE `/api/v1/customers/{customer_id}`

**Expected API Results:**
- HTTP 400
- Error: cannot delete customer with recent sales

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Customer with recent sales is deleted (orphaned sales) | Critical |
| Customer without sales cannot be deleted | Low |

---

## 10. Phase 9: Pricing Contracts

### 10.1 Create Price Contract — Percentage Discount

| Field | Value |
|---|---|
| **Scenario** | Create an active price contract with percentage discount |
| **Preconditions** | Customer exists, manager authenticated |
| **User Role** | Manager (requires `manage_pricing`) |

**Steps:**
1. POST `/api/v1/price-contracts/`:
   ```json
   {
     "name": "AUDIT 10% Senior Discount",
     "contract_type": "discount",
     "discount_type": "percentage",
     "discount_value": 10.0,
     "effective_from": "{today-1}",
     "effective_to": "{today+90}",
     "status": "active",
     "branch_ids": ["{AUDIT-ACCRA_ID}", "{AUDIT-KUMASI_ID}"],
     "customer_tiers": ["bronze", "silver"],
     "daily_usage_limit": 100,
     "max_usage_per_customer": 10,
     "requires_verification": false
   }
   ```

**Expected API Results:**
- HTTP 201
- Contract object with `id`, discount rules

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Cross-org contract creation possible | Critical |
| Effective_to before effective_from accepted | Medium |
| Discount_value > 100% for percentage type accepted | Medium |
| Contract created without branches (no scope) | Medium |

---

### 10.2 Sale Using Price Contract — Verify Discount Applied

| Field | Value |
|---|---|
| **Scenario** | Process sale with active price contract — verify discount calculation |
| **Preconditions** | Contract active, customer qualifies, stock available |
| **User Role** | Cashier |

**Steps:**
1. POST `/api/v1/sales/`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "customer_id": "{customer_id}",
     "price_contract_id": "{contract_id}",
     "items": [
       {"drug_id": "{AUDIT-PCM-500_ID}", "quantity": 10, "unit_price": 5.00},
       {"drug_id": "{AUDIT-VITC-1_ID}", "quantity": 5, "unit_price": 10.00}
     ],
     "payment_method": "cash",
     "amount_paid": 100.00
   }
   ```

**Expected Pricing Calculation:**
- PCM subtotal: 10 × 5.00 = 50.00
- VITC subtotal: 5 × 10.00 = 50.00
- Total subtotal: 100.00
- 10% discount: 10.00
- Discounted subtotal: 90.00
- Tax on discounted prices
- Total: discounted_subtotal + tax

**Expected API Results:**
- HTTP 201
- `discount_amount` = 10.00 (10% of 100.00)
- Contract snapshot fields populated on sale record (contract_id, contract_name, discount details)

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Discount not applied | Critical |
| Wrong discount calculation | High |
| Contract snapshot not stored on sale record | Medium |
| Drug not in `excluded_drug_ids` but excluded anyway | High |
| Drug excluded but discount still applied | High |

---

### 10.3 Contract Daily Usage Limit Enforcement

| Field | Value |
|---|---|
| **Scenario** | Exceed contract daily usage limit |
| **Preconditions** | Contract with `daily_usage_limit=1` |
| **User Role** | Cashier |

**Steps:**
1. Process sale with contract (should succeed — first usage)
2. Process sale with same contract (should fail — second usage)

**Expected API Results:**
- First sale: 201
- Second sale: 400 with daily limit exceeded error

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Daily limit not enforced (both sales succeed) | Critical |
| Limit counted against wrong org/branch | High |
| Contract usage reset at wrong time (not midnight) | Medium |

**Bug Investigation:**
- Verify concurrent sales both pass the daily limit check (race condition)
- Check: does the count consider only completed sales, or all sales?

---

### 10.4 Contract Verification Token

| Field | Value |
|---|---|
| **Scenario** | Sale with contract requiring verification token |
| **Preconditions** | Contract with `requires_verification=true`, verification token generated |
| **User Role** | Cashier |

**Steps:**
1. Generate verification token: POST `/api/v1/contracts/{contract_id}/generate-token`
2. POST sale with `contract_verification_token` set

**Expected API Results:**
- HTTP 201
- Without token: HTTP 400

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Sale without required token succeeds | Critical |
| Token from different org accepted | High |
| Expired token accepted | Medium |

---

### 10.5 Fixed Price Contract Override

| Field | Value |
|---|---|
| **Scenario** | Contract that overrides price to a fixed value for specific drugs |
| **Preconditions** | Contract with `discount_type=fixed_price` and `discount_value` set |
| **User Role** | Cashier |

**Steps:**
1. Create contract item: POST `/api/v1/price-contract-items/` with `fixed_price=3.00` for PCM-500
2. POST sale using this contract with PCM-500

**Expected Pricing:**
- PCM unit price should be 3.00 (not 5.00)

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Fixed price not applied | High |
| Fixed price below cost price accepted | Medium |

---

## 11. Phase 10: Stock Transfers & Adjustments

### 11.1 Stock Adjustment — Damage

| Field | Value |
|---|---|
| **Scenario** | Record damaged stock |
| **Preconditions** | AUDIT-ACCRA has stock of AUDIT-PCM-500 |
| **User Role** | Pharmacist (requires `manage_inventory`) |

**Steps:**
1. POST `/api/v1/inventory/adjust`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "drug_id": "{AUDIT-PCM-500_ID}",
     "quantity_change": -5,
     "adjustment_type": "damage",
     "reason": "AUDIT TEST — Broken bottles"
   }
   ```

**Expected API Results:**
- HTTP 200
- Adjustment record with `previous_quantity`, `new_quantity`

**Expected Database Changes:**
| Table | Change |
|---|---|
| `stock_adjustments` | 1 row, type=`damage`, quantity_change=-5, previous=190, new=185 |
| `drug_batches` | PCM batch remaining: 190→185 (FEFO from earliest) |
| `branch_inventory` | PCM: 190→185 |
| `inventory_movements` | 1 row, movement_type=`damage`, quantity_change=-5 |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| quantity_change > available stock allowed (negative inventory) | Critical |
| No batch consumed (inventory decreases without batch tracking) | Critical |
| Wrong FEFO batch consumed | Medium |
| `_recalculate_inventory_quantity()` not called (drift not corrected) | Medium |

---

### 11.2 Stock Adjustment — Positive (Correction)

| Field | Value |
|---|---|
| **Scenario** | Add stock via adjustment (e.g., found unrecorded stock) |
| **Preconditions** | Drug exists at branch |
| **User Role** | Pharmacist |

**Steps:**
1. POST `/api/v1/inventory/adjust`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "drug_id": "{AUDIT-IBU-400_ID}",
     "quantity_change": 10,
     "adjustment_type": "correction",
     "reason": "AUDIT TEST — Found unrecorded stock"
   }
   ```

**Expected Database Changes:**
| Table | Change |
|---|---|
| `stock_adjustments` | 1 row, quantity_change=+10 |
| `branch_inventory` | Ibuprofen: 25→35 |
| `drug_batches` | New ADJ-* batch created with +10 remaining |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Adjustment type `correction` used in sale flow overlaps with manual correction — audit trail confusion | Medium |
| No batch created for positive adjustment | Medium |

---

### 11.3 Stock Transfer Between Branches

| Field | Value |
|---|---|
| **Scenario** | Transfer stock from AUDIT-ACCRA to AUDIT-KUMASI |
| **Preconditions** | Source has stock, destination exists, different branches |
| **User Role** | Pharmacist (requires `manage_inventory`) |

**Steps:**
1. POST `/api/v1/inventory/transfer`:
   ```json
   {
     "from_branch_id": "{AUDIT-ACCRA_ID}",
     "to_branch_id": "{AUDIT-KUMASI_ID}",
     "drug_id": "{AUDIT-MET-500_ID}",
     "quantity": 30,
     "reason": "AUDIT TEST — Branch reallocation"
   }
   ```

**Expected API Results:**
- HTTP 200
- Transfer details with source/destination changes

**Expected Database Changes:**
| Table | Change |
|---|---|
| Source `branch_inventory` | MET: 110→80 |
| Dest `branch_inventory` | MET: 0→30 |
| Source `drug_batches` | MET-001 remaining: 110→80 (FEFO) |
| Dest `drug_batches` | New batch created (prices from source) or appended to existing |
| `stock_adjustments` | 2 rows (source: transfer, dest: transfer) |
| `inventory_movements` | 2 rows (transfer_out, transfer_in) |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Transfer to same branch allowed | Medium |
| Transfer quantity > available (not reserved) not blocked | High |
| Transfer across different organizations allowed | Critical |
| Source batch not decremented (inventory double-counted) | Critical |
| Destination gets wrong prices (not from source batch) | High |

---

### 11.4 Stock Adjustment — Expired

| Field | Value |
|---|---|
| **Scenario** | Write off expired stock |
| **Preconditions** | Batch has passed expiry date (manually create a batch with past expiry) |
| **User Role** | Pharmacist |

**Steps:**
1. Create a batch for AUDIT-PCM-500 with expiry_date in the past, quantity=10 (via receive with past expiry or direct DB)
2. POST `/api/v1/inventory/adjust`:
   ```json
   {
     "branch_id": "{AUDIT-ACCRA_ID}",
     "drug_id": "{AUDIT-PCM-500_ID}",
     "quantity_change": -10,
     "adjustment_type": "expired",
     "reason": "AUDIT TEST — Expired stock write-off"
   }
   ```

**Expected Behavior:**
- For adjustment_type=`expired`: consumes from expired batches FIRST, then from non-expired if more needed

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Expired adjustment consumes from non-expired batches first (should consume expired first) | High |
| Expired batches remain available for sale after adjustment | Critical |

**Bug Investigation:**
- Verify `_apply_adjustment` for `expired` type: uses `_consume_expired_batches_first` logic
- Confirm expired batches are filtered out in `load_fefo_batches` for sales (`expiry_date >= CURRENT_DATE`)

---

## 12. Phase 11: Reporting & Valuation

### 12.1 Inventory Valuation Report

| Field | Value |
|---|---|
| **Scenario** | Get inventory valuation for a branch |
| **Preconditions** | Branch has stock with multiple batches at different cost prices |
| **User Role** | Manager / Pharmacist (requires `view_reports`) |

**Steps:**
1. GET `/api/v1/inventory/reports/valuation/{AUDIT-ACCRA_ID}`

**Expected API Results:**
- HTTP 200
- List of drugs with:
  - `total_quantity`
  - `cost_value` (sum of batch.remaining × batch.cost_price)
  - `selling_value` (sum of batch.remaining × effective_selling_price)
  - `profit_margin`
- Totals at end

**Expected Calculation Example (AUDIT Amlodipine 10mg):**
- 1 batch, remaining=90 (after sales), cost_price=7.50
- cost_value = 90 × 7.50 = 675.00
- selling_value = 90 × 15.00 = 1,350.00
- profit_margin = (1350 - 675) / 1350 = 50%

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Wrong cost/selling values reported | High |
| Cross-branch valuation leak | Critical |
| Report includes soft-deleted drugs | Medium |

---

### 12.2 Daily Sales Summary

| Field | Value |
|---|---|
| **Scenario** | Get daily sales summary |
| **Preconditions** | Sales exist from Phase 6 |
| **User Role** | Manager |

**Steps:**
1. GET `/api/v1/reports/daily-sales?branch_id={AUDIT-ACCRA_ID}&date={today}`

**Expected API Results:**
- HTTP 200
- Total sales count, total revenue, payment method breakdown

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Report includes refunded sales in total (should exclude) | High |
| Report excludes periods (timezone issues) | Medium |

---

### 12.3 Audit Log Retrieval

| Field | Value |
|---|---|
| **Scenario** | Retrieve audit logs for the organization |
| **Preconditions** | Multiple operations performed during audit |
| **User Role** | Manager / Admin (requires `view_audit_logs`) |

**Steps:**
1. GET `/api/v1/audit-logs?entity_type=sale&limit=10`

**Expected API Results:**
- HTTP 200
- List of audit entries with action, timestamp, user, changes

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Audit logs empty despite operations | Critical |
| Cross-org audit log leak | Critical |
| Changes field empty (before/after not captured) | High |

---

## 13. Phase 12: Authentication & Security

### 13.1 Login → Protected Access → Logout

| Field | Value |
|---|---|
| **Scenario** | Full login session lifecycle |
| **Preconditions** | User exists |
| **User Role** | Any |

**Steps:**
1. POST `/api/v1/auth/login` with valid credentials → get tokens
2. Access protected endpoint with access token → 200
3. POST `/api/v1/auth/logout` → session revoked
4. Access protected endpoint with same token → 401

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Token works after logout | Critical |
| Login without rate limiting (brute force) | Medium |
| Session not recorded in user_sessions | Low |

---

### 13.2 Force Password Change Flow

| Field | Value |
|---|---|
| **Scenario** | User with must_change_password=True goes through full flow |
| **Preconditions** | Newly created user |
| **User Role** | Any new user |

**Steps:**
1. Login as user with `must_change_password=True` → success
2. Access protected endpoint → should it be blocked? (Check current behavior)
3. POST `/api/v1/auth/force-change-password` with new password
4. Login with new password → success
5. Check `must_change_password` flag → false

**Expected API Results:**
- After force change: 200, `detail=PASSWORD_CHANGED`
- Re-login works with new password
- Old password no longer works

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Old password still works after change | Critical |
| must_change_password still true after change | High |
| Can force-change without being authenticated | Critical |
| New password fails strength validation silently | Medium |

---

### 13.3 Account Lockout

| Field | Value |
|---|---|
| **Scenario** | Account locks after 5 failed attempts |
| **Preconditions** | User exists with known password |
| **User Role** | Any |

**Steps:**
1. POST login 5 times with wrong password
2. 6th attempt with wrong password → account locked message
3. Attempt with correct password → still locked (401)
4. Check DB: `account_locked_until` is set

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Account never locks | High |
| Correct password unlocks without waiting | High |
| Lockout timer not set / not enforced | High |
| Failed attempts counter resets on successful login (shouldn't) | Medium |

---

### 13.4 Forgot Password Flow

| Field | Value |
|---|---|
| **Scenario** | Complete forgot-password reset |
| **Preconditions** | User exists with known email |
| **User Role** | Anonymous |

**Steps:**
1. POST `/api/v1/auth/forgot-password` with user's email
2. Extract reset token from database (`users.reset_token_hash`)
3. POST `/api/v1/auth/reset-password` with token and new password
4. Login with new password → success
5. Login with old password → 401

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Token not created in DB | High |
| Token accepted after expiry | Medium |
| Token can be reused | High |
| Password reset for non-existent email gives different response (user enumeration) | Low |

---

### 13.5 Permission Enforcement — Branch Isolation

| Field | Value |
|---|---|
| **Scenario** | User assigned to AUDIT-ACCRA cannot access AUDIT-KUMASI data |
| **Preconditions** | `audit_cashier_01` assigned only to AUDIT-ACCRA |
| **User Role** | Cashier |

**Steps:**
1. Login as `audit_cashier_01`
2. GET `/api/v1/inventory?branch_id={AUDIT-KUMASI_ID}`
3. GET `/api/v1/sales?branch_id={AUDIT-KUMASI_ID}`
4. GET `/api/v1/branches/{AUDIT-KUMASI_ID}`

**Expected API Results:**
- All endpoints should return 403 or filter results to exclude KUMASI data

**Failure Conditions:**
| Condition | Severity |
|---|---|
| User can see other branch's inventory | Critical |
| User can see other branch's sales | Critical |
| Empty result returned instead of 403 (still leaks count) | High |
| User can create sale in other branch | Critical |

---

### 13.6 Permission Enforcement — Role-Based

| Field | Value |
|---|---|
| **Scenario** | Cashier cannot access manager-only endpoints |
| **Preconditions** | `audit_cashier_01` has only Cashier role (level=10) |
| **User Role** | Cashier |

**Steps:**
1. Login as `audit_cashier_01`
2. POST `/api/v1/drugs/` → should fail (requires `manage_drugs`)
3. POST `/api/v1/inventory/adjust` → should fail (requires `manage_inventory`)
4. GET `/api/v1/roles/permissions` → should work (any authenticated)
5. GET `/api/v1/audit-logs` → should fail (requires `view_audit_logs`)
6. POST `/api/v1/sales/` → should work (Cashier has `process_sales`)

**Expected Results:**
- Protected endpoints return 403
- Permitted endpoints return successfully

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Cashier can manage drugs | Critical |
| Cashier can view audit logs | High |
| Cashier cannot process sales | Critical |

---

### 13.7 MFA Flow

| Field | Value |
|---|---|
| **Scenario** | Enable, use, and disable MFA |
| **Preconditions** | User authenticated |
| **User Role** | Any |

**Steps:**
1. POST `/api/v1/auth/mfa/setup` → get provisioning URI + secret
2. Generate TOTP code from secret
3. POST `/api/v1/auth/mfa/verify` with valid code → MFA enabled
4. Logout → Login → should respond with MFA code required
5. POST login with MFA code → success
6. POST `/api/v1/auth/mfa/disable` with password → MFA disabled

**Failure Conditions:**
| Condition | Severity |
|---|---|
| MFA can be bypassed | Critical |
| Invalid TOTP code accepted | Critical |
| MFA secret stored in plaintext (should be encrypted) | High |
| Login without MFA works after enabling | Critical |

---

## 14. Phase 13: Offline Sync

### 14.1 Pull Delta

| Field | Value |
|---|---|
| **Scenario** | Pull records modified since last sync timestamp |
| **Preconditions** | Data exists on server |
| **User Role** | Any authenticated (sync client) |

**Steps:**
1. POST `/api/v1/sync/pull`:
   ```json
   {
     "tables": ["drugs", "branch_inventory", "drug_batches", "customers", "sales"],
     "last_sync_at": "2020-01-01T00:00:00Z",
     "branch_id": "{AUDIT-ACCRA_ID}"
   }
   ```

**Expected API Results:**
- HTTP 200
- Records grouped by table
- Only records for AUDIT-ACCRA branch returned
- Records normalized to `sync_status='synced'`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Cross-branch data leaked in pull | Critical |
| Pull returns empty for existing data | High |
| last_sync_at filter ignored (full dump) | Medium |
| Soft-deleted records included | Medium |

---

### 14.2 Push Records

| Field | Value |
|---|---|
| **Scenario** | Push offline-created records to server |
| **Preconditions** | Client has pending records |
| **User Role** | Sync client |

**Steps:**
1. POST `/api/v1/sync/push`:
   ```json
   {
     "records": {
       "customers": [
         {
           "id": "{new_uuid}",
           "organization_id": "{org_id}",
           "first_name": "Offline",
           "last_name": "Patient",
           "phone": "+233501234599",
           "customer_type": "registered",
           "sync_version": 1
         }
       ]
     },
     "branch_id": "{AUDIT-ACCRA_ID}"
   }
   ```

**Expected API Results:**
- HTTP 200
- `accepted` list includes the customer
- `conflicts` list empty
- `failed` list empty

**Expected Database Changes:**
| Table | Change |
|---|---|
| `customers` | 1 new row |
| `sync_operation_receipts` | 1 receipt for the push operation |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate push creates duplicate customer (no idempotency) | Critical |
| Push accepted but no receipt created | High |
| Customer from wrong org accepted | Critical |

---

### 14.3 Duplicate Push Prevention (Idempotency)

| Field | Value |
|---|---|
| **Scenario** | Same push sent twice — second should be skipped |
| **Preconditions** | First push succeeded from 14.2 |
| **User Role** | Sync client |

**Steps:**
1. Send the exact same push payload again

**Expected API Results:**
- HTTP 200
- `accepted` list empty (or marked as duplicate)
- `conflicts` list empty
- No duplicate customer created

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Duplicate customer created | Critical |
| Receipt not checked on subsequent push | High |

---

### 14.4 Sync Conflict Detection (Customer)

| Field | Value |
|---|---|
| **Scenario** | Server has newer version of customer — conflict detected |
| **Preconditions** | Customer exists on server with sync_version=2 |
| **User Role** | Sync client |

**Steps:**
1. Push customer record with sync_version=1 (older than server's version=2)

**Expected API Results:**
- Customer appears in `conflicts` list
- Resolution = `manual_required`

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Conflict not detected (server silently overwritten) | Critical |
| Wrong resolution strategy applied | Medium |
| Force flag not respected | Medium |

---

### 14.5 Offline Sale Push (Protocol v2)

| Field | Value |
|---|---|
| **Scenario** | Push an offline-created sale — inventory deducted on server |
| **Preconditions** | Sufficient stock at branch |
| **User Role** | Sync client |

**Steps:**
1. Create a sale-like record offline and push via sync push
2. Push payload should include sale, sale_items, sale_item_batch_allocations

**Expected Database Changes:**
| Table | Change |
|---|---|
| `sales` | 1 new row |
| `drug_batches` | remaining decreased |
| `branch_inventory` | quantity decreased |
| `inventory_movements` | 1+ rows |
| `sync_operation_receipts` | 1 receipt |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Inventory deducted without sale recorded | Critical |
| Sale recorded without inventory deduction | Critical |
| Offline sale accepted for out-of-stock drug | Medium |

---

### 14.6 Sync Integrity Check

| Field | Value |
|---|---|
| **Scenario** | Run integrity check and verify consistency |
| **Preconditions** | Data exists |
| **User Role** | Admin |

**Steps:**
1. GET `/api/v1/admin/sync-recovery/check-integrity`

**Expected API Results:**
- List of SyncIntegrityIssue objects
- If data is consistent: empty list

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Integrity check returns errors for known-good data | Medium |
| Integrity check misses known-bad data | High |

---

## 15. Phase 14: Concurrent Operations & Race Conditions

### 15.1 Concurrent Sale — Same Batch (Critical Race Condition)

| Field | Value |
|---|---|
| **Scenario** | Two POS terminals sell from same batch simultaneously |
| **Preconditions** | AUDIT-PCM-500 has 185 units in single batch at AUDIT-ACCRA |
| **User Role** | Two cashier users |

**Steps:**
1. Open two terminals simultaneously
2. Terminal A: POST `/api/v1/sales/` with qty=100 of AUDIT-PCM-500
3. Terminal B: POST `/api/v1/sales/` simultaneously with qty=100 of AUDIT-PCM-500
4. Both requests should fire within milliseconds of each other

**Expected API Results:**
- One sale succeeds (185→85)
- One sale fails with insufficient stock (100 > 85 remaining)

**Expected Database Changes:**
| Table | Change |
|---|---|
| `branch_inventory` | PCM: 185→85 |
| `drug_batches` | PCM batch: 185→85 |

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Both sales succeed (185→85→(-15)) → inventory goes negative | Critical |
| Both sales fail (double rollback but no explanation) | Medium |
| One sale partially succeeds (deducts from wrong batch) | Critical |

**Bug Investigation:**
- Verify `FOR UPDATE` lock is acquired BEFORE FEFO batch selection, not after
- Check if `SELECT ... FOR UPDATE SKIP LOCKED` is used
- Verify `sale_item_batch_allocations` shows batch consumed correctly

---

### 15.2 Concurrent Contract Usage

| Field | Value |
|---|---|
| **Scenario** | Two sales simultaneously hit daily usage limit |
| **Preconditions** | Contract with `daily_usage_limit=5`, 4 sales already used it today |
| **User Role** | Two cashier users |

**Steps:**
1. Create 4 sales using the contract today
2. Send 5th and 6th sale simultaneously

**Expected API Results:**
- 5th sale succeeds
- 6th sale fails: daily limit exceeded

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Both succeed (7 sales today, limit=5) — race condition | Critical |
| Both fail (quantum entang — unexpected) | Medium |

**Bug Investigation:**
- Verify `load_and_validate_contract()` uses `FOR UPDATE` or runs inside the main transaction
- Check: does the COUNT query lock the contract sales counter?

---

### 15.3 Concurrent Stock Adjustment and Sale

| Field | Value |
|---|---|
| **Scenario** | Stock adjustment deducting same units as concurrent sale |
| **Preconditions** | AUDIT-IBU-400 has 35 units |
| **User Role** | Pharmacist + Cashier |

**Steps:**
1. Fire sale (qty=20) and adjustment (qty=-20, type=damage) simultaneously

**Expected API Results:**
- One succeeds (35→15)
- Other fails (15 < 20)

**Failure Conditions:**
| Condition | Severity |
|---|---|
| Both succeed (35→15→(-5)) → negative inventory | Critical |
| Batch-level inconsistency (branch_inventory disagrees with batch sum) | High |

---

## 16. Bug Investigation Matrix

### 16.1 Likely Defects by Module

| Module | Likely Defect | Severity | Detection Scenario |
|---|---|---|---|
| **Onboarding** | Branch codes not respecting provided `code` field | Medium | 2.1 |
| **Onboarding** | Operating hours silently discarded under strict validation | Medium | 2.1 |
| **Onboarding** | New admin user has `must_change_password=False` | High | 2.1 |
| **Branch** | `func.array_length()` on JSON text column crashes branch creation | High | 2.2 |
| **User** | Greenlet error when assigning roles during user creation | High | 3.4 |
| **User** | Role not loaded with `selectinload` causing async greenlet error | High | 3.4 |
| **User** | `assigned_branches` set to empty removes all access | Medium | 3.6 |
| **Drug** | SKU/barcode uniqueness not enforced with soft-deleted records | Medium | 4.2 |
| **Drug** | `search_vector` not populated for FTS | Medium | 4.2 |
| **PO** | Over-receipt allowed (quantity_received > quantity_ordered) | High | 5.5 |
| **PO** | Partial receipt closes PO prematurely | Medium | 5.6 |
| **Sale** | FEFO batch selection and FOR UPDATE lock in separate queries → race | Critical | 15.1 |
| **Sale** | Expired batch not re-checked after race window | High | 15.1 |
| **Sale** | Total amount calculation inconsistent: subtotal - discount + tax | High | 6.1, DB query |
| **Refund** | Inventory restored with current batch prices instead of sale-time prices | High | 7.1 |
| **Refund** | Loyalty points not deducted on refund | Medium | 7.3 |
| **Refund** | Prescription refills not restored on refund | High | 8.8 |
| **Refund** | Duplicate refund not prevented by `refunded_quantity` check | Critical | 7.5 |
| **Prescription** | Status not automatically transitioned to `expired` on expiry_date | Medium | 8.4 |
| **Prescription** | Sale with expired prescription not blocked | Critical | 8.4 |
| **Prescription** | Refill with 0 remaining not blocked | Critical | 8.5 |
| **Contract** | Daily usage limit check has race condition (two concurrent sales pass) | Critical | 15.2 |
| **Contract** | Drug excluded from contract but discount still applied | High | 10.2 |
| **Contract** | Verification token not required when `requires_verification=true` | Critical | 10.4 |
| **Inventory** | `_recalculate_inventory_quantity()` not called in all paths → drift | High | 11.1 |
| **Inventory** | Adjustment type `expired` doesn't consume expired batches first | High | 11.4 |
| **Sync** | Receipt written before inventory deduction succeeds → silent drift | Critical | 14.5 |
| **Sync** | Duplicate push creates duplicate records (no idempotency) | Critical | 14.3 |
| **Sync** | `_validate_and_fix_sale_fks()` silently clears invalid FKs | Medium | 14.1 |
| **Auth** | Token works after logout | Critical | 13.1 |
| **Auth** | MFA bypass (no MFA code required after enabling) | Critical | 13.7 |
| **Auth** | Account lockout timer not enforced | High | 13.3 |
| **Branch** | User can access other branch data (branch isolation broken) | Critical | 13.5 |
| **Branch** | User can create sale in unassigned branch | Critical | 13.5 |
| **Permissions** | Cashier can access manager endpoints | Critical | 13.6 |
| **Permissions** | User can create records in other orgs (IDOR) | Critical | Multiple |
| **Audit** | Audit log not created for critical operations | Critical | Multiple |
| **Audit** | Audit log changes JSONB contains PII unencrypted | Low | 12.3 |

### 16.2 Likely Data Consistency Issues

| Issue | Impact | Detection |
|---|---|---|
| `branch_inventory.quantity` ≠ SUM(`drug_batches.remaining_quantity`) | Wrong stock counts | DB query |
| `sales.total_amount` ≠ `subtotal - discount_amount + tax_amount` | Financial reporting wrong | DB query |
| `sale_items.refunded_quantity` > SUM(allocations.refunded_quantity) | Over-refund possible | DB query |
| `prescriptions.refills_remaining` > `refills_allowed` | Excess refills granted | DB query |
| Negative `branch_inventory.quantity` | Invalid state | DB query |
| Negative `drug_batches.remaining_quantity` | Invalid state | DB query |
| Orphaned `sales.customer_id` pointing to deleted customer | FK violation | DB query |
| Orphaned `sales.cashier_id` pointing to deleted user | FK violation | DB query |
| `purchase_order_items.received_quantity > ordered_quantity` | Over-receipt | DB query |

### 16.3 Likely Race Conditions

| Condition | Window | Severity |
|---|---|---|
| Two concurrent sales consuming same FEFO batch | Between batch SELECT and FOR UPDATE | Critical |
| Two concurrent sales passing contract daily limit | Between COUNT SELECT and COMMIT | Critical |
| Sale and adjustment concurrently modifying same stock | Any overlapping execution | High |
| Sync push inventory deduction failing after receipt written | Between receipt INSERT and savepoint | Critical |

### 16.4 Likely Permission / Isolation Issues

| Issue | Module | Severity |
|---|---|---|
| Cross-organization data access via API (no org filter) | All endpoints | Critical |
| Cross-branch data access via API (branch_id not validated) | Inventory, Sales | Critical |
| Low-privilege user accessing admin endpoints | All endpoints | Critical |
| Unauthenticated user accessing protected endpoints | Auth | Critical |
| Unauthenticated user can enumerate users (forgot password) | Auth | Low |
| Rate limiting not applied to login attempts | Auth | Medium |

### 16.5 Likely Offline Sync Issues

| Issue | Module | Severity |
|---|---|---|
| Offline sale duplicated on retry | Sync push | Critical |
| Inventory not deducted for offline sale (protocol v2) | Sync push | Critical |
| Conflict not detected (server_wins overwrites newer offline data) | Sync push | High |
| Silent FK clearance hides data loss | Sync push | Medium |
| Customer deduplication not possible (manual_required never resolves) | Sync push | Medium |
| Large pull payload causes timeout | Sync pull | Medium |
| last_sync_at drift (server and client clock mismatch) | Sync pull | Medium |

---

## 17. Completion Criteria

A workflow is **complete** only when ALL of the following are verified:

### ✅ API Works
- Endpoint returns correct HTTP status code (200/201/204 for success, 4xx for client errors)
- Response body matches expected schema
- Response contains correct computed values (totals, discounts, taxes)
- Error responses are structured and informative

### ✅ Database State Is Correct
- Expected rows inserted/updated in all affected tables
- No orphaned records
- Referential integrity maintained (FKs valid)
- Business constraints satisfied (no negative inventory, no over-refund)
- For sales: inventory movements balance (sum of all movements = current stock)
- For financial: `total_amount` = `subtotal - discount_amount + tax_amount`

### ✅ Audit Records Correct
- At least one audit log entry exists for each mutating operation
- Audit entry contains: correct `action`, `entity_type`, `entity_id`, `changes` JSONB
- `changes` contains meaningful before/after diff
- Audit log belongs to correct organization

### ✅ UI Works (Frontend Verification)
- Data appears correctly in relevant views (inventory list, sales history, reports)
- Actions are reflected in UI after refresh
- Error messages displayed for failure cases
- Pagination works for list views
- Search returns expected results

### ✅ No Regression
- All previously passing scenarios still pass
- Existing data unaffected by new operations
- Cross-cutting concerns (branch isolation, org isolation, permission enforcement) still hold

### ✅ Edge Cases Covered
- Empty payloads rejected with 422
- Missing required fields rejected with 422
- Invalid references (wrong org, non-existent IDs) rejected with 404/403
- Duplicate submissions prevented or idempotent
- Boundary values tested (zero quantity, negative prices, max lengths)

### ✅ Concurrent Safety (Where Applicable)
- Race condition scenarios tested for concurrent stock operations
- Contract usage limits withstand concurrent access
- No negative stock under concurrent load

---

## 18. Database Verification Queries

Run after each workflow to verify consistency:

### Inventory Consistency
```sql
-- Branch inventory vs sum of batch remaining_quantity
SELECT bi.branch_id, bi.drug_id, bi.quantity AS bi_quantity,
       COALESCE(SUM(db.remaining_quantity), 0) AS batch_sum
FROM branch_inventory bi
LEFT JOIN drug_batches db ON db.branch_id = bi.branch_id AND db.drug_id = bi.drug_id
GROUP BY bi.branch_id, bi.drug_id, bi.quantity
HAVING bi.quantity != COALESCE(SUM(db.remaining_quantity), 0);
```

### Financial Integrity
```sql
-- Sales where total_amount != subtotal - discount + tax
SELECT id, sale_number, subtotal, discount_amount, tax_amount, total_amount,
       (subtotal - discount_amount + tax_amount) AS computed_total
FROM sales
WHERE ABS(total_amount - (subtotal - discount_amount + tax_amount)) > 0.01;
```

### Refund Limits
```sql
-- Refunded quantity exceeds original quantity
SELECT si.sale_id, si.id AS item_id, si.quantity, si.refunded_quantity,
       SUM(siba.refunded_quantity) AS batch_refund_sum
FROM sale_items si
JOIN sale_item_batch_allocations siba ON siba.sale_item_id = si.id
GROUP BY si.sale_id, si.id, si.quantity, si.refunded_quantity
HAVING si.refunded_quantity > si.quantity
   OR SUM(siba.refunded_quantity) > si.quantity;
```

### Prescription Refill Limits
```sql
-- Refills exceeding allowed
SELECT id, prescription_number, refills_allowed, refills_remaining
FROM prescriptions
WHERE refills_remaining > refills_allowed;
```

### Negative Inventory
```sql
SELECT * FROM branch_inventory WHERE quantity < 0;
SELECT * FROM drug_batches WHERE remaining_quantity < 0;
```

### Orphaned Records
```sql
-- Sales with missing customers
SELECT id, sale_number FROM sales
WHERE customer_id IS NOT NULL
  AND customer_id NOT IN (SELECT id FROM customers);

-- Sales with missing cashiers
SELECT id, sale_number FROM sales
WHERE cashier_id IS NOT NULL
  AND cashier_id NOT IN (SELECT id FROM users WHERE is_deleted = false);

-- Batches with missing drugs
SELECT id FROM drug_batches
WHERE drug_id NOT IN (SELECT id FROM drugs WHERE is_deleted = false);
```

### Audit Log Completeness
```sql
-- Count audit entries by action for the audit org
SELECT action, COUNT(*) as count
FROM audit_logs
WHERE organization_id = '{org_id}'
GROUP BY action
ORDER BY count DESC;
```

### Price Contract Usage
```sql
-- Today's usage count per contract
SELECT price_contract_id, COUNT(*) as usage_count
FROM sales
WHERE price_contract_id IS NOT NULL
  AND created_at >= CURRENT_DATE
  AND created_at < CURRENT_DATE + INTERVAL '1 day'
  AND status IN ('completed', 'partially_refunded')
GROUP BY price_contract_id;
```
