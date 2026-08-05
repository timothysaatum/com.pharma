# Phase 2 — CRR Migration Discovery Report

> **Date:** 2026-07-10
> **Status:** DRAFT — awaiting merge-strategy sign-off per table

---

## 1. Infrastructure Status (Phase 1)

### 1.1 Reconciliation Loop — ✅ Generic

`_reconcile_all_tables()` in `main.py:129` iterates over `_CRR_TABLES` dict keys
generically — adding a new table requires no wiring changes.

### 1.2 `_merge_conflicting_rows()` — ❌ Hardcoded for `branch_inventory`

`shadow_db.py:351` references `quantity`, `reserved_quantity`, `location`,
`selling_price` by name. **Must be generalised** before Phase 3 to accept a
table-specific merge config (strategy + column names).

### 1.3 `_CRR_TABLES` Dict — Currently `{ "branch_inventory": DDL }`

Every new table needs:
- A `"table_name": "DDL string"` entry in `_CRR_TABLES` (`shadow_db.py:33`)
- A `"table_name"` entry in the client-side `CRR_TABLES` set (`localDb.ts:708`)
- A client migration to `SELECT crsql_as_crr('table_name')`
- A server migration to remove Postgres UNIQUE constraints (if any)

### 1.4 Postgres Test Environment — ✅ Exists

`tests/e2e_crr_sync_pg.py` verifies upsert, type coercion, duplicate detection,
FK enforcement, and crash recovery against real Postgres. Reusable for every
table with a `_pg_upsert` row dict per table.

---

## 2. Per-Table Schema Audit

### Table A — `drug_batches`

| Property | Detail |
|---|---|
| **Postgres table** | `drug_batches` |
| **Client SQLite table** | `drug_batches` |
| **PK** | `id TEXT NOT NULL` — ✅ cr-sqlite compatible |
| **UNIQUE constraints beyond PK** | `uq_branch_drug_batch (branch_id, drug_id, batch_number)` — ❌ **BLOCKER** must be removed |
| **NOT NULL columns missing DEFAULT** | `branch_id`, `drug_id`, `batch_number`, `quantity`, `remaining_quantity`, `expiry_date`, `updated_at`, `created_at` |
| **DEFAULTs to add** | `updated_at TEXT NOT NULL DEFAULT ''`, `created_at` same; others need defaults per client schema |
| **Client columns (subset of Postgres)** | All same except Postgres has `last_synced_at`, `sync_hash` (not in client) |
| **Items** | No child-table on client; items are the row itself |
| **Child tables on server** | `inventory_movements.batch_id` (cascade), `sale_items.batch_id` (SET NULL), `sale_item_batch_allocations.batch_id` (SET NULL) — all server-only, no FK concern |
| **Natural business key** | `(branch_id, drug_id, batch_number)` |
| **Collision scenario** | Two offline clients receive stock of the same drug+batch from different suppliers and both create a row |
| **Proposed merge strategy** | **Sum** for `quantity` + `remaining_quantity`; **newest-wins** for `cost_price`, `selling_price`, `supplier`, `expiry_date`. **🟡 Needs business sign-off** |
| **Risk rating** | **Low** — no customer data, no financial impact, simple schema |

<details>
<summary>Client DDL (matches shadow proposal)</summary>

```sql
CREATE TABLE drug_batches (
    id                  TEXT NOT NULL PRIMARY KEY,
    branch_id           TEXT NOT NULL DEFAULT '',
    drug_id             TEXT NOT NULL DEFAULT '',
    batch_number        TEXT NOT NULL DEFAULT '',
    quantity            INTEGER NOT NULL DEFAULT 0,
    remaining_quantity  INTEGER NOT NULL DEFAULT 0,
    manufacturing_date  TEXT,
    expiry_date         TEXT NOT NULL DEFAULT '',
    cost_price          REAL,
    selling_price       REAL,
    supplier            TEXT,
    purchase_order_id   TEXT,
    sync_status         TEXT NOT NULL DEFAULT 'synced',
    sync_version        INTEGER NOT NULL DEFAULT 1,
    synced_at           TEXT,
    updated_at          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT ''
);
```
</details>

---

### Table B — `customers`

| Property | Detail |
|---|---|
| **Postgres table** | `customers` |
| **Client SQLite table** | `customers` |
| **PK** | `id TEXT NOT NULL` — ✅ |
| **UNIQUE constraints beyond PK** | None — ✅ no blocker |
| **NOT NULL columns missing DEFAULT** | `organization_id`, `updated_at`, `created_at` |
| **DEFAULTs to add** | Minor; client schema already has `DEFAULT` on most columns |
| **Client vs Server columns** | Client has fewer columns (no address/medical data/allergies/chronic conditions etc.) — shadow only needs client columns |
| **Child tables** | `sales.customer_id` (SET NULL), `prescriptions.customer_id` (RESTRICT) |
| **Natural business key** | Phone + email (soft-match, not a DB constraint) |
| **Collision scenario** | Two branches create the same walk-in customer offline with different IDs |
| **Existing merge logic** | Server already has customer-merge-by-phone/email logic in `CrrSyncService` |
| **Proposed merge strategy** | **Last-write-wins** on the customer row; the existing **post-merge phone/email dedup** runs separately. **🟡 Needs business sign-off** |
| **Risk rating** | **Low** — merge logic already exists, no unique constraints to remove |

<details>
<summary>Client DDL (matches shadow proposal)</summary>

```sql
CREATE TABLE customers (
    id                      TEXT NOT NULL PRIMARY KEY,
    organization_id         TEXT NOT NULL DEFAULT '',
    customer_type           TEXT NOT NULL DEFAULT 'walk_in',
    first_name              TEXT,
    last_name               TEXT,
    phone                   TEXT,
    email                   TEXT,
    date_of_birth           TEXT,
    loyalty_points          INTEGER NOT NULL DEFAULT 0,
    loyalty_tier            TEXT NOT NULL DEFAULT 'bronze',
    insurance_provider_id   TEXT,
    insurance_member_id     TEXT,
    preferred_contract_id   TEXT,
    is_active               INTEGER NOT NULL DEFAULT 1,
    is_deleted              INTEGER NOT NULL DEFAULT 0,
    sync_status             TEXT NOT NULL DEFAULT 'synced',
    sync_version            INTEGER NOT NULL DEFAULT 1,
    synced_at               TEXT,
    updated_at              TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL DEFAULT ''
);
```
</details>

---

### Table C — `prescriptions`

| Property | Detail |
|---|---|
| **Postgres table** | `prescriptions` |
| **Client SQLite table** | `prescriptions` |
| **PK** | `id TEXT NOT NULL` — ✅ |
| **UNIQUE constraints beyond PK** | Client: `prescription_number UNIQUE` — ❌ **BLOCKER**. Postgres: `uq_prescription_org_number (organization_id, prescription_number)` — ❌ **BLOCKER** |
| **NOT NULL columns missing DEFAULT** | `organization_id`, `branch_id`, `prescription_number`, `customer_id`, `prescriber_name`, `prescriber_license`, `issue_date`, `expiry_date`, `medications`, `updated_at`, `created_at` |
| **DEFAULTs to add** | Client already has most defaults; `medications` needs `DEFAULT '[]'` |
| **Child tables** | `sales.prescription_id` (SET NULL) — server-only |
| **Natural business key** | `(organization_id, prescription_number)` |
| **Collision scenario** | Two different prescribers issue different prescriptions with the same number at different branches offline |
| **Proposed merge strategy** | **Last-write-wins** — prescription is a self-contained document (`medications` is JSON). If number collides, newer _updated_at_ wins. **🟡 Needs business sign-off** |
| **Risk rating** | **Low-Medium** — prescriptions are regulatory documents; merge should be conservative |

<details>
<summary>Client DDL (matches shadow proposal)</summary>

```sql
CREATE TABLE prescriptions (
    id                    TEXT NOT NULL PRIMARY KEY,
    organization_id       TEXT NOT NULL DEFAULT '',
    branch_id             TEXT NOT NULL DEFAULT '',
    prescription_number   TEXT NOT NULL DEFAULT '',
    customer_id           TEXT NOT NULL DEFAULT '',
    prescriber_name       TEXT NOT NULL DEFAULT '',
    prescriber_license    TEXT NOT NULL DEFAULT '',
    prescriber_phone      TEXT,
    prescriber_address    TEXT,
    issue_date            TEXT NOT NULL DEFAULT '',
    expiry_date           TEXT NOT NULL DEFAULT '',
    medications           TEXT NOT NULL DEFAULT '[]',
    diagnosis             TEXT,
    notes                 TEXT,
    special_instructions  TEXT,
    refills_allowed       INTEGER NOT NULL DEFAULT 0,
    refills_remaining     INTEGER NOT NULL DEFAULT 0,
    last_refill_date      TEXT,
    status                TEXT NOT NULL DEFAULT 'active',
    verified_by           TEXT,
    verified_at           TEXT,
    sync_status           TEXT NOT NULL DEFAULT 'synced',
    sync_version          INTEGER NOT NULL DEFAULT 1,
    synced_at             TEXT,
    updated_at            TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT ''
);
```
</details>

---

### Table D — `purchase_orders`

| Property | Detail |
|---|---|
| **Postgres table** | `purchase_orders` |
| **Client SQLite table** | `purchase_orders` |
| **PK** | `id TEXT NOT NULL` — ✅ |
| **UNIQUE constraints beyond PK** | Postgres: `uq_po_branch_number (branch_id, po_number)` — ❌ **BLOCKER**. Client has NO UNIQUE on `po_number` (no explicit UNIQUE in SQLite DDL) |
| **NOT NULL columns missing DEFAULT** | `organization_id`, `branch_id`, `po_number`, `supplier_id`, `subtotal`, `total_amount`, `ordered_by`, `updated_at`, `created_at` |
| **DEFAULTs to add** | Client already has `DEFAULT 0` on money columns; others need defaults |
| **Items** | Embedded as JSON in `items_json` TEXT column on client |
| **Child tables** | `purchase_order_items.purchase_order_id` (CASCADE, server-only), `drug_batches.purchase_order_id` (SET NULL) |
| **Natural business key** | `(branch_id, po_number)` |
| **Collision scenario** | Two branch staff create purchase orders with the same PO number offline |
| **Proposed merge strategy** | **Last-write-wins** for header + `items_json`. The `items_json` is atomic — not merged field-by-field. **🟡 Needs business sign-off** |
| **Risk rating** | **Medium** — affects financial records, links to drug_batches |

<details>
<summary>Client DDL (matches shadow proposal)</summary>

```sql
CREATE TABLE purchase_orders (
    id                    TEXT NOT NULL PRIMARY KEY,
    organization_id       TEXT NOT NULL DEFAULT '',
    branch_id             TEXT NOT NULL DEFAULT '',
    po_number             TEXT NOT NULL DEFAULT '',
    supplier_id           TEXT NOT NULL DEFAULT '',
    subtotal              REAL NOT NULL DEFAULT 0,
    tax_amount            REAL NOT NULL DEFAULT 0,
    shipping_cost         REAL NOT NULL DEFAULT 0,
    total_amount          REAL NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'draft',
    ordered_by            TEXT NOT NULL DEFAULT '',
    approved_by           TEXT,
    approved_at           TEXT,
    expected_delivery_date TEXT,
    received_date         TEXT,
    notes                 TEXT,
    items_json            TEXT NOT NULL DEFAULT '[]',
    sync_status           TEXT NOT NULL DEFAULT 'synced',
    sync_version          INTEGER NOT NULL DEFAULT 1,
    synced_at             TEXT,
    updated_at            TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT ''
);
```
</details>

---

### Table E — `sales`

| Property | Detail |
|---|---|
| **Postgres table** | `sales` |
| **Client SQLite table** | `sales` |
| **PK** | `id TEXT NOT NULL` — ✅ |
| **UNIQUE constraints beyond PK** | Client: `sale_number UNIQUE` — ❌ **BLOCKER**. Postgres: `uq_sale_branch_number (branch_id, sale_number)` — ❌ **BLOCKER** |
| **NOT NULL columns missing DEFAULT** | `organization_id`, `branch_id`, `sale_number`, `subtotal`, `total_amount`, `cashier_id`, `updated_at`, `created_at` |
| **DEFAULTs to add** | Client already has `NOT NULL DEFAULT 0` on most money columns; others need defaults |
| **Items** | Embedded as JSON in `items_json` TEXT on client |
| **Child tables** | `sale_items.sale_id` (CASCADE) — server-only; no client-side child table |
| **Natural business key** | `(branch_id, sale_number)` |
| **Collision scenario** | Two cashiers at the same branch generate different sales offline; sale_number counter diverges and collides |
| **Proposed merge strategy** | **Last-write-wins** for header + `items_json` atomically. Items are embedded JSON — no field-level merge across line items. **🟡 Needs business sign-off** — this is the highest-impact table because sales are financial records |
| **Risk rating** | **High** — financial records, affects accounting, most columns of any table |

<details>
<summary>Client DDL (matches shadow proposal)</summary>

```sql
CREATE TABLE sales (
    id                            TEXT NOT NULL PRIMARY KEY,
    organization_id               TEXT NOT NULL DEFAULT '',
    branch_id                     TEXT NOT NULL DEFAULT '',
    sale_number                   TEXT NOT NULL DEFAULT '',
    customer_id                   TEXT,
    customer_name                 TEXT,
    subtotal                      REAL NOT NULL DEFAULT 0,
    discount_amount               REAL NOT NULL DEFAULT 0,
    tax_amount                    REAL NOT NULL DEFAULT 0,
    total_amount                  REAL NOT NULL DEFAULT 0,
    price_contract_id             TEXT,
    contract_name                 TEXT,
    contract_discount_percentage  REAL,
    contract_type                 TEXT,
    payment_method                TEXT NOT NULL DEFAULT 'cash',
    payment_status                TEXT NOT NULL DEFAULT 'completed',
    amount_paid                   REAL,
    change_amount                 REAL NOT NULL DEFAULT 0,
    payment_reference             TEXT,
    split_payment_details         TEXT,
    insurance_preauth_number      TEXT,
    prescription_id               TEXT,
    prescription_number           TEXT,
    prescriber_name               TEXT,
    prescriber_license            TEXT,
    cashier_id                    TEXT NOT NULL DEFAULT '',
    pharmacist_id                 TEXT,
    insurance_claim_number        TEXT,
    patient_copay_amount          REAL,
    insurance_covered_amount      REAL,
    insurance_verified            INTEGER NOT NULL DEFAULT 0,
    insurance_verified_at         TEXT,
    insurance_verified_by         TEXT,
    notes                         TEXT,
    status                        TEXT NOT NULL DEFAULT 'completed',
    cancelled_at                  TEXT,
    cancelled_by                  TEXT,
    cancellation_reason           TEXT,
    refund_amount                 REAL,
    refunded_at                   TEXT,
    refunded_by                   TEXT,
    refund_reason                 TEXT,
    refund_reference              TEXT,
    receipt_printed               INTEGER NOT NULL DEFAULT 0,
    receipt_emailed               INTEGER NOT NULL DEFAULT 0,
    items_json                    TEXT NOT NULL DEFAULT '[]',
    items_count                   INTEGER NOT NULL DEFAULT 0,
    sync_status                   TEXT NOT NULL DEFAULT 'synced',
    sync_version                  INTEGER NOT NULL DEFAULT 1,
    synced_at                     TEXT,
    updated_at                    TEXT NOT NULL DEFAULT '',
    created_at                    TEXT NOT NULL DEFAULT ''
);
```
</details>

---

## 3. Migration Order Recommendation

| Order | Table | Rationale |
|---|---|---|
| **1** | `drug_batches` | Simple schema, no real merge conflicts (sum is safe), no customer data, no financial impact. FK dependents are server-only (no orphan risk). Quick win. |
| **2** | `customers` | Already has server-side merge logic. No UNIQUE constraints to remove. Low blast radius — a wrong merge just means duplicate customers (status quo). |
| **3** | `prescriptions` | One FK dependent (`sales`, SET NULL). UNIQUE constraint to remove. `medications` is JSON so the row is atomic. |
| **4** | `purchase_orders` | FKs to `drug_batches` (SET NULL) and `purchase_order_items` (CASCADE, server-only). `items_json` is atomic. |
| **5** | `sales` | Most complex. Financial records, largest column set, most FK dependents. Leave until last to benefit from lessons learned on earlier migrations. |

### Justification

- **drug_batches first** because it has the clearest merge semantics (sum quantities, newest-wins metadata) and no downstream blast radius.
- **customers second** because it requires the least schema surgery (no UNIQUE constraints to remove) and has pre-existing merge logic.
- **sales last** because a wrong merge strategy has financial implications. By the time we reach it, all the infrastructure kinks should be worked out.

---

## 4. Pre-Implementation Checklist (for Phase 3)

For each table, before implementation:

- [ ] Add client migration: `SELECT crsql_as_crr('table_name')` + add to `CRR_TABLES` set
- [ ] Add shadow DDL to `_CRR_TABLES` dict
- [ ] Remove UNIQUE constraints from Postgres (if any)
- [ ] Add Postgres migration for any needed DEFAULT changes
- [ ] Remove UNIQUE constraint from client SQLite (if any)
- [ ] Add `enqueue()` skip in legacy sync for this table
- [ ] Generalise `_merge_conflicting_rows()` to accept per-table merge config
- [ ] Add e2e test scenario(s) for the new table
- [ ] Run full Postgres e2e suite
- [ ] Run full pytest suite

---

## 5. Merge Strategies Requiring Your Sign-Off

| Table | Strategy | Why this strategy |
|---|---|---|
| `drug_batches` | **Sum** for quantities, **newest-wins** for metadata | Quantities from two branches receiving the same batch should combine; metadata (price, supplier) is authoritative per the latest update |
| `customers` | **Last-write-wins** + existing server-side dedup | Customer record is a document; phone/email dedup already handles the only meaningful collision |
| `prescriptions` | **Last-write-wins** | Prescription is a self-contained document with JSON `medications`; collisions should be rare |
| `purchase_orders` | **Last-write-wins** for header + items atomic | `items_json` is embedded — no field-level merge across line items; PO header is a document |
| `sales` | **Last-write-wins** for header + items atomic | Same as purchase_orders; items are embedded. Financial records must be conservative — never sum amounts |

**Please review and either approve or override each strategy before Phase 3 begins.**
