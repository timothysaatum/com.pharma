import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import { useAuthStore } from "@/stores/authStore";
import { syncEngine } from "@/lib/syncEngine";
import { AppShell } from "@/components/layout/AppShell";
import { UpdateManager } from "@/components/layout/UpdateManager";
import OnboardingPage from "@/pages/OnboardingPage";
import LoginPage from "@/pages/LoginPage";
import SetupRequiredPage from "@/pages/SetupRequiredPage";
import POSPage from "@/pages/POSPage";
import CustomersPage from "@/pages/CustomersPage";
import SalesHistoryPage from "@/pages/SalesHistoryPage";
import PrescriptionsPage from "@/pages/PrescriptionsPage";
import SettingsPage from "@/pages/SettingsPage";
import UsersPage from "@/pages/UsersPage";
import ReportsPage from "@/pages/ReportsPage";
import AdminPage from "@/pages/AdminPage";
import AuditLogPage from "@/pages/AuditLogPage";
import { getHomePath } from "@/lib/routes";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 1000 * 60 * 5, retry: 1 },
  },
});

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
 *
 * Allows: super admins, and any user with role level >= 20 (admin/manager).
 * The login endpoint doesn't compute max_role_level (defaults to 0),
 * so we fall back to the max level from the user's assigned roles.
 */
function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { user } = useAuthStore();
  if (!user) return <Navigate to="/login" replace />;

  // Super admin always has access
  if (user.is_super_admin) return <>{children}</>;

  // Check role level — login endpoint doesn't compute this, so
  // effective_permissions.max_role_level will be 0 (falsy); fall back to roles.
  const effectiveLevel = user.effective_permissions?.max_role_level;
  const maxLevel = effectiveLevel || Math.max(...user.roles.map(r => r.level), 0);

  if (maxLevel >= 20) return <>{children}</>;

  return <Navigate to={getHomePath(user)} replace />;
}

/**
 * Guards routes that require manager-level or higher access.
 */
function RequireManager({ children }: { children: React.ReactNode }) {
  const { user } = useAuthStore();
  // Basically all authenticated users for now, as specific pages handle granular permissions
  if (!user) {
    return <Navigate to="/login" replace />;
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
  const { isAuthenticated, setupState, user } = useAuthStore();

  if (isAuthenticated && setupState === "ready") {
    return <Navigate to={getHomePath(user)} replace />;
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
  const { isAuthenticated, setupState, user } = useAuthStore();

  // Fully authenticated + all set up — nothing to do here
  if (isAuthenticated && setupState === "ready") {
    return <Navigate to={getHomePath(user)} replace />;
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
  const { isAuthenticated, setupState, isLoading, initialize, user } = useAuthStore();

  useEffect(() => {
    initialize();
    const handler = () => { window.location.href = "/login"; };
    window.addEventListener("auth:logout", handler);

    // Sync when coming back online
    const onlineHandler = () => {
      console.log("App: Network is back online, triggering sync...");
      void syncEngine.sync();
    };
    window.addEventListener("online", onlineHandler);

    return () => {
      window.removeEventListener("auth:logout", handler);
      window.removeEventListener("online", onlineHandler);
    };
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
          path="/pos"
          element={<RequireAuth><AppShell><POSPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/customers"
          element={<RequireAuth><AppShell><CustomersPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/sales"
          element={<RequireAuth><AppShell><SalesHistoryPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/prescriptions"
          element={<RequireAuth><AppShell><PrescriptionsPage /></AppShell></RequireAuth>}
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
          path="/audit-logs"
          element={
            <RequireAuth>
              <RequireManager>
                <AppShell><AuditLogPage /></AppShell>
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
          element={<RequireAuth><AppShell><ReportsPage /></AppShell></RequireAuth>}
        />
        <Route
          path="/admin"
          element={
            <RequireAuth>
              <RequireManager>
                <AppShell><AdminPage /></AppShell>
              </RequireManager>
            </RequireAuth>
          }
        />
        <Route
          path="/admin/:tab"
          element={
            <RequireAuth>
              <RequireManager>
                <AppShell><AdminPage /></AppShell>
              </RequireManager>
            </RequireAuth>
          }
        />

        <Route path="/drugs" element={<Navigate to="/admin/drugs" replace />} />
        <Route path="/inventory" element={<Navigate to="/admin/inventory" replace />} />
        <Route path="/purchases" element={<Navigate to="/admin/purchases" replace />} />
        <Route path="/contracts" element={<Navigate to="/admin/contracts" replace />} />
        <Route path="/admin/prescriptions" element={<Navigate to="/prescriptions" replace />} />
        <Route path="/organization-stats" element={<Navigate to="/settings/organization" replace />} />
        <Route path="/branches" element={<Navigate to="/settings/branches" replace />} />
        <Route path="/drug-management" element={<Navigate to="/admin/drugs" replace />} />

        {/* ── Redirects ── */}
        <Route path="/dashboard" element={<Navigate to={getHomePath(user)} replace />} />
        <Route
          path="/"
          element={<Navigate to={isReady ? getHomePath(user) : "/login"} replace />}
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
        <UpdateManager />
        <Toaster position="top-right" richColors closeButton />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
