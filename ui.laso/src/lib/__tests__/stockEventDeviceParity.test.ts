/**
 * Stock-event parity: does replaying a real service call's events actually drive
 * a device to the server's state?
 *
 * The backend half (backend.laso/tests/integration/test_stock_event_device_parity.py)
 * calls the REAL service functions against disposable PostgreSQL, captures every
 * event they appended, and writes both the events and the server's resulting rows
 * to fixtures/stock-parity.json. This file is the other half: it replays those
 * events through the REAL projectors onto a real in-memory device and compares.
 *
 * Two device states per scenario, because they fail differently:
 *
 *   fresh    nothing local. Every event is new.
 *   partial  some lots already present, with the server's PRE-change quantities,
 *            as a device that synced earlier and missed some events would hold.
 *
 * Compared per scenario: branch_inventory.quantity, every drug_batches row
 * (id, remaining_quantity, expiry_date) and sellable_quantity. They must match
 * the server exactly. A device that disagrees is a device showing wrong stock.
 *
 * The table is printed to stderr on every run.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb, resetTables } from "./realDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";

interface ServerBatch {
  id: string;
  batch_number: string;
  quantity: number;
  remaining_quantity: number;
  expiry_date: string | null;
}
interface ServerState {
  branch_inventory: { quantity: number; reserved_quantity: number } | null;
  drug_batches: ServerBatch[];
}
interface Scenario {
  branch_id: string;
  drug_id: string;
  events: EventEnvelope[];
  server: ServerState;
  note: string;
}
type Contract = Record<string, Scenario>;

const CONTRACT: Contract = JSON.parse(
  readFileSync(join(__dirname, "fixtures", "stock-parity.json"), "utf8")
);

let applyEventLocally: (e: EventEnvelope) => Promise<void>;

interface Row {
  scenario: string;
  device: string;
  serverQty: number;
  localQty: number;
  lotsMatch: boolean;
  sellableMatch: boolean;
  ok: boolean;
  detail: string;
}
const rows: Row[] = [];

/** The device's own view, in the same shape as the server's. */
function deviceState(branchId: string, drugId: string) {
  const inv = rawDb()
    .prepare(
      "SELECT quantity, sellable_quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?"
    )
    .get(branchId, drugId) as
    | { quantity: number; sellable_quantity: number }
    | undefined;

  const batches = (
    rawDb()
      .prepare(
        "SELECT id, batch_number, quantity, remaining_quantity, expiry_date FROM drug_batches WHERE branch_id = ? AND drug_id = ? ORDER BY batch_number"
      )
      .all(branchId, drugId) as Array<{
      id: string;
      batch_number: string;
      quantity: number;
      remaining_quantity: number;
      expiry_date: string | null;
    }>
  ).map((b) => ({
    id: b.id,
    batch_number: b.batch_number,
    quantity: Number(b.quantity),
    remaining_quantity: Number(b.remaining_quantity),
    expiry_date: b.expiry_date,
  }));

  return {
    branch_inventory: inv
      ? { quantity: Number(inv.quantity), sellable_quantity: Number(inv.sellable_quantity) }
      : null,
    drug_batches: batches,
  };
}

/** Compare id, remaining_quantity and expiry for every lot, order-insensitively. */
function lotsMatch(server: ServerBatch[], local: ServerBatch[]): { ok: boolean; detail: string } {
  const norm = (xs: ServerBatch[]) =>
    xs
      .map((b) => `${b.id}|${b.remaining_quantity}|${b.expiry_date ?? ""}`)
      .sort();
  const a = norm(server);
  const b = norm(local);
  if (a.length === b.length && a.every((v, i) => v === b[i])) return { ok: true, detail: "" };
  return {
    ok: false,
    detail: `server[${a.join(" ")}] vs device[${b.join(" ")}]`,
  };
}

/**
 * Seed a "partial" device: the lot ROWS already exist, at quantity 0.
 *
 * The point is to exercise the "row is already here" path — the ON CONFLICT
 * upsert in _drugBatchUpserted, and the derivation reading an existing lot set
 * rather than inserting one. Every stock event carries an ABSOLUTE
 * remaining_quantity (that is the ruling), so replaying it over a stale row must
 * correct the row to the server's value; seeding at 0 makes that visible instead
 * of coincidentally pre-loading the answer.
 *
 * Timestamps are required columns, hence the explicit values.
 */
async function seedPartial(scenario: Scenario): Promise<void> {
  const now = new Date().toISOString();
  for (const e of scenario.events) {
    if (e.event_type !== "drug_batch_created" && e.event_type !== "drug_batch_updated") {
      continue;
    }
    const p = e.payload as Record<string, unknown>;
    if (
      rawDb()
        .prepare("SELECT id FROM drug_batches WHERE id = ?")
        .get(String(e.aggregate_id))
    ) {
      continue;
    }
    rawDb()
      .prepare(
        `INSERT INTO drug_batches
           (id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
            manufacturing_date, expiry_date, sync_status, sync_version,
            synced_at, updated_at, created_at)
         VALUES (?,?,?,?,0,0,NULL,?,'synced',1,NULL,?,?)`
      )
      .run(
        String(e.aggregate_id),
        scenario.branch_id,
        scenario.drug_id,
        String(p.batch_number ?? ""),
        (p.expiry_date as string) ?? null,
        now,
        now
      );
  }
}

async function runScenario(
  name: string,
  scenario: Scenario,
  device: "fresh" | "partial"
): Promise<void> {
  resetTables(["applied_events", "branch_inventory", "drug_batches", "sales", "drugs"]);
  if (device === "partial") await seedPartial(scenario);

  for (const ev of scenario.events) {
    await applyEventLocally({ ...ev, event_id: `${ev.event_id}-${device}` });
  }

  const local = deviceState(scenario.branch_id, scenario.drug_id);
  const server = scenario.server;

  const serverQty = server.branch_inventory?.quantity ?? 0;
  const localQty = local.branch_inventory?.quantity ?? 0;
  const lots = lotsMatch(server.drug_batches, local.drug_batches);

  // sellable_quantity has NO server column, so parity means "agrees with the
  // device's own rule applied to the server's lots": every lot unexpired and
  // positive sums to the server quantity, with no leases held.
  const expectedSellable = server.drug_batches
    .filter((b) => b.remaining_quantity > 0)
    .reduce((t, b) => t + b.remaining_quantity, 0);
  const localSellable = local.branch_inventory?.sellable_quantity ?? 0;
  const sellableMatch = localSellable === expectedSellable;

  const ok = localQty === serverQty && lots.ok && sellableMatch;
  rows.push({
    scenario: name,
    device,
    serverQty,
    localQty,
    lotsMatch: lots.ok,
    sellableMatch,
    ok,
    detail: lots.ok ? (sellableMatch ? "" : `sellable ${localSellable} != ${expectedSellable}`) : lots.detail,
  });

  expect(localQty, `${name} [${device}] quantity`).toBe(serverQty);
  expect(lots.ok, `${name} [${device}] lots — ${lots.detail}`).toBe(true);
  expect(sellableMatch, `${name} [${device}] sellable`).toBe(true);
}

describe("stock events drive a device to the server's state", () => {
  beforeAll(async () => {
    await installRealDb();
    ({ applyEventLocally } = await import("@/lib/localProjectors"));
  });

  beforeEach(() => {
    // localStorage is read by currentTerminalId(); leases are not part of this
    // contract, so an empty store keeps the sellable maths deterministic.
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
      clear: () => {},
      key: () => null,
      length: 0,
    } as unknown as Storage;
  });

  afterAll(() => {
    const pad = (s: string, n: number) => s.padEnd(n);
    const padL = (s: string, n: number) => s.padStart(n);
    let out = "";
    out += "\n\n" + "=".repeat(112) + "\n";
    out += "STOCK EVENT PARITY — real service call replayed on a real device\n";
    out += "=".repeat(112) + "\n";
    out +=
      pad("scenario", 40) +
      pad("device", 9) +
      padL("server", 8) +
      padL("local", 8) +
      pad("  lots", 8) +
      pad("sellable", 10) +
      "verdict\n";
    out += "-".repeat(112) + "\n";
    for (const r of rows) {
      out +=
        pad(r.scenario.slice(0, 39), 40) +
        pad(r.device, 9) +
        padL(String(r.serverQty), 8) +
        padL(String(r.localQty), 8) +
        pad(r.lotsMatch ? "match" : "DIFFER", 8) +
        pad(r.sellableMatch ? "match" : "DIFFER", 10) +
        (r.ok ? "OK" : "MISMATCH") +
        (r.detail ? `  ${r.detail}` : "") +
        "\n";
    }
    out += "-".repeat(112) + "\n";
    const bad = rows.filter((r) => !r.ok);
    out += bad.length
      ? `${bad.length} of ${rows.length} scenario/device combinations DISAGREE with the server.\n`
      : `all ${rows.length} scenario/device combinations agree with the server.\n`;
    out += "=".repeat(112) + "\n";
    console.error(out);
  });

  for (const [name, scenario] of Object.entries(CONTRACT)) {
    it(`${name} — fresh device`, async () => {
      await runScenario(name, scenario, "fresh");
    });
    it(`${name} — partial device`, async () => {
      await runScenario(name, scenario, "partial");
    });
  }
});
