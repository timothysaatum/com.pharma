# Full API Test Plan — 148 Endpoints (203 Scenario Calls)

Covers every backend endpoint in strict dependency order. Each phase builds state
required by the next. Generated: 2026-08-11.

---

## Phase 1 — Health & Baseline

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 1.1 | GET | `/health` | Basic liveness |
| 1.2 | GET | `/health/deep` | DB + dependency connectivity |
| 1.3 | GET | `/` | Root info |

---

## Phase 2 — Organization Onboarding

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 2.1 | POST | `/api/v1/organizations/onboard` | Atomic org creation, idempotency key |
| 2.2 | GET | `/api/v1/organizations` | List orgs (super-admin view) |
| 2.3 | GET | `/api/v1/organizations/{id}` | Fetch created org |
| 2.4 | GET | `/api/v1/organizations/{id}/stats` | Org-level dashboard stats |
| 2.5 | GET | `/api/v1/organizations/{id}/settings` | JSONB settings (tax, loyalty config) |
| 2.6 | PATCH | `/api/v1/organizations/{id}/settings` | Update settings (enable loyalty, set tax rate) |
| 2.7 | PATCH | `/api/v1/organizations/{id}` | Update org name/contact |
| 2.8 | POST | `/api/v1/organizations/{id}/subscription` | Update subscription tier |
| 2.9 | POST | `/api/v1/organizations/{id}/deactivate` | Deactivate org |
| 2.10 | POST | `/api/v1/organizations/{id}/activate` | Reactivate org |

---

## Phase 3 — Auth (Full Flow)

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 3.1 | POST | `/api/v1/auth/login` | Login, get JWT + refresh token |
| 3.2 | POST | `/api/v1/auth/force-change-password` | Forced password change on first login |
| 3.3 | GET | `/api/v1/auth/me` | Current user profile |
| 3.4 | GET | `/api/v1/auth/permissions` | List all permissions for current user |
| 3.5 | GET | `/api/v1/auth/sessions` | List active sessions |
| 3.6 | POST | `/api/v1/auth/refresh` | Refresh expired access token |
| 3.7 | POST | `/api/v1/auth/change-password` | Voluntary password change |
| 3.8 | GET | `/api/v1/auth/verify` | Verify token validity |
| 3.9 | POST | `/api/v1/auth/mfa/setup` | Generate TOTP secret + QR |
| 3.10 | POST | `/api/v1/auth/mfa/verify` | Confirm TOTP code to activate MFA |
| 3.11 | POST | `/api/v1/auth/mfa/disable` | Disable MFA |
| 3.12 | POST | `/api/v1/auth/forgot-password` | Request password reset email |
| 3.13 | POST | `/api/v1/auth/reset-password` | Reset password via token from email |
| 3.14 | POST | `/api/v1/auth/register` | Register a super-admin account |
| 3.15 | POST | `/api/v1/auth/logout` | Revoke current session token |
| 3.16 | POST | `/api/v1/auth/logout-all` | Revoke all sessions for this user |

---

## Phase 4 — Roles & Users

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 4.1 | GET | `/api/v1/roles/permissions` | List all available permission strings |
| 4.2 | GET | `/api/v1/roles/` | List existing roles (admin/staff seeded at onboarding) |
| 4.3 | POST | `/api/v1/roles/` | Create a Pharmacist role with prescription permissions |
| 4.4 | POST | `/api/v1/roles/` | Create a Cashier role with sales-only permissions |
| 4.5 | GET | `/api/v1/roles/{id}` | Fetch a single role |
| 4.6 | PUT | `/api/v1/roles/{id}` | Update role permissions |
| 4.7 | GET | `/api/v1/users` | List all users |
| 4.8 | POST | `/api/v1/users` | Create a pharmacist user |
| 4.9 | POST | `/api/v1/users` | Create a cashier user |
| 4.10 | GET | `/api/v1/users/{id}` | Fetch a single user |
| 4.11 | PATCH | `/api/v1/users/{id}` | Update user details |
| 4.12 | POST | `/api/v1/users/{id}/deactivate` | Deactivate user |
| 4.13 | POST | `/api/v1/users/{id}/activate` | Reactivate user |
| 4.14 | POST | `/api/v1/users/{id}/reset-password` | Admin-triggered password reset |
| 4.15 | POST | `/api/v1/users/{id}/unlock` | Unlock account after failed login lockout |
| 4.16 | DELETE | `/api/v1/roles/{id}` | Delete an unused role |
| 4.17 | DELETE | `/api/v1/users/{id}` | Delete a test user |

---

## Phase 5 — Branches

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 5.1 | GET | `/api/v1/branches` | List all branches |
| 5.2 | POST | `/api/v1/branches` | Create Branch 2 (second location) |
| 5.3 | GET | `/api/v1/branches/my-branches` | Branches visible to current user |
| 5.4 | GET | `/api/v1/branches/{id}` | Fetch branch details |
| 5.5 | GET | `/api/v1/branches/code/{code}` | Lookup branch by code |
| 5.6 | POST | `/api/v1/branches/search` | Search branches by name/code |
| 5.7 | PATCH | `/api/v1/branches/{id}` | Update branch contact info |
| 5.8 | POST | `/api/v1/branches/assign-user` | Assign pharmacist to Branch 2 |
| 5.9 | GET | `/api/v1/branches/{id}/users` | List users assigned to a branch |
| 5.10 | POST | `/api/v1/branches/{id}/deactivate` | Deactivate Branch 2 |
| 5.11 | POST | `/api/v1/branches/{id}/activate` | Reactivate Branch 2 |
| 5.12 | DELETE | `/api/v1/branches/{id}` | Soft-delete a branch |

---

## Phase 6 — Insurance Providers

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 6.1 | POST | `/api/v1/insurance-providers/` | Create GLICO provider |
| 6.2 | POST | `/api/v1/insurance-providers/` | Create NHIS provider |
| 6.3 | GET | `/api/v1/insurance-providers/` | List all providers |
| 6.4 | GET | `/api/v1/insurance-providers/search` | Search by name |
| 6.5 | GET | `/api/v1/insurance-providers/{id}` | Fetch single provider |
| 6.6 | PATCH | `/api/v1/insurance-providers/{id}` | Update billing cycle |
| 6.7 | POST | `/api/v1/insurance-providers/{id}/deactivate` | Deactivate a provider |
| 6.8 | POST | `/api/v1/insurance-providers/{id}/activate` | Reactivate a provider |
| 6.9 | DELETE | `/api/v1/insurance-providers/{id}` | Delete unused provider |

---

## Phase 7 — Drug Catalog

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 7.1 | POST | `/api/v1/drugs/categories` | Create parent category: Analgesics |
| 7.2 | POST | `/api/v1/drugs/categories` | Create child category: NSAIDs (under Analgesics) |
| 7.3 | POST | `/api/v1/drugs/categories` | Create category: Antibiotics |
| 7.4 | GET | `/api/v1/drugs/categories` | List all categories |
| 7.5 | GET | `/api/v1/drugs/categories/tree` | Fetch hierarchical category tree |
| 7.6 | PATCH | `/api/v1/drugs/categories/{id}` | Rename a category |
| 7.7 | POST | `/api/v1/drugs` | Create Drug A: Paracetamol (OTC) |
| 7.8 | POST | `/api/v1/drugs` | Create Drug B: Amoxicillin (prescription) |
| 7.9 | POST | `/api/v1/drugs` | Create Drug C: Codeine (controlled) |
| 7.10 | POST | `/api/v1/drugs` | Create Drug D: Ibuprofen (OTC, for FEFO multi-batch test) |
| 7.11 | GET | `/api/v1/drugs` | List all drugs |
| 7.12 | POST | `/api/v1/drugs/search` | Search by name/generic/barcode |
| 7.13 | GET | `/api/v1/drugs/{id}` | Fetch single drug |
| 7.14 | PATCH | `/api/v1/drugs/{id}` | Update reorder level |
| 7.15 | GET | `/api/v1/drugs/{id}/with-inventory` | Fetch drug + branch inventory snapshot |
| 7.16 | POST | `/api/v1/drugs/bulk-update` | Bulk update markup/price on multiple drugs |
| 7.17 | POST | `/api/v1/drugs/import` | Import drugs via CSV/JSON payload |
| 7.18 | DELETE | `/api/v1/drugs/categories/{id}` | Delete empty category |
| 7.19 | DELETE | `/api/v1/drugs/{id}` | Soft-delete a drug |

---

## Phase 8 — Inventory & Batches

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 8.1 | POST | `/api/v1/inventory/batches` | Add Batch 1 for Ibuprofen — expires in 3 months (earlier) |
| 8.2 | POST | `/api/v1/inventory/batches` | Add Batch 2 for Ibuprofen — expires in 12 months (later) |
| 8.3 | POST | `/api/v1/inventory/batches` | Add batch for Paracetamol |
| 8.4 | POST | `/api/v1/inventory/batches` | Add batch for Amoxicillin |
| 8.5 | POST | `/api/v1/inventory/batches` | Add batch for Codeine |
| 8.6 | GET | `/api/v1/inventory/batches/drug/{drug_id}` | List batches for Ibuprofen, verify FEFO order |
| 8.7 | PATCH | `/api/v1/inventory/batches/{id}` | Update batch selling price |
| 8.8 | GET | `/api/v1/inventory/branch/{branch_id}` | Full branch inventory summary |
| 8.9 | POST | `/api/v1/inventory/branch/{branch_id}/drugs` | Manually link a drug to a branch with branch-specific price |
| 8.10 | PATCH | `/api/v1/inventory/branch/{branch_id}/drugs/{drug_id}` | Update branch-specific selling price override |
| 8.11 | GET | `/api/v1/inventory/branch/{branch_id}/drugs` | List all drugs at branch with quantities |
| 8.12 | GET | `/api/v1/inventory/low-stock` | Confirm low-stock flag triggered |
| 8.13 | GET | `/api/v1/inventory/expiring/{branch_id}` | Confirm expiring batch appears |
| 8.14 | GET | `/api/v1/inventory/reports/valuation/{branch_id}` | Inventory valuation (cost × qty) report |
| 8.15 | POST | `/api/v1/inventory/reserve` | Manually reserve units |
| 8.16 | POST | `/api/v1/inventory/release-reserved` | Release the reservation |
| 8.17 | POST | `/api/v1/inventory/batches/{id}/consume` | Directly consume units from a batch |
| 8.18 | POST | `/api/v1/inventory/adjust` | Manual adjustment: mark 2 units as damaged |
| 8.19 | POST | `/api/v1/inventory/transfer` | Transfer stock Branch 1 → Branch 2 |
| 8.20 | DELETE | `/api/v1/inventory/branch/{branch_id}/drugs/{drug_id}` | Remove a drug from a branch |

---

## Phase 9 — Contracts (Pricing)

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 9.1 | GET | `/api/v1/contracts/available/{branch_id}` | Confirm default Standard contract exists |
| 9.2 | POST | `/api/v1/contracts` | Create Corporate contract (15% off, all branches) |
| 9.3 | POST | `/api/v1/contracts` | Create Insurance contract (GLICO, copay 5 GHS, prescription only) |
| 9.4 | POST | `/api/v1/contracts` | Create Staff contract (20% off, requires role check) |
| 9.5 | GET | `/api/v1/contracts` | List all contracts |
| 9.6 | GET | `/api/v1/contracts/{id}` | Fetch single contract |
| 9.7 | GET | `/api/v1/contracts/{id}/details` | Fetch contract + all PriceContractItem overrides |
| 9.8 | GET | `/api/v1/contracts/check-code/{code}` | Verify contract code uniqueness |
| 9.9 | PATCH | `/api/v1/contracts/{id}` | Update contract discount percentage |
| 9.10 | POST | `/api/v1/contracts/{id}/activate` | Activate Corporate contract |
| 9.11 | POST | `/api/v1/contracts/{id}/activate` | Activate Insurance contract |
| 9.12 | POST | `/api/v1/contracts/{id}/approve` | Approve a contract requiring approval |
| 9.13 | POST | `/api/v1/contracts/verify-eligibility` | Verify customer eligibility for insurance contract |
| 9.14 | POST | `/api/v1/contracts/{id}/duplicate` | Duplicate a contract (new promo variant) |
| 9.15 | POST | `/api/v1/contracts/{id}/suspend` | Suspend the duplicated contract |
| 9.16 | DELETE | `/api/v1/contracts/{id}` | Delete draft contract |

---

## Phase 10 — Customers & Prescriptions

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 10.1 | POST | `/api/v1/customers` | Create walk-in customer (no personal data) |
| 10.2 | POST | `/api/v1/customers` | Create registered customer with allergy: "Ibuprofen" |
| 10.3 | POST | `/api/v1/customers` | Create insurance customer (linked to GLICO) |
| 10.4 | GET | `/api/v1/customers` | List all customers |
| 10.5 | GET | `/api/v1/customers/search` | Search by phone/name (POS lookup simulation) |
| 10.6 | GET | `/api/v1/customers/{id}` | Fetch single customer |
| 10.7 | PATCH | `/api/v1/customers/{id}` | Update customer contact info |
| 10.8 | POST | `/api/v1/customers/{id}/loyalty/award` | Manually award loyalty points |
| 10.9 | POST | `/api/v1/customers/{id}/loyalty/deduct` | Manually deduct loyalty points |
| 10.10 | POST | `/api/v1/prescriptions/` | Create prescription for registered customer (Amoxicillin, 2 refills) |
| 10.11 | GET | `/api/v1/prescriptions/` | List all prescriptions |
| 10.12 | GET | `/api/v1/prescriptions/customer/{id}` | List prescriptions for a specific customer |
| 10.13 | GET | `/api/v1/prescriptions/{id}` | Fetch single prescription |
| 10.14 | PATCH | `/api/v1/prescriptions/{id}` | Update prescriber info |
| 10.15 | POST | `/api/v1/prescriptions/{id}/refill` | Manually trigger a refill |
| 10.16 | PATCH | `/api/v1/prescriptions/{id}/cancel` | Cancel the prescription |
| 10.17 | DELETE | `/api/v1/prescriptions/{id}` | Delete a prescription |
| 10.18 | DELETE | `/api/v1/customers/{id}` | Soft-delete a test customer |

---

## Phase 11 — Suppliers & Purchase Orders

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 11.1 | POST | `/api/v1/suppliers/` | Create Supplier A |
| 11.2 | GET | `/api/v1/suppliers/` | List all suppliers |
| 11.3 | GET | `/api/v1/suppliers/{id}` | Fetch single supplier |
| 11.4 | PATCH | `/api/v1/suppliers/{id}` | Update supplier contact |
| 11.5 | POST | `/api/v1/purchase-orders/` | Create a PO (draft) for Paracetamol |
| 11.6 | GET | `/api/v1/purchase-orders/` | List all POs |
| 11.7 | GET | `/api/v1/purchase-orders/{id}` | Fetch PO details |
| 11.8 | POST | `/api/v1/purchase-orders/{id}/items` | Add line item to PO |
| 11.9 | GET | `/api/v1/purchase-orders/{id}/items` | List PO items |
| 11.10 | PATCH | `/api/v1/purchase-orders/{id}/items/{item_id}` | Update item quantity |
| 11.11 | DELETE | `/api/v1/purchase-orders/{id}/items/{item_id}` | Remove a line item |
| 11.12 | POST | `/api/v1/purchase-orders/{id}/submit` | Submit PO for approval |
| 11.13 | POST | `/api/v1/purchase-orders/{id}/reject` | Reject PO (test rejection path) |
| 11.14 | POST | `/api/v1/purchase-orders/` | Create another PO for full approval path |
| 11.15 | POST | `/api/v1/purchase-orders/{id}/submit` | Submit it |
| 11.16 | POST | `/api/v1/purchase-orders/{id}/approve` | Approve it |
| 11.17 | POST | `/api/v1/purchase-orders/{id}/receive` | Receive stock — creates DrugBatch, credits inventory |
| 11.18 | POST | `/api/v1/purchase-orders/{id}/cancel` | Cancel a draft PO |
| 11.19 | DELETE | `/api/v1/suppliers/{id}` | Delete unused supplier |

---

## Phase 12 — Online Sales (All Scenarios)

| # | Method | Endpoint | Scenario |
|---|---|---|---|
| 12.1 | POST | `/api/v1/sales/` | **Happy path** — OTC cash sale, walk-in customer, standard contract |
| 12.2 | POST | `/api/v1/sales/` | **FEFO multi-batch** — Ibuprofen qty exhausts Batch 1 and spills into Batch 2 |
| 12.3 | POST | `/api/v1/sales/` | **Prescription sale** — Amoxicillin, linked prescription, pharmacist user, decrement refills |
| 12.4 | POST | `/api/v1/sales/` | **Corporate contract** — 15% discount applied, verify discount_amount in response |
| 12.5 | POST | `/api/v1/sales/` | **Insurance sale** — GLICO contract, copay amount, insurance coverage fields |
| 12.6 | POST | `/api/v1/sales/` | **Split payment** — part cash, part mobile money |
| 12.7 | POST | `/api/v1/sales/` | **Allergy block** — customer allergic to Ibuprofen buys Ibuprofen, expect 400 + SystemAlert |
| 12.8 | POST | `/api/v1/sales/` | **Insufficient stock** — request more units than available, expect error |
| 12.9 | POST | `/api/v1/sales/` | **Idempotency** — replay sale 12.1 with same client_sale_id, expect same response, no duplicate |
| 12.10 | GET | `/api/v1/sales/` | List all sales, verify all completed sales appear |
| 12.11 | GET | `/api/v1/sales/{id}` | Fetch sale details including batch allocations |
| 12.12 | GET | `/api/v1/sales/{id}/receipt` | Fetch printable receipt |
| 12.13 | POST | `/api/v1/sales/{id}/cancel` | Cancel a draft/pending sale |
| 12.14 | POST | `/api/v1/sales/{id}/refund` | **Partial refund** — 1 of 3 units, verify stock partially restored |
| 12.15 | POST | `/api/v1/sales/{id}/refund` | **Full refund** — full quantity, stock fully restored + loyalty reversed |

---

## Phase 13 — Offline Sale Sync (All Protocols)

| # | Method | Endpoint | Scenario |
|---|---|---|---|
| 13.1 | GET | `/api/v1/sync/status` | Baseline sync status before any push |
| 13.2 | POST | `/api/v1/sync/pull` | Pull delta since epoch (first sync simulation) |
| 13.3 | POST | `/api/v1/sync/push` | **Offline sale replay** — push sale envelope with new client_sale_id |
| 13.4 | POST | `/api/v1/sync/push` | **Duplicate replay** — push same envelope again, verify idempotency receipt returned |
| 13.5 | POST | `/api/v1/sync/push` | **Stock conflict** — sale referencing drug at zero stock, verify clean rejection |
| 13.6 | POST | `/api/v1/sync/push` | **Stale prescription** — sale referencing cancelled prescription, verify rejection |
| 13.7 | POST | `/api/v1/sync/push-async` | Submit async push job |
| 13.8 | GET | `/api/v1/sync/push-async/{job_id}` | Poll job status until complete |
| 13.9 | POST | `/api/v1/sync/crr-push` | Push CRDT crsql_changes envelope (Branch 1 offline inventory mutation) |
| 13.10 | POST | `/api/v1/sync/crr-pull` | Pull merged CRDT state, verify pushed changes reflected |
| 13.11 | GET | `/api/v1/sync/status` | Verify sync watermarks updated after all pushes |
| 13.12 | POST | `/api/v1/sync/void-failed-sale` | Void a permanently rejected sale from the queue |

---

## Phase 14 — Admin Sync Recovery

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 14.1 | GET | `/api/v1/admin/sync-recovery/health` | Sync subsystem health |
| 14.2 | GET | `/api/v1/admin/sync-recovery/check-integrity` | Detect shadow DB vs Postgres divergence |
| 14.3 | GET | `/api/v1/admin/sync-recovery/report` | Full integrity report |
| 14.4 | POST | `/api/v1/admin/sync-recovery/fix-issue/{type}/{id}` | Fix a specific integrity issue found in 14.3 |
| 14.5 | POST | `/api/v1/admin/sync-recovery/bulk-fix` | Bulk-fix all detected issues |

---

## Phase 15 — Reports, Stats & Exports

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 15.1 | GET | `/api/v1/stats/{branch_id}` | Branch dashboard stats |
| 15.2 | GET | `/api/v1/stats/reports/summary` | Org-wide summary report |
| 15.3 | GET | `/api/v1/stats/reports/top-selling` | Top-selling drugs |
| 15.4 | GET | `/api/v1/reports/daily-sales-summary` | Sales aggregated by date/cashier/contract |
| 15.5 | GET | `/api/v1/reports/contract-performance` | Discount totals per contract |
| 15.6 | GET | `/api/v1/reports/drug-turnover` | Units sold + revenue per drug |
| 15.7 | GET | `/api/v1/reports/top-customers` | Loyalty tier distribution + top spenders |
| 15.8 | GET | `/api/v1/reports/inventory-alerts` | Low stock + expiring batch alerts |
| 15.9 | GET | `/api/v1/export/sales/excel` | Export sales as Excel |
| 15.10 | GET | `/api/v1/export/inventory/excel` | Export inventory as Excel |
| 15.11 | GET | `/api/v1/export/staff/excel` | Export staff as Excel |

---

## Phase 16 — Audit Trail

| # | Method | Endpoint | What it tests |
|---|---|---|---|
| 16.1 | GET | `/api/v1/audit/` | Full audit log — verify entries for every mutation across phases 1–15 |

---

## Coverage Summary

| Phase | Domain | Endpoints |
|---|---|---|
| 1 | Health | 3 |
| 2 | Organization | 10 |
| 3 | Auth | 16 |
| 4 | Roles & Users | 17 |
| 5 | Branches | 12 |
| 6 | Insurance Providers | 9 |
| 7 | Drug Catalog | 19 |
| 8 | Inventory & Batches | 20 |
| 9 | Contracts | 16 |
| 10 | Customers & Prescriptions | 18 |
| 11 | Suppliers & Purchase Orders | 19 |
| 12 | Online Sales | 15 |
| 13 | Offline Sync | 12 |
| 14 | Admin Sync Recovery | 5 |
| 15 | Reports, Stats & Exports | 11 |
| 16 | Audit | 1 |
| **Total** | | **203 scenario calls / 148 distinct endpoints** |
