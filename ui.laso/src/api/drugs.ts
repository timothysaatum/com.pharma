import { get, post, patch, del } from "./client";
import type {
    Drug,
    DrugCreate,
    DrugUpdate,
    DrugWithInventory,
    DrugCategory,
    DrugCategoryCreate,
    DrugCategoryUpdate,
    DrugCategoryTree,
    DrugSearchFilters,
    BulkDrugUpdate,
    BulkDrugUpdateResult,
    PaginatedResponse,
} from "@/types";

const BASE = "/drugs";

export const drugApi = {
    // ── Drug CRUD ─────────────────────────────────────────────────────────────

    /**
     * GET /drugs
     * Paginated, filterable drug list.
     * Pass an AbortSignal to cancel on component unmount.
     */
    list(
        params: DrugSearchFilters & { page?: number; page_size?: number } = {},
        signal?: AbortSignal
    ): Promise<PaginatedResponse<Drug>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<PaginatedResponse<Drug>>(`${BASE}?${qs}`, { signal });
    },

    /** GET /drugs/{id} */
    getById(id: string, signal?: AbortSignal): Promise<Drug> {
        return get<Drug>(`${BASE}/${id}`, { signal });
    },

    /**
     * GET /drugs/{id}/with-inventory
     * Returns the drug with aggregated inventory summary across branches.
     * Optionally scoped to a specific branch via `branch_id` query param.
     */
    getWithInventory(
        id: string,
        branchId?: string,
        signal?: AbortSignal
    ): Promise<DrugWithInventory> {
        const qs = branchId ? `?branch_id=${branchId}` : "";
        return get<DrugWithInventory>(`${BASE}/${id}/with-inventory${qs}`, { signal });
    },

    /** POST /drugs */
    create(data: DrugCreate): Promise<Drug> {
        return post<Drug>(BASE, data);
    },

    /** PATCH /drugs/{id} */
    update(id: string, data: DrugUpdate): Promise<Drug> {
        return patch<Drug>(`${BASE}/${id}`, data);
    },

    /** DELETE /drugs/{id}?hard_delete=false */
    remove(id: string, hardDelete = false): Promise<void> {
        return del<void>(`${BASE}/${id}?hard_delete=${hardDelete}`);
    },

    /**
     * POST /drugs/bulk-update
     * Updates all listed drugs or reports failures.
     * Returns { successful, failed, total, message } per the router.
     */
    bulkUpdate(data: BulkDrugUpdate): Promise<BulkDrugUpdateResult> {
        return post<BulkDrugUpdateResult>(`${BASE}/bulk-update`, data);
    },

    /**
     * POST /drugs/search
     * Advanced search via POST body for complex filter combinations.
     * Use `list()` for simple query-parameter searches.
     */
    searchAdvanced(
        filters: DrugSearchFilters,
        params: { page?: number; page_size?: number } = {}    ): Promise<PaginatedResponse<Drug>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null) qs.set(k, String(v));
        });
        const url = qs.toString() ? `${BASE}/search?${qs}` : `${BASE}/search`;
        return post<PaginatedResponse<Drug>>(url, filters);
    },

    // ── Categories ────────────────────────────────────────────────────────────

    /**
     * GET /drugs/categories
     * Returns a flat list of DrugCategory objects.
     * Pass `parentId` to get direct children of a specific parent
     * (null / omitted = root categories).
     *
     * NOTE: this endpoint returns a flat list, not a nested tree.
     * Use `listCategoriesTree()` for the nested structure.
     */
    listCategories(parentId?: string, signal?: AbortSignal): Promise<DrugCategory[]> {
        const qs = parentId ? `?parent_id=${parentId}` : "";
        return get<DrugCategory[]>(`${BASE}/categories${qs}`, { signal });
    },

    /**
     * GET /drugs/categories/tree
     * Returns a hierarchical tree of DrugCategoryTree nodes, each
     * with a nested `children` array.  Use for category picker UIs.
     */
    listCategoriesTree(signal?: AbortSignal): Promise<DrugCategoryTree[]> {
        return get<DrugCategoryTree[]>(`${BASE}/categories/tree`, { signal });
    },

    /** POST /drugs/categories */
    createCategory(data: DrugCategoryCreate): Promise<DrugCategory> {
        return post<DrugCategory>(`${BASE}/categories`, data);
    },

    /** PATCH /drugs/categories/{id} */
    updateCategory(id: string, data: DrugCategoryUpdate): Promise<DrugCategory> {
        return patch<DrugCategory>(`${BASE}/categories/${id}`, data);
    },
};