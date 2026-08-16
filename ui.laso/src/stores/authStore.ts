import { create } from "zustand";
import type { UserResponse as User } from "@/types";
import { authStorage } from "@/lib/storage";
import { authApi } from "@/api/auth";
import { syncEngine } from "@/lib/syncEngine";

// ─────────────────────────────────────────────────────────────────────────────
// What post-login destination does this user need?
//
//  "ready"              — has org + branch → go straight to /drugs
//  "needs_branch"       — has org but zero branches → go to /setup (add branch)
//  "needs_onboard"      — super_admin with no org context → go to /onboarding
//  "needs_pw_change"    — must change password before accessing the app
//  null                 — not yet determined (initial state / logged out)
// ─────────────────────────────────────────────────────────────────────────────
export type SetupState = "ready" | "needs_branch" | "needs_onboard" | "needs_pw_change" | null;

interface AuthState {
    user: User | null;
    isAuthenticated: boolean;
    isLoading: boolean;
    activeBranchId: string | null;
    /** Signals the router where to send the user after login */
    setupState: SetupState;

    initialize: () => Promise<void>;
    login: (username: string, password: string, totp_code?: string) => Promise<void>;
    logout: () => Promise<void>;
    setUser: (user: User) => void;
    setActiveBranch: (branchId: string) => void;
    /** Called by SetupRequiredPage once the user has finished setup */
    markReady: (branchId: string) => void;
    /** Called after forced password change succeeds */
    clearPasswordChangeRequired: () => void;
}

// ─────────────────────────────────────────────────────────────────────────────
// Derives SetupState from a freshly-loaded User.
//
// Rules:
//  1. super_admin                          → always needs_onboard
//     They are a platform-level account whose sole job is to create and onboard
//     organizations for clients.  They never operate inside a branch themselves,
//     so branch checks are irrelevant for them.
//  2. Any other role with assigned_branches → ready
//     (Auto-select the single branch; multi-branch selection handled later.)
//  3. Any other role with NO branches      → needs_branch
//     (Org exists but the admin skipped branch setup — prompt to add one.)
// ─────────────────────────────────────────────────────────────────────────────
function deriveSetupState(user: User): SetupState {
    // Must change password before accessing the app
    if (user.password_change_required) {
        return "needs_pw_change";
    }


    // Super admins are platform operators — they must onboard an org first,
    // unless they already have branches assigned (org already set up).
    if (user.is_super_admin && (user.assigned_branches?.length ?? 0) === 0) {
        return "needs_onboard";
    }

    // Use effective permissions (includes hierarchy inheritance).
    // The login endpoint doesn't compute _effective_permissions, so
    // effectivePerms may be an empty default object rather than undefined.
    // Fall back to role-level permissions when the computed field is empty.
    const computed = user.effective_permissions?.effective_permissions;
    const effectivePerms = (computed && computed.length > 0)
        ? computed
        : user.roles.flatMap(r => r.permissions);

    const hasOrgManagePerm =
        effectivePerms.includes("manage_organization") ||
        effectivePerms.includes("manage_branches") ||
        effectivePerms.includes("*");

    if (hasOrgManagePerm && (user.assigned_branches?.length ?? 0) === 0) {
        return "needs_branch";
    }

    if ((user.assigned_branches?.length ?? 0) > 0) {
        return "ready";
    }
    return "needs_branch";
}

export const useAuthStore = create<AuthState>((set, get) => ({
    user: null,
    isAuthenticated: false,
    isLoading: true,
    activeBranchId: null,
    setupState: null,

    initialize: async () => {
        set({ isLoading: true });
        try {
            const [token, user, branchId] = await Promise.all([
                authStorage.getAccessToken(),
                authStorage.getUser<User>(),
                authStorage.getActiveBranch(),
            ]);

            if (token && user) {
                const setupState = deriveSetupState(user);

                // Only restore the saved branch when the user is actually ready.
                // If they're in a setup state, the saved branchId is stale / irrelevant.
                const activeBranchId = setupState === "ready" ? branchId : null;

                set({ user, isAuthenticated: true, activeBranchId, setupState });

                if (activeBranchId) {
                    syncEngine.start(activeBranchId, user.organization_id);
                }

                // Cached identity keeps offline startup fast, but authority can
                // change server-side (for example after an RBAC migration).
                // Refresh it in the background so stale role data cannot keep
                // the user in the wrong setup flow.
                if (navigator.onLine) {
                    void authApi.me()
                        .then(async (freshUser) => {
                            const current = get();
                            if (!current.isAuthenticated || current.user?.id !== user.id) {
                                return;
                            }

                            const freshSetupState = deriveSetupState(freshUser);
                            let freshBranchId = current.activeBranchId;

                            if (freshSetupState !== "ready") {
                                freshBranchId = null;
                                syncEngine.stop();
                            } else if (
                                !freshBranchId &&
                                (freshUser.assigned_branches?.length ?? 0) === 1
                            ) {
                                freshBranchId = String(freshUser.assigned_branches[0]);
                                await authStorage.setActiveBranch(freshBranchId);
                                syncEngine.start(freshBranchId, freshUser.organization_id);
                            }

                            await authStorage.setUser(freshUser);
                            set({
                                user: freshUser,
                                setupState: freshSetupState,
                                activeBranchId: freshBranchId,
                            });
                        })
                        .catch(() => {
                            // Keep the cached identity when offline or when the
                            // backend is temporarily unavailable.
                        });
                }
            }
        } catch {
            try { await authStorage.clearTokens(); } catch { /* store may not be ready */ }
        } finally {
            set({ isLoading: false });
        }
    },

    login: async (username: string, password: string, totp_code?: string) => {
        const data = await authApi.login({ username, password, totp_code });
        await authStorage.setUser(data.user);

        const setupState = deriveSetupState(data.user);

        let branchId: string | null = null;
        if (setupState === "ready") {
            // Auto-select if exactly one branch; multi-branch picker handled elsewhere
            if ((data.user.assigned_branches?.length ?? 0) === 1) {
                branchId = String(data.user.assigned_branches![0]);
                await authStorage.setActiveBranch(branchId);
            }
        }

        set({
            user: data.user,
            isAuthenticated: true,
            activeBranchId: branchId,
            setupState,
        });

        if (branchId) {
            syncEngine.start(branchId, data.user.organization_id);
        }
    },

    // Also override setUser to recalc setupState
    setUser: (user) => {
        authStorage.setUser(user);
        const setupState = deriveSetupState(user);
        set({ user, setupState });
    },

    /**
     * Updates the user in the store after a successful forced password change.
     * Refetches user data from /auth/me and recalculates setupState.
     */
    clearPasswordChangeRequired: async () => {
        try {
            const fresh = await authApi.me();
            const setupState = deriveSetupState(fresh);
            await authStorage.setUser(fresh);
            set({ user: fresh, setupState });
        } catch {
            set({ setupState: "ready" });
        }
    },

    logout: async () => {
        syncEngine.stop();
        try {
            await authApi.logout();
        } catch {
            // Always clear state even if the API call fails
        }
        await authStorage.clearTokens();
        set({ user: null, isAuthenticated: false, activeBranchId: null, setupState: null });
    },

    setActiveBranch: (branchId) => {
        const { user, activeBranchId } = get();
        if (activeBranchId === branchId) {
            return;
        }
        authStorage.setActiveBranch(branchId);
        set({ activeBranchId: branchId });
        syncEngine.stop();
        syncEngine.start(branchId, user?.organization_id ?? null);
    },

    /**
     * Called by SetupRequiredPage (or BranchSelectPage) once the user has a
     * valid branch.  Transitions setupState → "ready" and starts the sync engine.
     */
    markReady: (branchId: string) => {
        const { user } = get();
        if (!user) return;
        authStorage.setActiveBranch(branchId);
        set({ activeBranchId: branchId, setupState: "ready" });
        syncEngine.start(branchId, user.organization_id);
    },
}));
