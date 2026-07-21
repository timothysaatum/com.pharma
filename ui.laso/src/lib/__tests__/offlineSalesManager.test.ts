import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Sale, SaleItem } from "@/types";

const mocks = vi.hoisted(() => ({
  select: vi.fn(),
  executeTransaction: vi.fn(),
  notifySyncQueueChanged: vi.fn(),
}));

vi.mock("@/lib/localDb", () => ({
  getDb: vi.fn(async () => ({
    select: mocks.select,
    executeTransaction: mocks.executeTransaction,
  })),
  notifySyncQueueChanged: mocks.notifySyncQueueChanged,
}));

vi.mock("@/lib/localWrite", () => ({
  buildLocalSalePayload: vi.fn((sale: Record<string, unknown>, items: unknown[], now: string) => ({
    id: sale.id,
    organization_id: sale.organization_id,
    branch_id: sale.branch_id,
    sale_number: sale.sale_number,
    cashier_id: sale.cashier_id,
    total_amount: sale.total_amount,
    items_json: JSON.stringify(items),
    items_count: items.length,
    sync_status: "pending",
    sync_version: 1,
    updated_at: now,
    created_at: now,
  })),
}));

import { offlineSalesManager } from "@/lib/offlineSalesManager";

const saleId = "4ea1562f-840f-4bc1-bba9-6eeabc1ee50f";
const drugId = "d06b10bd-e504-4465-9656-7a5d4618e8dd";

function sale(overrides: Record<string, unknown> = {}) {
  return {
    id: saleId,
    organization_id: "org-1",
    branch_id: "branch-1",
    sale_number: "OFFLINE-BRANCH-1",
    cashier_id: "user-1",
    customer_name: "Walk-in",
    total_amount: 25,
    prescription_id: null,
    items: [],
    created_at: "2026-07-18T10:00:00.000Z",
    updated_at: "2026-07-18T10:00:00.000Z",
    ...overrides,
  } as unknown as Omit<Sale, "sync_status" | "sync_version"> & { id: string };
}

function item(overrides: Record<string, unknown> = {}) {
  return {
    id: "item-1",
    sale_id: saleId,
    drug_id: drugId,
    quantity: 2,
    unit_price: 12.5,
    subtotal: 25,
    discount_amount: 0,
    tax_amount: 0,
    total_price: 25,
    ...overrides,
  } as unknown as SaleItem;
}

describe("offlineSalesManager atomic checkout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.select.mockImplementation(async (sql: string) => {
      if (sql.includes("drug_batches")) {
        return [{ id: "batch-1", remaining_quantity: 10 }];
      }
      return [];
    });
    mocks.executeTransaction.mockResolvedValue([]);
  });

  it("writes one stable protocol-v2 command and local projections", async () => {
    const result = await offlineSalesManager.recordSaleTransaction(
      sale(),
      [item()],
      [{ drug_id: drugId, delta: -2 }],
      `sale:${saleId}`,
    );

    expect(result).toEqual({ success: true, saleId });
    expect(mocks.executeTransaction).toHaveBeenCalledTimes(1);
    const statements = mocks.executeTransaction.mock.calls[0][0] as Array<{
      sql: string;
      values: unknown[];
      expectedRows: number;
    }>;
    expect(statements.map((statement) => statement.sql)).toEqual(expect.arrayContaining([
      expect.stringContaining("INSERT INTO offline_sales"),
      expect.stringContaining("INSERT INTO sales"),
      expect.stringContaining("INSERT INTO sync_queue"),
      expect.stringContaining("UPDATE branch_inventory"),
      expect.stringContaining("INSERT OR IGNORE INTO suppressed_crr_changes"),
    ]));

    const queueStatement = statements.find((statement) =>
      statement.sql.includes("INSERT INTO sync_queue"),
    )!;
    expect(queueStatement.values[0]).toBe(saleId);
    const queuePayload = JSON.parse(queueStatement.values[5] as string);
    expect(queuePayload.sync_protocol_version).toBe(2);
    expect(queuePayload.items).toEqual([expect.objectContaining({
      drug_id: drugId,
      quantity: 2,
      total_price: 25,
    })]);

    const stockStatement = statements.find((statement) =>
      statement.sql.includes("UPDATE branch_inventory"),
    )!;
    expect(stockStatement.sql).toContain("quantity + $1 >= reserved_quantity");
    expect(stockStatement.values).toEqual([-2, expect.any(String), "branch-1", drugId]);
    const suppressionStatement = statements.find((statement) =>
      statement.sql.includes("suppressed_crr_changes"),
    )!;
    expect(suppressionStatement.sql).toContain("'branch_inventory', 'prescriptions'");
    expect(suppressionStatement.values[0]).toBe(saleId);
    expect(mocks.notifySyncQueueChanged).toHaveBeenCalledOnce();
  });

  it("returns an exact idempotent replay without a second transaction", async () => {
    mocks.select.mockResolvedValue([{ id: saleId, idempotency_key: `sale:${saleId}` }]);

    const result = await offlineSalesManager.recordSaleTransaction(
      sale(),
      [item()],
      [{ drug_id: drugId, delta: -2 }],
      `sale:${saleId}`,
    );

    expect(result).toEqual({ success: true, saleId });
    expect(mocks.executeTransaction).not.toHaveBeenCalled();
  });

  it("rejects a quantity mismatch before touching SQLite", async () => {
    const result = await offlineSalesManager.recordSaleTransaction(
      sale(),
      [item({ quantity: 3 })],
      [{ drug_id: drugId, delta: -2 }],
      `sale:${saleId}`,
    );

    expect(result).toEqual({
      success: false,
      error: expect.stringContaining("does not match the sold quantity"),
    });
    expect(mocks.executeTransaction).not.toHaveBeenCalled();
  });

  it("surfaces a transaction guard failure and never reports success", async () => {
    mocks.executeTransaction.mockRejectedValue({
      message: `Insufficient available local stock for drug ${drugId}; no part of the sale was recorded.`,
    });

    const result = await offlineSalesManager.recordSaleTransaction(
      sale(),
      [item()],
      [{ drug_id: drugId, delta: -2 }],
      `sale:${saleId}`,
    );

    expect(result).toEqual({
      success: false,
      error: expect.stringContaining("Insufficient available local stock"),
    });
    expect(mocks.notifySyncQueueChanged).not.toHaveBeenCalled();
  });

  it("guards prescription ownership, status, and remaining refills in the same transaction", async () => {
    await offlineSalesManager.recordSaleTransaction(
      sale({ prescription_id: "rx-1" }),
      [item()],
      [{ drug_id: drugId, delta: -2 }],
      `sale:${saleId}`,
    );

    const statements = mocks.executeTransaction.mock.calls[0][0] as Array<{
      sql: string;
      values: unknown[];
    }>;
    const prescriptionStatement = statements.find((statement) =>
      statement.sql.includes("UPDATE prescriptions"),
    )!;
    expect(prescriptionStatement.sql).toContain("branch_id = $4");
    expect(prescriptionStatement.sql).toContain("status = 'active'");
    expect(prescriptionStatement.sql).toContain("refills_remaining > 0");
    expect(prescriptionStatement.values.slice(2)).toEqual(["rx-1", "branch-1"]);
  });
});
