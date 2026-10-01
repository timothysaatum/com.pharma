/**
 * sellableQty.ts
 * =============
 * Local mirror of the server's canonical sellable-quantity rule.
 *
 * Server reference: backend.laso/app/services/sync/_sellable_qty.py
 * (compute_sellable_quantities). The rules are kept identical on purpose:
 *
 *   base   = SUM(drug_batches.remaining_quantity)
 *            WHERE branch_id, drug_id
 *              AND remaining_quantity > 0
 *              AND (expiry_date IS NULL OR expiry_date >= today)
 *   locked = SUM(stock_leases.leased_quantity - consumed_quantity)
 *            WHERE branch_id, drug_id, status = 'active', expires_at > now
 *              AND terminal_id != this terminal
 *   result = MAX(0, base - locked)
 *
 * One deliberate difference from the server: when a device holds NO batch rows
 * at all for a (branch, drug), `base` falls back to branch_inventory.quantity.
 * The server has the authoritative batch rows and never needs that fallback,
 * but a device can legitimately hold an inventory row before its first
 * drug_batches event arrives. Falling back keeps the cart sellable instead of
 * reporting an empty shelf for a drug Inventory shows as stocked.
 *
 * The fallback keys off "does this device hold any batch row for this pair",
 * NOT off "did the filtered sum come out zero". Those are different states: a
 * drug whose only batch has expired must read 0 (expired stock is not sellable),
 * and must not be rescued by a stale branch_inventory.quantity.
 *
 * Why this exists: local branch_inventory.sellable_quantity is declared
 * NOT NULL DEFAULT 0, and localRead.getSellableQuantity() reads that column
 * before falling back to quantity. Nothing ever wrote the column, so every row
 * stayed at 0 and the POS cart showed "/0" and "Only 0 available" for drugs
 * that Inventory listed as fully stocked. Every writer of branch_inventory,
 * drug_batches, or stock_leases must call refreshSellableQuantity() so the
 * column is never stale.
 */

import type { Database } from "@/lib/localDb";

/** ISO date (YYYY-MM-DD) used for the batch expiry comparison. */
function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * This device's terminal id, matching LeaseEngine.getTerminalId(). A lease
 * held by this terminal is not "locked away from us", so it must be excluded
 * from the subtraction.
 */
export function currentTerminalId(): string {
  if (typeof localStorage === "undefined") return "TERM-SERVER";
  const tid = localStorage.getItem("laso_terminal_id");
  if (tid) return tid;
  const generated = "TERM-" + Math.random().toString(36).substring(2, 10).toUpperCase();
  try {
    localStorage.setItem("laso_terminal_id", generated);
  } catch {
    // Ignore: a terminal id that cannot be persisted still works in-memory.
  }
  return generated;
}

export interface SellableOptions {
  /**
   * Exclude leases held by this terminal from the subtraction. Pass
   * {@link currentTerminalId} in production. Omit to subtract every active
   * lease, which is what a test asserting raw arithmetic wants.
   */
  excludeTerminalId?: string | null;
}

/**
 * Compute the sellable quantity for one (branch_id, drug_id) from local state.
 * Pure read: writes nothing.
 */
export async function computeSellableQuantity(
  db: Database,
  branchId: string,
  drugId: string,
  options: SellableOptions = {}
): Promise<number> {
  // Does this device hold any batch row for the pair at all? Unfiltered by
  // expiry and remaining_quantity, because this only answers "has the device
  // received the batches yet", which is what licenses the fallback below.
  const anyBatchRows = await db.select<{ n: number }[]>(
    `SELECT COUNT(*) AS n FROM drug_batches WHERE branch_id = $1 AND drug_id = $2`,
    [branchId, drugId]
  );
  const hasAnyBatch = Number(anyBatchRows?.[0]?.n ?? 0) > 0;

  let base: number;
  if (!hasAnyBatch) {
    const invRows = await db.select<{ quantity: number | null }[]>(
      "SELECT quantity FROM branch_inventory WHERE branch_id = $1 AND drug_id = $2 LIMIT 1",
      [branchId, drugId]
    );
    base = Number(invRows?.[0]?.quantity ?? 0);
    if (Number.isNaN(base)) base = 0;
  } else {
    const batchRows = await db.select<{ total: number | null }[]>(
      `SELECT SUM(remaining_quantity) AS total
         FROM drug_batches
        WHERE branch_id = $1
          AND drug_id = $2
          AND remaining_quantity > 0
          AND (expiry_date IS NULL OR expiry_date >= $3)`,
      [branchId, drugId, todayIso()]
    );
    base = Number(batchRows?.[0]?.total ?? 0);
    if (Number.isNaN(base)) base = 0;
  }

  const nowUtc = new Date().toISOString();
  const exclude = options.excludeTerminalId ?? null;
  const leaseRows = exclude
    ? await db.select<{ total: number | null }[]>(
        `SELECT SUM(leased_quantity - consumed_quantity) AS total
           FROM stock_leases
          WHERE branch_id = $1
            AND drug_id = $2
            AND status = 'active'
            AND expires_at > $3
            AND terminal_id != $4`,
        [branchId, drugId, nowUtc, exclude]
      )
    : await db.select<{ total: number | null }[]>(
        `SELECT SUM(leased_quantity - consumed_quantity) AS total
           FROM stock_leases
          WHERE branch_id = $1
            AND drug_id = $2
            AND status = 'active'
            AND expires_at > $3`,
        [branchId, drugId, nowUtc]
      );

  const locked = Number(leaseRows?.[0]?.total ?? 0);

  return Math.max(0, base - (Number.isNaN(locked) ? 0 : locked));
}

/**
 * Recompute sellable_quantity and persist it onto the branch_inventory row for
 * (branch_id, drug_id). No-op when no such row exists: sellable_quantity is a
 * property of an inventory row, so there is nothing to write.
 *
 * Call this after any write that changes a branch_inventory quantity, a
 * drug_batches remaining_quantity, or a stock_leases row for that pair.
 */
export async function refreshSellableQuantity(
  db: Database,
  branchId: string,
  drugId: string,
  options: SellableOptions = {}
): Promise<void> {
  if (!branchId || !drugId) return;
  const exists = await db.select<{ id: string }[]>(
    "SELECT id FROM branch_inventory WHERE branch_id = $1 AND drug_id = $2 LIMIT 1",
    [branchId, drugId]
  );
  if (!exists || exists.length === 0) return;

  const sellable = await computeSellableQuantity(db, branchId, drugId, {
    excludeTerminalId: options.excludeTerminalId ?? currentTerminalId(),
  });

  await db.execute(
    "UPDATE branch_inventory SET sellable_quantity = $1 WHERE branch_id = $2 AND drug_id = $3",
    [sellable, branchId, drugId]
  );
}

/**
 * Refresh several (branch_id, drug_id) pairs, de-duplicated. Used by the
 * batch-applying projectors, which touch many drugs in one event.
 */
export async function refreshSellableForPairs(
  db: Database,
  pairs: Array<{ branchId: string; drugId: string }>,
  options: SellableOptions = {}
): Promise<void> {
  const seen = new Set<string>();
  const terminalId = options.excludeTerminalId ?? currentTerminalId();
  for (const { branchId, drugId } of pairs) {
    if (!branchId || !drugId) continue;
    const key = `${branchId}::${drugId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    await refreshSellableQuantity(db, branchId, drugId, { excludeTerminalId: terminalId });
  }
}