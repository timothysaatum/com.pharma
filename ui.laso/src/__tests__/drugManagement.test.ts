import { describe, it, expect, beforeEach, vi } from "vitest";
import { drugApi } from "@/api/drugs";
import type { DrugCategory, Drug, DrugCreate } from "@/types";

const { mockGet, mockPost, mockPatch, mockDel } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockDel: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  get: mockGet,
  post: mockPost,
  patch: mockPatch,
  del: mockDel,
}));

describe("Drug Management API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("Drug CRUD Operations", () => {
    const mockDrug: Drug = {
      id: "drug-001",
      name: "Paracetamol",
      generic_name: "Paracetamol",
      brand_name: "Tylenol",
      dosage_form: "tablet",
      strength: "500mg",
      manufacturer: "Pharma Co",
      supplier: "Supplier Inc",
      sku: "PARA-500-TAB",
      barcode: "123456789",
      category_id: "cat-001",
      drug_type: "otc",
      requires_prescription: false,
      unit_price: 50,
      cost_price: 30,
      markup_percentage: 66.67,
      tax_rate: 0.16,
      reorder_level: 100,
      reorder_quantity: 500,
      max_stock_level: 5000,
      unit_of_measure: "tablet",
      is_active: true,
      organization_id: "org-123",
      profit_margin: 40,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-05-26T00:00:00Z",
      sync_status: "synced",
      sync_version: 1,
      synced_at: "2026-05-26T00:00:00Z",
      is_deleted: false,
      deleted_at: null,
      deleted_by: null,
      ndc_code: null,
      controlled_substance_schedule: null,
      description: null,
      usage_instructions: null,
      side_effects: null,
      contraindications: null,
      storage_conditions: null,
      image_url: null,
    };

    it("should list drugs with pagination", async () => {
      const mockResponse = {
        items: [mockDrug],
        total: 1,
        page: 1,
        page_size: 50,
        total_pages: 1,
        has_next: false,
        has_prev: false,
      };

      mockGet.mockResolvedValueOnce(mockResponse);

      const result = await drugApi.list({ page: 1 });

      expect(mockGet).toHaveBeenCalled();
      expect(result.items).toHaveLength(1);
      expect(result.items[0].name).toBe("Paracetamol");
    });

    it("should get drug by ID", async () => {
      mockGet.mockResolvedValueOnce(mockDrug);

      const result = await drugApi.getById("drug-001");

      expect(mockGet).toHaveBeenCalled();
      expect(result.id).toBe("drug-001");
      expect(result.unit_price).toBe(50);
    });

    it("should create a drug", async () => {
      const createData: DrugCreate = {
        name: "Aspirin",
        generic_name: "Aspirin",
        brand_name: "Bufferin",
        dosage_form: "tablet",
        strength: "100mg",
        manufacturer: "Pharma Co",
        unit_price: 30,
        tax_rate: 0.16,
        reorder_level: 100,
        reorder_quantity: 500,
        unit_of_measure: "tablet",
        requires_prescription: false,
        drug_type: "otc",
        is_active: true,
        organization_id: "org-123",
      };

      const newDrug: Drug = { ...mockDrug, ...createData, id: "drug-002" };

      mockPost.mockResolvedValueOnce(newDrug);

      const result = await drugApi.create(createData);

      expect(mockPost).toHaveBeenCalled();
      expect(result.name).toBe("Aspirin");
    });

    it("should update a drug", async () => {
      const updateData = { unit_price: 55 };
      const updatedDrug = { ...mockDrug, ...updateData };

      mockPatch.mockResolvedValueOnce(updatedDrug);

      const result = await drugApi.update("drug-001", updateData);

      expect(mockPatch).toHaveBeenCalled();
      expect(result.unit_price).toBe(55);
    });

    it("should delete a drug", async () => {
      mockDel.mockResolvedValueOnce(undefined);

      await drugApi.remove("drug-001");

      expect(mockDel).toHaveBeenCalled();
    });

    it("should hard delete a drug", async () => {
      mockDel.mockResolvedValueOnce(undefined);

      await drugApi.remove("drug-001", true);

      expect(mockDel).toHaveBeenCalled();
    });
  });

  describe("Drug Category Management", () => {
    const mockCategory: DrugCategory = {
      id: "cat-001",
      name: "Antibiotics",
      description: "Antibiotic medications",
      parent_id: null,
      organization_id: "org-123",
      path: "/Antibiotics",
      level: 0,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-05-26T00:00:00Z",
      sync_status: "synced",
      sync_version: 1,
      synced_at: "2026-05-26T00:00:00Z",
      is_deleted: false,
      deleted_at: null,
      deleted_by: null,
    };

    it("should list drug categories", async () => {
      const mockResponse = [mockCategory];

      mockGet.mockResolvedValueOnce(mockResponse);

      const result = await drugApi.listCategories();

      expect(mockGet).toHaveBeenCalled();
      expect(result).toHaveLength(1);
      expect(result[0].name).toBe("Antibiotics");
    });

    it("should get category tree", async () => {
      const mockTree: any[] = [
        {
          ...mockCategory,
          children: [
            {
              ...mockCategory,
              id: "cat-002",
              name: "Penicillins",
              parent_id: "cat-001",
              children: [],
            },
          ],
        },
      ];

      mockGet.mockResolvedValueOnce(mockTree);

      const result = await drugApi.listCategoriesTree();

      expect(mockGet).toHaveBeenCalled();
      expect(result).toHaveLength(1);
      expect(result[0].children).toHaveLength(1);
    });

    it("should create a drug category", async () => {
      const createData = {
        name: "Pain Relievers",
        description: "Pain relief medications",
        organization_id: "org-123",
      };

      const newCategory: DrugCategory = {
        ...mockCategory,
        ...createData,
        id: "cat-002",
      };

      mockPost.mockResolvedValueOnce(newCategory);

      const result = await drugApi.createCategory(createData);

      expect(mockPost).toHaveBeenCalled();
      expect(result.name).toBe("Pain Relievers");
    });

    it("should update a drug category", async () => {
      const updateData = { description: "Updated antibiotics" };
      const updatedCategory = { ...mockCategory, ...updateData };

      mockPatch.mockResolvedValueOnce(updatedCategory);

      const result = await drugApi.updateCategory("cat-001", updateData);

      expect(mockPatch).toHaveBeenCalled();
      expect(result.description).toBe("Updated antibiotics");
    });

    it("should delete a drug category", async () => {
      mockDel.mockResolvedValueOnce(undefined);

      await drugApi.removeCategory("cat-001");

      expect(mockDel).toHaveBeenCalled();
    });
  });

  describe("Drug Search and Bulk Operations", () => {
    it("should perform advanced search", async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        total_pages: 0,
        has_next: false,
        has_prev: false,
      };

      mockPost.mockResolvedValueOnce(mockResponse);

      const result = await drugApi.searchAdvanced({
        manufacturer: "Pharma Co",
      });

      expect(mockPost).toHaveBeenCalled();
      expect(result.total).toBe(0);
    });
  });

  describe("Error Handling", () => {
    it("should handle API errors gracefully", async () => {
      const error = new Error("API Error");
      mockGet.mockRejectedValueOnce(error);

      await expect(drugApi.list()).rejects.toThrow("API Error");
    });

    it("should handle validation errors on create", async () => {
      const error = new Error("SKU must be unique");
      mockPost.mockRejectedValueOnce(error);

      const createData: DrugCreate = {
        name: "Duplicate Drug",
        generic_name: "Duplicate",
        drug_type: "otc",
        requires_prescription: false,
        unit_price: 50,
        tax_rate: 0.16,
        reorder_level: 100,
        reorder_quantity: 500,
        unit_of_measure: "tablet",
        is_active: true,
        organization_id: "org-123",
      };

      await expect(drugApi.create(createData)).rejects.toThrow("SKU must be unique");
    });
  });
});
