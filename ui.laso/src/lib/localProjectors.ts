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

import { getDb, setVersionVector, markEventApplied } from "@/lib/localDb";
import type { VectorClock } from "@/lib/localDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";
import { refreshQuantities, refreshQuantitiesForPairs } from "@/lib/sellableQty";
import { appEvents } from "@/lib/events";

type Db = Awaited<ReturnType<typeof getDb>>;

/**
 * Tell the UI the local prescriptions read model moved.
 *
 * Without this the Prescriptions page had no way to learn that a refill had been
 * consumed: it subscribes to nothing, and the event type did not even exist. The
 * `reason` is accepted and currently ignored by the bus (it carries no payload);
 * it is here so the call site documents itself at the point of the write.
 */
function emitPrescriptionsChanged(): void {
  appEvents.emit("prescriptions:changed");
}

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
    case "customer_loyalty_changed":
      await _customerLoyaltyChanged(db, envelope);
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
    // ── Price Contract ──────────────────────────────────────────────────
    case "price_contract_created":
      await _priceContractCreated(db, envelope);
      break;
    case "price_contract_updated":
      await _priceContractUpdated(db, envelope);
      break;
    case "price_contract_deleted":
      await _priceContractDeleted(db, envelope);
      break;

    default:
      console.warn(`[localProjectors] No projector for event_type=${envelope.event_type}`);
  }

  void p; // suppress unused var warning — p may not be used in dispatch
}

// ── Replay guard (non-idempotent projectors only) ───────────────────────────

/**
 * Event types whose local projectors apply raw increments and therefore
 * double-apply if the same event is delivered twice.
 *
 * A cursor reset replays the log, and the server may re-deliver events the
 * device already processed. Every other projector is an upsert, or guarded by
 * an existence check (`_saleCreated` checks `sales`), so replaying is harmless.
 * These four are not, so they consult `applied_events` first.
 *
 * Trade-off: the guard table only receives rows for these four types, so it
 * stays small. The residual gap is documented — a device that applied one of
 * these events BEFORE this table existed has no marker, so replaying that
 * event still double-applies it once. The organisation's current 69-event log
 * contains none of these types, so nothing is currently exposed; the exposure
 * is for future logs that do.
 */
const REPLAY_GUARDED_TYPES = new Set([
  "sale_voided",
  "prescription_refill_used",
  "stock_adjusted",
  "stock_transfer",
]);

/**
 * Returns true when this event must be SKIPPED because it was already applied.
 *
 * When it returns false the caller must call {@link recordGuardedApply} in the
 * same transaction as its mutations, so the marker and the mutation commit or
 * roll back together. Writing the marker without the mutation would suppress
 * the mutation on the next replay.
 */
async function shouldSkipReplay(e: EventEnvelope): Promise<boolean> {
  if (!REPLAY_GUARDED_TYPES.has(e.event_type)) return false;
  if (!e.event_id) return false;
  const db = await getDb();
  const rows = await db.select<{ event_id: string }[]>(
    "SELECT event_id FROM applied_events WHERE event_id = $1 LIMIT 1",
    [String(e.event_id)]
  );
  return rows.length > 0;
}

/** Mark a guarded event applied. Call inside the same transaction as the mutation. */
async function recordGuardedApply(e: EventEnvelope): Promise<void> {
  if (!REPLAY_GUARDED_TYPES.has(e.event_type)) return;
  if (!e.event_id) return;
  const db = await getDb();
  await markEventApplied(db, {
    event_id: String(e.event_id),
    org_id: e.org_id ?? "",
    seq: e.seq ?? 0,
    event_type: e.event_type,
    aggregate_id: e.aggregate_id ?? "",
  });
}

// ── Customer projectors ────────────────────────────────────────────────────

async function _customerCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = new Date().toISOString();
  const incomingVector = (p.version_vector ?? {}) as VectorClock;

  // Organization scope is fail-closed, mirroring the server's CustomerProjector
  // (`_validate_created`, which rejects a payload/envelope disagreement).
  //
  // The device table has no foreign key on organization_id, so a payload that
  // names another org used to be written verbatim: the row landed under the
  // wrong tenant, `INSERT OR IGNORE` then made the correct-org event a silent
  // no-op, and `organization_id` is absent from the `_customerUpdated` UPDATABLE
  // list — so the misattribution could never be repaired by any later event.
  const envelopeOrg = String(e.org_id ?? "");
  const payloadOrg = p.organization_id == null ? envelopeOrg : String(p.organization_id);
  if (payloadOrg !== envelopeOrg) {
    const msg =
      `customer_created ${e.event_id}: payload.organization_id (${payloadOrg}) does not ` +
      `match envelope.org_id (${envelopeOrg}). Refusing to insert a cross-org customer.`;
    console.error(`[localProjectors] ${msg}`);
    // Throwing is what makes this durable: syncEngine catches it and calls
    // recordEventProjectionFailure, then advances the cursor past the poison
    // event instead of freezing the device on it.
    throw new Error(msg);
  }

  // A same-id row that already exists under ANOTHER org is a collision we must
  // not paper over with INSERT OR IGNORE — that is exactly how the stale
  // foreign-org row survived in the first place. Fail loudly instead.
  const existing = await db.select<{ organization_id: string }[]>(
    "SELECT organization_id FROM customers WHERE id = $1",
    [String(e.aggregate_id)],
  );
  if (existing.length > 0 && String(existing[0].organization_id) !== envelopeOrg) {
    const msg =
      `customer_created ${e.event_id}: customer ${e.aggregate_id} already exists locally ` +
      `under organization ${existing[0].organization_id}, but this event is for ` +
      `${envelopeOrg}. Refusing to ignore the conflict — the local row needs the ` +
      `one-time foreign-org cleanup before this event can apply.`;
    console.error(`[localProjectors] ${msg}`);
    throw new Error(msg);
  }

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
      envelopeOrg,
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

/**
 * Apply the server's ABSOLUTE loyalty post-state.
 *
 * A deliberately separate handler from `_customerUpdated`, for two reasons:
 *
 *  1. `_customerUpdated` overwrites every field present in the payload, including
 *     the name, contact details and marketing consent. This event carries only a
 *     balance, and the server is authoritative about the balance alone. Routing
 *     it through the update handler would mean a loyalty event silently
 *     discarding edits the user made offline on a `pending` row.
 *
 *  2. It matches on id AND organization_id. A loyalty event for a customer this
 *     device does not hold - or holds under a different org - is recorded as a
 *     projection failure rather than silently ignored or applied to the wrong
 *     tenant.
 *
 * The value is absolute, so applying the same event twice lands on the same
 * number. See ADR 0010.
 */
async function _customerLoyaltyChanged(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = new Date().toISOString();
  const customerId = String(p.customer_id ?? e.aggregate_id ?? "");
  const points = Number(p.loyalty_points);
  const tier = String(p.loyalty_tier ?? "");

  if (!customerId || !Number.isFinite(points) || !tier) {
    throw new Error(
      `customer_loyalty_changed ${e.event_id}: payload must carry customer_id, ` +
      `loyalty_points and loyalty_tier`,
    );
  }

  const existing = await db.select<{ organization_id: string }[]>(
    "SELECT organization_id FROM customers WHERE id = $1",
    [customerId],
  );
  if (existing.length === 0) {
    throw new Error(
      `customer_loyalty_changed ${e.event_id}: customer ${customerId} is not in the ` +
      `local read model. The server is authoritative about the balance; this device ` +
      `needs the customer_created event first.`,
    );
  }
  if (String(existing[0].organization_id) !== String(e.org_id)) {
    throw new Error(
      `customer_loyalty_changed ${e.event_id}: customer ${customerId} is held locally ` +
      `under organization ${existing[0].organization_id}, not ${e.org_id}. Refusing to ` +
      `apply a loyalty balance across organizations.`,
    );
  }

  // ONLY these two columns. The name, contact details and every other locally
  // edited field are untouched by design.
  await db.execute(
    "UPDATE customers SET loyalty_points = $1, loyalty_tier = $2, updated_at = $3 " +
    "WHERE id = $4 AND organization_id = $5",
    [points, tier, now, customerId, String(e.org_id)],
  );

  // Without this the Customers page and the POS typeahead keep showing the
  // pre-sync balance until something else happens to re-render them.
  appEvents.emit("customers:changed");
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
  const now = e.authored_at ?? new Date().toISOString();
  const setClauses = fields.map((_, i) => `${fields[i][0]} = $${i + 1}`).join(", ");
  const result = await db.execute(
    `UPDATE customers SET ${setClauses}, updated_at = $${fields.length + 1} WHERE id = $${fields.length + 2}`,
    [...fields.map((f) => f[1]), now, String(e.aggregate_id)]
  );
  // If row didn't exist, fall back to a full insert.
  if (result.rowsAffected === 0) {
    await _customerCreated(db, e);
    return;
  }
  // Persist the authoritative vector so future offline edits read the correct base.
  if (Object.keys(incomingVector).length > 0) {
    await setVersionVector("customers", String(e.aggregate_id), incomingVector);
  }
}

async function _customerDeleted(db: Db, e: EventEnvelope): Promise<void> {
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    "UPDATE customers SET is_deleted = 1, is_active = 0, updated_at = $1 WHERE id = $2",
    [now, String(e.aggregate_id)]
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

  // branch_inventory.quantity is NOT decremented here. The batch rows above
  // already had their remaining_quantity reduced, and for any (branch, drug)
  // this device holds batches for, quantity is DERIVED from them. Deducting
  // here as well is what made a replayed sale double-charge stock.
  const branchId = String(p.branch_id ?? e.branch_id);
  const touched: Array<{ branchId: string; drugId: string }> = [];
  for (const item of items) {
    if (item.drug_id != null && item.quantity != null) {
      touched.push({ branchId, drugId: String(item.drug_id) });
    }
  }
  // Derives quantity and recomputes sellable_quantity in one step.
  await refreshQuantitiesForPairs(db, touched);
}

async function _saleVoided(db: Db, e: EventEnvelope): Promise<void> {
  // Guarded: this projector restores stock with a raw `quantity = quantity + n`,
  // so a replay would restore the same units twice.
  if (await shouldSkipReplay(e)) return;
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    `UPDATE sales SET status = 'voided', updated_at = $1 WHERE id = $2`,
    [now, String(e.aggregate_id)]
  );
  // Restore stock.
  const branchId = String(p.branch_id ?? e.branch_id);
  const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
  const batchIds = batchChanges.map((bc) => String(bc.batch_id));
  const touched: Array<{ branchId: string; drugId: string }> = [];
  // Resolve which drugs the restored batches belong to before mutating them:
  // a void can restore batches for drugs other than p.drug_id.
  if (batchIds.length > 0) {
    const placeholders = batchIds.map((_, i) => `$${i + 1}`).join(",");
    const ownerRows = await db.select<{ drug_id: string }[]>(
      `SELECT DISTINCT drug_id FROM drug_batches WHERE id IN (${placeholders})`,
      batchIds
    );
    for (const row of ownerRows) touched.push({ branchId, drugId: String(row.drug_id) });
  }
  for (const bc of batchChanges) {
    await db.execute(
      `UPDATE drug_batches
          SET remaining_quantity = remaining_quantity + $1
        WHERE id = $2`,
      [Number(bc.quantity_used ?? 0), String(bc.batch_id)]
    );
  }
  if (p.drug_id != null && p.quantity != null) {
    // No relative bump on quantity here either: the restored batches above are
    // the fact, and quantity is derived from them. See refreshQuantitiesForPairs.
    touched.push({ branchId, drugId: String(p.drug_id) });
  }
  await recordGuardedApply(e);
  await refreshQuantitiesForPairs(db, touched);
}

// ── Prescription projectors ────────────────────────────────────────────────

async function _prescriptionCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    `INSERT OR IGNORE INTO prescriptions
       (id, organization_id, branch_id, customer_id,
        prescription_number, prescriber_name, prescriber_license, prescriber_phone, prescriber_address,
        issue_date, expiry_date, diagnosis, notes, special_instructions, medications,
        refills_allowed, refills_remaining, status, verified_by, verified_at,
        sync_status, sync_version, synced_at, updated_at, created_at)
     VALUES
       ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
        'synced',1,NULL,$21,$22)`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.branch_id ?? e.branch_id),
      p.customer_id != null ? String(p.customer_id) : "",
      p.prescription_number != null ? String(p.prescription_number) : `RX-${String(e.aggregate_id).slice(0, 8)}`,
      p.prescriber_name != null ? String(p.prescriber_name) : "Unknown Prescriber",
      // null, not "": the canonical absent form (P2). An old event carrying
      // "" arrives here as "" and is preserved, which is fine — reads treat both
      // as absent.
      p.prescriber_license != null && p.prescriber_license !== ""
        ? String(p.prescriber_license)
        : null,
      p.prescriber_phone != null ? String(p.prescriber_phone) : null,
      p.prescriber_address != null ? String(p.prescriber_address) : null,
      p.issue_date != null ? String(p.issue_date) : now.slice(0, 10),
      p.expiry_date != null ? String(p.expiry_date) : new Date(Date.now() + 90 * 86400000).toISOString().slice(0, 10),
      p.diagnosis != null ? String(p.diagnosis) : null,
      p.notes != null ? String(p.notes) : null,
      p.special_instructions != null ? String(p.special_instructions) : null,
      JSON.stringify(p.medications ?? []),
      Number(p.refills_allowed ?? 0),
      Number(p.refills_remaining ?? 0),
      String(p.status ?? "active"),
      p.verified_by != null ? String(p.verified_by) : null,
      p.verified_at != null ? String(p.verified_at) : null,
      now,
      now,
    ]
  );
  emitPrescriptionsChanged();
}

async function _prescriptionUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const UPDATABLE = [
    "prescriber_name", "prescriber_license", "prescriber_phone", "prescriber_address",
    "issue_date", "expiry_date", "diagnosis", "notes", "special_instructions",
    "status", "refills_allowed", "refills_remaining", "verified_by", "verified_at",
  ];
  const fields: [string, unknown][] = [];
  if ("medications" in p) {
    fields.push(["medications", JSON.stringify(p.medications)]);
  }
  for (const key of UPDATABLE) {
    if (key in p) {
      fields.push([key, p[key]]);
    }
  }
  if (fields.length === 0) return;
  const now = e.authored_at ?? new Date().toISOString();
  const setClauses = fields.map((f, i) => `${f[0]} = $${i + 1}`).join(", ");
  await db.execute(
    `UPDATE prescriptions SET ${setClauses}, updated_at = $${fields.length + 1} WHERE id = $${fields.length + 2}`,
    [...fields.map((f) => f[1]), now, String(e.aggregate_id)]
  );
  emitPrescriptionsChanged();
}

async function _prescriptionCancelled(db: Db, e: EventEnvelope): Promise<void> {
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    "UPDATE prescriptions SET status = 'cancelled', updated_at = $1 WHERE id = $2",
    [now, String(e.aggregate_id)]
  );
  emitPrescriptionsChanged();
}

async function _prescriptionRefillUsed(db: Db, e: EventEnvelope): Promise<void> {
  // Guarded: `refills_remaining - 1` is a raw decrement, so a replay would
  // consume a second refill.
  //
  // A device that made the sale offline has ALREADY applied this decrement
  // locally, in the same transaction that recorded the marker in
  // `applied_events` (offlineSalesManager.ts). So the skip below is what stops
  // the originating device from decrementing twice when the server's echo of its
  // own dispense comes back over the next sync pull. That is the whole reason the
  // marker is written there rather than after the commit.
  if (await shouldSkipReplay(e)) return;

  const now = e.authored_at ?? new Date().toISOString();
  const p = e.payload ?? {};

  // The server sends ABSOLUTE post-state, so a device converges on the server's
  // number instead of re-deriving it. Every key is read defensively: this event
  // type has existed in the projectors for a long time without a single emitter,
  // so events already sitting in a device outbox carry none of these keys, and a
  // hard subscript would throw and abort the projection.
  const absoluteRemaining = toOptionalNumber(p.refills_remaining);
  const absoluteStatus = typeof p.status === "string" ? p.status : null;
  const lastRefillDate =
    typeof p.last_refill_date === "string" && p.last_refill_date.length > 0
      ? p.last_refill_date
      : now.slice(0, 10);
  const verifiedBy =
    typeof p.verified_by === "string" && p.verified_by.length > 0 ? p.verified_by : null;
  const verifiedAt =
    typeof p.verified_at === "string" && p.verified_at.length > 0 ? p.verified_at : now;

  if (absoluteRemaining !== null) {
    // Absolute path. `status` comes from the server when it sent one; when it did
    // not, COALESCE falls back to setting 'filled' at zero and otherwise LEAVES
    // the local status alone. The server's own relative path uses
    // `CASE WHEN refills_remaining <= 1 THEN 'filled' ELSE status END`
    // (projectors/prescription.py:320-347), which preserves a non-active status;
    // the older device SQL set 'active' unconditionally and would resurrect a
    // prescription this device had cancelled while the server still had it
    // active.
    const clamped = Math.max(0, absoluteRemaining);
    await db.execute(
      `UPDATE prescriptions
          SET refills_remaining = $1,
              status = COALESCE($2, CASE WHEN $1 <= 0 THEN 'filled' ELSE status END),
              last_refill_date = $3,
              verified_by = $4,
              verified_at = $5,
              updated_at = $6
        WHERE id = $7`,
      [
        clamped,
        absoluteStatus,
        lastRefillDate,
        verifiedBy,
        verifiedAt,
        now,
        String(e.aggregate_id),
      ]
    );
  } else {
    // Relative path, for an event with no post-state. Mirrors the server's
    // `_apply_refill_used`, including its `IN ('active','filled')` guard.
    await db.execute(
      `UPDATE prescriptions
          SET refills_remaining = MAX(0, refills_remaining - 1),
              status = CASE WHEN refills_remaining <= 1 THEN 'filled' ELSE status END,
              last_refill_date = $1,
              verified_by = COALESCE($2, verified_by),
              verified_at = COALESCE($3, verified_at),
              updated_at = $4
        WHERE id = $5
          AND status IN ('active', 'filled')
          AND refills_remaining > 0`,
      [lastRefillDate, verifiedBy, verifiedAt, now, String(e.aggregate_id)]
    );
  }

  await recordGuardedApply(e);
  emitPrescriptionsChanged();
}

/** Parse a number that may arrive as a number or a numeric string. */
function toOptionalNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

// ── Price Contract projectors ──────────────────────────────────────────────

async function _priceContractCreated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    `INSERT OR IGNORE INTO price_contracts (
      id, organization_id, contract_code, contract_name, contract_type,
      is_default_contract, discount_type, discount_percentage,
      applies_to_prescription_only, applies_to_otc, applies_to_all_branches,
      applicable_branch_ids, effective_from, effective_to,
      requires_verification, requires_approval, daily_usage_limit,
      per_customer_usage_limit, insurance_provider_id, requires_preauthorization,
      minimum_purchase_amount, maximum_purchase_amount, status, is_active,
      copay_amount, copay_percentage, is_deleted,
      sync_status, sync_version, synced_at, updated_at, created_at
    ) VALUES (
      $1, $2, $3, $4, $5,
      $6, $7, $8,
      $9, $10, $11,
      $12, $13, $14,
      $15, $16, $17,
      $18, $19, $20,
      $21, $22, $23, $24,
      $25, $26, 0,
      'synced', 1, NULL, $27, $27
    )`,
    [
      String(e.aggregate_id),
      String(p.organization_id ?? e.org_id),
      String(p.contract_code ?? "DEFAULT"),
      String(p.contract_name ?? "Standard"),
      String(p.contract_type ?? "individual"),
      p.is_default_contract ? 1 : 0,
      String(p.discount_type ?? "percentage"),
      Number(p.discount_percentage ?? 0),
      p.applies_to_prescription_only ? 1 : 0,
      p.applies_to_otc !== false ? 1 : 0,
      p.applies_to_all_branches !== false ? 1 : 0,
      JSON.stringify(p.applicable_branch_ids ?? []),
      String(p.effective_from ?? now),
      p.effective_to != null ? String(p.effective_to) : null,
      p.requires_verification ? 1 : 0,
      p.requires_approval ? 1 : 0,
      p.daily_usage_limit != null ? Number(p.daily_usage_limit) : null,
      p.per_customer_usage_limit != null ? Number(p.per_customer_usage_limit) : null,
      p.insurance_provider_id != null ? String(p.insurance_provider_id) : null,
      p.requires_preauthorization ? 1 : 0,
      p.minimum_purchase_amount != null ? Number(p.minimum_purchase_amount) : null,
      p.maximum_purchase_amount != null ? Number(p.maximum_purchase_amount) : null,
      String(p.status ?? "active"),
      p.is_active !== false ? 1 : 0,
      p.copay_amount != null ? Number(p.copay_amount) : null,
      p.copay_percentage != null ? Number(p.copay_percentage) : null,
      now,
    ]
  );
}

async function _priceContractUpdated(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const UPDATABLE = [
    "contract_code", "contract_name", "contract_type", "discount_type",
    "discount_percentage", "applies_to_prescription_only", "applies_to_otc",
    "applies_to_all_branches", "effective_from", "effective_to",
    "requires_verification", "requires_approval", "daily_usage_limit",
    "per_customer_usage_limit", "insurance_provider_id", "requires_preauthorization",
    "minimum_purchase_amount", "maximum_purchase_amount", "status", "is_active",
    "copay_amount", "copay_percentage",
  ];
  const fields: [string, unknown][] = [];
  if ("applicable_branch_ids" in p) {
    fields.push(["applicable_branch_ids", JSON.stringify(p.applicable_branch_ids)]);
  }
  for (const key of UPDATABLE) {
    if (key in p) {
      fields.push([key, p[key]]);
    }
  }
  if (fields.length === 0) return;
  const now = e.authored_at ?? new Date().toISOString();
  const setClauses = fields.map((f, i) => `${f[0]} = $${i + 1}`).join(", ");
  const result = await db.execute(
    `UPDATE price_contracts SET ${setClauses}, updated_at = $${fields.length + 1} WHERE id = $${fields.length + 2}`,
    [...fields.map((f) => f[1]), now, String(e.aggregate_id)]
  );
  if (result.rowsAffected === 0) {
    await _priceContractCreated(db, e);
  }
}

async function _priceContractDeleted(db: Db, e: EventEnvelope): Promise<void> {
  const now = e.authored_at ?? new Date().toISOString();
  await db.execute(
    "UPDATE price_contracts SET is_deleted = 1, is_active = 0, updated_at = $1 WHERE id = $2",
    [now, String(e.aggregate_id)]
  );
}

// ── Stock projectors ───────────────────────────────────────────────────────

async function _stockAdjusted(db: Db, e: EventEnvelope): Promise<void> {
  // Guarded: `quantity = MAX(0, quantity + delta)` is relative, so a replay
  // would double-count the adjustment.
  if (await shouldSkipReplay(e)) return;
  const p = e.payload as Record<string, unknown>;
  const qtyChange = Number(p.quantity_change ?? 0);
  const drugId = p.drug_id != null ? String(p.drug_id) : null;
  const branchId = String(p.branch_id ?? e.branch_id);

  if (!drugId) return;

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

  if (batchChanges.length === 0) {
    // No batch-level detail: this is an explicit relative adjustment, which is
    // the ONLY case that may move quantity directly. Once the device holds any
    // batch row for the pair, recomputeQuantityFromBatches below overwrites it
    // from the batch sum, so the two can never disagree.
    await db.execute(
      `UPDATE branch_inventory
          SET quantity = MAX(0, quantity + $1)
        WHERE branch_id = $2 AND drug_id = $3`,
      [qtyChange, branchId, drugId]
    );
  }

  await recordGuardedApply(e);
  await refreshQuantities(db, branchId, drugId);
}

// ── Stock transfer projector ───────────────────────────────────────────────

async function _stockTransfer(db: Db, e: EventEnvelope): Promise<void> {
  // Guarded: this projector moves stock with relative +/- updates on both
  // branches, so a replay would move it twice.
  if (await shouldSkipReplay(e)) return;
  const p = e.payload as Record<string, unknown>;
  const qty = Number(p.quantity ?? 0);
  const drugId = p.drug_id != null ? String(p.drug_id) : null;
  const srcBranch = p.source_branch_id != null ? String(p.source_branch_id) : null;
  const dstBranch = p.destination_branch_id != null ? String(p.destination_branch_id) : null;

  if (!drugId || !srcBranch || !dstBranch) return;

  // The movement itself is a relative +/- on quantity. A transfer is the one
  // case where that is unavoidable: the payload names only the SOURCE batches,
  // so there is nothing on the destination side to derive from yet.
  await db.execute(
    `UPDATE branch_inventory
        SET quantity = MAX(0, quantity - $1)
      WHERE branch_id = $2 AND drug_id = $3`,
    [qty, srcBranch, drugId]
  );
  await db.execute(
    `UPDATE branch_inventory
        SET quantity = quantity + $1
      WHERE branch_id = $2 AND drug_id = $3`,
    [qty, dstBranch, drugId]
  );

  // Apply per-batch movements (source side only — see the note below).
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
  await recordGuardedApply(e);

  // Derive both branches. The source is exact (its batches were debited). The
  // destination is exact only while it holds NO batch rows for this drug.
  //
  // KNOWN GAP, tracked deliberately: `batch_changes` entries carry a `batch_id`
  // and a quantity but no destination batch id, so a transfer cannot credit a
  // destination batch. If the destination branch already has its own batch rows
  // for this drug, the derivation overwrites the credit just applied above and
  // the destination quantity will not reflect the transfer until the event
  // carries destination batch ids. No live emitter publishes stock_transfer
  // yet (the stock emitter covers branch_inventory_* and drug_batch_* only), so
  // nothing is currently exposed; the payload must gain destination batch ids
  // before transfers are emitted. `localProjectors.replayGuard.spec.ts` pins the
  // current behaviour so the gap cannot regress unnoticed.
  await refreshQuantities(db, srcBranch, drugId);
  await refreshQuantities(db, dstBranch, drugId);
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
  const now = e.authored_at ?? new Date().toISOString();
  const result = await db.execute(
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
      p.updated_at != null ? String(p.updated_at) : now,
      String(e.aggregate_id),
    ]
  );
  if (result.rowsAffected === 0) {
    await _drugCreated(db, e);
  }
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
  const result = await db.execute(
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
  if (result.rowsAffected === 0) {
    await _drugCategoryCreated(db, e);
  }
}

// ── Drug Batch projectors ───────────────────────────────────────────────────

async function _drugBatchUpserted(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  const remainingQuantity = Number(p.remaining_quantity ?? 0);
  const branchId = String(p.branch_id ?? e.branch_id);
  const drugId = String(p.drug_id ?? "");

  // NOTE: this projector deliberately does NOT touch branch_inventory.quantity.
  //
  // It used to add `remaining_quantity` on create and `new - known_old` on
  // update. That relative bump is what made replay order matter: an arriving
  // batch was added on top of an inventory row that already accounted for it
  // (measured at 247 + 100 = 347), and a fresh device that received the
  // branch_inventory event first then doubled it (117 -> 234, 247 -> 494).
  //
  // branch_inventory.quantity is now DERIVED from the batch rows by
  // recomputeQuantityFromBatches once this row is written below. One writer, so
  // delivery order stops mattering.
  //
  // The only thing still created here is a missing branch_inventory ROW, so a
  // batch that arrives before any inventory event still has somewhere to live.
  // Its quantity is provisional and is replaced by the derivation immediately
  // after, which is why the inserted value is this batch's own quantity.
  {
    const invRows = await db.select<{ id: string }[]>(
      "SELECT id FROM branch_inventory WHERE branch_id = $1 AND drug_id = $2 LIMIT 1",
      [branchId, drugId]
    );
    if (!invRows || invRows.length === 0) {
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

  // Run last: the batch row is now present, so quantity is derived from the real
  // batch set and sellable_quantity sees the authoritative remaining_quantity
  // rather than a pre-batch snapshot. This is what makes the result independent
  // of whether the batch event or the inventory event arrived first.
  await refreshQuantities(db, branchId, drugId);
}

// ── Branch Inventory projectors ─────────────────────────────────────────────

async function _branchInventoryUpserted(db: Db, e: EventEnvelope): Promise<void> {
  const p = e.payload as Record<string, unknown>;
  const now = e.authored_at ?? new Date().toISOString();
  const branchId = String(p.branch_id ?? e.branch_id);
  const drugId = String(p.drug_id ?? e.aggregate_id);
  // Does the payload actually state an absolute quantity?
  //
  // The device's own buildBranchInventoryEnvelope (localWrite.ts) deliberately
  // omits `quantity` — it carries only branch-owned metadata. Reading a missing
  // quantity as 0 meant that any such event, wherever it was projected, ZEROED
  // existing stock. A branch_inventory event without a quantity must leave the
  // stored quantity untouched; only an INSERT needs the 0 default.
  const hasQty = p.quantity != null || p.sellable_quantity != null;
  const qty = p.quantity != null
    ? Number(p.quantity)
    : (p.sellable_quantity != null ? Number(p.sellable_quantity) : 0);
  const sellingPrice = p.selling_price != null ? Number(p.selling_price) : (p.branch_selling_price != null ? Number(p.branch_selling_price) : null);
  const location = p.location != null ? String(p.location) : (p.shelf_location != null ? String(p.shelf_location) : null);

  const existing = await db.select<{ id: string }[]>(
    "SELECT id FROM branch_inventory WHERE (id = $1) OR (branch_id = $2 AND drug_id = $3)",
    [String(e.aggregate_id), branchId, drugId]
  );

  if (existing.length > 0) {
    // On UPDATE, only write quantity when the event actually states one.
    await db.execute(
      hasQty
        ? `UPDATE branch_inventory SET
             quantity = $1,
             location = COALESCE($2, location),
             selling_price = COALESCE($3, selling_price),
             updated_at = $4,
             sync_status = 'synced'
           WHERE id = $5`
        : `UPDATE branch_inventory SET
             location = COALESCE($1, location),
             selling_price = COALESCE($2, selling_price),
             updated_at = $3,
             sync_status = 'synced'
           WHERE id = $4`,
      hasQty
        ? [qty, location, sellingPrice, now, existing[0].id]
        : [location, sellingPrice, now, existing[0].id]
    );
  } else {
    await db.execute(
      `INSERT OR IGNORE INTO branch_inventory
         (id, branch_id, drug_id, quantity, reserved_quantity, location, selling_price,
          sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1, $2, $3, $4, 0, $5, $6, 'synced', 1, NULL, $7, $7)`,
      [String(e.aggregate_id), branchId, drugId, qty, location, sellingPrice, now]
    );
  }

  // sellable_quantity is a derived column: nothing in the event payload can be
  // trusted to populate it (PostgreSQL branch_inventory has no such column, so
  // the server never emits one). Recompute it from local batches and leases.
  // If this device already holds batches for the pair, the payload's quantity is
  // NOT authoritative: it is a snapshot taken when the event was written, and
  // recomputeQuantityFromBatches replaces it with the live batch sum. That is
  // what makes the event harmless in any delivery order. With no batch rows, the
  // payload's value stands — the documented fallback.
  await refreshQuantities(db, branchId, drugId);
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
