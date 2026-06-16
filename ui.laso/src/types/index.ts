// ============================================================
// BASE TYPES
// ============================================================

export interface TimestampFields {
    created_at: string;
    updated_at: string;
}

/**
 * Sync tracking fields as they appear on the CLIENT side.
 *
 * Note on naming: the local SQLite schema intentionally uses `synced_at`
 * (not `last_synced_at`).  The server model uses `last_synced_at` but that
 * field name is an internal concern — the sync engine maps it to the local
 * column during upsert.  Components always read from the local layer so
 * `synced_at` is the correct name here.
 */
export interface SyncFields {
    sync_status: "synced" | "pending" | "conflict" | "deleted";
    sync_version: number;
    synced_at: string | null;
}

export interface SoftDeleteFields {
    is_deleted: boolean;
    deleted_at: string | null;
    deleted_by: string | null;
}

export interface PaginatedResponse<T> {
    items: T[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
    has_next: boolean;
    has_prev: boolean;
}

export interface ApiError {
    detail: string;
    errors?: Array<{
        loc: (string | number)[];
        msg: string;
        type: string;
        input?: unknown;
    }>;
    request_id?: string;
}

// ============================================================
// AUTH / USER TYPES
// ============================================================

export type UserRole =
    | "super_admin"
    | "admin"
    | "manager"
    | "pharmacist"
    | "cashier"
    | "viewer";

export interface UserPermissions {
    additional: string[];
    denied: string[];
}

export interface User extends TimestampFields, SyncFields {
    id: string;
    organization_id: string;
    username: string;
    email: string;
    full_name: string;
    role: UserRole;
    phone: string | null;
    employee_id: string | null;
    assigned_branches: string[];
    permissions: UserPermissions;
    account_locked_until: string | null;
    is_active: boolean;
    last_login: string | null;
    two_factor_enabled: boolean;
    deleted_at: string | null;
}

export interface UserCreate {
    username: string;
    email: string;
    full_name: string;
    password: string;
    role?: UserRole;
    phone?: string;
    employee_id?: string;
    organization_id?: string;
    assigned_branches?: string[];
}

/**
 * Admin account created during onboarding.
 * Omits `organization_id` — the org doesn't exist yet at that point.
 */
export interface AdminCreate {
    username: string;
    email: string;
    full_name: string;
    password: string;
    role: "admin";
    phone?: string;
    employee_id?: string;
}

export interface UserUpdate {
    full_name?: string;
    phone?: string;
    role?: UserRole;
    assigned_branches?: string[];
    is_active?: boolean;
}

export interface LoginRequest {
    username: string;
    password: string;
    device_info?: string;
}

export interface PasswordChange {
    old_password: string;
    new_password: string;
}

export interface TokenResponse {
    access_token: string;
    refresh_token: string;
    token_type: "bearer";
    expires_in: number;
    user: User;
}

export interface RefreshTokenRequest {
    refresh_token: string;
}

export type UserResponse = User;

// ============================================================
// ORGANIZATION TYPES
// ============================================================

export type OrgType = "otc" | "pharmacy" | "hospital_pharmacy" | "chain";
export type SubscriptionTier = "basic" | "professional" | "enterprise";

export interface OrgAddress {
    street?: string;
    city?: string;
    state?: string;
    zip?: string;
    country?: string;
}

export interface Organization extends TimestampFields, SyncFields {
    id: string;
    name: string;
    type: OrgType;
    license_number: string | null;
    tax_id: string | null;
    phone: string | null;
    email: string | null;
    address: OrgAddress | null;
    settings: Record<string, unknown>;
    is_active: boolean;
    subscription_tier: SubscriptionTier;
    subscription_expires_at: string | null;
}

export interface OrganizationCreate {
    name: string;
    type: OrgType;
    license_number?: string;
    tax_id?: string;
    phone?: string;
    email?: string;
    address?: OrgAddress;
    settings?: Record<string, unknown>;
    subscription_tier?: SubscriptionTier;
}

export interface OrganizationUpdate {
    name?: string;
    type?: OrgType;
    phone?: string;
    email?: string;
    address?: OrgAddress;
    settings?: Record<string, unknown>;
    is_active?: boolean;
}

// ============================================================
// ONBOARDING TYPES
// ============================================================

export interface OnboardingRequest {
    // Organization
    name: string;
    type: OrgType;
    license_number?: string;
    tax_id?: string;
    phone?: string;
    email?: string;
    address?: OrgAddress;
    subscription_tier: SubscriptionTier;
    currency: string;
    timezone: string;
    additional_settings?: Record<string, unknown>;
    // Admin — uses AdminCreate (no organization_id) instead of UserCreate
    admin: AdminCreate;
    // Branches (optional, max 10)
    branches?: BranchCreate[];
}

export interface OnboardingResponse {
    organization: Organization;
    admin_user: User;
    branches: Branch[];
    message: string;
    temp_credentials?: Record<string, string>;
}

export interface OrganizationStats {
    organization_id: string;
    total_branches: number;
    total_users: number;
    total_drugs: number;
    total_customers: number;
    total_sales_today: number;
    total_sales_this_month: number;
    subscription_status: string;
    subscription_expires_at: string | null;
    days_until_expiry: number | null;
    is_active: boolean;
}

// ============================================================
// BRANCH TYPES
// ============================================================

export interface BranchAddress {
    street?: string;
    city?: string;
    state?: string;
    zip_code?: string;
    country: string;
}

export interface OperatingHours {
    open_time?: string;
    close_time?: string;
    is_closed: boolean;
}

export interface WeeklyOperatingHours {
    monday?: OperatingHours;
    tuesday?: OperatingHours;
    wednesday?: OperatingHours;
    thursday?: OperatingHours;
    friday?: OperatingHours;
    saturday?: OperatingHours;
    sunday?: OperatingHours;
}

export interface Branch extends TimestampFields, SyncFields {
    id: string;
    organization_id: string;
    name: string;
    code: string;
    phone: string | null;
    email: string | null;
    address: BranchAddress | null;
    manager_id: string | null;
    manager_name: string | null;
    is_active: boolean;
    operating_hours: WeeklyOperatingHours | null;
}

export interface BranchCreate {
    name: string;
    code?: string;
    phone?: string;
    email?: string;
    address?: BranchAddress;
    manager_id?: string;
    operating_hours?: WeeklyOperatingHours;
    is_active?: boolean;
    organization_id?: string;
}

export interface BranchUpdate {
    name?: string;
    code?: string;
    phone?: string;
    email?: string;
    address?: BranchAddress;
    manager_id?: string;
    operating_hours?: WeeklyOperatingHours;
    is_active?: boolean;
}

export interface BranchListItem {
    id: string;
    organization_id: string;
    name: string;
    code: string;
    is_active: boolean;
    manager_id: string | null;
    manager_name: string | null;
    phone: string | null;
    email: string | null;
    created_at: string;
}

// ============================================================
// DRUG CATEGORY TYPES
// ============================================================

export interface DrugCategory extends TimestampFields, SyncFields, SoftDeleteFields {
    id: string;
    organization_id: string;
    name: string;
    description: string | null;
    parent_id: string | null;
    path: string | null;
    level: number;
}

export interface DrugCategoryCreate {
    name: string;
    description?: string;
    parent_id?: string;
    organization_id: string;
}

export interface DrugCategoryUpdate {
    name?: string;
    description?: string | null;
    parent_id?: string | null;
}

export interface DrugCategoryTree extends DrugCategory {
    children: DrugCategoryTree[];
}

// ============================================================
// DRUG TYPES
// ============================================================

export type DrugType = "prescription" | "otc" | "controlled" | "herbal" | "supplement";

export interface Drug extends TimestampFields, SyncFields, SoftDeleteFields {
    id: string;
    organization_id: string;
    name: string;
    generic_name: string | null;
    brand_name: string | null;
    sku: string | null;
    barcode: string | null;
    category_id: string | null;
    drug_type: DrugType;
    dosage_form: string | null;
    strength: string | null;
    manufacturer: string | null;
    supplier: string | null;
    ndc_code: string | null;
    requires_prescription: boolean;
    controlled_substance_schedule: string | null;
    unit_price: number;
    cost_price: number | null;
    markup_percentage: number | null;
    tax_rate: number;
    reorder_level: number;
    reorder_quantity: number;
    max_stock_level: number | null;
    unit_of_measure: string;
    description: string | null;
    usage_instructions: string | null;
    side_effects: string | null;
    contraindications: string | null;
    storage_conditions: string | null;
    image_url: string | null;
    is_active: boolean;
    /** Computed by the API: ((unit_price - cost_price) / cost_price) * 100 */
    profit_margin: number | null;
}

export interface DrugWithInventory extends Drug {
    total_quantity: number;
    available_quantity: number;
    reserved_quantity: number;
    inventory_status: "in_stock" | "low_stock" | "out_of_stock" | "unknown";
    needs_reorder: boolean;
}

export interface DrugCreate {
    name: string;
    generic_name?: string;
    brand_name?: string;
    sku?: string;
    barcode?: string;
    category_id?: string;
    drug_type: DrugType;
    dosage_form?: string;
    strength?: string;
    manufacturer?: string;
    supplier?: string;
    ndc_code?: string;
    requires_prescription: boolean;
    controlled_substance_schedule?: string;
    unit_price: number;
    cost_price?: number;
    markup_percentage?: number;
    tax_rate: number;
    reorder_level: number;
    reorder_quantity: number;
    max_stock_level?: number;
    unit_of_measure: string;
    description?: string;
    usage_instructions?: string;
    side_effects?: string;
    contraindications?: string;
    storage_conditions?: string;
    image_url?: string;
    is_active: boolean;
    organization_id: string;
}

export interface DrugUpdate {
    name?: string;
    generic_name?: string | null;
    brand_name?: string | null;
    sku?: string | null;
    barcode?: string | null;
    category_id?: string | null;
    drug_type?: DrugType;
    dosage_form?: string | null;
    strength?: string | null;
    manufacturer?: string | null;
    supplier?: string | null;
    ndc_code?: string | null;
    requires_prescription?: boolean;
    controlled_substance_schedule?: string | null;
    unit_price?: number;
    cost_price?: number | null;
    markup_percentage?: number | null;
    tax_rate?: number;
    reorder_level?: number;
    reorder_quantity?: number;
    max_stock_level?: number | null;
    unit_of_measure?: string;
    description?: string | null;
    usage_instructions?: string | null;
    side_effects?: string | null;
    contraindications?: string | null;
    storage_conditions?: string | null;
    image_url?: string | null;
    is_active?: boolean;
}

export interface DrugSearchFilters {
    search?: string;
    category_id?: string;
    drug_type?: DrugType;
    requires_prescription?: boolean;
    is_active?: boolean;
    min_price?: number;
    max_price?: number;
    manufacturer?: string;
    supplier?: string;
    branch_id?: string;
}

export interface BulkDrugUpdate {
    drug_ids: string[];
    updates: DrugUpdate;
}

/**
 * Response from the bulk-update endpoint.
 * Shape matches the router response exactly:
 *   { successful, failed, total, message }
 */
export interface BulkDrugUpdateResult {
    successful: number;
    failed: number;
    total: number;
    message: string;
}

// ============================================================
// INVENTORY TYPES
// ============================================================

export interface BranchInventory extends TimestampFields, SyncFields {
    id: string;
    branch_id: string;
    drug_id: string;
    quantity: number;
    reserved_quantity: number;
    location: string | null;
    selling_price: number | null;
}

export interface BranchInventoryWithDetails extends BranchInventory {
    drug_name: string;
    drug_sku: string | null;
    drug_type?: DrugType;
    requires_prescription?: boolean;
    catalog_unit_price?: number;
    drug_unit_price: number;
    effective_unit_price: number;
    /**
     * Joined from Drug.reorder_level so the inventory list can render stock
     * status badges without a separate per-drug API call.
     */
    drug_reorder_level: number;
    branch_name: string;
    branch_code: string;
    /**
     * Client-computed convenience field: `quantity - reserved_quantity`.
     * The server does not return this; compute it where needed:
     *   const available = inv.available_quantity ?? (inv.quantity - inv.reserved_quantity);
     */
    available_quantity?: number;
}

export interface DrugBatch extends TimestampFields, SyncFields {
    id: string;
    branch_id: string;
    drug_id: string;
    batch_number: string;
    quantity: number;
    remaining_quantity: number;
    manufacturing_date: string | null;
    expiry_date: string;
    cost_price: number | null;
    selling_price: number | null;
    supplier: string | null;
    purchase_order_id: string | null;
    /**
     * Client-computed: `(new Date(expiry_date).getTime() - Date.now()) / 86_400_000 | 0`
     * Not stored in SQLite or returned by the server.
     */
    days_until_expiry?: number;
    /** Client-computed: `new Date(expiry_date) < new Date()` */
    is_expired?: boolean;
    /** Client-computed: `days_until_expiry !== undefined && days_until_expiry <= 90` */
    is_expiring_soon?: boolean;
}

export interface DrugBatchWithDetails extends DrugBatch {
    drug_name: string;
    drug_generic_name: string | null;
    drug_sku: string | null;
    branch_name: string;
}

export interface DrugBatchCreate {
    branch_id: string;
    drug_id: string;
    purchase_order_id?: string;
    batch_number: string;
    quantity: number;
    // FIX: remaining_quantity removed — the server always sets it equal to
    // quantity on creation.  Exposing it here let callers pass a different
    // value, which would immediately corrupt the batch's inventory count.
    manufacturing_date?: string;
    expiry_date: string;
    cost_price?: number;
    selling_price?: number;
    supplier?: string;
}

export interface DrugBatchUpdate {
    batch_number?: string;
    quantity?: number;
    remaining_quantity?: number;
    manufacturing_date?: string | null;
    expiry_date?: string;
    cost_price?: number | null;
    selling_price?: number | null;
    supplier?: string | null;
}

export interface StockAdjustmentCreate {
    branch_id: string;
    drug_id: string;
    /**
     * FIX: "sale" removed — the backend's CheckConstraint does not include it.
     * Sale-driven deductions use "correction" (handled internally by SalesService,
     * not by a separate client-initiated adjustment call).
     */
    adjustment_type: "damage" | "expired" | "theft" | "return" | "correction" | "transfer";
    quantity_change: number;
    reason?: string;
    transfer_to_branch_id?: string;
}

export interface StockAdjustmentResponse {
    id: string;
    branch_id: string;
    drug_id: string;
    adjustment_type: string;
    quantity_change: number;
    previous_quantity: number;
    new_quantity: number;
    reason: string | null;
    // FIX: was `created_by` — the model column is `adjusted_by`
    adjusted_by: string;
    transfer_to_branch_id: string | null;
    created_at: string;
}

// ── Inter-branch stock transfer ───────────────────────────────

export interface StockTransferCreate {
    from_branch_id: string;
    to_branch_id: string;
    drug_id: string;
    quantity: number;
    /** Free-text reason; required by the backend route. */
    reason: string;
}

export interface StockTransferResponse {
    source_adjustment: StockAdjustmentResponse;
    destination_adjustment: StockAdjustmentResponse;
    success: boolean;
    message: string;
}

// ── Inventory reports ─────────────────────────────────────────

export interface LowStockReport {
    organization_id: string;
    /** null means the report covers all branches in the organisation */
    branch_id: string | null;
    report_date: string;
    items: LowStockItem[];
    total_items: number;
    out_of_stock_count: number;
    low_stock_count: number;
}

export interface ExpiringBatchReport {
    organization_id: string;
    branch_id: string | null;
    report_date: string;
    days_threshold: number;
    items: ExpiringBatchItem[];
    total_items: number;
    total_quantity: number;
    total_cost_value: number;
    total_selling_value: number;
}

export interface LowStockItem {
    drug_id: string;
    drug_name: string;
    sku: string | null;
    branch_id: string;
    branch_name: string;
    quantity: number;
    reorder_level: number;
    reorder_quantity: number;
    status: "out_of_stock" | "low_stock";
    recommended_order_quantity: number;
}

export interface ExpiringBatchItem {
    batch_id: string;
    drug_id: string;
    drug_name: string;
    batch_number: string;
    branch_id: string;
    branch_name: string;
    remaining_quantity: number;
    expiry_date: string;
    days_until_expiry: number;
    cost_value: number;
    // selling_value is computed server-side and may be absent in older API
    // versions. Always guard with ?? before calling .toFixed().
    selling_value: number | null;
}

// ============================================================
// INVENTORY VALUATION TYPES
// (previously missing — getValuation() returned Promise<unknown>)
// ============================================================

export interface InventoryValuationItem {
    drug_id: string;
    drug_name: string;
    sku: string | null;
    quantity: number;
    cost_price: number;
    selling_price: number;
    branch_selling_price: number | null;
    total_cost_value: number;
    total_selling_value: number;
    potential_profit: number;
}

export interface InventoryValuationResponse {
    branch_id: string;
    branch_name: string;
    valuation_date: string;
    items: InventoryValuationItem[];
    total_items: number;
    total_quantity: number;
    total_cost_value: number;
    total_selling_value: number;
    total_potential_profit: number;
    profit_margin_percentage: number;
}

// ============================================================
// CUSTOMER TYPES
// ============================================================

export type CustomerType = "walk_in" | "registered" | "insurance" | "corporate";
export type LoyaltyTier = "bronze" | "silver" | "gold" | "platinum";

export interface Customer extends TimestampFields, SyncFields, SoftDeleteFields {
    id: string;
    organization_id: string;
    customer_type: CustomerType;
    first_name: string | null;
    last_name: string | null;
    phone: string | null;
    email: string | null;
    date_of_birth: string | null;
    address: OrgAddress | null;
    allergies: string[];
    chronic_conditions: string[];
    loyalty_points: number;
    loyalty_tier: LoyaltyTier;
    /** Denormalized: total completed sales count, kept in sync by SalesService */
    total_orders: number;
    /** Denormalized: cumulative spend in GHS, kept in sync by SalesService */
    total_value: number;
    preferred_contact_method: "email" | "phone" | "sms";
    marketing_consent: boolean;
    is_active: boolean;
    insurance_provider_id: string | null;
    insurance_member_id: string | null;
    insurance_card_image_url: string | null;
    preferred_contract_id: string | null;
}

// ============================================================
// PRICING / CONTRACT TYPES
// ============================================================

export type ContractType =
    | "insurance"
    | "corporate"
    | "staff"
    | "senior_citizen"
    | "standard"
    | "wholesale";

export type ContractStatus = "draft" | "active" | "suspended" | "expired" | "cancelled";
export type DiscountType = "percentage" | "fixed_amount" | "custom";

export interface PriceContract extends TimestampFields, SyncFields, SoftDeleteFields {
    id: string;
    organization_id: string;
    contract_code: string;
    contract_name: string;
    description: string | null;
    contract_type: ContractType;
    is_default_contract: boolean;
    discount_type: DiscountType;
    discount_percentage: number;
    applies_to_prescription_only: boolean;
    applies_to_otc: boolean;
    excluded_drug_categories: string[];
    excluded_drug_ids: string[];
    minimum_price_override: number | null;
    maximum_discount_amount: number | null;
    minimum_purchase_amount: number | null;
    maximum_purchase_amount: number | null;
    applies_to_all_branches: boolean;
    applicable_branch_ids: string[];
    effective_from: string;
    effective_to: string | null;
    requires_verification: boolean;
    allowed_user_roles: string[];
    insurance_provider_id: string | null;
    copay_amount: number | null;
    copay_percentage: number | null;
    status: ContractStatus;
    is_active: boolean;
    total_transactions: number;
    total_discount_given: number;
    last_used_at: string | null;
    created_by: string;
    approved_by: string | null;
    approved_at: string | null;
}

/**
 * Per-drug pricing override within a contract.
 * Corresponds to the PriceContractItem model.
 */
export interface PriceContractItem {
    id: string;
    contract_id: string;
    drug_id: string;
    override_discount_percentage: number | null;
    fixed_price: number | null;
    is_excluded: boolean;
    notes: string | null;
    created_at: string;
    updated_at: string;
}

export interface InsuranceProvider extends TimestampFields, SyncFields, SoftDeleteFields {
    id: string;
    organization_id: string;
    name: string;
    code: string;
    logo_url: string | null;
    phone: string | null;
    email: string | null;
    website: string | null;
    billing_cycle: "daily" | "weekly" | "monthly" | "quarterly" | "annually";
    payment_terms: string;
    requires_card_verification: boolean;
    requires_preauth: boolean;
    is_active: boolean;
}

// ============================================================
// SALES TYPES
// ============================================================

export type PaymentMethod = "cash" | "card" | "mobile_money" | "insurance" | "credit" | "split";
export type PaymentStatus = "pending" | "completed" | "partial" | "refunded" | "cancelled";
export type SaleStatus = "draft" | "completed" | "cancelled" | "refunded";

/** Matches SaleItemCreate Pydantic schema */
export interface SaleItemCreate {
    drug_id: string;
    quantity: number;
    batch_id?: string;
    requires_prescription: boolean;
    prescription_verified: boolean;
}

/** Matches SaleCreate Pydantic schema exactly */
export interface SaleCreate {
    branch_id: string;
    price_contract_id: string;
    contract_verification_token?: string;
    customer_id?: string;
    customer_name?: string;
    items: SaleItemCreate[];
    payment_method: PaymentMethod;
    amount_paid?: number;
    payment_reference?: string;
    split_payment_details?: Record<string, number>;
    prescription_id?: string;
    insurance_verified: boolean;
    insurance_claim_number?: string;
    insurance_preauth_number?: string;
    notes?: string;
}

export interface SaleItem extends TimestampFields {
    id: string;
    sale_id: string;
    drug_id: string;
    drug_name: string;
    drug_sku: string | null;
    drug_generic_name: string | null;
    usage_instructions: string | null;
    batch_id: string | null;
    batch_number: string | null;
    batch_expiry_date: string | null;
    quantity: number;
    unit_price: number;
    subtotal: number;
    discount_percentage: number;
    discount_amount: number;
    contract_discount_percentage: number;
    contract_discount_amount: number;
    additional_discount_amount: number;
    total_discount_amount: number;
    tax_rate: number;
    tax_amount: number;
    total_price: number;
    applied_contract_id: string | null;
    applied_contract_name: string | null;
    insurance_covered: boolean;
    patient_copay: number | null;
    requires_prescription: boolean;
    prescription_verified: boolean;
    prescription_id: string | null;
    allergy_check_performed: boolean;
}

export interface Sale extends TimestampFields, SyncFields {
    id: string;
    organization_id: string;
    branch_id: string;
    sale_number: string;
    customer_id: string | null;
    customer_name: string | null;
    customer_full_name?: string | null;
    subtotal: number;
    discount_amount: number;
    tax_amount: number;
    total_amount: number;
    price_contract_id: string | null;
    contract_name: string | null;
    contract_discount_percentage: number | null;
    payment_method: PaymentMethod;
    payment_status: PaymentStatus;
    amount_paid: number | null;
    change_amount: number | null;
    payment_reference: string | null;
    prescription_id: string | null;
    prescription_number: string | null;
    prescriber_name: string | null;
    cashier_id: string;
    pharmacist_id: string | null;
    insurance_claim_number: string | null;
    patient_copay_amount: number | null;
    insurance_covered_amount: number | null;
    insurance_verified: boolean;
    insurance_verified_at: string | null;
    insurance_verified_by: string | null;
    notes: string | null;
    status: SaleStatus;
    receipt_printed: boolean;
    receipt_emailed: boolean;
    // items is only present on SaleWithDetails (single-sale fetch).
    // The list endpoint returns SaleResponse which omits items but includes items_count.
    items?: SaleItem[];
    items_count: number;
}

/** Sale with all resolved detail fields — used by ProcessSaleResponse and receipt display */
export interface SaleWithDetails extends Sale {
    contract_discount_amount: number;
    additional_discount_amount: number;
    total_discount_amount: number;
    contract_type: string | null;
    insurance_preauth_number: string | null;
    split_payment_details: Record<string, number> | null;
    cashier_name: string | null;
    pharmacist_name: string | null;
    manager_approval_user_id: string | null;
    customer_full_name: string | null;
    customer_phone: string | null;
    customer_email: string | null;
    customer_loyalty_tier: string | null;
    branch_name: string;
    branch_address: Record<string, unknown> | null;
    organization_name: string;
    organization_tax_id: string | null;
}

// ============================================================
// PRESCRIPTION TYPES
// ============================================================

export type PrescriptionStatus = "active" | "filled" | "expired" | "cancelled";

export interface PrescriptionMedication {
    drug_id: string;
    drug_name: string;
    dosage: string;
    frequency: string;
    duration: string;
    quantity: number;
}

export interface Prescription extends TimestampFields, SyncFields {
    id: string;
    organization_id: string;
    prescription_number: string;
    customer_id: string;
    prescriber_name: string;
    prescriber_license: string;
    prescriber_phone: string | null;
    prescriber_address: string | null;
    issue_date: string;
    expiry_date: string;
    medications: PrescriptionMedication[];
    diagnosis: string | null;
    notes: string | null;
    special_instructions: string | null;
    refills_allowed: number;
    refills_remaining: number;
    last_refill_date: string | null;
    status: PrescriptionStatus;
    verified_by: string | null;
    verified_at: string | null;
}

// // ============================================================
// // SUPPLIER / PURCHASE ORDER TYPES
// // ============================================================

// export type PurchaseOrderStatus =
//     | "draft"
//     | "pending"
//     | "approved"
//     | "ordered"
//     | "received"
//     | "cancelled";

// export interface Supplier extends TimestampFields, SyncFields, SoftDeleteFields {
//     id: string;
//     organization_id: string;
//     name: string;
//     contact_person: string | null;
//     phone: string | null;
//     email: string | null;
//     address: OrgAddress | null;
//     tax_id: string | null;
//     payment_terms: string | null;
//     credit_limit: number | null;
//     rating: number | null;
//     total_orders: number;
//     total_value: number;
//     is_active: boolean;
// }

// export interface PurchaseOrderItem extends TimestampFields {
//     id: string;
//     purchase_order_id: string;
//     drug_id: string;
//     quantity_ordered: number;
//     quantity_received: number;
//     unit_cost: number;
//     total_cost: number;
//     batch_number: string | null;
//     expiry_date: string | null;
// }

// export interface PurchaseOrder extends TimestampFields, SyncFields {
//     id: string;
//     organization_id: string;
//     branch_id: string;
//     po_number: string;
//     supplier_id: string;
//     subtotal: number;
//     tax_amount: number;
//     shipping_cost: number;
//     total_amount: number;
//     status: PurchaseOrderStatus;
//     ordered_by: string;
//     approved_by: string | null;
//     approved_at: string | null;
//     expected_delivery_date: string | null;
//     received_date: string | null;
//     notes: string | null;
//     items: PurchaseOrderItem[];
// }
// ============================================================
// PURCHASE ORDER TYPES  (drop-in replacement for the
// SUPPLIER / PURCHASE ORDER TYPES section in index.ts)
// ============================================================

export type PurchaseOrderStatus =
    | "draft"
    | "pending"
    | "approved"
    | "ordered"       // partially received
    | "received"      // fully received
    | "cancelled";    // rejected or manually cancelled

// ── Supplier ─────────────────────────────────────────────────

export interface Supplier extends TimestampFields, SyncFields, SoftDeleteFields {
    id: string;
    organization_id: string;
    name: string;
    contact_person: string | null;
    phone: string | null;
    email: string | null;
    address: OrgAddress | null;
    tax_id: string | null;
    registration_number: string | null;
    payment_terms: string | null;
    credit_limit: number | null;
    rating: number | null;
    total_orders: number;
    total_value: number;
    is_active: boolean;
}

export interface SupplierCreate {
    organization_id: string;
    name: string;
    contact_person?: string;
    phone?: string;
    email?: string;
    address?: OrgAddress;
    tax_id?: string;
    registration_number?: string;
    payment_terms?: string;
    credit_limit?: number;
}

export interface SupplierUpdate {
    name?: string;
    contact_person?: string | null;
    phone?: string | null;
    email?: string | null;
    address?: OrgAddress | null;
    tax_id?: string | null;
    registration_number?: string | null;
    payment_terms?: string | null;
    credit_limit?: number | null;
    is_active?: boolean;
}

// ── Purchase Order Items ──────────────────────────────────────

export interface PurchaseOrderItem extends TimestampFields {
    id: string;
    purchase_order_id: string;
    drug_id: string;
    quantity_ordered: number;
    quantity_received: number;
    unit_cost: number;
    total_cost: number;
    batch_number: string | null;
    expiry_date: string | null;          // ISO date string (YYYY-MM-DD)
    /** Computed by server: quantity_received >= quantity_ordered */
    is_fully_received: boolean;
    /** Computed by server: max(0, quantity_ordered - quantity_received) */
    remaining_quantity: number;
}

export interface PurchaseOrderItemWithDetails extends PurchaseOrderItem {
    drug_name: string;
    drug_sku: string | null;
    drug_generic_name: string | null;
}

export interface PurchaseOrderItemCreate {
    drug_id: string;
    quantity_ordered: number;            // must be > 0
    unit_cost: number;                   // must be > 0
}

// ── Purchase Order ────────────────────────────────────────────

export interface PurchaseOrder extends TimestampFields, SyncFields {
    id: string;
    organization_id: string;
    branch_id: string;
    po_number: string;
    supplier_id: string;
    subtotal: number;
    tax_amount: number;
    shipping_cost: number;
    total_amount: number;
    status: PurchaseOrderStatus;
    ordered_by: string;
    approved_by: string | null;
    approved_at: string | null;
    expected_delivery_date: string | null;
    received_date: string | null;
    notes: string | null;
}

/**
 * Alias matching the Python `PurchaseOrderResponse` schema name.
 * Components that talk directly to the API should use this name;
 * `PurchaseOrder` is kept for the local SQLite / sync layer.
 */
export type PurchaseOrderResponse = PurchaseOrder;

export interface PurchaseOrderWithDetails extends PurchaseOrder {
    items: PurchaseOrderItemWithDetails[];
    supplier_name: string;
    branch_name: string;
    ordered_by_name: string;
    approved_by_name: string | null;
    /** Server-computed: all items fully received */
    is_fully_received: boolean;
    /** Server-computed: count of fully received items */
    total_items_received: number;
    /** Server-computed: e.g. "3 / 5 items received" */
    receipt_progress: string;
}

export interface PurchaseOrderCreate {
    branch_id: string;
    supplier_id: string;
    items: PurchaseOrderItemCreate[];    // at least 1 required
    expected_delivery_date?: string;     // ISO date string
    shipping_cost?: number;
    notes?: string;
}

export interface PurchaseOrderUpdate {
    supplier_id?: string;
    expected_delivery_date?: string | null;
    shipping_cost?: number;
    notes?: string | null;
}

// ── Workflow action payloads ──────────────────────────────────

export interface PurchaseOrderSubmit {
    notes?: string;
}

export interface PurchaseOrderApprove {
    notes?: string;
}

export interface PurchaseOrderReject {
    reason: string;                      // required, max 500 chars
}

export interface PurchaseOrderCancel {
    reason: string;                      // required, max 500 chars
}

// ── Receiving ─────────────────────────────────────────────────

export interface ReceiveItemData {
    purchase_order_item_id: string;
    quantity_received: number;           // must be > 0
    batch_number: string;
    manufacturing_date?: string;         // ISO date string (optional)
    expiry_date: string;                 // ISO date string — must be future
}

export interface ReceivePurchaseOrder {
    received_date?: string;              // ISO date string, defaults to today
    items: ReceiveItemData[];            // at least 1 required
    notes?: string;
}

export interface ReceivePurchaseOrderResponse {
    purchase_order: PurchaseOrderWithDetails;
    batches_created: number;
    inventory_updated: number;
    success: boolean;
    message: string;
}

// ── Filtering & Reporting ─────────────────────────────────────

export interface PurchaseOrderFilters {
    status?: PurchaseOrderStatus;
    supplier_id?: string;
    branch_id?: string;
    start_date?: string;
    end_date?: string;
    min_amount?: number;
    max_amount?: number;
    ordered_by?: string;
}

export interface SupplierPerformance {
    supplier_id: string;
    supplier_name: string;
    total_orders: number;
    total_value: number;
    average_order_value: number;
    on_time_deliveries: number;
    late_deliveries: number;
    /** 0–100 */
    on_time_rate: number;
    rating: number | null;
}

export interface PurchaseSummary {
    total_orders: number;
    total_value: number;
    average_order_value: number;
    draft: number;
    pending_approval: number;
    approved: number;
    pending_delivery: number;
    received: number;
    cancelled: number;
    start_date: string;
    end_date: string;
}

// ============================================================
// SYSTEM / AUDIT TYPES
// ============================================================

export type AlertType =
    | "low_stock"
    | "expiry_warning"
    | "out_of_stock"
    | "system_error"
    | "security"
    | "system_info";

export type AlertSeverity = "low" | "medium" | "high" | "critical";

export interface SystemAlert {
    id: string;
    organization_id: string;
    branch_id: string | null;
    alert_type: AlertType;
    severity: AlertSeverity;
    title: string;
    message: string;
    drug_id: string | null;
    is_resolved: boolean;
    resolved_by: string | null;
    resolved_at: string | null;
    resolution_notes: string | null;
    created_at: string;
}

export interface AuditLog {
    id: string;
    organization_id: string;
    user_id: string | null;
    action: string;
    entity_type: string | null;
    entity_id: string | null;
    changes: { before?: Record<string, unknown>; after?: Record<string, unknown> } | null;
    ip_address: string | null;
    created_at: string;
}

// ============================================================
// ANALYTICS TYPES
// ============================================================

export interface SalesSummary {
    total_sales: number;
    total_revenue: number;
    average_sale: number;
    total_discount: number;
    total_tax: number;
    start_date: string;
    end_date: string;
}

export interface LowStockAlert {
    drug_id: string;
    drug_name: string;
    current_quantity: number;
    reorder_level: number;
    reorder_quantity: number;
    branch_id: string;
    branch_name: string;
}

export interface ExpiringBatch {
    batch_id: string;
    drug_id: string;
    drug_name: string;
    batch_number: string;
    remaining_quantity: number;
    expiry_date: string;
    days_until_expiry: number;
    branch_id: string;
}

// ============================================================
// SYNC WIRE TYPES
//
// These are the exact shapes sent over the network between the
// client and the FastAPI sync endpoints.  They live here (not in
// syncEngine.ts) so any module can import them without pulling in
// the engine singleton.
// ============================================================

export interface PullRequest {
    branch_id: string;
    last_sync_at?: string | null;
    tables?: string[];
}

export interface PullResponse {
    drugs: Drug[];
    drug_categories: DrugCategory[];
    price_contracts: PriceContract[];
    customers: Customer[];
    prescriptions: Prescription[];
    branch_inventory: BranchInventory[];
    drug_batches: DrugBatch[];
    sales: Sale[];
    purchase_orders: PurchaseOrder[];
    sync_timestamp: string;
    has_more: boolean;
    total_records: number;
}

export interface PushRecord {
    table_name: string;
    local_id: string;
    operation: "create" | "update" | "delete";
    sync_version: number;
    data: Record<string, unknown>;
    created_offline_at: string;
}

export interface PushRequest {
    branch_id: string;
    records: PushRecord[];
}

export interface PushResult {
    local_id: string;
    table_name: string;
    server_id?: string;
    success: boolean;
    error?: string;
}

export interface PushConflict {
    local_id: string;
    table_name: string;
    local_version: number;
    server_version: number;
    server_record: Record<string, unknown>;
    resolution: "server_wins" | "local_wins" | "manual_required";
}

export interface PushResponse {
    accepted: PushResult[];
    conflicts: PushConflict[];
    failed: PushResult[];
    total_received: number;
    total_accepted: number;
    total_conflicts: number;
    total_failed: number;
    sync_timestamp: string;
    next_pull_timestamp: string;
}

export type SyncStatus = "idle" | "syncing" | "error" | "offline";

// ============================================================
// LEGACY SYNC OPERATION TYPES
// (used for local queue tracking, not wire protocol)
// ============================================================

export interface SyncOperation {
    operation: "create" | "update" | "delete";
    table_name: string;
    record_id: string;
    data: Record<string, unknown>;
    timestamp: string;
}

export interface SyncConflict {
    table_name: string;
    record_id: string;
    local_version: number;
    server_version: number;
    local_data: Record<string, unknown>;
    server_data: Record<string, unknown>;
    resolution: "server_wins" | "local_wins" | "merged" | "manual_required" | null;
}
