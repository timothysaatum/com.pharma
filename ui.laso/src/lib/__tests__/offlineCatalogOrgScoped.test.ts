/**
 * The Drug Catalogue is organization-wide and must not depend on
 * branch_inventory.
 *
 * Regression: localRead.searchDrugs LEFT JOINed branch_inventory and pushed
 * `bi.branch_id = $n` into the WHERE clause, which demotes the LEFT JOIN to an
 * inner join. A drug in the org catalogue with no branch_inventory row (added to
 * the branch but never stocked) vanished from the offline list, so the catalogue
 * reported 0 while the server reported the full org list for identical data.
 *
 * Runs against real in-memory SQLite built by the production migration chain
 * (installRealDb), so the schema and query are the real ones.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { DatabaseSync } from "node:sqlite";
import {
  GEBEDOL,
  OTHER_BRANCH,
  TEST_BRANCH,
  insertInventory,
  installRealDb,
  rawDb,
  resetTables,
} from "@/lib/__tests__/realDb";

let localRead: typeof import("@/lib/localRead").localRead;

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const OTHER_ORG = "99999999-9999-9999-9999-999999999999";

interface DrugRow {
  id: string;
  organization_id: string;
  name: string;
  sku: string | null;
  is_deleted: number;
  is_active: number;
  updated_at: string;
}

/** Insert a catalogue row directly, the way a projector would. */
function insertDrug(
  raw: DatabaseSync,
  opts: { id: string; name: string; organizationId?: string; isDeleted?: number; isActive?: number }
) {
  raw
    .prepare(
      `INSERT INTO drugs (id, organization_id, name, sku, drug_type, unit_of_measure,
                          unit_price, reorder_level, reorder_quantity, tax_rate,
                          is_active, is_deleted, updated_at, created_at)
       VALUES (?, ?, ?, ?, 'otc', 'unit', 5.00, 10, 0, 0, ?, ?, ?, ?)`
    )
    .run(
      opts.id,
      opts.organizationId ?? ORG,
      opts.name,
      null,
      opts.isActive ?? 1,
      opts.isDeleted ?? 0,
      new Date().toISOString(),
      new Date().toISOString()
    );
}

beforeAll(async () => {
  await installRealDb();
  ({ localRead } = await import("@/lib/localRead"));
});

beforeEach(() => {
  resetTables(["drugs", "branch_inventory", "drug_batches", "stock_leases"]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("offline Drug Catalogue is organization-scoped", () => {
  it("returns catalogue drugs that have no branch_inventory row at all", async () => {
    // Three catalogue entries, zero stock rows anywhere.
    insertDrug(rawDb(), { id: GEBEDOL, name: "Gebedol" });
    insertDrug(rawDb(), {
      id: "aaaaaaaa-0000-0000-0000-000000000001",
      name: "Added Not Stocked",
    });
    insertDrug(rawDb(), {
      id: "aaaaaaaa-0000-0000-0000-000000000002",
      name: "Also Not Stocked",
    });

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);

    expect(result.total).toBe(3);
    const names = result.items.map((d) => (d as unknown as DrugRow).name).sort();
    expect(names).toEqual(["Added Not Stocked", "Also Not Stocked", "Gebedol"]);
  });

  it("still lists a drug whose stock exists only at a different branch", async () => {
    insertDrug(rawDb(), { id: GEBEDOL, name: "Gebedol" });
    insertInventory(rawDb(), {
      drugId: GEBEDOL,
      branchId: OTHER_BRANCH,
      quantity: 40,
    });

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);

    expect(result.total).toBe(1);
    expect(result.items[0].name).toBe("Gebedol");
  });

  it("ignores branch_id entirely: same rows with or without it", async () => {
    insertDrug(rawDb(), { id: GEBEDOL, name: "Gebedol" });
    insertDrug(rawDb(), {
      id: "bbbbbbbb-0000-0000-0000-000000000001",
      name: "Never Stocked",
    });
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117 });

    const without = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);
    const with_ = await localRead.searchDrugs(
      { organization_id: ORG, branch_id: TEST_BRANCH },
      1,
      20
    );

    expect(with_.total).toBe(without.total);
    expect(with_.total).toBe(2);
  });

  it("scopes to the requested organization", async () => {
    insertDrug(rawDb(), { id: GEBEDOL, name: "Gebedol" });
    insertDrug(rawDb(), {
      id: "cccccccc-0000-0000-0000-000000000001",
      name: "Other Tenant Drug",
      organizationId: OTHER_ORG,
    });

    const ours = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);
    const theirs = await localRead.searchDrugs({ organization_id: OTHER_ORG }, 1, 20);

    expect(ours.total).toBe(1);
    expect(ours.items[0].name).toBe("Gebedol");
    expect(theirs.total).toBe(1);
    expect(theirs.items[0].name).toBe("Other Tenant Drug");
  });

  it("excludes soft-deleted rows", async () => {
    insertDrug(rawDb(), { id: GEBEDOL, name: "Gebedol" });
    insertDrug(rawDb(), {
      id: "dddddddd-0000-0000-0000-000000000001",
      name: "Deleted Drug",
      isDeleted: 1,
    });

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);

    expect(result.total).toBe(1);
    expect(result.items[0].name).toBe("Gebedol");
  });

  it("keeps honouring is_active and search filters", async () => {
    insertDrug(rawDb(), { id: GEBEDOL, name: "Gebedol" });
    insertDrug(rawDb(), {
      id: "eeeeeeee-0000-0000-0000-000000000001",
      name: "Paracetamol 500mg",
      isActive: 0,
    });

    const activeOnly = await localRead.searchDrugs(
      { organization_id: ORG, is_active: true },
      1,
      20
    );
    expect(activeOnly.total).toBe(1);

    const bySearch = await localRead.searchDrugs(
      { organization_id: ORG, search: "gebedol" },
      1,
      20
    );
    expect(bySearch.total).toBe(1);
    expect(bySearch.items[0].name).toBe("Gebedol");
  });

  it("matches the server's org-wide behaviour for the real six-drug shape", async () => {
    // Six catalogue rows, two of them with stock at the active branch. This is
    // the exact asymmetry that produced "Catalogue 0".
    const six = [
      GEBEDOL,
      "9038e0e0-459f-4a6f-8bd1-72bb2b8447e0",
      "c13dae4f-8f37-4558-a723-18c69bfd3ad0",
      "378b5603-324b-47cd-87ff-069e7eb5ff77",
      "5dadb522-766f-4789-930e-dddefb0964ef",
      "be0052c2-c08b-41a2-9707-54bedca1a01c",
    ];
    six.forEach((id, i) => insertDrug(rawDb(), { id, name: `Drug ${i}` }));
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117 });

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);
    expect(result.total).toBe(6);
  });
});