import { create } from "zustand";
import type { UserResponse as User } from "@/types";
import { authStorage } from "@/lib/storage";
import { authApi } from "@/api/auth";
import { syncEngine } from "@/lib/syncEngine";

// ─────────────────────────────────────────────────────────────────────────────
// What post-login destination does this user need?
//
//  "ready"          — has org + branch → go straight to /drugs
//  "needs_branch"   — has org but zero branches → go to /setup (add branch)
//  "needs_onboard"  — super_admin with no org context → go to /onboarding
//  null             — not yet determined (initial state / logged out)
// ─────────────────────────────────────────────────────────────────────────────
export type SetupState = "ready" | "needs_branch" | "needs_onboard" | null;

interface AuthState {
    user: User | null;
    isAuthenticated: boolean;
    isLoading: boolean;
    activeBranchId: string | null;
    /** Signals the router where to send the user after login */
    setupState: SetupState;

    initialize: () => Promise<void>;
    login: (username: string, password: string) => Promise<void>;
    logout: () => Promise<void>;
    setUser: (user: User) => void;
    setActiveBranch: (branchId: string) => void;
    /** Called by SetupRequiredPage once the user has finished setup */
    markReady: (branchId: string) => void;
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
    // super_admin is a platform operator — always send to onboarding wizard
    if (user.role === "super_admin") {
        return "needs_onboard";
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
                    syncEngine.start(activeBranchId);
                }
            }
        } catch {
            try { await authStorage.clearTokens(); } catch { /* store may not be ready */ }
        } finally {
            set({ isLoading: false });
        }
    },

    login: async (username, password) => {
        const data = await authApi.login({ username, password });
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
            syncEngine.start(branchId);
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

    setUser: (user) => {
        authStorage.setUser(user);
        set({ user });
    },

    setActiveBranch: (branchId) => {
        authStorage.setActiveBranch(branchId);
        set({ activeBranchId: branchId });
        syncEngine.stop();
        syncEngine.start(branchId);
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
        syncEngine.start(branchId);
    },
}));