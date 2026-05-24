import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import { useAuthStore } from "@/stores/authStore";
import { syncEngine } from "@/lib/syncEngine";
import { AppShell } from "@/components/layout/AppShell";
import OnboardingPage from "@/pages/OnboardingPage";
import LoginPage from "@/pages/LoginPage";
import SetupRequiredPage from "@/pages/SetupRequiredPage";
import DrugListPage from "@/pages/DrugListPage";
import InventoryPage from "@/pages/InventoryPage";
import POSPage from "@/pages/POSPage";
import ContractsPage from "@/pages/ContractsPage";
import CustomersPage from "@/pages/CustomersPage";
import PurchasesPage from "@/pages/PurchasesPage";
import SalesHistoryPage from "@/pages/SalesHistoryPage";
import SettingsPage from "@/pages/SettingsPage";
import UsersPage from "@/pages/UsersPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 1000 * 60 * 5, retry: 1 },
  },
});

function ComingSoon({ label }: { label: string }) {
  return (
    <div className="flex-1 flex items-center justify-center text-ink-muted">
      <p className="text-sm">{label} coming soon</p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Route guards
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Protects app routes.
 *
 * Three outcomes:
 *  1. Not authenticated at all → /login
 *  2. Authenticated but setup incomplete → /setup
 *     (covers needs_branch AND needs_onboard — SetupRequiredPage handles both)
 *  3. Authenticated + ready → render children
 */
function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, setupState } = useAuthStore();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  if (setupState !== "ready") {
    return <Navigate to="/setup" replace />;
  }
  return <>{children}</>;
}

/**
 * Guards routes that require admin or super_admin role.
 * Assumes the user is already authenticated (used inside RequireAuth).
 */
function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { user } = useAuthStore();
  if (!user || !["admin", "super_admin"].includes(user.role)) {
    return <Navigate to="/drugs" replace />;
  }
  return <>{children}</>;
}

/**
 * Guards routes that require admin, super_admin, or manager role.
 * Managers have read access to users in their branch scope.
 */
function RequireManager({ children }: { children: React.ReactNode }) {
  const { user } = useAuthStore();
  if (!user || !["admin", "super_admin", "manager"].includes(user.role)) {
    return <Navigate to="/drugs" replace />;
  }
  return <>{children}</>;
}

/**
 * Guards /login so a fully-ready user doesn't land back on the login page.
 * Authenticated users who are NOT ready are allowed through to /login
 * only if they explicitly landed there — normally the auth flow routes them
 * to /setup automatically.  Keeping the redirect tight here prevents loops.
 */
function RequireUnauthenticated({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, setupState } = useAuthStore();

  if (isAuthenticated && setupState === "ready") {
    return <Navigate to="/drugs" replace />;
  }
  return <>{children}</>;
}

/**
 * Guards /onboarding so an already-ready user can't accidentally re-run setup.
 *
 * We intentionally allow needs_onboard (super_admin) and needs_branch (admin
 * who skipped) through — they legitimately need to be here.
 */
function RequireSetupAccess({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, setupState } = useAuthStore();

  // Fully authenticated + all set up — nothing to do here
  if (isAuthenticated && setupState === "ready") {
    return <Navigate to="/drugs" replace />;
  }
  return <>{children}</>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Sync gate — waits for the initial sync to settle before showing app content.
// Only active when the user is fully ready (has a branch).
// ─────────────────────────────────────────────────────────────────────────────

function SyncGate({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, activeBranchId } = useAuthStore();
  const [initialSyncDone, setInitialSyncDone] = useState(false);
  const hasPassedGate = useRef(false);

  useEffect(() => {
    if (!isAuthenticated || !activeBranchId) return;

    if (hasPassedGate.current) {
      setInitialSyncDone(true);
      return;
    }

    const currentStatus = syncEngine.status;
    if (
      currentStatus === "idle" ||
      currentStatus === "offline" ||
      currentStatus === "error"
    ) {
      hasPassedGate.current = true;
      setInitialSyncDone(true);
      return;
    }

    const unsub = syncEngine.subscribe((status) => {
      if (status === "idle" || status === "offline" || status === "error") {
        hasPassedGate.current = true;
        setInitialSyncDone(true);
      }
    });

    const safety = setTimeout(() => {
      hasPassedGate.current = true;
      setInitialSyncDone(true);
    }, 5_000);

    return () => {
      unsub();
      clearTimeout(safety);
    };
  }, [isAuthenticated, activeBranchId]);

  if (isAuthenticated && activeBranchId && !initialSyncDone) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full animate-spin" />
          <div className="text-center">
            <p className="text-sm font-medium text-ink">Syncing your data…</p>
            <p className="text-xs text-ink-muted mt-1">
              Fetching latest drugs, inventory and contracts
            </p>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Routes
// ─────────────────────────────────────────────────────────────────────────────

function AppRoutes() {
  const { isAuthenticated, setupState, isLoading, initialize } = useAuthStore();

  useEffect(() => {
    initialize();
    const handler = () => { window.location.href = "/login"; };
    window.addEventListener("auth:logout", handler);
    return () => window.removeEventListener("auth:logout", handler);
  }, [initialize]);

  if (isLoading) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-ink-muted">Loading…</p>
        </div>
      </div>
    );
  }

  const isReady = isAuthenticated && setupState === "ready";

  return (
    <SyncGate>
      <Routes>
        {/* ── Public / setup ── */}

        <Route
          path="/login"
          element={
            <RequireUnauthenticated>
              <LoginPage />
            </RequireUnauthenticated>
          }
        />

        {/*
         * /setup — shown when authenticated but setup is incomplete.
         * RequireSetupAccess blocks fully-ready users from entering.
         * SetupRequiredPage renders the correct panel based on setupState.
         */}
        <Route
          path="/setup"
          element={
            <RequireSetupAccess>
              <SetupRequiredPage />
            </RequireSetupAccess>
          }
        />

        {/*
         * /onboarding — super_admin org creation wizard.
         * Also uses RequireSetupAccess so a ready user can't accidentally
         * re-run onboarding, but a needs_onboard super_admin can proceed.
         */}
        <Route
          path="/onboarding"
          element={
            <RequireSetupAccess>
              <OnboardingPage />
            </RequireSetupAccess>
          }
        />

        {/* ── Protected app routes ── */}
        <Route
          path="/drugs"
          element={<RequireAuth><AppShell><DrugListPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/inventory"
          element={<RequireAuth><AppShell><InventoryPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/pos"
          element={<RequireAuth><AppShell><POSPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/customers"
          element={<RequireAuth><AppShell><CustomersPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/contracts"
          element={<RequireAuth><AppShell><ContractsPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/purchases"
          element={<RequireAuth><AppShell><PurchasesPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/sales"
          element={<RequireAuth><AppShell><SalesHistoryPage /></AppShell></RequireAuth>}
        />

        {/*
         * /users — user management.
         * Accessible to admin, super_admin, and manager.
         * The page itself enforces per-role data scoping (managers only see
         * users in their branches; only admins/super_admins can create/delete).
         */}
        <Route
          path="/users"
          element={
            <RequireAuth>
              <RequireManager>
                <AppShell><UsersPage /></AppShell>
              </RequireManager>
            </RequireAuth>
          }
        />

        <Route
          path="/settings"
          element={
            <RequireAuth>
              <RequireAdmin>
                <AppShell><SettingsPage /></AppShell>
              </RequireAdmin>
            </RequireAuth>
          }
        />
        <Route
          path="/settings/:tab"
          element={
            <RequireAuth>
              <RequireAdmin>
                <AppShell><SettingsPage /></AppShell>
              </RequireAdmin>
            </RequireAuth>
          }
        />
        <Route
          path="/reports"
          element={<RequireAuth><AppShell><ComingSoon label="Reports" /></AppShell></RequireAuth>}
        />

        {/* ── Redirects ── */}
        <Route path="/dashboard" element={<Navigate to="/drugs" replace />} />
        <Route
          path="/"
          element={<Navigate to={isReady ? "/drugs" : "/login"} replace />}
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </SyncGate>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
        <Toaster position="top-right" richColors closeButton />
      </BrowserRouter>
    </QueryClientProvider>
  );
}