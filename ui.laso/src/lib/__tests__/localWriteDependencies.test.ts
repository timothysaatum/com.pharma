/**
 * Event dependencies for locally-authored writes.
 *
 * A create event and everything that references it must ship in the same
 * dependency order. Before this, every builder hardcoded dependencies: [] and
 * the server's FK check turned an offline customer+prescription or
 * customer+sale into a transient rejection.
 *
 * Real localDb migration chain, real writers, no mocks.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb, resetTables } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const USER = "bae475d9-994a-4d5b-abb2-32aa4b082602";
const CUSTOMER = "cccccccc-1111-2222-3333-444444444444";
const RX = "99999999-1111-2222-3333-444444444444";
const SALE = "4ea1562f-840f-4bc1-bba9-6eeabc1ee50f";
const PO = "77777777-1111-2222-3333-444444444444";
const BATCH = "88888888-1111-2222-3333-444444444444";
const DRUG = "d06b10bd-e504-4465-9656-7a5d4618e8dd";
const NOW = "2026-10-25T10:00:00.000Z";

async function db() {
  return (await import("@/lib/localDb")).getDb();
}

type OutboxRow = {
  event_id: string;
  event_type: string;
  aggregate_id: string;
  dependencies: string;
  status: string;
};

async function outbox(): Promise<Array<OutboxRow & { dependencies: string[] }>> {
  const rows = await (await db()).select<OutboxRow[]>(
    `SELECT event_id, event_type, aggregate_id, dependencies, status
       FROM event_outbox
      ORDER BY created_at ASC, event_id ASC`,
  );
  return rows.map((row) => ({ ...row, dependencies: JSON.parse(row.dependencies) }));
}

function seedCustomer(id: string, syncStatus: string) {
  rawDb()
    .prepare(
      `INSERT INTO customers
         (id, organization_id, customer_type, first_name, last_name,
          is_active, is_deleted, sync_status, sync_version, created_at, updated_at)
       VALUES (?,?,'registered','Test','Customer',1,0,?,1,?,?)`,
    )
    .run(id, ORG, syncStatus, NOW, NOW);
}

function customer(id = CUSTOMER) {
  return {
    id,
    organization_id: ORG,
    customer_type: "registered",
    first_name: "Test",
    last_name: "Customer",
    phone: "0500000001",
    loyalty_points: 0,
    loyalty_tier: "bronze",
    is_active: true,
    is_deleted: false,
    created_at: NOW,
    updated_at: NOW,
  } as never;
}

function prescription(id = RX, customerId = CUSTOMER) {
  return {
    id,
    organization_id: ORG,
    branch_id: BRANCH,
    prescription_number: `RX-${id.slice(0, 4)}`,
    customer_id: customerId,
    prescriber_name: "Dr. Test",
    prescriber_license: "MED-1",
    prescriber_phone: null,
    prescriber_address: null,
    issue_date: "2026-10-01",
    expiry_date: "2026-12-01",
    medications: [],
    diagnosis: null,
    notes: null,
    special_instructions: null,
    refills_allowed: 1,
    refills_remaining: 1,
    status: "active",
    verified_by: null,
    verified_at: null,
    created_offline_at: NOW,
    synced_at: null,
    last_refill_date: null,
    created_at: NOW,
    updated_at: NOW,
  } as never;
}

function sale(customerId: string | null = CUSTOMER, prescriptionId: string | null = RX) {
  return {
    id: SALE,
    organization_id: ORG,
    branch_id: BRANCH,
    sale_number: "SALE-1",
    customer_id: customerId,
    customer_name: "Test Customer",
    cashier_id: USER,
    payment_method: "cash",
    payment_status: "completed",
    subtotal: 10,
    total_amount: 10,
    amount_paid: 10,
    change_amount: 0,
    prescription_id: prescriptionId,
    status: "completed",
    items: [],
    created_at: NOW,
    updated_at: NOW,
  } as never;
}

function purchaseOrder() {
  return {
    id: PO,
    organization_id: ORG,
    branch_id: BRANCH,
    po_number: "PO-1",
    supplier_id: null,
    subtotal: 0,
    tax_amount: 0,
    shipping_cost: 0,
    total_amount: 0,
    status: "draft",
    ordered_by: USER,
    approved_by: null,
    approved_at: null,
    expected_delivery_date: null,
    received_date: null,
    notes: null,
    items: [],
    created_at: NOW,
    updated_at: NOW,
  } as never;
}

function drugBatch() {
  return {
    id: BATCH,
    organization_id: ORG,
    branch_id: BRANCH,
    drug_id: DRUG,
    batch_number: "B1",
    quantity: 10,
    remaining_quantity: 10,
    expiry_date: "2027-10-01",
    cost_price: 1,
    selling_price: 2,
    supplier: null,
    purchase_order_id: PO,
    created_at: NOW,
    updated_at: NOW,
  } as never;
}

beforeEach(async () => {
  await installRealDb();
  resetTables([
    "event_outbox",
    "customers",
    "prescriptions",
    "sales",
    "purchase_orders",
    "drug_batches",
    "audit_logs",
  ]);
});

describe("local write dependency declarations", () => {
  it("links prescription_created to a locally-created customer", async () => {
    const { writeLocal } = await import("@/lib/localWrite");

    await writeLocal.customer(customer(), "create", BRANCH);
    await writeLocal.prescription(prescription());

    const rows = await outbox();
    const customerCreated = rows.find((row) => row.event_type === "customer_created")!;
    const prescriptionCreated = rows.find((row) => row.event_type === "prescription_created")!;

    expect(prescriptionCreated.dependencies).toEqual([customerCreated.event_id]);
  });

  it("links sale_created to both the customer and prescription creates", async () => {
    const { writeLocal } = await import("@/lib/localWrite");

    await writeLocal.customer(customer(), "create", BRANCH);
    await writeLocal.prescription(prescription());
    await writeLocal.sale(sale());

    const rows = await outbox();
    const customerCreated = rows.find((row) => row.event_type === "customer_created")!;
    const prescriptionCreated = rows.find((row) => row.event_type === "prescription_created")!;
    const saleCreated = rows.find((row) => row.event_type === "sale_created")!;

    expect(saleCreated.dependencies).toEqual([
      customerCreated.event_id,
      prescriptionCreated.event_id,
    ]);
  });

  it("does not depend on a server-origin customer that is already synced", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    seedCustomer(CUSTOMER, "synced");

    await writeLocal.prescription(prescription());

    const rows = await outbox();
    const prescriptionCreated = rows.find((row) => row.event_type === "prescription_created")!;
    expect(prescriptionCreated.dependencies).toEqual([]);
  });

  it("refuses to queue a reference to an unsynced local row with no create event", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    seedCustomer(CUSTOMER, "pending");

    await expect(writeLocal.prescription(prescription())).rejects.toThrow(
      /no customer_created event is queued/,
    );

    expect(await outbox()).toHaveLength(0);
    const prescriptions = await (await db()).select(
      "SELECT id FROM prescriptions WHERE id = $1",
      [RX],
    );
    expect(prescriptions).toHaveLength(0);
  });

  it("refuses to queue a reference to a permanently rejected create", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    await writeLocal.customer(customer(), "create", BRANCH);
    await (await db()).execute(
      "UPDATE event_outbox SET status = 'rejected_permanent' WHERE event_type = 'customer_created'",
    );

    await expect(writeLocal.prescription(prescription())).rejects.toThrow(
      /permanently rejected/,
    );
  });

  it("links customer_updated to its own customer_created event", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    await writeLocal.customer(customer(), "create", BRANCH);
    await writeLocal.customer(customer(), "update", BRANCH);

    const rows = await outbox();
    const created = rows.find((row) => row.event_type === "customer_created")!;
    const updated = rows.find((row) => row.event_type === "customer_updated")!;
    expect(updated.dependencies).toEqual([created.event_id]);
  });

  it("links prescription_updated to its customer and prescription creates", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    await writeLocal.customer(customer(), "create", BRANCH);
    await writeLocal.prescription(prescription());
    await writeLocal.prescription(
      { ...(prescription() as Record<string, unknown>), status: "filled", refills_remaining: 0 } as never,
      "update",
    );

    const rows = await outbox();
    const customerCreated = rows.find((row) => row.event_type === "customer_created")!;
    const prescriptionCreated = rows.find((row) => row.event_type === "prescription_created")!;
    const prescriptionUpdated = rows.find((row) => row.event_type === "prescription_updated")!;
    expect(prescriptionUpdated.dependencies).toEqual([
      customerCreated.event_id,
      prescriptionCreated.event_id,
    ]);
  });

  it("links purchase_order_updated and drug_batch_created to their creates", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    await writeLocal.purchaseOrder(purchaseOrder(), "create");
    await writeLocal.purchaseOrder(
      { ...(purchaseOrder() as Record<string, unknown>), status: "ordered" } as never,
      "update",
    );
    await writeLocal.drugBatch(drugBatch(), "create");

    const rows = await outbox();
    const poCreated = rows.find((row) => row.event_type === "purchase_order_created")!;
    const poUpdated = rows.find((row) => row.event_type === "purchase_order_updated")!;
    const batchCreated = rows.find((row) => row.event_type === "drug_batch_created")!;

    expect(poUpdated.dependencies).toEqual([poCreated.event_id]);
    expect(batchCreated.dependencies).toEqual([poCreated.event_id]);
  });

  it("links drug_batch_updated to its own create and the purchase order create", async () => {
    const { writeLocal } = await import("@/lib/localWrite");
    await writeLocal.purchaseOrder(purchaseOrder(), "create");
    await writeLocal.drugBatch(drugBatch(), "create");
    await writeLocal.drugBatch(drugBatch(), "update");

    const rows = await outbox();
    const poCreated = rows.find((row) => row.event_type === "purchase_order_created")!;
    const batchCreated = rows.find((row) => row.event_type === "drug_batch_created")!;
    const batchUpdated = rows.find((row) => row.event_type === "drug_batch_updated")!;
    expect(batchUpdated.dependencies).toEqual([
      batchCreated.event_id,
      poCreated.event_id,
    ]);
  });

  it("keeps the outbox lock usable after a builder failure", async () => {
    const { appendOutboxEvent } = await import("@/lib/localWrite");

    await expect(
      appendOutboxEvent(async () => {
        throw new Error("builder failed");
      }),
    ).rejects.toThrow("builder failed");

    let ran = false;
    await appendOutboxEvent(async (hashPrev) => {
      ran = true;
      return {
        event_id: "01J00000000000000000000000",
        aggregate_type: "customer",
        event_type: "customer_created",
        aggregate_id: CUSTOMER,
        org_id: ORG,
        branch_id: BRANCH,
        authored_by: USER,
        authored_at: NOW,
        schema_version: 1,
        payload: {},
        dependencies: [],
        hash_prev: hashPrev,
        hash_self: "a".repeat(64),
        status: "pending",
        attempts: 0,
        error_code: null,
        error_message: null,
      };
    });

    expect(ran).toBe(true);
    expect(await outbox()).toHaveLength(1);
  });
});
