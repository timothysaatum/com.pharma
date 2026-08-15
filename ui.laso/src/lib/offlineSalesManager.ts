/**
 * offlineSalesManager.ts
 * ======================
 * Robust offline-first sales queue management with:
 *   - Atomic sale, queue, inventory, prescription, and journal persistence
 *   - Retry state coordinated with the durable sync queue
 *   - Duplicate detection and prevention
 *   - Inventory sync consistency
 *   - Comprehensive error recovery
 *   - Audit trail for offline transactions
 *   - FEFO batch allocation at recording time
 */

import {
  getDb,
  notifySyncQueueChanged,
  type Database,
  type DbTransactionStatement,
} from "@/lib/localDb";
import { LeaseEngine } from "@/lib/leaseEngine";
import { buildLocalSalePayload } from "@/lib/localWrite";
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
   * Atomically persist the sale, its protocol-v2 sync envelope, local inventory
   * projection, optional prescription projection, and audit trail. The Rust DB
   * bridge holds one SQLite transaction for the complete statement list, so an
   * unavailable item or prescription rolls back every write.
   */
  async recordSaleTransaction(
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: SaleItem[],
    inventoryDeltas: Array<{ drug_id: string; delta: number }>,
    idempotencyKey: string
  ): Promise<{ success: true; saleId: string } | { success: false; error: string }> {
    const db = await getDb();
    const now = new Date().toISOString();

    try {
      const validationError = this.validateTransaction(
        sale,
        items,
        inventoryDeltas,
        idempotencyKey,
      );
      if (validationError) return { success: false, error: validationError };

      const existing = await db.select<Array<{ id: string; idempotency_key: string }>>(
        `SELECT id, idempotency_key FROM offline_sales
         WHERE id = $1 OR idempotency_key = $2
         LIMIT 1`,
        [sale.id, idempotencyKey],
      );
      if (existing.length > 0) {
        if (existing[0].id === sale.id && existing[0].idempotency_key === idempotencyKey) {
          return { success: true, saleId: sale.id };
        }
        return {
          success: false,
          error: "Offline checkout identity is already assigned to a different sale.",
        };
      }

      if (!db.executeTransaction) {
        return {
          success: false,
          error: "Durable offline checkout is unavailable outside the desktop app.",
        };
      }

    // Pre-compute FEFO batch allocations before building transaction statements
    const batchAllocs = new Map<string, Array<{ batchId: string; allocatedQty: number }>>();
    for (const { drug_id, delta } of inventoryDeltas) {
      const qtyToSell = Math.abs(delta);
      if (qtyToSell <= 0) continue;
      const allocs = await this.allocateBatchesForDrug(db, sale.branch_id, drug_id, qtyToSell);
      if (allocs.length > 0) {
        batchAllocs.set(drug_id, allocs);
      }
    }

    const statements = this.buildTransactionStatements(
        sale,
        items,
        inventoryDeltas,
        batchAllocs,
        idempotencyKey,
        now,
    );
    await db.executeTransaction(statements);
      notifySyncQueueChanged();
      console.info(
        `[OfflineSalesManager] offline_sale_recorded sale_id=${sale.id} `
        + `items=${items.length} operation_id=${sale.id}`,
      );
      return { success: true, saleId: sale.id };
    } catch (err) {
      console.error("[OfflineSalesManager] Transaction failed:", err);

      // A concurrent retry can win the unique idempotency insert while this
      // transaction waits for the SQLite lock. Treat that exact identity as a
      // successful replay; all statements in the losing transaction rolled back.
      const replay = await db.select<Array<{ id: string; idempotency_key: string }>>(
        "SELECT id, idempotency_key FROM offline_sales WHERE id = $1 LIMIT 1",
        [sale.id],
      );
      if (replay[0]?.idempotency_key === idempotencyKey) {
        return { success: true, saleId: sale.id };
      }
      return {
        success: false,
        error: this.errorMessage(err),
      };
    }
  }

  private buildTransactionStatements(
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: SaleItem[],
    inventoryDeltas: Array<{ drug_id: string; delta: number }>,
    batchAllocs: Map<string, Array<{ batchId: string; allocatedQty: number }>>,
    idempotencyKey: string,
    now: string,
  ): DbTransactionStatement[] {
    const { items: _saleItems, ...saleData } = sale as Record<string, unknown>;
    const salePayload = buildLocalSalePayload(
      { ...sale, items } as Parameters<typeof buildLocalSalePayload>[0],
      items,
      now,
    );
    // Attach batch allocations to sync queue payload items
    const itemsWithProvisional = items.map((item) => {
      const allocs = batchAllocs.get(item.drug_id) ?? [];
      return {
        ...item,
        provisional_batch_allocations: allocs.map((a) => ({
          allocation_id: crypto.randomUUID(),
          batch_id: a.batchId,
          quantity: a.allocatedQty,
        })),
      };
    });
    const queuePayload = {
      ...salePayload,
      items: itemsWithProvisional,
      sync_protocol_version: 2,
      terminal_id: LeaseEngine.getTerminalId(),
    };
    const offlineRecord: Record<string, unknown> = {
      id: sale.id,
      idempotency_key: idempotencyKey,
      sale_data: JSON.stringify(saleData),
      sale_items: JSON.stringify(items),
      inventory_updates: JSON.stringify(inventoryDeltas),
      recorded_at: now,
      sync_status: "pending",
      retry_count: 0,
      last_retry_at: null,
      next_retry_at: null,
      error_message: null,
      crr_start_db_version: 0,
      created_at: now,
      updated_at: now,
    };

    const statements: DbTransactionStatement[] = [
      this.insertStatement(
        "offline_sales",
        offlineRecord,
        "Offline checkout identity is already recorded.",
      ),
      {
        sql: `UPDATE offline_sales
              SET crr_start_db_version = COALESCE(
                (SELECT MAX(db_version) FROM crsql_changes), 0
              )
              WHERE id = $1`,
        values: [sale.id],
        expectedRows: 1,
        errorMessage: "Unable to checkpoint local sync state for the offline sale.",
      },
      this.insertStatement(
        "sales",
        salePayload,
        "The local sale ID already exists without a matching offline audit.",
      ),
      this.insertStatement("sync_queue", {
        operation_id: sale.id,
        table_name: "sales",
        record_id: sale.id,
        operation: "create",
        sync_version: 1,
        payload_json: JSON.stringify(queuePayload),
        created_offline_at: now,
        attempts: 0,
        last_attempt_at: null,
        next_attempt_at: null,
        error: null,
        conflict_json: null,
      }, "The sale already has a different sync operation."),
    ];

    // FEFO batch deduction (allocations pre-computed in recordSaleTransaction)
    for (const [, allocs] of batchAllocs) {
      for (const { batchId, allocatedQty } of allocs) {
        statements.push({
          sql: `UPDATE drug_batches
                SET remaining_quantity = remaining_quantity - $1, updated_at = $2
                WHERE id = $3
                  AND remaining_quantity >= $1`,
          values: [allocatedQty, now, batchId],
          expectedRows: 1,
          errorMessage:
            `Batch ${batchId} has insufficient remaining quantity; ` +
            `no part of the sale was recorded.`,
        });
      }
    }

    for (const { drug_id, delta } of inventoryDeltas) {
      statements.push({
        sql: `UPDATE branch_inventory
              SET quantity = quantity + $1, updated_at = $2
              WHERE branch_id = $3 AND drug_id = $4
                AND quantity + $1 >= reserved_quantity`,
        values: [delta, now, sale.branch_id, drug_id],
        expectedRows: 1,
        errorMessage:
          `Insufficient available local stock for drug ${drug_id}; no part of the sale was recorded.`,
      });
    }

    if (sale.prescription_id) {
      statements.push({
        sql: `UPDATE prescriptions
              SET refills_remaining = refills_remaining - 1,
                  status = CASE WHEN refills_remaining - 1 = 0 THEN 'filled' ELSE 'active' END,
                  last_refill_date = $1,
                  sync_status = 'synced',
                  updated_at = $2
              WHERE id = $3 AND branch_id = $4
                AND status = 'active' AND refills_remaining > 0`,
        values: [now.slice(0, 10), now, sale.prescription_id, sale.branch_id],
        expectedRows: 1,
        errorMessage:
          "Prescription is missing, inactive, out of refills, or belongs to another branch; no part of the sale was recorded.",
      });
    }

    // Inventory and prescription writes above are local projections of the
    // queued sale command. Suppress only the CRR changes created since this
    // transaction's checkpoint so protocol v2 remains the sole server writer.
    statements.push({
      sql: `INSERT OR IGNORE INTO suppressed_crr_changes
              (table_name, db_version, record_id, reason, created_at)
            SELECT DISTINCT "table", db_version, $1, 'offline_sale_projection', $2
            FROM crsql_changes
            WHERE db_version > (
              SELECT crr_start_db_version FROM offline_sales WHERE id = $1
            )
              AND "table" IN ('branch_inventory', 'prescriptions')`,
      values: [sale.id, now],
    });

    return statements;
  }

  private async allocateBatchesForDrug(
    db: Database,
    branchId: string,
    drugId: string,
    quantity: number,
  ): Promise<Array<{ batchId: string; allocatedQty: number }>> {
    const rows = await db.select<Array<{ id: string; remaining_quantity: number }>>(
      `SELECT id, remaining_quantity FROM drug_batches
       WHERE branch_id = $1
         AND drug_id = $2
         AND remaining_quantity > 0
         AND (expiry_date IS NULL OR expiry_date = '' OR DATE(expiry_date) >= DATE('now'))
       ORDER BY expiry_date ASC, created_at ASC`,
      [branchId, drugId]
    );
    let remaining = quantity;
    const allocations: Array<{ batchId: string; allocatedQty: number }> = [];
    for (const batch of rows) {
      if (remaining <= 0) break;
      const take = Math.min(Number(batch.remaining_quantity) || 0, remaining);
      if (take <= 0) continue;
      allocations.push({ batchId: batch.id, allocatedQty: take });
      remaining -= take;
    }
    if (remaining > 0) {
      throw new Error(
        `Insufficient non-expired batch stock for drug ${drugId}. ` +
        `Available: ${quantity - remaining}, requested: ${quantity}.`
      );
    }
    return allocations;
  }

  private insertStatement(
    table: string,
    record: Record<string, unknown>,
    errorMessage: string,
  ): DbTransactionStatement {
    const columns = Object.keys(record);
    return {
      sql: `INSERT INTO ${table} (${columns.join(", ")}) VALUES (${columns
        .map((_, index) => `$${index + 1}`)
        .join(", ")})`,
      values: columns.map((column) => record[column] ?? null),
      expectedRows: 1,
      errorMessage,
    };
  }

  private validateTransaction(
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: SaleItem[],
    inventoryDeltas: Array<{ drug_id: string; delta: number }>,
    idempotencyKey: string,
  ): string | null {
    if (!sale.id || !idempotencyKey || !sale.branch_id || !sale.organization_id || !sale.cashier_id) {
      return "Offline sale identity, organization, branch, cashier, and idempotency key are required.";
    }
    if (items.length === 0) return "An offline sale must contain at least one item.";

    const quantities = new Map<string, number>();
    for (const item of items) {
      if (!item.drug_id || !Number.isInteger(item.quantity) || item.quantity <= 0) {
        return "Every offline sale item requires a drug and a positive whole-number quantity.";
      }
      quantities.set(item.drug_id, (quantities.get(item.drug_id) ?? 0) + item.quantity);
    }
    const deltas = new Map<string, number>();
    for (const update of inventoryDeltas) {
      if (!update.drug_id || !Number.isInteger(update.delta) || update.delta >= 0) {
        return "Offline inventory updates must be negative whole-number sale deductions.";
      }
      deltas.set(update.drug_id, (deltas.get(update.drug_id) ?? 0) + update.delta);
    }
    if (quantities.size !== deltas.size) {
      return "Offline sale items and inventory deductions do not match.";
    }
    for (const [drugId, quantity] of quantities) {
      if (deltas.get(drugId) !== -quantity) {
        return `Offline inventory deduction does not match the sold quantity for drug ${drugId}.`;
      }
    }
    return null;
  }

  private errorMessage(error: unknown): string {
    if (error instanceof Error) return error.message;
    if (error && typeof error === "object" && "message" in error) {
      return String((error as { message: unknown }).message);
    }
    return typeof error === "string" ? error : "Failed to record sale transaction";
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
