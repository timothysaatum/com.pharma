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
        try {
            const { invoke } = await import("@tauri-apps/api/core");
            return await invoke<T | null>("secure_get", { key });
        } catch (err) {
            console.warn(`[storage] Tauri secure_get failed for key "${key}", falling back:`, err);
        }
    }
    const store = await getStore();
    if (store) {
        try {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const val: any = await store.get(key);
            return (val ?? null) as T | null;
        } catch (err) {
            console.warn(`[storage] Tauri store get failed for key "${key}":`, err);
        }
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
        try {
            const { invoke } = await import("@tauri-apps/api/core");
            await invoke("secure_set", { key, value });
            return;
        } catch (err) {
            console.warn(`[storage] Tauri secure_set failed for key "${key}", falling back:`, err);
        }
    }
    const store = await getStore();
    if (store) {
        try {
            await store.set(key, value);
            return;
        } catch (err) {
            console.warn(`[storage] Tauri store set failed for key "${key}":`, err);
        }
    }
    const serialized = JSON.stringify(value);
    browserStorage(key).setItem(key, serialized);
}

async function storageDel(key: string): Promise<void> {
    if (IS_TAURI && _SENSITIVE_KEYS.has(key)) {
        try {
            const { invoke } = await import("@tauri-apps/api/core");
            await invoke("secure_delete", { key });
            return;
        } catch (err) {
            console.warn(`[storage] Tauri secure_delete failed for key "${key}", falling back:`, err);
        }
    }
    const store = await getStore();
    if (store) {
        try {
            await store.delete(key);
            return;
        } catch (err) {
            console.warn(`[storage] Tauri store delete failed for key "${key}":`, err);
        }
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
type CacheEnvelope<T> = {
    value: T;
    cached_at: string;
    expires_at: string;
};

const CACHE_KEYS = {
    BRANCHES: "cache.branches",
    USERS: "cache.users",
    ORGANIZATION: "cache.organization",
    ORGANIZATION_STATS: "cache.organization_stats",
} as const;

const DEFAULT_REFERENCE_TTL_MS = 6 * 60 * 60 * 1000;

async function cacheSet<T>(
    key: string,
    value: T,
    ttlMs = DEFAULT_REFERENCE_TTL_MS,
): Promise<void> {
    const now = Date.now();
    await storageSet(key, {
        value,
        cached_at: new Date(now).toISOString(),
        expires_at: new Date(now + ttlMs).toISOString(),
    } satisfies CacheEnvelope<T>);
}

async function cacheGet<T>(
    key: string,
    options: { allowExpired?: boolean } = {},
): Promise<T | null> {
    const cached = await storageGet<T | CacheEnvelope<T>>(key);
    if (!cached) return null;
    if (
        typeof cached === "object"
        && cached !== null
        && "value" in cached
        && "expires_at" in cached
    ) {
        const envelope = cached as CacheEnvelope<T>;
        if (options.allowExpired || Date.parse(envelope.expires_at) > Date.now()) {
            return envelope.value;
        }
        return null;
    }
    return cached as T;
}

export const offlineCache = {
    setBranches: (branches: BranchListItem[]) => cacheSet(CACHE_KEYS.BRANCHES, branches),
    getBranches: (options?: { allowExpired?: boolean }) => cacheGet<BranchListItem[]>(CACHE_KEYS.BRANCHES, options),
    async getBranchName(id: string): Promise<string | null> {
        const branches = await cacheGet<BranchListItem[]>(
            CACHE_KEYS.BRANCHES,
            { allowExpired: true },
        );
        return branches?.find((branch) => String(branch.id) === String(id))?.name ?? null;
    },
    setUsers: (users: CachedUserPage) => cacheSet(CACHE_KEYS.USERS, users),
    getUsers: (options?: { allowExpired?: boolean }) => cacheGet<CachedUserPage>(CACHE_KEYS.USERS, options),
    setOrganization: (org: Organization) => cacheSet(CACHE_KEYS.ORGANIZATION, org),
    getOrganization: (options?: { allowExpired?: boolean }) => cacheGet<Organization>(CACHE_KEYS.ORGANIZATION, options),
    setOrganizationStats: (stats: OrganizationStats) => cacheSet(CACHE_KEYS.ORGANIZATION_STATS, stats),
    getOrganizationStats: (options?: { allowExpired?: boolean }) => cacheGet<OrganizationStats>(CACHE_KEYS.ORGANIZATION_STATS, options),
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
