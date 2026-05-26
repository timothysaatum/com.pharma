import { describe, it, expect, beforeEach, vi } from "vitest";
import { branchApi } from "@/api/branches";
import type { Branch, BranchCreate, BranchAddress } from "@/types";

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

describe("Branch Management API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("Branch CRUD Operations", () => {
    const mockAddress: BranchAddress = {
      street: "123 Main St",
      city: "Nairobi",
      state: "Nairobi",
      zip_code: "00100",
      country: "Kenya",
    };

    const mockBranch: Branch = {
      id: "br-001",
      code: "MAIN",
      name: "Main Branch",
      phone: "+254700000000",
      email: "main@pharmacy.com",
      address: mockAddress as BranchAddress,
      is_active: true,
      organization_id: "org-123",
      manager_id: "user-456",
      manager_name: "John Manager",
      operating_hours: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-05-26T00:00:00Z",
      sync_status: "synced",
      sync_version: 1,
      synced_at: "2026-05-26T00:00:00Z",
    };

    it("should list all branches", async () => {
      const mockResponse = {
        items: [mockBranch],
        total: 1,
        page: 1,
        page_size: 50,
        total_pages: 1,
        has_next: false,
        has_prev: false,
      };

      mockGet.mockResolvedValueOnce(mockResponse);

      const result = await branchApi.list();

      expect(mockGet).toHaveBeenCalled();
      expect(result.items).toHaveLength(1);
      expect(result.items[0].code).toBe("MAIN");
    });

    it("should get user's assigned branches", async () => {
      const mockResponse = [mockBranch];

      mockGet.mockResolvedValueOnce(mockResponse);

      const result = await branchApi.listMine();

      expect(mockGet).toHaveBeenCalled();
      expect(result).toHaveLength(1);
    });

    it("should get branch by ID", async () => {
      mockGet.mockResolvedValueOnce(mockBranch);

      const result = await branchApi.getById("br-001");

      expect(mockGet).toHaveBeenCalled();
      expect(result.id).toBe("br-001");
      expect(result.code).toBe("MAIN");
    });

    it("should get branch by code", async () => {
      mockGet.mockResolvedValueOnce(mockBranch);

      const result = await branchApi.getByCode("MAIN");

      expect(mockGet).toHaveBeenCalled();
      expect(result.code).toBe("MAIN");
    });

    it("should create a new branch", async () => {
      const createData: BranchCreate = {
        code: "BR-002",
        name: "Secondary Branch",
        organization_id: "org-123",
        address: { street: "456 Branch Ave", city: "Mombasa", state: "Mombasa", zip_code: "80100", country: "Kenya" },
      };

      const newBranch: Branch = { 
        ...mockBranch, 
        id: "br-002",
        code: "BR-002",
        name: "Secondary Branch",
        address: createData.address ?? null,
      };

      mockPost.mockResolvedValueOnce(newBranch);

      const result = await branchApi.create(createData);

      expect(mockPost).toHaveBeenCalled();
      expect(result.code).toBe("BR-002");
      expect(result.name).toBe("Secondary Branch");
    });

    it("should update a branch", async () => {
      const updateData = { name: "Updated Main Branch" };
      const updatedBranch = { ...mockBranch, ...updateData };

      mockPatch.mockResolvedValueOnce(updatedBranch);

      const result = await branchApi.update("br-001", updateData);

      expect(mockPatch).toHaveBeenCalled();
      expect(result.name).toBe("Updated Main Branch");
    });

    it("should delete a branch (soft delete)", async () => {
      mockDel.mockResolvedValueOnce(undefined);

      await branchApi.remove("br-001", false);

      expect(mockDel).toHaveBeenCalled();
    });

    it("should hard delete a branch", async () => {
      mockDel.mockResolvedValueOnce(undefined);

      await branchApi.remove("br-001", true);

      expect(mockDel).toHaveBeenCalled();
    });
  });

  describe("Branch Status Management", () => {
    const mockBranch: Branch = {
      id: "br-001",
      code: "MAIN",
      name: "Main Branch",
      is_active: true,
      organization_id: "org-123",
      phone: null,
      email: null,
      address: null,
      manager_id: null,
      manager_name: null,
      operating_hours: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-05-26T00:00:00Z",
      sync_status: "synced",
      sync_version: 1,
      synced_at: "2026-05-26T00:00:00Z",
    };

    it("should activate a deactivated branch", async () => {
      const activeBranch = { ...mockBranch, is_active: true };

      mockPost.mockResolvedValueOnce(activeBranch);

      const result = await branchApi.activate("br-001");

      expect(mockPost).toHaveBeenCalled();
      expect(result.is_active).toBe(true);
    });

    it("should deactivate an active branch", async () => {
      const inactiveBranch = { ...mockBranch, is_active: false };

      mockPost.mockResolvedValueOnce(inactiveBranch);

      const result = await branchApi.deactivate("br-001");

      expect(mockPost).toHaveBeenCalled();
      expect(result.is_active).toBe(false);
    });
  });

  describe("Branch User Management", () => {
    it("should get users assigned to a branch", async () => {
      const mockUsers = [
        { id: "user-1", username: "john", full_name: "John Doe", email: "john@example.com", role: "manager" as const },
        { id: "user-2", username: "jane", full_name: "Jane Smith", email: "jane@example.com", role: "pharmacist" as const },
      ];

      mockGet.mockResolvedValueOnce(mockUsers);

      const result = await branchApi.getUsers("br-001");

      expect(mockGet).toHaveBeenCalled();
      expect(result).toHaveLength(2);
    });

    it("should assign users to branches", async () => {
      const assignmentResult = {
        success: true,
        message: "User assigned to 2 branches",
        user_id: "user-1",
        assigned_branches: ["br-001", "br-002"],
      };

      mockPost.mockResolvedValueOnce(assignmentResult);

      const result = await branchApi.assignUser("user-1", ["br-001", "br-002"]);

      expect(mockPost).toHaveBeenCalled();
      expect(result.assigned_branches).toHaveLength(2);
    });
  });

  describe("Branch Search", () => {
    it("should search branches with advanced filters", async () => {
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

      const result = await branchApi.searchAdvanced({
        search: "Nairobi",
        is_active: true,
      });

      expect(mockPost).toHaveBeenCalled();
      expect(result.total).toBe(0);
    });
  });

  describe("Error Handling", () => {
    it("should handle API errors gracefully", async () => {
      const error = new Error("API Error");
      mockGet.mockRejectedValueOnce(error);

      await expect(branchApi.list()).rejects.toThrow("API Error");
    });

    it("should handle validation errors", async () => {
      const error = new Error("Code must be unique");
      mockPost.mockRejectedValueOnce(error);

      const createData: BranchCreate = {
        code: "MAIN",
        name: "Branch",
        organization_id: "org-123",
      };

      await expect(branchApi.create(createData)).rejects.toThrow("Code must be unique");
    });
  });
});

