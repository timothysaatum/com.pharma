/** @vitest-environment jsdom */
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

async function loadUseCategories(overrides: { backendReachable?: boolean; browserOnline?: boolean } = {}) {
  vi.resetModules();
  const backendReachable = overrides.backendReachable ?? false;
  const browserOnline = overrides.browserOnline ?? true;
  Object.defineProperty(navigator, "onLine", {
    configurable: true,
    value: browserOnline,
  });

  const listCategories = vi.fn().mockResolvedValue([]);
  const listCategoriesTree = vi.fn().mockResolvedValue([]);
  const getDrugCategories = vi.fn().mockResolvedValue([
    {
      id: "cat-1",
      organization_id: "org-abc",
      name: "Antibiotics",
      description: null,
      parent_id: null,
      path: "/",
      level: 0,
      is_deleted: false,
      sync_status: "synced",
      sync_version: 1,
      synced_at: null,
      updated_at: "2026-07-15T00:00:00Z",
      created_at: "2026-07-15T00:00:00Z",
    },
  ]);
  const getDrugCategoryTree = vi.fn().mockResolvedValue([
    {
      id: "cat-1",
      organization_id: "org-abc",
      name: "Antibiotics",
      description: null,
      parent_id: null,
      path: "/",
      level: 0,
      is_deleted: false,
      sync_status: "synced",
      sync_version: 1,
      synced_at: null,
      updated_at: "2026-07-15T00:00:00Z",
      created_at: "2026-07-15T00:00:00Z",
      children: [],
    },
  ]);

  vi.doMock("@/api/drugs", () => ({
    drugApi: { listCategories, listCategoriesTree },
  }));
  vi.doMock("@/api/client", () => ({
    isBackendReachable: () => backendReachable,
    isOfflineError: () => true,
    parseApiError: (err: unknown) => err instanceof Error ? err.message : String(err),
  }));
  vi.doMock("@/lib/localRead", () => ({
    localRead: { getDrugCategories, getDrugCategoryTree },
  }));
  vi.doMock("@/stores/authStore", () => ({
    useAuthStore: (selector: (state: unknown) => unknown) =>
      selector({ user: { organization_id: "org-abc" } }),
  }));

  return {
    ...(await import("@/hooks/useCategories")),
    mocks: {
      listCategories,
      listCategoriesTree,
      getDrugCategories,
      getDrugCategoryTree,
    },
  };
}

describe("useCategories offline loading", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("loads flat categories from SQLite when backend is unreachable", async () => {
    const { useCategories, mocks } = await loadUseCategories();

    const { result } = renderHook(() => useCategories());

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.categories.map((category) => category.name)).toEqual(["Antibiotics"]);
    expect(mocks.getDrugCategories).toHaveBeenCalledWith(undefined, "org-abc");
    expect(mocks.listCategories).not.toHaveBeenCalled();
  });

  it("keeps flat categories from SQLite when the online API returns an empty list", async () => {
    const { useCategories, mocks } = await loadUseCategories({ backendReachable: true });

    const { result } = renderHook(() => useCategories());

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.categories.map((category) => category.name)).toEqual(["Antibiotics"]);
    expect(mocks.listCategories).toHaveBeenCalled();
    expect(mocks.getDrugCategories).toHaveBeenCalledWith(undefined, "org-abc");
  });

  it("loads category tree from SQLite when backend is unreachable", async () => {
    const { useCategoryTree, mocks } = await loadUseCategories();

    const { result } = renderHook(() => useCategoryTree());

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.tree.map((category) => category.name)).toEqual(["Antibiotics"]);
    expect(mocks.getDrugCategoryTree).toHaveBeenCalledWith("org-abc");
    expect(mocks.listCategoriesTree).not.toHaveBeenCalled();
  });

  it("keeps category tree from SQLite when the online API returns an empty list", async () => {
    const { useCategoryTree, mocks } = await loadUseCategories({ backendReachable: true });

    const { result } = renderHook(() => useCategoryTree());

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.tree.map((category) => category.name)).toEqual(["Antibiotics"]);
    expect(mocks.listCategoriesTree).toHaveBeenCalled();
    expect(mocks.getDrugCategoryTree).toHaveBeenCalledWith("org-abc");
  });
});
