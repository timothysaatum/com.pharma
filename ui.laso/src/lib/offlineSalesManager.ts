/**
 * offlineSalesManager.ts
 * ======================
 * Robust offline-first sales queue management with:
 *   - Fail-fast sale recording with compensating rollback
 *   - Retry state coordinated with the durable sync queue
 *   - Duplicate detection and prevention
 *   - Inventory sync consistency
 *   - Comprehensive error recovery
 *   - Audit trail for offline transactions
 */

import { getDb } from "@/lib/localDb";
import { writeLocal } from "@/lib/localWrite";
import type { Sale, SaleItem } from "@/types";

export interface OfflineSaleRecord {
  id: string;
  sale_data: Record<string, unknown>;
  sale_items: Record<string, unknown>[];
  inventory_updates: Array<{ drug_id: string; delta: number }>;
  recorded_at: string;
  sync_status: "pending" | "syncing" | "synced" | "failed" | "duplicate";
  retry_count: number;
  last_retry_at: string | null;
  next_retry_at: string | null;
  error_message: string | null;
  idempotency_key: string; // For duplicate detection
  created_at: string;
  updated_at: string;
}

const MAX_RETRIES = 5;
const INITIAL_RETRY_DELAY_MS = 1000; // 1 second

class OfflineSalesManager {
  /**
   * Record a sale and compensate all completed local writes if a later step
   * fails. The Tauri SQL guest API does not expose a multi-call transaction,
   * so every failure must be surfaced and explicitly reversed.
   * 
   * Prescription refills and inventory are updated locally for immediate UX,
   * but the sale remains the single queued server operation. The server applies
   * those side effects atomically when processing sync protocol v2.
   */
  async recordSaleTransaction(
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: SaleItem[],
    inventoryDeltas: Array<{ drug_id: string; delta: number }>,
    idempotencyKey: string
  ): Promise<{ success: true; saleId: string } | { success: false; error: string }> {
    const db = await getDb();
    const now = new Date().toISOString();
    const appliedInventoryDeltas: Array<{ drug_id: string; delta: number }> = [];
    let saleWritten = false;
    let prescriptionDecremented = false;

    try {
      const existing = await db.select<Array<{ id: string; sync_status: string }>>(
        "SELECT id, sync_status FROM offline_sales WHERE idempotency_key = $1",
        [idempotencyKey]
      );

      if (existing.length > 0) {
        return { success: true, saleId: existing[0].id };
      }

      // ─── Step 1: Validate prescription refills before proceeding ──────────────
      if (sale.prescription_id) {
        const refillStatus = await this.validatePrescriptionRefills(sale.prescription_id);
        if (!refillStatus.valid) {
          return { success: false, error: refillStatus.error };
        }
      }

      await writeLocal.sale({ ...sale, items } as Parameters<typeof writeLocal.sale>[0]);
      saleWritten = true;

      for (const { drug_id, delta } of inventoryDeltas) {
        await writeLocal.inventory(sale.branch_id, drug_id, delta, false);
        appliedInventoryDeltas.push({ drug_id, delta });
      }

      // ─── Step 2: Decrement prescription refills IMMEDIATELY (optimistic update) ──
      if (sale.prescription_id) {
        const decrementStatus = await this.decrementPrescriptionRefillsOffline(sale.prescription_id);
        if (!decrementStatus.success) {
          throw new Error(
            decrementStatus.error ?? "Failed to decrement prescription refills"
          );
        }
        prescriptionDecremented = true;
      }

      await this.recordAuditRow(db, sale, items, inventoryDeltas, idempotencyKey, now);
      return { success: true, saleId: sale.id };
    } catch (err) {
      console.error("[OfflineSalesManager] Transaction failed:", err);
      await this.compensateFailedSale({
        saleId: sale.id,
        branchId: sale.branch_id,
        prescriptionId: sale.prescription_id ?? null,
        prescriptionDecremented,
        saleWritten,
        appliedInventoryDeltas,
      });
      return {
        success: false,
        error: err instanceof Error ? err.message : "Failed to record sale transaction",
      };
    }
  }

  /**
   * Validate that prescription has refills remaining before sale.
   */
  private async validatePrescriptionRefills(
    prescriptionId: string
  ): Promise<{ valid: boolean; error: string }> {
    const db = await getDb();
    const result = await db.select<Array<{ refills_remaining: number; status: string }>>(
      `SELECT refills_remaining, status FROM prescriptions WHERE id = $1`,
      [prescriptionId]
    );

    if (!result.length) {
      return { valid: false, error: "Prescription not found" };
    }

    const { refills_remaining, status } = result[0];

    if (status !== "active") {
      return { valid: false, error: `Prescription is ${status}, cannot be used for sale` };
    }

    if (refills_remaining <= 0) {
      return { valid: false, error: "No refills remaining for this prescription" };
    }

    return { valid: true, error: "" };
  }

  /**
   * Decrement prescription refills immediately (optimistic offline update).
   * This is a local projection of the queued sale, not an independent
   * prescription edit. Keep the server version unchanged so the pull that
   * follows a successful sale push can apply the authoritative version.
   */
  private async decrementPrescriptionRefillsOffline(
    prescriptionId: string
  ): Promise<{ success: boolean; error?: string }> {
    const db = await getDb();
    const now = new Date().toISOString();

    try {
      // Get current prescription state
      const result = await db.select<Array<{
        refills_remaining: number;
        sync_version: number;
        status: string;
      }>>(
        `SELECT refills_remaining, sync_version, status FROM prescriptions WHERE id = $1`,
        [prescriptionId]
      );

      if (!result.length) {
        return { success: false, error: "Prescription not found" };
      }

      const { refills_remaining, sync_version } = result[0];

      if (refills_remaining <= 0) {
        return { success: false, error: "No refills remaining" };
      }

      // Decrement refills locally without creating a second sync operation.
      const newRefillsRemaining = refills_remaining - 1;
      const newStatus = newRefillsRemaining === 0 ? "filled" : "active";
      const lastRefillDate = new Date().toISOString().split("T")[0]; // ISO date only

      await db.execute(
        `UPDATE prescriptions
         SET refills_remaining = $1,
             status = $2,
             last_refill_date = $3,
             sync_status = 'synced',
             updated_at = $4
         WHERE id = $5`,
        [newRefillsRemaining, newStatus, lastRefillDate, now, prescriptionId]
      );

      console.info(
        `[OfflineSalesManager] Decremented prescription ${prescriptionId} refills: ` +
        `${refills_remaining} → ${newRefillsRemaining}, sync_version: ${sync_version}`
      );

      return { success: true };
    } catch (err) {
      return { success: false, error: err instanceof Error ? err.message : "Unknown error" };
    }
  }

  private async recordAuditRow(
    db: Awaited<ReturnType<typeof getDb>>,
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: SaleItem[],
    inventoryDeltas: Array<{ drug_id: string; delta: number }>,
    idempotencyKey: string,
    now: string
  ): Promise<void> {
      const { items: _saleItems, ...saleData } = sale as Record<string, unknown>;

      const offlineRecord: OfflineSaleRecord = {
        id: sale.id,
        sale_data: saleData,
        sale_items: items.map((item) => ({
          drug_id: item.drug_id,
          quantity: item.quantity,
          unit_price: item.unit_price,
          discount_percentage: (item as any).discount_percentage || 0,
          discount_amount: (item as any).discount_amount || 0,
        })),
        inventory_updates: inventoryDeltas,
        recorded_at: now,
        sync_status: "pending",
        retry_count: 0,
        last_retry_at: null,
        next_retry_at: null,
        error_message: null,
        idempotency_key: idempotencyKey,
        created_at: now,
        updated_at: now,
      };

      const cols = Object.keys(offlineRecord);
      const vals = cols.map((c) => {
        const v = offlineRecord[c as keyof OfflineSaleRecord];
        if (typeof v === "boolean") return v ? 1 : 0;
        if (v === null) return null;
        if (Array.isArray(v) || typeof v === "object") return JSON.stringify(v);
        return v;
      });
      const placeholders = cols.map((_, i) => `$${i + 1}`).join(", ");

      await db.execute(
        `INSERT OR REPLACE INTO offline_sales (${cols.join(", ")}) VALUES (${placeholders})`,
        vals
      );
  }

  private async compensateFailedSale({
    saleId,
    branchId,
    prescriptionId,
    prescriptionDecremented,
    saleWritten,
    appliedInventoryDeltas,
  }: {
    saleId: string;
    branchId: string;
    prescriptionId: string | null;
    prescriptionDecremented: boolean;
    saleWritten: boolean;
    appliedInventoryDeltas: Array<{ drug_id: string; delta: number }>;
  }): Promise<void> {
    const db = await getDb();

    for (const { drug_id, delta } of [...appliedInventoryDeltas].reverse()) {
      try {
        await writeLocal.inventory(branchId, drug_id, -delta, false);
      } catch (error) {
        console.error(
          `[OfflineSalesManager] Failed to restore inventory for ${drug_id}:`,
          error
        );
      }
    }

    if (prescriptionDecremented && prescriptionId) {
      try {
        await db.execute(
          `UPDATE prescriptions
           SET refills_remaining = refills_remaining + 1,
               status = 'active',
               sync_status = 'synced',
               updated_at = $1
           WHERE id = $2`,
          [new Date().toISOString(), prescriptionId]
        );
      } catch (error) {
        console.error(
          `[OfflineSalesManager] Failed to restore prescription ${prescriptionId}:`,
          error
        );
      }
    }

    if (saleWritten) {
      try {
        await db.execute(
          "DELETE FROM sync_queue WHERE table_name = 'sales' AND record_id = $1",
          [saleId]
        );
        await db.execute("DELETE FROM sales WHERE id = $1", [saleId]);
      } catch (error) {
        console.error(
          `[OfflineSalesManager] Failed to remove incomplete sale ${saleId}:`,
          error
        );
      }
    }

    try {
      await db.execute("DELETE FROM offline_sales WHERE id = $1", [saleId]);
    } catch (error) {
      console.error(
        `[OfflineSalesManager] Failed to remove audit row for ${saleId}:`,
        error
      );
    }
  }

  /**
   * Retrieve all pending offline sales for retry.
   */
  async getPendingSales(): Promise<OfflineSaleRecord[]> {
    const db = await getDb();
    const rows = await db.select<Array<Record<string, unknown>>>(
      `SELECT * FROM offline_sales 
       WHERE sync_status IN ('pending', 'failed')
       ORDER BY created_at ASC`
    );

    return rows.map((row) => this.deserializeSaleRecord(row));
  }

  /**
   * Mark a sale as synced successfully.
   */
  async markSynced(saleId: string): Promise<void> {
    const db = await getDb();
    const now = new Date().toISOString();
    await db.execute(
      `UPDATE offline_sales 
       SET sync_status = 'synced', updated_at = $1 
       WHERE id = $2`,
      [now, saleId]
    );
  }

  /**
   * Mark a sale as failed with error and schedule retry.
   */
  async markFailed(
    saleId: string,
    error: string,
    retryCount: number
  ): Promise<void> {
    const db = await getDb();
    const now = new Date().toISOString();

    if (retryCount >= MAX_RETRIES) {
      // Max retries exceeded
      await db.execute(
        `UPDATE offline_sales 
         SET sync_status = 'failed', 
             error_message = $1, 
             retry_count = $2,
             updated_at = $3
         WHERE id = $4`,
        [error, retryCount, now, saleId]
      );
    } else {
      // Schedule next retry with exponential backoff
      const delayMs =
        INITIAL_RETRY_DELAY_MS * Math.pow(2, retryCount);
      const nextRetryAt = new Date(Date.now() + delayMs).toISOString();

      await db.execute(
        `UPDATE offline_sales 
         SET sync_status = 'failed', 
             error_message = $1, 
             retry_count = $2,
             last_retry_at = $3,
             next_retry_at = $4,
             updated_at = $5
         WHERE id = $6`,
        [error, retryCount + 1, now, nextRetryAt, now, saleId]
      );
    }
  }

  /**
   * Get sales ready for retry based on next_retry_at.
   */
  async getSalesReadyForRetry(): Promise<OfflineSaleRecord[]> {
    const db = await getDb();
    const now = new Date().toISOString();
    const rows = await db.select<Array<Record<string, unknown>>>(
      `SELECT * FROM offline_sales 
       WHERE sync_status = 'failed' 
       AND retry_count < $1
       AND (next_retry_at IS NULL OR next_retry_at <= $2)
       ORDER BY next_retry_at ASC`,
      [MAX_RETRIES, now]
    );

    return rows.map((row) => this.deserializeSaleRecord(row));
  }

  /**
   * Clear a sale record after successful sync.
   */
  async clearSaleRecord(saleId: string): Promise<void> {
    const db = await getDb();
    await db.execute(
      "DELETE FROM offline_sales WHERE id = $1",
      [saleId]
    );
  }

  /**
   * Get audit trail of offline sales.
   */
  async getAuditTrail(limit: number = 100): Promise<OfflineSaleRecord[]> {
    const db = await getDb();
    const rows = await db.select<Array<Record<string, unknown>>>(
      `SELECT * FROM offline_sales 
       ORDER BY created_at DESC 
       LIMIT $1`,
      [limit]
    );

    return rows.map((row) => this.deserializeSaleRecord(row));
  }

  // ─── Helpers ─────────────────────────────────────────────────────────────

  private deserializeSaleRecord(
    row: Record<string, unknown>
  ): OfflineSaleRecord {
    return {
      id: row.id as string,
      sale_data: this.parseJson(row.sale_data),
      sale_items: this.parseJson(row.sale_items),
      inventory_updates: this.parseJson(row.inventory_updates),
      recorded_at: row.recorded_at as string,
      sync_status: row.sync_status as OfflineSaleRecord["sync_status"],
      retry_count: (row.retry_count as number) ?? 0,
      last_retry_at: (row.last_retry_at as string | null) ?? null,
      next_retry_at: (row.next_retry_at as string | null) ?? null,
      error_message: (row.error_message as string | null) ?? null,
      idempotency_key: row.idempotency_key as string,
      created_at: row.created_at as string,
      updated_at: row.updated_at as string,
    };
  }

  private parseJson(value: unknown): any {
    if (typeof value === "string") {
      try {
        return JSON.parse(value);
      } catch {
        return value;
      }
    }
    return value;
  }
}

export const offlineSalesManager = new OfflineSalesManager();
