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

import { getDb } from "@/lib/localDb";
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

    default:
      console.warn(`[localProjectors] No projector for event_type=${envelope.event_type}`);
  }

  void p; // suppress unused var warning — p may not be used in dispatch
}

// ── Customer projectors ────────────────────────────────────────────────────

async function _customerCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = new Date().toISOString();
  await db.execute(
    `INSERT OR IGNORE INTO customers
       (id, organization_id, first_name, last_name, phone, email,
        date_of_birth, address, allergies, chronic_conditions,
        customer_type, loyalty_points, loyalty_tier,
        preferred_contact_method, marketing_consent,
        is_active, is_deleted,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,1,0,
        'synced',1,NULL,$16,$16)`,
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
      e.authored_at ?? now,
    ]
  );
}

async function _customerUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
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
  const items = (p.items ?? []) as unknown[];
  const now = new Date().toISOString();

  // Write the sale row.
  await db.execute(
    `INSERT OR IGNORE INTO sales
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

  // Deduct stock for each item using FEFO batch changes if provided.
  const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
  if (batchChanges.length > 0) {
    for (const bc of batchChanges) {
      const batchId = String(bc.batch_id);
      const qty = Number(bc.quantity_used ?? 0);
      // Deduct from drug_batches.
      await db.execute(
        `UPDATE drug_batches
            SET quantity_remaining = MAX(0, quantity_remaining - $1)
          WHERE id = $2`,
        [qty, batchId]
      );
    }
  }

  // Deduct from branch_inventory (aggregate).
  if (p.drug_id != null && p.quantity != null) {
    await db.execute(
      `UPDATE branch_inventory
          SET quantity = MAX(0, quantity - $1)
        WHERE branch_id = $2 AND drug_id = $3`,
      [Number(p.quantity), String(p.branch_id ?? e.branch_id), String(p.drug_id)]
    );
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
          SET quantity_remaining = quantity_remaining + $1
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
          SET quantity_remaining = MAX(0, quantity_remaining + $1)
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
          SET quantity_remaining = MAX(0, quantity_remaining - $1)
        WHERE id = $2`,
      [batchQty, String(bc.batch_id)]
    );
  }
}
