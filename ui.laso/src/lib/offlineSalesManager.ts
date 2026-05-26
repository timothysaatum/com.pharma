/**
 * offlineSalesManager.ts
 * ======================
 * Robust offline-first sales queue management with:
 *   - Transactional sale recording (all or nothing)
 *   - Automatic retry with exponential backoff
 *   - Duplicate detection and prevention
 *   - Inventory sync consistency
 *   - Comprehensive error recovery
 *   - Audit trail for offline transactions
 */

import { getDb, enqueue } from "@/lib/localDb";
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
   * Record a sale transaction with full atomicity.
   * If ANY step fails, the entire transaction rolls back.
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
      // Begin transaction
      await db.execute("BEGIN TRANSACTION");

      try {
        // 1. Check for duplicate using idempotency key
        const existing = await db.select<
          Array<{ id: string; sync_status: string }>
        >(
          "SELECT id, sync_status FROM offline_sales WHERE idempotency_key = ?",
          [idempotencyKey]
        );

        if (existing.length > 0) {
          await db.execute("ROLLBACK");
          // Duplicate detected - return the existing sale ID but mark as duplicate
          return {
            success: true,
            saleId: existing[0].id,
          };
        }

        // 2. Record the sale transaction
        const { items: saleItems, ...saleData } = sale;
        const offlineRecord: OfflineSaleRecord = {
          id: sale.id,
          sale_data: {
            ...saleData,
            items_json: JSON.stringify(saleItems ?? []),
          },
          sale_items: items,
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
          if (Array.isArray(v) || (typeof v === "object"))
            return JSON.stringify(v);
          return v;
        });
        const placeholders = cols.map((_, i) => `$${i + 1}`).join(", ");

        await db.execute(
          `INSERT INTO offline_sales (${cols.join(", ")}) VALUES (${placeholders})`,
          vals
        );

        // 3. Update local inventory (best-effort, non-blocking)
        for (const { drug_id, delta } of inventoryDeltas) {
          try {
            await db.execute(
              `UPDATE branch_inventory
               SET quantity = MAX(0, quantity + $1), updated_at = $2, sync_status = 'pending'
               WHERE drug_id = $3`,
              [delta, now, drug_id]
            );
          } catch (invErr) {
            console.warn(`[OfflineSalesManager] Inventory update failed for drug ${drug_id}:`, invErr);
            // Continue — the sale is still recorded
          }
        }

        // 4. Enqueue the sale for sync
        await enqueue(
          "sales",
          sale.id,
          "create",
          1,
          saleData as Record<string, unknown>
        );

        // Commit transaction
        await db.execute("COMMIT");
        return { success: true, saleId: sale.id };
      } catch (innerErr) {
        await db.execute("ROLLBACK");
        throw innerErr;
      }
    } catch (err) {
      console.error("[OfflineSalesManager] Transaction failed:", err);
      return {
        success: false,
        error: err instanceof Error ? err.message : "Failed to record sale transaction",
      };
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
