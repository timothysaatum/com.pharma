/**
 * localProjectors.ts
 * ==================
 * TypeScript mirror of the server-side Python projectors.
 *
 * Each projector applies a single event type to the local SQLite read
 * model (same tables that already exist: sales, customers, prescriptions,
 * branch_inventory, drug_batches). All writes are idempotent — an event
 * can be replayed without producing a different final state.
 *
 * Called by pullEvents() in syncEngine.ts after receiving envelopes from
 * GET /sync/events. Also called at event-creation time so the UI reflects
 * local mutations immediately (optimistic local write before push).
 *
 * Per-event idempotency strategy:
 *   - Creates:  INSERT OR IGNORE (duplicate = no-op)
 *   - Updates:  UPDATE WHERE id = ? (duplicate = same state, no-op in effect)
 *   - Deletes:  UPDATE SET is_deleted=1 (idempotent)
 */

import { getDb, setVersionVector } from "@/lib/localDb";
import type { VectorClock } from "@/lib/localDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";

type Db = Awaited<ReturnType<typeof getDb>>;

// ── Dispatch ──────────────────────────────────────────────────────────────

/** Apply a single event to the local SQLite read model. */
export async function applyEventLocally(envelope: EventEnvelope): Promise<void> {
  const db = await getDb();
  const p = envelope.payload;

  switch (envelope.event_type) {
    // ── Customer ────────────────────────────────────────────────────────
    case "customer_created":
      await _customerCreated(db, envelope);
      break;
    case "customer_updated":
      await _customerUpdated(db, envelope);
      break;
    case "customer_deleted":
      await _customerDeleted(db, envelope);
      break;

    // ── Sale ────────────────────────────────────────────────────────────
    case "sale_created":
      await _saleCreated(db, envelope);
      break;
    case "sale_voided":
      await _saleVoided(db, envelope);
      break;

    // ── Prescription ────────────────────────────────────────────────────
    case "prescription_created":
      await _prescriptionCreated(db, envelope);
      break;
    case "prescription_updated":
      await _prescriptionUpdated(db, envelope);
      break;
    case "prescription_cancelled":
      await _prescriptionCancelled(db, envelope);
      break;
    case "prescription_refill_used":
      await _prescriptionRefillUsed(db, envelope);
      break;

    // ── Stock ───────────────────────────────────────────────────────────
    case "stock_adjusted":
      await _stockAdjusted(db, envelope);
      break;

    // ── Stock Transfer ──────────────────────────────────────────────────
    case "stock_transfer":
      await _stockTransfer(db, envelope);
      break;

    // ── Drug ────────────────────────────────────────────────────────────
    case "drug_created":
      await _drugCreated(db, envelope);
      break;
    case "drug_updated":
      await _drugUpdated(db, envelope);
      break;

    // ── Drug Category ───────────────────────────────────────────────────
    case "drug_category_created":
      await _drugCategoryCreated(db, envelope);
      break;
    case "drug_category_updated":
      await _drugCategoryUpdated(db, envelope);
      break;

    // ── Drug Batch ──────────────────────────────────────────────────────
    case "drug_batch_created":
    case "drug_batch_updated":
      await _drugBatchUpserted(db, envelope);
      break;

    // ── Branch Inventory ────────────────────────────────────────────────
    case "branch_inventory_created":
    case "branch_inventory_updated":
      await _branchInventoryUpserted(db, envelope);
      break;

    // ── Purchase Order ──────────────────────────────────────────────────
    case "purchase_order_created":
    case "purchase_order_updated":
      await _purchaseOrderUpserted(db, envelope);
      break;

    default:
      console.warn(`[localProjectors] No projector for event_type=${envelope.event_type}`);
  }

  void p; // suppress unused var warning — p may not be used in dispatch
}

// ── Customer projectors ────────────────────────────────────────────────────

async function _customerCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = new Date().toISOString();
  const incomingVector = (p.version_vector ?? {}) as VectorClock;
  await db.execute(
    `INSERT OR IGNORE INTO customers
       (id, organization_id, first_name, last_name, phone, email,
        date_of_birth, address, allergies, chronic_conditions,
        customer_type, loyalty_points, loyalty_tier,
        preferred_contact_method, marketing_consent,
        is_active, is_deleted, version_vector,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,1,0,$16,
        'synced',1,NULL,$17,$17)`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.first_name ?? ""),
      String(p.last_name ?? ""),
      p.phone != null ? String(p.phone) : null,
      p.email != null ? String(p.email) : null,
      p.date_of_birth != null ? String(p.date_of_birth) : null,
      p.address != null ? JSON.stringify(p.address) : null,
      p.allergies != null ? JSON.stringify(p.allergies) : null,
      p.chronic_conditions != null ? JSON.stringify(p.chronic_conditions) : null,
      String(p.customer_type ?? "registered"),
      Number(p.loyalty_points ?? 0),
      String(p.loyalty_tier ?? "bronze"),
      p.preferred_contact_method != null ? String(p.preferred_contact_method) : null,
      p.marketing_consent != null ? (p.marketing_consent ? 1 : 0) : 0,
      JSON.stringify(incomingVector),
      e.authored_at ?? now,
    ]
  );
}

async function _customerUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const incomingVector = (p.version_vector ?? {}) as VectorClock;

  // Apply only fields present in the payload (partial patch).
  const fields: [string, unknown][] = [];
  const UPDATABLE = [
    "first_name", "last_name", "phone", "email", "date_of_birth",
    "address", "allergies", "chronic_conditions",
    "loyalty_points", "loyalty_tier",
    "preferred_contact_method", "marketing_consent", "is_active",
    "insurance_provider_id", "insurance_member_id",
    "insurance_card_image_url", "preferred_contract_id",
  ];
  for (const key of UPDATABLE) {
    if (key in p) {
      const v = p[key];
      if (key === "address" || key === "allergies" || key === "chronic_conditions") {
        fields.push([key, v != null ? JSON.stringify(v) : null]);
      } else if (key === "marketing_consent" || key === "is_active") {
        fields.push([key, v ? 1 : 0]);
      } else {
        fields.push([key, v]);
      }
    }
  }
  if (fields.length === 0) return;
  const setClauses = fields.map((_, i) => `${fields[i][0]} = $${i + 1}`).join(", ");
  await db.execute(
    `UPDATE customers SET ${setClauses}, updated_at = $${fields.length + 1} WHERE id = $${fields.length + 2}`,
    [...fields.map((f) => f[1]), e.authored_at, String(e.aggregate_id)]
  );
  // Persist the authoritative vector so future offline edits read the correct base.
  if (Object.keys(incomingVector).length > 0) {
    await setVersionVector("customers", String(e.aggregate_id), incomingVector);
  }
}

async function _customerDeleted(db: Db, e: EventEnvelope): Promise<void> {
  await db.execute(
    "UPDATE customers SET is_deleted = 1, is_active = 0, updated_at = $1 WHERE id = $2",
    [e.authored_at, String(e.aggregate_id)]
  );
}

// ── Sale projectors ────────────────────────────────────────────────────────

async function _saleCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const items = (p.items ?? []) as Array<Record<string, unknown>>;
  const now = new Date().toISOString();

  // Idempotency guard: skip entirely if already applied (online sale the client
  // authored directly, or duplicate event delivery).
  const existing = await db.select<{ id: string }[]>(
    "SELECT id FROM sales WHERE id = $1",
    [String(e.aggregate_id)]
  );
  if (existing.length > 0) return;

  // Write the sale row.
  await db.execute(
    `INSERT INTO sales
       (id, organization_id, branch_id, sale_number, customer_id, customer_name,
        subtotal, discount_amount, tax_amount, total_amount,
        price_contract_id, contract_name, contract_discount_percentage,
        payment_method, payment_status, amount_paid, change_amount,
        payment_reference, prescription_id, prescription_number, prescriber_name,
        cashier_id, pharmacist_id,
        insurance_claim_number, patient_copay_amount, insurance_covered_amount,
        insurance_verified, insurance_verified_at, insurance_verified_by,
        notes, status, receipt_printed, receipt_emailed,
        items_json, items_count,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,
        $20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,
        'synced',1,NULL,$36,$36)`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.branch_id ?? e.branch_id),
      p.sale_number != null ? String(p.sale_number) : null,
      p.customer_id != null ? String(p.customer_id) : null,
      p.customer_name != null ? String(p.customer_name) : null,
      Number(p.subtotal ?? 0),
      Number(p.discount_amount ?? 0),
      Number(p.tax_amount ?? 0),
      Number(p.total_amount ?? 0),
      p.price_contract_id != null ? String(p.price_contract_id) : null,
      p.contract_name != null ? String(p.contract_name) : null,
      p.contract_discount_percentage != null ? Number(p.contract_discount_percentage) : null,
      String(p.payment_method ?? "cash"),
      String(p.payment_status ?? "paid"),
      p.amount_paid != null ? Number(p.amount_paid) : null,
      p.change_amount != null ? Number(p.change_amount) : null,
      p.payment_reference != null ? String(p.payment_reference) : null,
      p.prescription_id != null ? String(p.prescription_id) : null,
      p.prescription_number != null ? String(p.prescription_number) : null,
      p.prescriber_name != null ? String(p.prescriber_name) : null,
      String(p.cashier_id ?? e.authored_by),
      p.pharmacist_id != null ? String(p.pharmacist_id) : null,
      p.insurance_claim_number != null ? String(p.insurance_claim_number) : null,
      p.patient_copay_amount != null ? Number(p.patient_copay_amount) : null,
      p.insurance_covered_amount != null ? Number(p.insurance_covered_amount) : null,
      p.insurance_verified ? 1 : 0,
      p.insurance_verified_at != null ? String(p.insurance_verified_at) : null,
      p.insurance_verified_by != null ? String(p.insurance_verified_by) : null,
      p.notes != null ? String(p.notes) : null,
      String(p.status ?? "completed"),
      p.receipt_printed ? 1 : 0,
      p.receipt_emailed ? 1 : 0,
      JSON.stringify(items),
      items.length,
      e.authored_at ?? now,
    ]
  );

  // Deduct drug_batches via FEFO batch changes.
  const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
  for (const bc of batchChanges) {
    await db.execute(
      `UPDATE drug_batches
          SET remaining_quantity = MAX(0, remaining_quantity - $1)
        WHERE id = $2`,
      [Number(bc.quantity_used ?? 0), String(bc.batch_id)]
    );
  }

  // Deduct branch_inventory for every sale item.
  const branchId = String(p.branch_id ?? e.branch_id);
  for (const item of items) {
    if (item.drug_id != null && item.quantity != null) {
      await db.execute(
        `UPDATE branch_inventory
            SET quantity = MAX(0, quantity - $1)
          WHERE branch_id = $2 AND drug_id = $3`,
        [Number(item.quantity), branchId, String(item.drug_id)]
      );
    }
  }
}

async function _saleVoided(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  await db.execute(
    `UPDATE sales SET status = 'voided', updated_at = $1 WHERE id = $2`,
    [e.authored_at, String(e.aggregate_id)]
  );
  // Restore stock.
  const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
  for (const bc of batchChanges) {
    await db.execute(
      `UPDATE drug_batches
          SET remaining_quantity = remaining_quantity + $1
        WHERE id = $2`,
      [Number(bc.quantity_used ?? 0), String(bc.batch_id)]
    );
  }
  if (p.drug_id != null && p.quantity != null) {
    await db.execute(
      `UPDATE branch_inventory
          SET quantity = quantity + $1
        WHERE branch_id = $2 AND drug_id = $3`,
      [Number(p.quantity), String(p.branch_id ?? e.branch_id), String(p.drug_id)]
    );
  }
}

// ── Prescription projectors ────────────────────────────────────────────────

async function _prescriptionCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    `INSERT OR IGNORE INTO prescriptions
       (id, organization_id, branch_id, customer_id,
        prescription_number, prescriber_name, prescriber_license,
        diagnosis, notes, status, refills_remaining, refills_used,
        items_json,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
        'synced',1,NULL,$14,$14)`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.branch_id ?? e.branch_id),
      p.customer_id != null ? String(p.customer_id) : null,
      p.prescription_number != null ? String(p.prescription_number) : null,
      p.prescriber_name != null ? String(p.prescriber_name) : null,
      p.prescriber_license != null ? String(p.prescriber_license) : null,
      p.diagnosis != null ? String(p.diagnosis) : null,
      p.notes != null ? String(p.notes) : null,
      String(p.status ?? "active"),
      Number(p.refills_remaining ?? 0),
      0,
      JSON.stringify(p.medications ?? []),
      now,
    ]
  );
}

async function _prescriptionUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const UPDATABLE = [
    "prescriber_name", "prescriber_license", "diagnosis", "notes",
    "status", "refills_remaining",
  ];
  const fields: [string, unknown][] = [];
  // medications → items_json column (payload key differs from SQLite column name)
  if ("medications" in p) {
    fields.push(["items_json", JSON.stringify(p.medications)]);
  }
  for (const key of UPDATABLE) {
    if (key in p) {
      fields.push([key, p[key]]);
    }
  }
  if (fields.length === 0) return;
  const setClauses = fields.map((f, i) => `${f[0]} = $${i + 1}`).join(", ");
  await db.execute(
    `UPDATE prescriptions SET ${setClauses}, updated_at = $${fields.length + 1} WHERE id = $${fields.length + 2}`,
    [...fields.map((f) => f[1]), e.authored_at, String(e.aggregate_id)]
  );
}

async function _prescriptionCancelled(db: Db, e: EventEnvelope): Promise<void> {
  await db.execute(
    "UPDATE prescriptions SET status = 'cancelled', updated_at = $1 WHERE id = $2",
    [e.authored_at, String(e.aggregate_id)]
  );
}

async function _prescriptionRefillUsed(db: Db, e: EventEnvelope): Promise<void> {
  await db.execute(
    `UPDATE prescriptions
        SET refills_remaining = MAX(0, refills_remaining - 1),
            refills_used = refills_used + 1,
            updated_at = $1
      WHERE id = $2`,
    [e.authored_at, String(e.aggregate_id)]
  );
}

// ── Stock projectors ───────────────────────────────────────────────────────

async function _stockAdjusted(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const qtyChange = Number(p.quantity_change ?? 0);
  const drugId = p.drug_id != null ? String(p.drug_id) : null;
  const branchId = String(p.branch_id ?? e.branch_id);

  if (!drugId) return;

  await db.execute(
    `UPDATE branch_inventory
        SET quantity = MAX(0, quantity + $1)
      WHERE branch_id = $2 AND drug_id = $3`,
    [qtyChange, branchId, drugId]
  );

  // Apply batch-level changes if provided.
  const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
  for (const bc of batchChanges) {
    await db.execute(
      `UPDATE drug_batches
          SET remaining_quantity = MAX(0, remaining_quantity + $1)
        WHERE id = $2`,
      [Number(bc.quantity_change ?? 0), String(bc.batch_id)]
    );
  }
}

// ── Stock transfer projector ───────────────────────────────────────────────

async function _stockTransfer(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const qty = Number(p.quantity ?? 0);
  const drugId = p.drug_id != null ? String(p.drug_id) : null;
  const srcBranch = p.source_branch_id != null ? String(p.source_branch_id) : null;
  const dstBranch = p.destination_branch_id != null ? String(p.destination_branch_id) : null;

  if (!drugId || !srcBranch || !dstBranch) return;

  // Deduct source branch inventory.
  await db.execute(
    `UPDATE branch_inventory
        SET quantity = MAX(0, quantity - $1)
      WHERE branch_id = $2 AND drug_id = $3`,
    [qty, srcBranch, drugId]
  );

  // Credit destination branch inventory.
  await db.execute(
    `UPDATE branch_inventory
        SET quantity = quantity + $1
      WHERE branch_id = $2 AND drug_id = $3`,
    [qty, dstBranch, drugId]
  );

  // Apply per-batch movements.
  const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
  for (const bc of batchChanges) {
    const batchQty = Number(bc.quantity ?? 0);
    await db.execute(
      `UPDATE drug_batches
          SET remaining_quantity = MAX(0, remaining_quantity - $1)
        WHERE id = $2`,
      [batchQty, String(bc.batch_id)]
    );
  }
}

// ── Drug projectors ────────────────────────────────────────────────────────

async function _drugCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    `INSERT OR IGNORE INTO drugs
       (id, organization_id, name, generic_name, brand_name,
        sku, barcode, category_id, drug_type, dosage_form,
        strength, manufacturer, supplier,
        requires_prescription, controlled_substance_schedule,
        ndc_code, unit_price, cost_price, markup_percentage,
        tax_rate, reorder_level, reorder_quantity, max_stock_level,
        unit_of_measure, description, usage_instructions,
        side_effects, contraindications, storage_conditions,
        is_active, is_deleted,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,
        $20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,0,
        'synced',1,NULL,$31,$32)`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.name ?? ""),
      p.generic_name != null ? String(p.generic_name) : null,
      p.brand_name != null ? String(p.brand_name) : null,
      p.sku != null ? String(p.sku) : null,
      p.barcode != null ? String(p.barcode) : null,
      p.category_id != null ? String(p.category_id) : null,
      String(p.drug_type ?? "otc"),
      p.dosage_form != null ? String(p.dosage_form) : null,
      p.strength != null ? String(p.strength) : null,
      p.manufacturer != null ? String(p.manufacturer) : null,
      p.supplier != null ? String(p.supplier) : null,
      p.requires_prescription ? 1 : 0,
      p.controlled_substance_schedule != null ? String(p.controlled_substance_schedule) : null,
      p.ndc_code != null ? String(p.ndc_code) : null,
      Number(p.unit_price ?? 0),
      p.cost_price != null ? Number(p.cost_price) : null,
      p.markup_percentage != null ? Number(p.markup_percentage) : null,
      Number(p.tax_rate ?? 0),
      Number(p.reorder_level ?? 10),
      Number(p.reorder_quantity ?? 50),
      p.max_stock_level != null ? Number(p.max_stock_level) : null,
      String(p.unit_of_measure ?? "unit"),
      p.description != null ? String(p.description) : null,
      p.usage_instructions != null ? String(p.usage_instructions) : null,
      p.side_effects != null ? String(p.side_effects) : null,
      p.contraindications != null ? String(p.contraindications) : null,
      p.storage_conditions != null ? String(p.storage_conditions) : null,
      p.is_active !== false ? 1 : 0,
      p.updated_at != null ? String(p.updated_at) : now,
      p.created_at != null ? String(p.created_at) : now,
    ]
  );
}

async function _drugUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  await db.execute(
    `UPDATE drugs SET
       name = $1, generic_name = $2, brand_name = $3,
       sku = $4, barcode = $5, category_id = $6,
       drug_type = $7, dosage_form = $8, strength = $9,
       manufacturer = $10, supplier = $11,
       requires_prescription = $12, controlled_substance_schedule = $13,
       ndc_code = $14, unit_price = $15, cost_price = $16,
       markup_percentage = $17, tax_rate = $18,
       reorder_level = $19, reorder_quantity = $20, max_stock_level = $21,
       unit_of_measure = $22, description = $23, usage_instructions = $24,
       side_effects = $25, contraindications = $26, storage_conditions = $27,
       is_active = $28, sync_status = 'synced', updated_at = $29
     WHERE id = $30`,
    [
      String(p.name ?? ""),
      p.generic_name != null ? String(p.generic_name) : null,
      p.brand_name != null ? String(p.brand_name) : null,
      p.sku != null ? String(p.sku) : null,
      p.barcode != null ? String(p.barcode) : null,
      p.category_id != null ? String(p.category_id) : null,
      String(p.drug_type ?? "otc"),
      p.dosage_form != null ? String(p.dosage_form) : null,
      p.strength != null ? String(p.strength) : null,
      p.manufacturer != null ? String(p.manufacturer) : null,
      p.supplier != null ? String(p.supplier) : null,
      p.requires_prescription ? 1 : 0,
      p.controlled_substance_schedule != null ? String(p.controlled_substance_schedule) : null,
      p.ndc_code != null ? String(p.ndc_code) : null,
      Number(p.unit_price ?? 0),
      p.cost_price != null ? Number(p.cost_price) : null,
      p.markup_percentage != null ? Number(p.markup_percentage) : null,
      Number(p.tax_rate ?? 0),
      Number(p.reorder_level ?? 10),
      Number(p.reorder_quantity ?? 50),
      p.max_stock_level != null ? Number(p.max_stock_level) : null,
      String(p.unit_of_measure ?? "unit"),
      p.description != null ? String(p.description) : null,
      p.usage_instructions != null ? String(p.usage_instructions) : null,
      p.side_effects != null ? String(p.side_effects) : null,
      p.contraindications != null ? String(p.contraindications) : null,
      p.storage_conditions != null ? String(p.storage_conditions) : null,
      p.is_active !== false ? 1 : 0,
      p.updated_at != null ? String(p.updated_at) : (e.authored_at ?? new Date().toISOString()),
      String(e.aggregate_id),
    ]
  );
}

// ── Drug category projectors ───────────────────────────────────────────────

async function _drugCategoryCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    `INSERT OR IGNORE INTO drug_categories
       (id, organization_id, name, description,
        parent_id, path, level, is_deleted,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES ($1,$2,$3,$4,$5,$6,$7,0,'synced',1,NULL,$8,$9)`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.name ?? ""),
      p.description != null ? String(p.description) : null,
      p.parent_id != null ? String(p.parent_id) : null,
      p.path != null ? String(p.path) : null,
      Number(p.level ?? 0),
      p.updated_at != null ? String(p.updated_at) : now,
      p.created_at != null ? String(p.created_at) : now,
    ]
  );
}

async function _drugCategoryUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  await db.execute(
    `UPDATE drug_categories SET
       name = $1, description = $2,
       parent_id = $3, path = $4, level = $5,
       sync_status = 'synced', updated_at = $6
     WHERE id = $7`,
    [
      String(p.name ?? ""),
      p.description != null ? String(p.description) : null,
      p.parent_id != null ? String(p.parent_id) : null,
      p.path != null ? String(p.path) : null,
      Number(p.level ?? 0),
      p.updated_at != null ? String(p.updated_at) : (e.authored_at ?? new Date().toISOString()),
      String(e.aggregate_id),
    ]
  );
}

// ── Drug Batch projectors ───────────────────────────────────────────────────

async function _drugBatchUpserted(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  const remainingQuantity = Number(p.remaining_quantity ?? 0);
  const branchId = String(p.branch_id ?? e.branch_id);
  const drugId = String(p.drug_id ?? "");

  if (e.event_type === "drug_batch_updated") {
    const existing = await db.select<{ remaining_quantity: number }[]>(
      "SELECT remaining_quantity FROM drug_batches WHERE id = $1",
      [String(e.aggregate_id)]
    );
    if (existing.length > 0) {
      const delta = remainingQuantity - existing[0].remaining_quantity;
      if (delta !== 0) {
        await db.execute(
          `UPDATE branch_inventory
           SET quantity = quantity + $1, updated_at = $2
           WHERE branch_id = $3 AND drug_id = $4`,
          [delta, now, branchId, drugId]
        );
      }
    }
  } else if (e.event_type === "drug_batch_created") {
    const res = await db.execute(
      `UPDATE branch_inventory
       SET quantity = quantity + $1, updated_at = $2
       WHERE branch_id = $3 AND drug_id = $4`,
      [remainingQuantity, now, branchId, drugId]
    );
    if (res.rowsAffected === 0) {
      await db.execute(
        `INSERT INTO branch_inventory
           (id, branch_id, drug_id, quantity, reserved_quantity, location, selling_price,
            sync_status, sync_version, synced_at, updated_at, created_at)
         VALUES ($1,$2,$3,$4,0,NULL,NULL,'synced',1,NULL,$5,$5)`,
        [crypto.randomUUID(), branchId, drugId, remainingQuantity, now]
      );
    }
  }

  await db.execute(
    `INSERT INTO drug_batches
       (id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
        manufacturing_date, expiry_date, cost_price, selling_price,
        supplier, purchase_order_id,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'synced',1,NULL,$13,$14)
     ON CONFLICT(id) DO UPDATE SET
       batch_number=excluded.batch_number,
       quantity=excluded.quantity,
       remaining_quantity=excluded.remaining_quantity,
       manufacturing_date=excluded.manufacturing_date,
       expiry_date=excluded.expiry_date,
       cost_price=excluded.cost_price,
       selling_price=excluded.selling_price,
       supplier=excluded.supplier,
       purchase_order_id=excluded.purchase_order_id,
       updated_at=excluded.updated_at,
       sync_status='synced'`,
    [
      String(e.aggregate_id),
      branchId,
      drugId,
      String(p.batch_number ?? ""),
      Number(p.quantity ?? 0),
      remainingQuantity,
      p.manufacturing_date != null ? String(p.manufacturing_date) : null,
      String(p.expiry_date ?? ""),
      p.cost_price != null ? Number(p.cost_price) : null,
      p.selling_price != null ? Number(p.selling_price) : null,
      p.supplier != null ? String(p.supplier) : null,
      p.purchase_order_id != null ? String(p.purchase_order_id) : null,
      now,
      p.received_date != null ? String(p.received_date) : now
    ]
  );
}

// ── Branch Inventory projectors ─────────────────────────────────────────────

async function _branchInventoryUpserted(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  
  if (e.event_type === "branch_inventory_created") {
    await db.execute(
      `INSERT OR IGNORE INTO branch_inventory
         (id, branch_id, drug_id, quantity, reserved_quantity, location, selling_price,
          sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1,$2,$3,0,0,$4,$5,'synced',1,NULL,$6,$6)`,
      [
        String(e.aggregate_id),
        String(p.branch_id ?? e.branch_id),
        String(p.drug_id ?? ""),
        p.shelf_location != null ? String(p.shelf_location) : null,
        p.branch_selling_price != null ? Number(p.branch_selling_price) : null,
        now
      ]
    );
  } else if (e.event_type === "branch_inventory_updated") {
    await db.execute(
      `UPDATE branch_inventory SET
         location = $1, selling_price = $2, updated_at = $3, sync_status = 'synced'
       WHERE id = $4`,
      [
        p.shelf_location != null ? String(p.shelf_location) : null,
        p.branch_selling_price != null ? Number(p.branch_selling_price) : null,
        now,
        String(e.aggregate_id)
      ]
    );
  }
}

// ── Purchase Order projectors ───────────────────────────────────────────────

async function _purchaseOrderUpserted(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  
  await db.execute(
    `INSERT INTO purchase_orders
       (id, organization_id, branch_id, po_number, supplier_id,
        subtotal, tax_amount, shipping_cost, total_amount, status,
        ordered_by, approved_by, approved_at, expected_delivery_date, received_date,
        notes, items_json,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,'synced',1,NULL,$18,$19)
     ON CONFLICT(id) DO UPDATE SET
       po_number=excluded.po_number,
       supplier_id=excluded.supplier_id,
       status=excluded.status,
       received_date=excluded.received_date,
       notes=excluded.notes,
       items_json=excluded.items_json,
       updated_at=excluded.updated_at,
       sync_status='synced'`,
    [
      String(e.aggregate_id),
      String(p.org_id ?? e.org_id),
      String(p.branch_id ?? e.branch_id),
      String(p.po_number ?? ""),
      String(p.supplier_name ?? p.supplier_id ?? "unknown"),
      Number(p.subtotal ?? 0),
      Number(p.tax_amount ?? 0),
      Number(p.shipping_cost ?? 0),
      Number(p.total_amount ?? 0),
      String(p.status ?? "draft"),
      String(p.ordered_by ?? e.authored_by),
      p.approved_by != null ? String(p.approved_by) : null,
      p.approved_at != null ? String(p.approved_at) : null,
      p.expected_delivery_date != null ? String(p.expected_delivery_date) : null,
      p.received_at != null ? String(p.received_at) : null,
      p.notes != null ? String(p.notes) : null,
      JSON.stringify(p.items ?? []),
      now,
      p.ordered_at != null ? String(p.ordered_at) : now
    ]
  );
}
