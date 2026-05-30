import { getDb } from "@/lib/localDb";
import type {
  Drug,
  DrugCategory,
  DrugCategoryTree,
  PaginatedResponse,
  PriceContract,
  Customer,
  BranchInventoryWithDetails,
  LowStockReport,
  ExpiringBatchReport,
  InventoryValuationResponse,
  DrugBatch,
  Sale,
  SaleWithDetails,
  SaleItem,
  SaleStatus,
  PaymentMethod,
  PaymentStatus,
  PurchaseOrder,
  PurchaseOrderWithDetails,
  Supplier,
} from "@/types";
import type { ContractResponse, AvailableContract } from "@/api/contracts";
import type { CustomerListResponse } from "@/api/customers";
import type { ReceiptData } from "@/api/sales";

interface DrugSearchParams {
  search?: string;
  category_id?: string;
  drug_type?: string;
  branch_id?: string;
  is_active?: boolean;
  organization_id?: string;
}

interface CustomerSearchParams {
  search?: string;
  customer_type?: string;
  loyalty_tier?: string;
  is_active?: boolean;
  organization_id?: string;
}

interface ContractSearchParams {
  search?: string;
  contract_type?: string;
  status?: string;
  is_active?: boolean;
  organization_id?: string;
}

function sqlLike(value: string): string {
  return `%${value.toLowerCase().trim()}%`;
}

function boolToInt(value: boolean | undefined): number | undefined {
  return value === undefined ? undefined : value ? 1 : 0;
}

function toNumber(value: unknown, fallback = 0): number {
  if (value === null || value === undefined) return fallback;
  return Number(value) || fallback;
}

function toBoolean(value: unknown): boolean {
  return value === 1 || value === true || value === "1" || value === "true";
}

function buildPagination<T>(items: T[], page: number, page_size: number, total: number): PaginatedResponse<T> {
  return {
    items,
    page,
    page_size,
    total,
    total_pages: Math.max(1, Math.ceil(total / page_size)),
    has_next: page * page_size < total,
    has_prev: page > 1,
  };
}

function toContractResponse(row: Record<string, unknown>): ContractResponse {
  return {
    id: String(row.id),
    organization_id: String(row.organization_id),
    contract_code: String(row.contract_code),
    contract_name: String(row.contract_name),
    contract_type: String(row.contract_type) as any,
    is_default_contract: toBoolean(row.is_default_contract),
    discount_type: String(row.discount_type) as any,
    discount_percentage: toNumber(row.discount_percentage),
    applies_to_prescription_only: toBoolean(row.applies_to_prescription_only),
    applies_to_otc: toBoolean(row.applies_to_otc),
    applies_to_all_branches: toBoolean(row.applies_to_all_branches),
    applicable_branch_ids: parseJsonArray<string>(row.applicable_branch_ids),
    effective_from: String(row.effective_from),
    effective_to: row.effective_to === null ? null : String(row.effective_to),
    status: String(row.status) as any,
    is_active: toBoolean(row.is_active),
    copay_amount: row.copay_amount === null ? null : toNumber(row.copay_amount),
    copay_percentage: row.copay_percentage === null ? null : toNumber(row.copay_percentage),
    is_deleted: toBoolean(row.is_deleted),
    sync_status: String(row.sync_status) as any,
    sync_version: toNumber(row.sync_version, 1),
    synced_at: row.synced_at === null ? null : String(row.synced_at),
    updated_at: String(row.updated_at),
    created_at: String(row.created_at),
    usage_count: 0,
    total_sales_amount: 0,
    total_discount_given: 0,
    average_sale_amount: 0,
    last_used_at: null,
    unique_customers_count: 0,
    created_by: "",
    approved_by: null,
    approved_at: row.approved_at === null ? null : String(row.approved_at),
    last_modified_by: null,
    last_modified_at: null,
  } as unknown as ContractResponse;
}

function parseJsonArray<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[];
  if (typeof value === "string") {
    try {
      return JSON.parse(value) as T[];
    } catch {
      return [];
    }
  }
  return [];
}

function toSale(row: Record<string, unknown>): Sale {
  return {
    id: String(row.id),
    organization_id: String(row.organization_id),
    branch_id: String(row.branch_id),
    sale_number: String(row.sale_number),
    customer_id: row.customer_id === null ? null : String(row.customer_id),
    customer_name: row.customer_name === null ? null : String(row.customer_name),
    subtotal: toNumber(row.subtotal),
    discount_amount: toNumber(row.discount_amount),
    tax_amount: toNumber(row.tax_amount),
    total_amount: toNumber(row.total_amount),
    price_contract_id: row.price_contract_id === null ? null : String(row.price_contract_id),
    contract_name: row.contract_name === null ? null : String(row.contract_name),
    contract_discount_percentage: row.contract_discount_percentage === null ? null : toNumber(row.contract_discount_percentage),
    payment_method: String(row.payment_method) as PaymentMethod,
    payment_status: String(row.payment_status) as PaymentStatus,
    amount_paid: row.amount_paid === null ? null : toNumber(row.amount_paid),
    change_amount: row.change_amount === null ? null : toNumber(row.change_amount),
    payment_reference: row.payment_reference === null ? null : String(row.payment_reference),
    prescription_id: row.prescription_id === null ? null : String(row.prescription_id),
    prescription_number: row.prescription_number === null ? null : String(row.prescription_number),
    prescriber_name: row.prescriber_name === null ? null : String(row.prescriber_name),
    cashier_id: String(row.cashier_id),
    pharmacist_id: row.pharmacist_id === null ? null : String(row.pharmacist_id),
    insurance_claim_number: row.insurance_claim_number === null ? null : String(row.insurance_claim_number),
    patient_copay_amount: row.patient_copay_amount === null ? null : toNumber(row.patient_copay_amount),
    insurance_covered_amount: row.insurance_covered_amount === null ? null : toNumber(row.insurance_covered_amount),
    insurance_verified: toBoolean(row.insurance_verified),
    insurance_verified_at: row.insurance_verified_at === null ? null : String(row.insurance_verified_at),
    insurance_verified_by: row.insurance_verified_by === null ? null : String(row.insurance_verified_by),
    notes: row.notes === null ? null : String(row.notes),
    status: String(row.status) as SaleStatus,
    receipt_printed: toBoolean(row.receipt_printed),
    receipt_emailed: toBoolean(row.receipt_emailed),
    items_count: row.items_count === null || row.items_count === undefined ? 0 : toNumber(row.items_count),
    created_at: String(row.created_at),
    updated_at: String(row.updated_at),
    sync_status: String(row.sync_status) as any,
    sync_version: toNumber(row.sync_version, 1),
    synced_at: row.synced_at === null ? null : String(row.synced_at),
  };
}

function toSaleWithDetails(row: Record<string, unknown>): SaleWithDetails {
  const items = parseJsonArray<SaleItem>(row.items_json);
  const jsonItemsCount = items.reduce((sum, item) => sum + (item?.quantity ?? 0), 0);
  const rowItemsCount = row.items_count === null || row.items_count === undefined ? undefined : toNumber(row.items_count);
  return {
    ...toSale(row),
    contract_discount_amount: row.discount_amount === null ? 0 : toNumber(row.discount_amount),
    additional_discount_amount: 0,
    total_discount_amount: row.discount_amount === null ? 0 : toNumber(row.discount_amount),
    contract_type: null,
    insurance_preauth_number: null,
    split_payment_details: null,
    cashier_name: null,
    pharmacist_name: null,
    manager_approval_user_id: null,
    customer_full_name: row.customer_name === null ? null : String(row.customer_name),
    customer_phone: null,
    customer_email: null,
    customer_loyalty_tier: null,
    branch_name: "",
    branch_address: null,
    organization_name: "",
    organization_tax_id: null,
    items,
    items_count: rowItemsCount ?? jsonItemsCount,
  };
}

function toPurchaseOrder(row: Record<string, unknown>): PurchaseOrder {
  return {
    id: String(row.id),
    organization_id: String(row.organization_id),
    branch_id: String(row.branch_id),
    po_number: String(row.po_number),
    supplier_id: String(row.supplier_id),
    subtotal: toNumber(row.subtotal),
    tax_amount: toNumber(row.tax_amount),
    shipping_cost: toNumber(row.shipping_cost),
    total_amount: toNumber(row.total_amount),
    status: String(row.status) as any,
    ordered_by: String(row.ordered_by),
    approved_by: row.approved_by === null ? null : String(row.approved_by),
    approved_at: row.approved_at === null ? null : String(row.approved_at),
    expected_delivery_date: row.expected_delivery_date === null ? null : String(row.expected_delivery_date),
    received_date: row.received_date === null ? null : String(row.received_date),
    notes: row.notes === null ? null : String(row.notes),
    created_at: String(row.created_at),
    updated_at: String(row.updated_at),
    sync_status: String(row.sync_status) as any,
    sync_version: toNumber(row.sync_version, 1),
    synced_at: row.synced_at === null ? null : String(row.synced_at),
  };
}

function toPurchaseOrderWithDetails(row: Record<string, unknown>): PurchaseOrderWithDetails {
  const items = parseJsonArray<unknown>(row.items_json) as any[];
  const totalItems = items.length;
  const received = items.filter((item) => typeof item.quantity_received === "number" && typeof item.quantity_ordered === "number" && item.quantity_received >= item.quantity_ordered).length;
  const supplierName = typeof row.supplier_name === "string" && row.supplier_name.trim()
    ? row.supplier_name
    : String(row.supplier_id);
  const branchName = typeof row.branch_name === "string" && row.branch_name.trim()
    ? row.branch_name
    : "";
  const orderedByName = typeof row.ordered_by_name === "string" && row.ordered_by_name.trim()
    ? row.ordered_by_name
    : String(row.ordered_by);
  return {
    ...toPurchaseOrder(row),
    items: items as any,
    supplier_name: supplierName,
    branch_name: branchName,
    ordered_by_name: orderedByName,
    approved_by_name: row.approved_by === null ? null : String(row.approved_by),
    is_fully_received: totalItems > 0 ? received >= totalItems : false,
    total_items_received: received,
    receipt_progress: `${received} / ${totalItems} items received`,
  };
}

function toSupplierFromPurchaseOrders(row: Record<string, unknown>): Supplier {
  const supplierId = String(row.supplier_id);
  const now = new Date().toISOString();
  return {
    id: supplierId,
    organization_id: "",
    name: supplierId,
    contact_person: null,
    phone: null,
    email: null,
    address: null,
    tax_id: null,
    registration_number: null,
    payment_terms: null,
    credit_limit: null,
    rating: null,
    total_orders: 0,
    total_value: 0,
    is_active: true,
    is_deleted: false,
    created_at: now,
    updated_at: now,
    sync_status: "synced",
    sync_version: 1,
    synced_at: null,
    deleted_at: null,
    deleted_by: null,
  };
}

export const localRead = {
  async searchDrugs(
    params: DrugSearchParams = {},
    page = 1,
    page_size = 20
  ): Promise<PaginatedResponse<Drug>> {
    console.log(`[LocalRead] searchDrugs: search=${params.search}, type=${params.drug_type}, page=${page}`);
    const db = await getDb();
    const qualifiers: string[] = ["d.is_deleted = 0"];
    const values: unknown[] = [];
    let join = "";

    if (params.branch_id) {
      values.push(params.branch_id);
      join = "LEFT JOIN branch_inventory bi ON bi.drug_id = d.id";
      qualifiers.push(`bi.branch_id = $${values.length}`);
    }
    if (params.is_active !== undefined) {
      values.push(boolToInt(params.is_active));
      qualifiers.push(`is_active = $${values.length}`);
    }
    if (params.organization_id) {
      values.push(params.organization_id);
      qualifiers.push(`d.organization_id = $${values.length}`);
    }
    if (params.drug_type) {
      values.push(params.drug_type);
      qualifiers.push(`drug_type = $${values.length}`);
    }
    if (params.category_id) {
      values.push(params.category_id);
      qualifiers.push(`category_id = $${values.length}`);
    }
    if (params.search) {
      values.push(sqlLike(params.search));
      qualifiers.push(`(
        LOWER(name) LIKE $${values.length} OR
        LOWER(generic_name) LIKE $${values.length} OR
        LOWER(brand_name) LIKE $${values.length} OR
        LOWER(sku) LIKE $${values.length} OR
        LOWER(barcode) LIKE $${values.length} OR
        LOWER(manufacturer) LIKE $${values.length}
      )`);
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";
    const countRows = await db.select<{ total: number }[]>(`SELECT COUNT(*) AS total FROM drugs d ${join} ${where}`, values);
    const total = countRows[0]?.total ?? 0;
    console.log(`[LocalRead] searchDrugs: found ${total} total drugs`);

    const offset = (page - 1) * page_size;
    const rows = await db.select<Drug[]>(
      `SELECT d.* FROM drugs d ${join} ${where} ORDER BY d.updated_at DESC LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    return buildPagination(rows, page, page_size, total);
  },

  async getDrugCategories(parentId?: string): Promise<DrugCategory[]> {
    const db = await getDb();
    if (parentId) {
      return db.select<DrugCategory[]>(
        `SELECT * FROM drug_categories WHERE is_deleted = 0 AND parent_id = $1 ORDER BY name`,
        [parentId]
      );
    }
    return db.select<DrugCategory[]>(
      `SELECT * FROM drug_categories WHERE is_deleted = 0 AND (parent_id IS NULL OR parent_id = '') ORDER BY name`
    );
  },

  async getDrugCategoryTree(): Promise<DrugCategoryTree[]> {
    const db = await getDb();
    const rows = await db.select<DrugCategory[]>(
      `SELECT * FROM drug_categories WHERE is_deleted = 0 ORDER BY name`
    );
    const byId = new Map<string, DrugCategoryTree>();
    const roots: DrugCategoryTree[] = [];

    for (const row of rows) {
      const node: DrugCategoryTree = {
        ...row,
        children: [],
      };
      byId.set(row.id, node);
    }

    for (const node of byId.values()) {
      const parentId = node.parent_id;
      if (parentId && byId.has(parentId)) {
        byId.get(parentId)!.children!.push(node);
      } else {
        roots.push(node);
      }
    }

    return roots;
  },

  async searchCustomers(
    params: CustomerSearchParams = {},
    page = 1,
    page_size = 25
  ): Promise<CustomerListResponse> {
    const db = await getDb();
    const qualifiers: string[] = ["is_deleted = 0"];
    const values: unknown[] = [];

    if (params.is_active !== undefined) {
      values.push(boolToInt(params.is_active));
      qualifiers.push(`is_active = $${values.length}`);
    }
    if (params.customer_type) {
      values.push(params.customer_type);
      qualifiers.push(`customer_type = $${values.length}`);
    }
    if (params.loyalty_tier) {
      values.push(params.loyalty_tier);
      qualifiers.push(`loyalty_tier = $${values.length}`);
    }
    if (params.organization_id) {
      values.push(params.organization_id);
      qualifiers.push(`organization_id = $${values.length}`);
    }
    if (params.search) {
      values.push(sqlLike(params.search));
      qualifiers.push(`(
        LOWER(first_name) LIKE $${values.length} OR
        LOWER(last_name) LIKE $${values.length} OR
        LOWER(phone) LIKE $${values.length} OR
        LOWER(email) LIKE $${values.length}
      )`);
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";
    const totalRows = await db.select<{ total: number }[]>(`SELECT COUNT(*) AS total FROM customers ${where}`, values);
    const total = totalRows[0]?.total ?? 0;

    const offset = (page - 1) * page_size;
    const rows = await db.select<Customer[]>(
      `SELECT * FROM customers ${where} ORDER BY updated_at DESC LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    return {
      customers: rows,
      total,
      page,
      page_size,
      total_pages: Math.max(1, Math.ceil(total / page_size)),
    };
  },

  async searchContracts(
    params: ContractSearchParams = {},
    page = 1,
    page_size = 20
  ): Promise<PaginatedResponse<ContractResponse>> {
    const db = await getDb();
    const qualifiers: string[] = ["is_deleted = 0"];
    const values: unknown[] = [];

    if (params.is_active !== undefined) {
      values.push(boolToInt(params.is_active));
      qualifiers.push(`is_active = $${values.length}`);
    }
    if (params.contract_type) {
      values.push(params.contract_type);
      qualifiers.push(`contract_type = $${values.length}`);
    }
    if (params.status) {
      values.push(params.status);
      qualifiers.push(`status = $${values.length}`);
    }
    if (params.search) {
      values.push(sqlLike(params.search));
      qualifiers.push(`(
        LOWER(contract_name) LIKE $${values.length} OR
        LOWER(contract_code) LIKE $${values.length}
      )`);
    }
    if (params.organization_id) {
      values.push(params.organization_id);
      qualifiers.push(`organization_id = $${values.length}`);
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";
    const totalRows = await db.select<{ total: number }[]>(`SELECT COUNT(*) AS total FROM price_contracts ${where}`, values);
    const total = totalRows[0]?.total ?? 0;

    const offset = (page - 1) * page_size;
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT * FROM price_contracts ${where} ORDER BY updated_at DESC LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    return buildPagination(rows.map(toContractResponse), page, page_size, total);
  },

  async searchSales(
    params: {
      page?: number;
      page_size?: number;
      branch_id?: string;
      status?: string;
      payment_method?: string;
      start_date?: string;
      end_date?: string;
      search?: string;
    } = {},
    page = 1,
    page_size = 25
  ): Promise<PaginatedResponse<Sale>> {
    const db = await getDb();
    const qualifiers: string[] = [];
    const values: unknown[] = [];

    if (params.branch_id) {
      values.push(params.branch_id);
      qualifiers.push(`branch_id = $${values.length}`);
    }
    if (params.status) {
      values.push(params.status);
      qualifiers.push(`status = $${values.length}`);
    }
    if (params.payment_method) {
      values.push(params.payment_method);
      qualifiers.push(`payment_method = $${values.length}`);
    }
    if (params.start_date) {
      values.push(params.start_date);
      qualifiers.push(`DATE(created_at) >= DATE($${values.length})`);
    }
    if (params.end_date) {
      values.push(params.end_date);
      qualifiers.push(`DATE(created_at) <= DATE($${values.length})`);
    }
    if (params.search) {
      values.push(sqlLike(params.search));
      qualifiers.push(`(
        LOWER(sale_number) LIKE $${values.length} OR
        LOWER(customer_name) LIKE $${values.length}
      )`);
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";
    const totalRows = await db.select<{ total: number }[]>(`SELECT COUNT(*) AS total FROM sales ${where}`, values);
    const total = totalRows[0]?.total ?? 0;
    const offset = (page - 1) * page_size;
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT * FROM sales ${where} ORDER BY created_at DESC LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    return buildPagination(rows.map(toSale), page, page_size, total);
  },

  async getSaleById(id: string): Promise<SaleWithDetails | null> {
    const db = await getDb();
    const rows = await db.select<Record<string, unknown>[]>(`SELECT * FROM sales WHERE id = $1`, [id]);
    if (!rows.length) return null;
    return toSaleWithDetails(rows[0]);
  },

  async getSaleReceipt(id: string): Promise<ReceiptData | null> {
    const sale = await this.getSaleById(id);
    if (!sale) return null;

    return {
      receipt_number: sale.sale_number,
      receipt_date: sale.created_at,
      organization: {
        name: sale.organization_name || "",
        tax_id: sale.organization_tax_id,
        phone: null,
        email: null,
      },
      branch: {
        name: sale.branch_name || "",
        address: sale.branch_address,
        phone: null,
      },
      customer: {
        name: sale.customer_full_name || sale.customer_name,
        phone: sale.customer_phone,
      },
      items: (sale.items ?? []).map((item) => ({
        name: item.drug_name,
        generic_name: (item as any).drug_generic_name ?? null,
        quantity: item.quantity,
        unit_price: item.unit_price,
        subtotal: (item as any).subtotal ?? item.quantity * item.unit_price,
        contract_discount: (item as any).contract_discount_amount ?? 0,
        additional_discount: (item as any).additional_discount_amount ?? 0,
        total_discount: (item as any).total_discount_amount ?? 0,
        tax: (item as any).tax_amount ?? 0,
        total: (item as any).total_price ?? item.quantity * item.unit_price,
        batch_number: (item as any).batch_number ?? null,
        usage_instructions: (item as any).usage_instructions ?? null,
        insurance_covered: Boolean((item as any).insurance_covered),
        patient_copay: (item as any).patient_copay_amount ?? 0,
      })),
      subtotal: sale.subtotal,
      contract_discount: sale.contract_discount_amount,
      additional_discount: sale.additional_discount_amount,
      total_discount: sale.total_discount_amount,
      tax: sale.tax_amount,
      total: sale.total_amount,
      amount_paid: sale.amount_paid ?? sale.total_amount,
      change: sale.change_amount ?? 0,
      payment_method: sale.payment_method,
      cashier: sale.cashier_name,
      contract: sale.contract_name
        ? {
            name: sale.contract_name,
            type: sale.contract_type ?? "",
            discount_percentage: sale.contract_discount_percentage ?? 0,
          }
        : null,
      insurance: sale.insurance_claim_number
        ? {
            claim_number: sale.insurance_claim_number,
            preauth_number: sale.insurance_preauth_number,
            patient_copay: sale.patient_copay_amount ?? 0,
            insurance_covered: sale.insurance_covered_amount ?? 0,
            verified: sale.insurance_verified,
          }
        : null,
    };
  },

  async searchPurchaseOrders(
    params: {
      page?: number;
      page_size?: number;
      branch_id?: string;
      status?: string;
      supplier_id?: string;
    } = {},
    page = 1,
    page_size = 20
  ): Promise<PaginatedResponse<PurchaseOrder>> {
    const db = await getDb();
    const qualifiers: string[] = [];
    const values: unknown[] = [];

    if (params.branch_id) {
      values.push(params.branch_id);
      qualifiers.push(`branch_id = $${values.length}`);
    }
    if (params.status) {
      values.push(params.status);
      qualifiers.push(`status = $${values.length}`);
    }
    if (params.supplier_id) {
      values.push(params.supplier_id);
      qualifiers.push(`supplier_id = $${values.length}`);
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";
    const totalRows = await db.select<{ total: number }[]>(`SELECT COUNT(*) AS total FROM purchase_orders ${where}`, values);
    const total = totalRows[0]?.total ?? 0;
    const offset = (page - 1) * page_size;
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT * FROM purchase_orders ${where} ORDER BY updated_at DESC LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    return buildPagination(rows.map(toPurchaseOrder), page, page_size, total);
  },

  async getPurchaseOrderById(id: string): Promise<PurchaseOrderWithDetails | null> {
    const db = await getDb();
    const rows = await db.select<Record<string, unknown>[]>(`SELECT * FROM purchase_orders WHERE id = $1`, [id]);
    if (!rows.length) return null;
    return toPurchaseOrderWithDetails(rows[0]);
  },

  async getCachedSuppliers(branchId?: string): Promise<Supplier[]> {
    const db = await getDb();
    const values: unknown[] = [];
    const where = branchId ? `WHERE branch_id = $1` : "";
    if (branchId) values.push(branchId);
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT DISTINCT supplier_id FROM purchase_orders ${where}`,
      values,
    );

    return rows.map(toSupplierFromPurchaseOrders);
  },

  async getAvailableContractsForPos(branchId: string, organizationId?: string): Promise<AvailableContract[]> {
    const db = await getDb();
    const today = new Date().toISOString().slice(0, 10);
    const values: unknown[] = [today, `%"${branchId}"%`];
    let query = `SELECT * FROM price_contracts
       WHERE is_deleted = 0
         AND is_active = 1
         AND status = 'active'
         AND effective_from <= $1
         AND (effective_to IS NULL OR effective_to >= $1)
         AND (
           applies_to_all_branches = 1
           OR applicable_branch_ids LIKE $2
         )`;

    if (organizationId) {
      values.push(organizationId);
      query += ` AND organization_id = $${values.length}`;
    }

    query += ` ORDER BY is_default_contract DESC, contract_name ASC`;

    const rows = await db.select<PriceContract[]>(query, values);

    return rows.map((row) => ({
      id: row.id,
      code: row.contract_code,
      name: row.contract_name,
      type: row.contract_type as any,
      discount_percentage: row.discount_percentage,
      is_default: toBoolean(row.is_default_contract),
      requires_verification: false,
      requires_approval: false,
      display: row.contract_name,
      warning: null,
      copay_amount: row.copay_amount === null ? null : Number(row.copay_amount),
      copay_percentage: row.copay_percentage === null ? null : Number(row.copay_percentage),
      requires_preauthorization: false,
      insurance_provider_id: null,
      daily_usage_limit: null,
      per_customer_usage_limit: null,
      applies_to_prescription_only: toBoolean(row.applies_to_prescription_only),
      applies_to_otc: toBoolean(row.applies_to_otc),
      minimum_purchase_amount: null,
      maximum_purchase_amount: null,
    }));
  },

  async getBranchInventory(
    branchId: string,
    params: {
      search?: string;
      low_stock_only?: boolean;
      include_zero_stock?: boolean;
      drug_type?: string;
    } = {},
    page = 1,
    page_size = 25
  ): Promise<PaginatedResponse<BranchInventoryWithDetails>> {
    console.log(`[LocalRead] getBranchInventory: branch=${branchId}, search=${params.search}, low_stock=${params.low_stock_only}, page=${page}`);
    const db = await getDb();
    const qualifiers = ["bi.branch_id = $1"];
    const values: unknown[] = [branchId];

    if (params.search) {
      values.push(sqlLike(params.search));
      qualifiers.push(`(
        LOWER(d.name) LIKE $${values.length} OR
        LOWER(d.sku) LIKE $${values.length} OR
        LOWER(d.barcode) LIKE $${values.length}
      )`);
    }
    if (params.low_stock_only) {
      qualifiers.push("(bi.quantity - bi.reserved_quantity) <= COALESCE(d.reorder_level, 0)");
    }
    if (params.drug_type) {
      values.push(params.drug_type);
      if (params.drug_type === "prescription") {
        qualifiers.push(`(d.drug_type = $${values.length} OR d.requires_prescription = 1)`);
      } else {
        qualifiers.push(`d.drug_type = $${values.length}`);
      }
    }
    if (!params.include_zero_stock) {
      qualifiers.push("bi.quantity > 0");
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";

    const countRows = await db.select<{ total: number }[]>(
      `SELECT COUNT(*) AS total FROM branch_inventory bi LEFT JOIN drugs d ON d.id = bi.drug_id ${where}`,
      values
    );
    const total = countRows[0]?.total ?? 0;
    console.log(`[LocalRead] getBranchInventory: found ${total} total rows`);
    const offset = (page - 1) * page_size;

    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT bi.*, d.name as drug_name, d.sku as drug_sku,
         d.drug_type as drug_type, d.requires_prescription as requires_prescription,
         d.organization_id as organization_id, d.generic_name as drug_generic_name,
         d.strength as drug_strength, d.tax_rate as drug_tax_rate,
         d.unit_price as drug_unit_price, d.reorder_level as drug_reorder_level
       FROM branch_inventory bi
       LEFT JOIN drugs d ON d.id = bi.drug_id
       ${where}
       ORDER BY bi.updated_at DESC
       LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    console.log(`[LocalRead] getBranchInventory: returning ${rows.length} items for page ${page}`);
    const items = rows.map((row) => ({
      id: String(row.id),
      branch_id: String(row.branch_id),
      drug_id: String(row.drug_id),
      quantity: toNumber(row.quantity),
      reserved_quantity: toNumber(row.reserved_quantity),
      location: row.location === null ? null : String(row.location),
      selling_price: row.selling_price === null ? null : toNumber(row.selling_price),
      sync_status: String(row.sync_status) as any,
      sync_version: toNumber(row.sync_version, 1),
      synced_at: row.synced_at === null ? null : String(row.synced_at),
      updated_at: String(row.updated_at),
      created_at: String(row.created_at),
      drug_name: String(row.drug_name ?? ""),
      drug_sku: row.drug_sku === null ? null : String(row.drug_sku),
      drug_type: String(row.drug_type ?? "otc") as any,
      requires_prescription: toBoolean(row.requires_prescription),
      organization_id: row.organization_id === null ? "" : String(row.organization_id ?? ""),
      drug_generic_name: row.drug_generic_name === null ? null : String(row.drug_generic_name ?? ""),
      drug_strength: row.drug_strength === null ? null : String(row.drug_strength ?? ""),
      drug_tax_rate: toNumber(row.drug_tax_rate),
      catalog_unit_price: toNumber(row.drug_unit_price),
      drug_unit_price: toNumber(row.drug_unit_price),
      effective_unit_price: row.selling_price !== null ? toNumber(row.selling_price) : toNumber(row.drug_unit_price),
      drug_reorder_level: toNumber(row.drug_reorder_level),
      branch_name: "",
      branch_code: "",
      available_quantity: toNumber(row.quantity) - toNumber(row.reserved_quantity),
    }));

    return buildPagination(items, page, page_size, total);
  },

  async getBatchesForDrug(
    drugId: string,
    params: { branch_id?: string; include_expired?: boolean; include_empty?: boolean } = {},
    page = 1,
    page_size = 25
  ): Promise<PaginatedResponse<DrugBatch>> {
    const db = await getDb();
    const qualifiers = ["drug_id = $1"];
    const values: unknown[] = [drugId];

    if (params.branch_id) {
      values.push(params.branch_id);
      qualifiers.push(`branch_id = $${values.length}`);
    }
    if (!params.include_empty) {
      qualifiers.push("remaining_quantity > 0");
    }
    if (!params.include_expired) {
      qualifiers.push("DATE(expiry_date) >= DATE('now')");
    }

    const where = qualifiers.length ? `WHERE ${qualifiers.join(" AND ")}` : "";
    const countRows = await db.select<{ total: number }[]>(`SELECT COUNT(*) AS total FROM drug_batches ${where}`, values);
    const total = countRows[0]?.total ?? 0;
    const offset = (page - 1) * page_size;
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT * FROM drug_batches ${where} ORDER BY expiry_date ASC LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
      [...values, page_size, offset]
    );

    const items = rows.map((row) => {
      const expiry = String(row.expiry_date);
      const daysUntilExpiry = Math.floor((new Date(expiry).getTime() - Date.now()) / 86_400_000);
      return {
        id: String(row.id),
        branch_id: String(row.branch_id),
        drug_id: String(row.drug_id),
        batch_number: String(row.batch_number),
        quantity: toNumber(row.quantity),
        remaining_quantity: toNumber(row.remaining_quantity),
        manufacturing_date: row.manufacturing_date === null ? null : String(row.manufacturing_date),
        expiry_date: expiry,
        cost_price: row.cost_price === null ? null : toNumber(row.cost_price),
        selling_price: row.selling_price === null ? null : toNumber(row.selling_price),
        supplier: row.supplier === null ? null : String(row.supplier),
        purchase_order_id: row.purchase_order_id === null ? null : String(row.purchase_order_id),
      sync_status: String(row.sync_status) as any,
        sync_version: toNumber(row.sync_version, 1),
        synced_at: row.synced_at === null ? null : String(row.synced_at),
        updated_at: String(row.updated_at),
        created_at: String(row.created_at),
        days_until_expiry: daysUntilExpiry,
        is_expired: daysUntilExpiry < 0,
        is_expiring_soon: daysUntilExpiry <= 90,
      };
    });

    return buildPagination(items, page, page_size, total);
  },

  async getLowStock(branchId: string): Promise<LowStockReport> {
    const db = await getDb();
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT bi.branch_id, d.id as drug_id, d.name as drug_name, d.sku as sku,
        bi.quantity, d.reorder_level, d.reorder_quantity
       FROM branch_inventory bi
       LEFT JOIN drugs d ON d.id = bi.drug_id
       WHERE bi.branch_id = $1
         AND bi.quantity <= COALESCE(d.reorder_level, 0)`,
      [branchId]
    );

    const items = rows.map((row) => {
      const quantity = toNumber(row.quantity);
      const reorder_level = toNumber(row.reorder_level);
      const status: "out_of_stock" | "low_stock" = quantity === 0 ? "out_of_stock" : "low_stock";
      const recommended_order_quantity = Math.max(0, (toNumber(row.reorder_quantity) || 0) - quantity);
      return {
        branch_id: String(row.branch_id),
        drug_id: String(row.drug_id),
        drug_name: String(row.drug_name ?? ""),
        batch_number: String(row.batch_number ?? ""),
        sku: row.sku === null ? null : String(row.sku),
        branch_name: "",
        quantity,
        reorder_level,
        reorder_quantity: toNumber(row.reorder_quantity),
        status,
        recommended_order_quantity,
      };
    });

    return {
      organization_id: "",
      branch_id: branchId,
      report_date: new Date().toISOString(),
      items,
      total_items: items.length,
      out_of_stock_count: items.filter((item) => item.status === "out_of_stock").length,
      low_stock_count: items.filter((item) => item.status === "low_stock").length,
    };
  },

  async getExpiring(branchId: string, daysThreshold: number): Promise<ExpiringBatchReport> {
    const db = await getDb();
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT db.branch_id, db.drug_id, d.name as drug_name, d.sku as sku,
        db.id as batch_id, db.batch_number, db.remaining_quantity,
        db.expiry_date, db.cost_price, db.selling_price
       FROM drug_batches db
       LEFT JOIN drugs d ON d.id = db.drug_id
       WHERE db.branch_id = $1
         AND db.remaining_quantity > 0
         AND DATE(db.expiry_date) >= DATE('now')
         AND DATE(db.expiry_date) <= DATE('now', '+' || $2 || ' days')
       ORDER BY db.expiry_date ASC`,
      [branchId, daysThreshold]
    );

    const items = rows.map((row) => {
      const expiry = String(row.expiry_date);
      const daysUntilExpiry = Math.max(0, Math.floor((new Date(expiry).getTime() - Date.now()) / 86_400_000));
      return {
        batch_id: String(row.batch_id),
        drug_id: String(row.drug_id),
        drug_name: String(row.drug_name ?? ""),
        batch_number: String(row.batch_number ?? ""),
        sku: row.sku === null ? null : String(row.sku),
        branch_id: String(row.branch_id),
        branch_name: "",
        remaining_quantity: toNumber(row.remaining_quantity),
        expiry_date: expiry,
        days_until_expiry: daysUntilExpiry,
        cost_value: toNumber(row.cost_price) * toNumber(row.remaining_quantity),
        selling_value: row.selling_price === null ? null : toNumber(row.selling_price) * toNumber(row.remaining_quantity),
      };
    });

    return {
      organization_id: "",
      branch_id: branchId,
      report_date: new Date().toISOString(),
      days_threshold: daysThreshold,
      items,
      total_items: items.length,
      total_quantity: items.reduce((sum, item) => sum + item.remaining_quantity, 0),
      total_cost_value: items.reduce((sum, item) => sum + item.cost_value, 0),
      total_selling_value: items.reduce((sum, item) => sum + (item.selling_value ?? 0), 0),
    };
  },

  async getValuation(branchId: string): Promise<InventoryValuationResponse> {
    const db = await getDb();
    const rows = await db.select<Record<string, unknown>[]>(
      `SELECT bi.branch_id, bi.drug_id, bi.quantity, coalesce(d.name, '') as drug_name, d.sku,
         coalesce(d.cost_price, 0) as cost_price, coalesce(bi.selling_price, d.unit_price, 0) as branch_selling_price,
         coalesce(d.unit_price, 0) as selling_price
       FROM branch_inventory bi
       LEFT JOIN drugs d ON d.id = bi.drug_id
       WHERE bi.branch_id = $1`,
      [branchId]
    );

    const items = rows.map((row) => {
      const quantity = toNumber(row.quantity);
      const cost_price = toNumber(row.cost_price);
      const selling_price = toNumber(row.selling_price);
      return {
        drug_id: String(row.drug_id),
        drug_name: String(row.drug_name ?? ""),
        sku: row.sku === null ? null : String(row.sku),
        quantity,
        cost_price,
        selling_price,
        branch_selling_price: toNumber(row.branch_selling_price),
        total_cost_value: quantity * cost_price,
        total_selling_value: quantity * selling_price,
        potential_profit: quantity * (selling_price - cost_price),
      };
    });

    const total_cost_value = items.reduce((sum, item) => sum + item.total_cost_value, 0);
    const total_selling_value = items.reduce((sum, item) => sum + item.total_selling_value, 0);
    const total_potential_profit = items.reduce((sum, item) => sum + item.potential_profit, 0);

    return {
      branch_id: branchId,
      branch_name: "",
      valuation_date: new Date().toISOString(),
      items,
      total_items: items.length,
      total_quantity: items.reduce((sum, item) => sum + item.quantity, 0),
      total_cost_value,
      total_selling_value,
      total_potential_profit,
      profit_margin_percentage: total_selling_value === 0 ? 0 : (total_potential_profit / total_selling_value) * 100,
    };
  },
};
