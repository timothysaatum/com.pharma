/**
 * Backfill parity: does replaying the backfilled event log give a device the
 * server's stock?
 *
 * The backend half (backend.laso/tests/integration/test_backfill_stock_events.py)
 * runs the REAL backfill script against a disposable PostgreSQL, then writes the
 * resulting log plus the server's rows to
 * fixtures/stock-backfill-parity.json. This file replays that log through the
 * REAL projectors and checks the device ends up holding all six real drugs with
 * the agreed quantities.
 *
 * Two device states:
 *
 *   fresh    nothing local — the situation this whole backfill exists to fix.
 *   partial  the device already holds SOME data (here: a stand-in for the 69
 *            events the live device has already applied), then pulls the rest.
 *            It must converge to the same state, not accumulate.
 *
 * Quantities are asserted against the ruling's numbers explicitly rather than
 * only against the fixture, so a fixture change cannot quietly move the goalposts.
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
interface ServerDrug {
  id: string;
  name: string;
  quantity: number;
  reserved_quantity: number;
  batches: ServerBatch[];
}
interface Contract {
  branch_id: string;
  events: EventEnvelope[];
  drugs: ServerDrug[];
  expected_qty: Record<string, number>;
}

const CONTRACT: Contract = JSON.parse(
  readFileSync(join(__dirname, "fixtures", "stock-backfill-parity.json"), "utf8")
);

/** The ruling. Asserted directly so the fixture cannot redefine success. */
const RULING = {
  Amoxicilin: 247,
  Gebedol: 117,
  "Ibuprofen-0561": 40,
  "Minoxidil Oil": 100,
  "Paracetamol 500mg": 198,
  "Paracetamol-9014": 50,
} as const;

let applyEventLocally: (e: EventEnvelope) => Promise<void>;

interface Row {
  drug: string;
  device: string;
  serverQty: number;
  localQty: number;
  sellable: number;
  lots: number;
  ok: boolean;
  detail: string;
}
const rows: Row[] = [];

/** Insert a drug row so _drugCreated's upsert has something to update. */
function seedDrug(id: string, name: string): void {
  const now = new Date().toISOString();
  rawDb()
    .prepare(
      `INSERT INTO drugs (id, name, sku, drug_type, reorder_quantity,
                          unit_price, reorder_level, is_active, is_deleted,
                          sync_status, sync_version, created_at, updated_at)
       VALUES (?,?,NULL,'otc',0,'0.00',0,1,0,'synced',1,?,?)`
    )
    .run(id, name, now, now);
}

function readQty(drugId: string, branchId: string) {
  const row = rawDb()
    .prepare(
      "SELECT quantity, sellable_quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?"
    )
    .get(branchId, drugId) as
    | { quantity: number; sellable_quantity: number }
    | undefined;
  return {
    quantity: row ? Number(row.quantity) : -1,
    sellable: row ? Number(row.sellable_quantity) : -1,
  };
}

function readLots(drugId: string, branchId: string) {
  return (
    rawDb()
      .prepare(
        "SELECT id, remaining_quantity FROM drug_batches WHERE branch_id = ? AND drug_id = ? ORDER BY id"
      )
      .all(branchId, drugId) as Array<{ id: string; remaining_quantity: number }>
  ).map((b) => `${b.id}|${Number(b.remaining_quantity)}`);
}

/**
 * A device that already holds some of the data.
 *
 * The live device has applied 69 events, so it holds drug and category rows but
 * none of the stock. That is modelled by seeding every drug row and replaying
 * the drug events ONLY — the stock events are then pulled on top, which is the
 * case that matters: the derivation must land on the same numbers whether the
 * device started empty or not.
 */
async function seedPartial(): Promise<number> {
  for (const d of CONTRACT.drugs) seedDrug(d.id, d.name);
  let n = 0;
  for (const ev of CONTRACT.events) {
    if (ev.event_type !== "drug_created" && ev.event_type !== "drug_category_created") {
      continue;
    }
    await applyEventLocally({ ...ev, event_id: `${ev.event_id}-pre` });
    n += 1;
  }
  return n;
}

async function runScenario(device: "fresh" | "partial"): Promise<void> {
  resetTables([
    "applied_events", "branch_inventory", "drug_batches",
    "drugs", "drug_categories", "stock_leases",
  ]);

  let preApplied = 0;
  if (device === "partial") preApplied = await seedPartial();

  for (const ev of CONTRACT.events) {
    await applyEventLocally({ ...ev, event_id: `${ev.event_id}-${device}` });
  }

  for (const d of CONTRACT.drugs) {
    const local = readQty(d.id, CONTRACT.branch_id);
    const serverLots = d.batches
      .map((b) => `${b.id}|${b.remaining_quantity}`)
      .sort();
    const localLots = readLots(d.id, CONTRACT.branch_id).sort();

    const expected = RULING[d.name as keyof typeof RULING];
    const lotsMatch = serverLots.length === localLots.length &&
      serverLots.every((v, i) => v === localLots[i]);
    // sellable must equal the derived quantity: every lot is unexpired and there
    // are no leases, so sellable == quantity == sum of positive lots.
    const ok = local.quantity === expected && local.sellable === expected && lotsMatch;

    let detail = "";
    if (local.quantity !== expected) detail = `qty ${local.quantity} != ${expected}`;
    else if (local.sellable !== expected) detail = `sellable ${local.sellable} != ${expected}`;
    else if (!lotsMatch) detail = `lots differ`;

    rows.push({
      drug: d.name,
      device,
      serverQty: d.quantity,
      localQty: local.quantity,
      sellable: local.sellable,
      lots: localLots.length,
      ok,
      detail,
    });

    expect(local.quantity, `${d.name} [${device}] quantity`).toBe(expected);
    expect(local.sellable, `${d.name} [${device}] sellable_quantity`).toBe(expected);
    expect(lotsMatch, `${d.name} [${device}] lots`).toBe(true);
  }

  // The contract itself must carry the ruling's quantities, so a drift in the
  // fixture is caught here rather than silently redefining the target.
  expect(CONTRACT.expected_qty).toEqual({ ...RULING });
  expect(preApplied >= 0).toBe(true);
}

describe("backfilled event log rebuilds the device", () => {
  beforeAll(async () => {
    await installRealDb();
    ({ applyEventLocally } = await import("@/lib/localProjectors"));
  });

  beforeEach(() => {
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
    let out = "\nBACKFILL DEVICE PARITY - replaying the backfilled log on a real device\n";
    out += "drug | device | server qty | local qty | sellable | lots | verdict\n";
    for (const r of rows) {
      out +=
        `${r.drug} | ${r.device} | ${r.serverQty} | ${r.localQty} | ${r.sellable} | ` +
        `${r.lots} | ${r.ok ? "OK" : "MISMATCH"}${r.detail ? ` (${r.detail})` : ""}\n`;
    }
    const bad = rows.filter((r) => !r.ok);
    out += bad.length
      ? `${bad.length} of ${rows.length} DISAGREE with the server\n`
      : `all ${rows.length} drug/device combinations agree with the server\n`;
    console.error(out);
  });

  it("a fresh device ends up with all six drugs at the agreed quantities", async () => {
    await runScenario("fresh");
  });

  it("a partial device converges to the same state", async () => {
    await runScenario("partial");
  });

  it("replaying the whole log twice does not change anything", async () => {
    resetTables([
      "applied_events", "branch_inventory", "drug_batches",
      "drugs", "drug_categories", "stock_leases",
    ]);
    for (const ev of CONTRACT.events) {
      await applyEventLocally({ ...ev, event_id: `${ev.event_id}-replay1` });
    }
    const first = CONTRACT.drugs.map((d) => readQty(d.id, CONTRACT.branch_id));

    // A second pass, as a cursor rewind would do.
    for (const ev of CONTRACT.events) {
      await applyEventLocally({ ...ev, event_id: `${ev.event_id}-replay2` });
    }
    const second = CONTRACT.drugs.map((d) => readQty(d.id, CONTRACT.branch_id));

    for (const d of CONTRACT.drugs) {
      const expected = RULING[d.name as keyof typeof RULING];
      expect(readQty(d.id, CONTRACT.branch_id).quantity, `${d.name} after a second pass`).toBe(
        expected
      );
    }
    expect(second).toEqual(first);
  });
});
