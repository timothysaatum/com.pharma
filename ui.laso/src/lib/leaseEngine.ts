import { getDb } from "./localDb";
import { fetchAuthSession } from "aws-amplify/auth";
import { z } from "zod";

const API_BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const LeaseResponseSchema = z.array(z.object({
  id: z.string(),
  branch_id: z.string(),
  drug_id: z.string(),
  terminal_id: z.string(),
  leased_quantity: z.number(),
  consumed_quantity: z.number(),
  expires_at: z.string(),
  status: z.string()
}));

export class LeaseEngine {
  private timer: number | null = null;
  private isRunning = false;

  public static getTerminalId(): string {
    let tid = localStorage.getItem("laso_terminal_id");
    if (!tid) {
      tid = "TERM-" + Math.random().toString(36).substring(2, 10).toUpperCase();
      localStorage.setItem("laso_terminal_id", tid);
    }
    return tid;
  }

  public start(branchId: string, intervalMs = 60000) {
    if (this.isRunning) return;
    this.isRunning = true;
    
    // Run immediately
    this.runCycle(branchId).catch(console.error);

    // Then schedule
    this.timer = window.setInterval(() => {
      this.runCycle(branchId).catch(console.error);
    }, intervalMs);
  }

  public stop() {
    this.isRunning = false;
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }

  private async runCycle(branchId: string) {
    if (!navigator.onLine) return;

    try {
      const db = await getDb();
      const terminalId = LeaseEngine.getTerminalId();

      // We need to request leases for fast moving items, or items we currently have in stock.
      // For simplicity, let's request leases for all items with sellable_quantity > 0, up to a certain limit (e.g., 50 per terminal).
      // Or we can just request 10 units for every drug in the branch inventory?
      // Actually, a smarter approach is to look at average sales, but for now we'll request a fixed amount 
      // or half of the unleased pool. Let's request 10 units for top 100 drugs, or whatever.
      
      const rows = await db.select<{drug_id: string, sellable_quantity: number}[]>(
        `SELECT drug_id, sellable_quantity FROM branch_inventory 
         WHERE branch_id = $1 AND sellable_quantity > 0 
         ORDER BY updated_at DESC LIMIT 100`,
        [branchId]
      );

      if (rows.length === 0) return;

      const items = rows.map(r => [r.drug_id, Math.min(10, r.sellable_quantity)] as [string, number]);
      
      // Request from server
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) return;

      const response = await fetch(`${API_BASE_URL}/api/v1/inventory/branch/${branchId}/leases/acquire`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          terminal_id: terminalId,
          items: items,
          ttl_seconds: 3600
        })
      });

      if (!response.ok) {
        console.error("[LeaseEngine] Failed to acquire leases:", await response.text());
        return;
      }

      const data = await response.json();
      const leases = LeaseResponseSchema.parse(data);

      // Save to local db
      await db.execute("BEGIN TRANSACTION");
      try {
        for (const lease of leases) {
          await db.execute(
            `INSERT INTO stock_leases (id, branch_id, drug_id, terminal_id, leased_quantity, consumed_quantity, expires_at, status, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
             ON CONFLICT(id) DO UPDATE SET 
               leased_quantity = excluded.leased_quantity,
               consumed_quantity = excluded.consumed_quantity,
               expires_at = excluded.expires_at,
               status = excluded.status,
               updated_at = excluded.updated_at`,
            [lease.id, lease.branch_id, lease.drug_id, lease.terminal_id, lease.leased_quantity, lease.consumed_quantity, lease.expires_at, lease.status, new Date().toISOString(), new Date().toISOString()]
          );
        }
        await db.execute("COMMIT");
      } catch (err) {
        await db.execute("ROLLBACK");
        throw err;
      }
      
    } catch (err) {
      console.error("[LeaseEngine] Cycle error:", err);
    }
  }
}

export const leaseEngine = new LeaseEngine();
