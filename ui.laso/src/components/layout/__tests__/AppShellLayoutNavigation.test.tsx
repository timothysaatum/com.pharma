/** @vitest-environment jsdom */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { AppShell } from "../AppShell";
import { useAuthStore } from "@/stores/authStore";

// Mock dependencies
vi.mock("@/stores/authStore");
vi.mock("@/hooks/useSyncStatus", () => ({
  useSyncStatus: () => ({
    status: "idle",
    isOffline: false,
    lastSyncAt: new Date().toISOString(),
    conflicts: [],
    failures: [],
    pendingCount: 0,
  }),
}));
vi.mock("@/api/branches", () => ({
  branchApi: {
    list: vi.fn().mockResolvedValue({ items: [{ id: "b-1", name: "Main Branch", code: "MB01" }] }),
    listMine: vi.fn().mockResolvedValue([{ id: "b-1", name: "Main Branch", code: "MB01" }]),
    getById: vi.fn().mockResolvedValue({ id: "b-1", name: "Main Branch", code: "MB01" }),
  },
}));
vi.mock("@/api/organization", () => ({
  organizationApi: {
    getById: vi.fn().mockResolvedValue({ id: "org-1", name: "Test Pharmacy" }),
  },
}));

describe("AppShell Layout Route Client-Side Navigation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const mockState = {
      user: {
        id: "usr-1",
        full_name: "Test Admin",
        is_super_admin: true,
        organization_id: "org-1",
        assigned_branches: ["b-1"],
        roles: [{ name: "admin", level: 30, permissions: ["*"] }],
      },
      logout: vi.fn(),
      activeBranchId: "b-1",
      setActiveBranch: vi.fn(),
    };
    (useAuthStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector?: (state: typeof mockState) => any) => {
      return selector ? selector(mockState) : mockState;
    });
  });

  it("renders all sidebar navigation links without raw href reloads", () => {
    render(
      <MemoryRouter initialEntries={["/pos"]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/pos" element={<div data-testid="pos-page">POS Content</div>} />
            <Route path="/sales" element={<div data-testid="sales-page">Sales Content</div>} />
            <Route path="/customers" element={<div data-testid="customers-page">Customers Content</div>} />
            <Route path="/prescriptions" element={<div data-testid="prescriptions-page">Prescriptions Content</div>} />
            <Route path="/reports" element={<div data-testid="reports-page">Reports Content</div>} />
            <Route path="/admin" element={<div data-testid="admin-page">Admin Content</div>} />
            <Route path="/users" element={<div data-testid="users-page">Users Content</div>} />
            <Route path="/settings" element={<div data-testid="settings-page">Settings Content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    );

    // Verify initially mounted POS page
    expect(screen.getByTestId("pos-page")).toBeTruthy();

    // Verify all sidebar links are present
    const links = screen.getAllByRole("link");
    expect(links.length).toBeGreaterThan(0);
    
    // Verify none of the links have Javascript or raw page reload protocols
    links.forEach((link) => {
      const href = link.getAttribute("href");
      expect(href).not.toContain("javascript:");
      expect(href).toMatch(/^\//);
    });
  });

  it("navigates client-side between all sidebar items while keeping AppShell mounted", async () => {
    render(
      <MemoryRouter initialEntries={["/pos"]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/pos" element={<div data-testid="page-pos">POS View</div>} />
            <Route path="/sales" element={<div data-testid="page-sales">Sales History View</div>} />
            <Route path="/customers" element={<div data-testid="page-customers">Customers View</div>} />
            <Route path="/prescriptions" element={<div data-testid="page-prescriptions">Prescriptions View</div>} />
            <Route path="/reports" element={<div data-testid="page-reports">Reports View</div>} />
            <Route path="/admin" element={<div data-testid="page-admin">Admin View</div>} />
            <Route path="/users" element={<div data-testid="page-users">Users View</div>} />
            <Route path="/settings" element={<div data-testid="page-settings">Settings View</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    );

    // Initial state: POS page active
    expect(screen.getByTestId("page-pos")).toBeTruthy();

    // Click Sales History link
    fireEvent.click(screen.getByText("Sales History"));
    expect(screen.getByTestId("page-sales")).toBeTruthy();
    expect(screen.queryByTestId("page-pos")).toBeNull();

    // Click Customers link
    fireEvent.click(screen.getByText("Customers"));
    expect(screen.getByTestId("page-customers")).toBeTruthy();

    // Click Prescriptions link
    fireEvent.click(screen.getByText("Prescriptions"));
    expect(screen.getByTestId("page-prescriptions")).toBeTruthy();

    // Click Reports link
    fireEvent.click(screen.getByText("Reports"));
    expect(screen.getByTestId("page-reports")).toBeTruthy();

    // Click Admin link
    fireEvent.click(screen.getByText("Admin"));
    expect(screen.getByTestId("page-admin")).toBeTruthy();

    // Click Users link
    fireEvent.click(screen.getByText("Users"));
    expect(screen.getByTestId("page-users")).toBeTruthy();

    // Click Settings link
    fireEvent.click(screen.getByText("Settings"));
    expect(screen.getByTestId("page-settings")).toBeTruthy();
  });

  it("preserves active branch name across renders without flickering to Loading", async () => {
    localStorage.setItem("cache.branch_name.b-1", "Main Branch");

    render(
      <MemoryRouter initialEntries={["/pos"]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/pos" element={<div data-testid="page-pos">POS View</div>} />
            <Route path="/audit-logs" element={<div data-testid="page-audit">Audit Logs View</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    );

    // Initial state: Branch name is immediately visible
    expect(screen.getByText("Main Branch")).toBeTruthy();
    expect(screen.queryByText("Loading…")).toBeNull();

    // Navigate to Audit Logs
    fireEvent.click(screen.getByText("Audit Logs"));
    expect(screen.getByTestId("page-audit")).toBeTruthy();

    // Branch name remains steady without reset
    expect(screen.getByText("Main Branch")).toBeTruthy();
    expect(screen.queryByText("Loading…")).toBeNull();
  });
});
