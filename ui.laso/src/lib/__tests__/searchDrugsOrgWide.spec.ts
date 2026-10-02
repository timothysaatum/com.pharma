/**
 * Phase 1 regression tests: the offline Drug Catalogue is org-wide.
 *
 * The bug: `localRead.searchDrugs` accepted `branch_id` and joined
 * `branch_inventory` to filter rows down to drugs stocked at the branch. The
 * online catalogue is organization-wide, so the two disagreed — and a device
 * that had received no stock events rendered the catalogue as empty (or as the
 * handful of sentinel rows an E2E run had left behind).
 *
 * Runs against a REAL in-memory SQLite built by the real localDb migration
 * chain via the repo's installRealDb() helper. Never touches laso.db.
 */
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const OTHER_ORG = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const GEBEDOL = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";
const OTHER_BRANCH = "11111111-2222-3333-4444-555555555555";

let localRead: any;

beforeAll(async () => {
  await installRealDb();
  localRead = (await import("@/lib/localRead")).localRead;
});

function insertDrug(
  id: string,
  name: string,
  opts: { org?: string; deleted?: boolean; sku?: string } = {}
): void {
  rawDb()
    .prepare(
      `INSERT INTO drugs (id, organization_id, name, generic_name, brand_name, sku, barcode,
        category_id, drug_type, dosage_form, strength, manufacturer, supplier,
        requires_prescription, controlled_substance_schedule, ndc_code, unit_price, cost_price,
        markup_percentage, tax_rate, reorder_level, reorder_quantity, max_stock_level,
        unit_of_measure, description, usage_instructions, side_effects, contraindications,
        storage_conditions, image_url, is_active, is_deleted,
        sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES (?,'${opts.org ?? ORG}',?,NULL,NULL,?,NULL,NULL,'otc',NULL,NULL,NULL,NULL,
        0,NULL,NULL,5.0,NULL,NULL,0.0,10,50,NULL,'unit',NULL,NULL,NULL,NULL,NULL,NULL,1,?,
        'synced',1,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`
    )
    .run(id, name, opts.sku ?? `SKU-${id.slice(0, 4)}`, opts.deleted ? 1 : 0);
}

beforeEach(() => {
  rawDb().exec("PRAGMA foreign_keys = OFF");
  rawDb().exec("DELETE FROM drugs");
  rawDb().exec("DELETE FROM branch_inventory");
});

describe("localRead.searchDrugs is org-wide", () => {
  it("returns every org drug even when branch_inventory is completely empty", async () => {
    insertDrug(GEBEDOL, "Gebedol");
    insertDrug("d2", "Amoxicilin");
    insertDrug("d3", "Minoxidil Oil");

    expect(rawDb().prepare("SELECT COUNT(*) n FROM branch_inventory").get()).toEqual({ n: 0 });

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);

    expect(result.total).toBe(3);
    expect(result.items.map((d: any) => d.name).sort()).toEqual([
      "Amoxicilin",
      "Gebedol",
      "Minoxidil Oil",
    ]);
  });

  it("returns drugs the branch has no branch_inventory row for", async () => {
    insertDrug(GEBEDOL, "Gebedol");
    // Only a DIFFERENT drug has stock at the branch.
    insertDrug("d2", "Stocked Drug");
    rawDb()
      .prepare(
        `INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, reserved_quantity,
          sellable_quantity, location, selling_price, sync_status, sync_version, synced_at,
          updated_at, created_at)
         VALUES ('bi-1', ?, 'd2', 50, 0, 50, NULL, 5.0, 'synced', 1, NULL,
          '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`
      )
      .run(BRANCH);

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);

    // Both drugs appear. Under the old branch-filtered query, Gebedol
    // (no branch_inventory row) would have been excluded.
    expect(result.total).toBe(2);
    expect(result.items.map((d: any) => d.name).sort()).toEqual([
      "Gebedol",
      "Stocked Drug",
    ]);
  });

  it("ignores a branch_id argument entirely — the catalogue is not branch-scoped", async () => {
    insertDrug(GEBEDOL, "Gebedol");
    insertDrug("d2", "Amoxicilin");
    // branch_inventory has a row for d2 only, at the branch.
    rawDb()
      .prepare(
        `INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, reserved_quantity,
          sellable_quantity, location, selling_price, sync_status, sync_version, synced_at,
          updated_at, created_at)
         VALUES ('bi-1', ?, 'd2', 50, 0, 50, NULL, 5.0, 'synced', 1, NULL,
          '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`
      )
      .run(BRANCH);

    // Passing branch_id — which DrugListPage used to do — must not filter.
    const withBranch = await localRead.searchDrugs(
      { organization_id: ORG, branch_id: BRANCH } as any,
      1,
      20
    );
    const withoutBranch = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);

    expect(withBranch.total).toBe(withoutBranch.total);
    expect(withBranch.total).toBe(2);
  });

  it("still returns drugs stocked only at ANOTHER branch", async () => {
    insertDrug(GEBEDOL, "Gebedol");
    insertDrug("d2", "Amoxicilin");
    rawDb()
      .prepare(
        `INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, reserved_quantity,
          sellable_quantity, location, selling_price, sync_status, sync_version, synced_at,
          updated_at, created_at)
         VALUES ('bi-x', ?, 'd2', 50, 0, 50, NULL, 5.0, 'synced', 1, NULL,
          '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`
      )
      .run(OTHER_BRANCH);

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);
    expect(result.total).toBe(2);
  });

  it("scopes to organization_id when supplied", async () => {
    insertDrug(GEBEDOL, "Gebedol", { org: ORG });
    insertDrug("d2", "Other Tenant Drug", { org: OTHER_ORG });

    const mine = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);
    expect(mine.total).toBe(1);
    expect(mine.items[0].name).toBe("Gebedol");

    const theirs = await localRead.searchDrugs({ organization_id: OTHER_ORG }, 1, 20);
    expect(theirs.total).toBe(1);
    expect(theirs.items[0].name).toBe("Other Tenant Drug");
  });

  it("still excludes soft-deleted drugs", async () => {
    insertDrug(GEBEDOL, "Gebedol");
    insertDrug("d2", "Deleted Drug", { deleted: true });

    const result = await localRead.searchDrugs({ organization_id: ORG }, 1, 20);
    expect(result.total).toBe(1);
    expect(result.items[0].name).toBe("Gebedol");
  });

  it("still honours search, drug_type, category_id, is_active and pagination", async () => {
    insertDrug(GEBEDOL, "Gebedol", { sku: "GEB-300" });
    insertDrug("d2", "Amoxicillin", { sku: "AMX-500" });
    insertDrug("d3", "Minoxidil Oil");
    rawDb().prepare("UPDATE drugs SET category_id = 'cat-1' WHERE id = ?").run(GEBEDOL);
    rawDb().prepare("UPDATE drugs SET drug_type = 'supplement' WHERE id = 'd3'").run();
    rawDb().prepare("UPDATE drugs SET is_active = 0 WHERE id = 'd2'").run();

    expect((await localRead.searchDrugs({ organization_id: ORG, search: "gebe" }, 1, 20)).total).toBe(1);
    expect((await localRead.searchDrugs({ organization_id: ORG, search: "GEB-300" }, 1, 20)).total).toBe(1);
    expect((await localRead.searchDrugs({ organization_id: ORG, drug_type: "supplement" }, 1, 20)).total).toBe(1);
    expect((await localRead.searchDrugs({ organization_id: ORG, category_id: "cat-1" }, 1, 20)).total).toBe(1);
    expect((await localRead.searchDrugs({ organization_id: ORG, is_active: false }, 1, 20)).total).toBe(1);
    expect((await localRead.searchDrugs({ organization_id: ORG, is_active: true }, 1, 20)).total).toBe(2);

    // Pagination still works and the count is org-wide.
    const page1 = await localRead.searchDrugs({ organization_id: ORG }, 1, 2);
    const page2 = await localRead.searchDrugs({ organization_id: ORG }, 2, 2);
    expect(page1.total).toBe(3);
    expect(page1.items.length).toBe(2);
    expect(page2.items.length).toBe(1);
  });

  it("searches generic_name, brand_name and manufacturer as before", async () => {
    insertDrug(GEBEDOL, "Gebedol");
    rawDb()
      .prepare("UPDATE drugs SET generic_name = 'Paracetamol', manufacturer = 'Bayer' WHERE id = ?")
      .run(GEBEDOL);

    expect((await localRead.searchDrugs({ organization_id: ORG, search: "paracetamol" }, 1, 20)).total).toBe(1);
    expect((await localRead.searchDrugs({ organization_id: ORG, search: "bayer" }, 1, 20)).total).toBe(1);
  });
});