/**
 * Tauri-safe storage layer.
 * Uses @tauri-apps/plugin-store in native Tauri context.
 * Browser/dev auth tokens use sessionStorage so they are cleared when the tab
 * closes; non-sensitive cached data uses localStorage.
 *
 * plugin-store v2 changed API: use load() instead of new Store()
 *
 * Native auth tokens are stored in the operating-system credential vault via
 * narrow Rust commands. Browser development uses sessionStorage, so tokens are
 * removed when the browser session closes.
 */

import type { BranchListItem, Organization, OrganizationStats, PaginatedResponse, UserResponse } from "@/types";

const IS_TAURI =
    typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

const _SENSITIVE_KEYS = new Set([
    "auth.access_token",
    "auth.refresh_token",
]);

function browserStorage(key: string): Storage {
    return _SENSITIVE_KEYS.has(key) ? sessionStorage : localStorage;
}

// ── Tauri store ─────────────────────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyStore = any;

let _storePromise: Promise<AnyStore | null> | null = null;

function getStore(): Promise<AnyStore | null> {
    if (!IS_TAURI) return Promise.resolve(null);
    if (_storePromise) return _storePromise;

    _storePromise = (async () => {
        try {
            const mod = await import(/* @vite-ignore */ "@tauri-apps/plugin-store");
            // v2 API: use load() static method, not new Store()
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const store = await (mod as any).load("laso.bin", { autoSave: true });
            return store;
        } catch {
            // Plugin unavailable — fall back to localStorage
            return null;
        }
    })();

    return _storePromise;
}

async function storageGet<T>(key: string): Promise<T | null> {
    if (IS_TAURI && _SENSITIVE_KEYS.has(key)) {
        const { invoke } = await import("@tauri-apps/api/core");
        return await invoke<T | null>("secure_get", { key });
    }
    const store = await getStore();
    if (store) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const val: any = await store.get(key);
        return (val ?? null) as T | null;
    }
    const raw = browserStorage(key).getItem(key);
    if (!raw) return null;
    try {
        return JSON.parse(raw) as T;
    } catch {
        return raw as unknown as T;
    }
}

async function storageSet(key: string, value: unknown): Promise<void> {
    if (IS_TAURI && _SENSITIVE_KEYS.has(key)) {
        if (typeof value !== "string") {
            throw new TypeError("Secure auth values must be strings.");
        }
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("secure_set", { key, value });
        return;
    }
    const store = await getStore();
    if (store) {
        await store.set(key, value);
        return;
    }
    const serialized = JSON.stringify(value);
    browserStorage(key).setItem(key, serialized);
}

async function storageDel(key: string): Promise<void> {
    if (IS_TAURI && _SENSITIVE_KEYS.has(key)) {
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("secure_delete", { key });
        return;
    }
    const store = await getStore();
    if (store) {
        await store.delete(key);
        return;
    }
    browserStorage(key).removeItem(key);
}

// ── Auth-specific helpers ──────────────────────────────────
const KEYS = {
    ACCESS_TOKEN: "auth.access_token",
    REFRESH_TOKEN: "auth.refresh_token",
    USER: "auth.user",
    BRANCH: "session.branch_id",
} as const;

type CachedUserPage = PaginatedResponse<UserResponse>;

const CACHE_KEYS = {
    BRANCHES: "cache.branches",
    USERS: "cache.users",
    ORGANIZATION: "cache.organization",
    ORGANIZATION_STATS: "cache.organization_stats",
} as const;

export const offlineCache = {
    setBranches: (branches: BranchListItem[]) => storageSet(CACHE_KEYS.BRANCHES, branches),
    getBranches: () => storageGet<BranchListItem[]>(CACHE_KEYS.BRANCHES),
    async getBranchName(id: string): Promise<string | null> {
        const branches = await storageGet<BranchListItem[]>(CACHE_KEYS.BRANCHES);
        return branches?.find((branch) => String(branch.id) === String(id))?.name ?? null;
    },
    setUsers: (users: CachedUserPage) => storageSet(CACHE_KEYS.USERS, users),
    getUsers: () => storageGet<CachedUserPage>(CACHE_KEYS.USERS),
    setOrganization: (org: Organization) => storageSet(CACHE_KEYS.ORGANIZATION, org),
    getOrganization: () => storageGet<Organization>(CACHE_KEYS.ORGANIZATION),
    setOrganizationStats: (stats: OrganizationStats) => storageSet(CACHE_KEYS.ORGANIZATION_STATS, stats),
    getOrganizationStats: () => storageGet<OrganizationStats>(CACHE_KEYS.ORGANIZATION_STATS),
};

export const authStorage = {
    getAccessToken: () => storageGet<string>(KEYS.ACCESS_TOKEN),
    getRefreshToken: () => storageGet<string>(KEYS.REFRESH_TOKEN),

    async setTokens(access: string, refresh: string) {
        await storageSet(KEYS.ACCESS_TOKEN, access);
        await storageSet(KEYS.REFRESH_TOKEN, refresh);
    },

    async clearTokens() {
        await Promise.all([
            storageDel(KEYS.ACCESS_TOKEN),
            storageDel(KEYS.REFRESH_TOKEN),
            storageDel(KEYS.USER),
            storageDel(KEYS.BRANCH),
            storageDel(CACHE_KEYS.BRANCHES),
            storageDel(CACHE_KEYS.USERS),
            storageDel(CACHE_KEYS.ORGANIZATION),
            storageDel(CACHE_KEYS.ORGANIZATION_STATS),
        ]);
    },

    setUser: (user: unknown) => storageSet(KEYS.USER, user),
    getUser: <T>() => storageGet<T>(KEYS.USER),
    setActiveBranch: (id: string) => storageSet(KEYS.BRANCH, id),
    getActiveBranch: () => storageGet<string>(KEYS.BRANCH),
};
