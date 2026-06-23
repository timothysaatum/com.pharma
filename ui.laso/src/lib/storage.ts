/**
 * Tauri-safe storage layer.
 * Uses @tauri-apps/plugin-store in native Tauri context (encrypted),
 * falls back to localStorage with AES-GCM encryption for browser / dev mode.
 *
 * plugin-store v2 changed API: use load() instead of new Store()
 *
 * Security note (browser fallback):
 *   Tokens are encrypted with AES-GCM using a random key derived at page load.
 *   The encryption key lives only in memory (not persisted), so a new page
 *   load or XSS that reads localStorage will get ciphertext only.
 *   This mitigates — but does not fully eliminate — localStorage XSS risk.
 *   Production deployments should use the Tauri native store.
 */

import type { BranchListItem, Organization, OrganizationStats, PaginatedResponse, UserResponse } from "@/types";

const IS_TAURI =
    typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

// ── Browser-only encryption helpers ─────────────────────────

let _browserKey: CryptoKey | null = null;

async function _getBrowserKey(): Promise<CryptoKey | null> {
    if (_browserKey) return _browserKey;
    try {
        // Generate an ephemeral AES-GCM key that lives only in memory
        _browserKey = await crypto.subtle.generateKey(
            { name: "AES-GCM", length: 256 },
            false, // not extractable — key never leaves memory
            ["encrypt", "decrypt"],
        );
        return _browserKey;
    } catch {
        return null; // crypto unavailable — store in plaintext
    }
}

async function _encrypt(plaintext: string): Promise<string> {
    const key = await _getBrowserKey();
    if (!key) return plaintext;
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const encoded = new TextEncoder().encode(plaintext);
    const ciphertext = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        key,
        encoded,
    );
    // Prepend IV to ciphertext and encode as base64
    const combined = new Uint8Array(iv.length + ciphertext.byteLength);
    combined.set(iv);
    combined.set(new Uint8Array(ciphertext), iv.length);
    return btoa(String.fromCharCode(...combined));
}

async function _decrypt(data: string): Promise<string | null> {
    const key = await _getBrowserKey();
    if (!key) return data;
    try {
        const combined = Uint8Array.from(atob(data), c => c.charCodeAt(0));
        const iv = combined.slice(0, 12);
        const ciphertext = combined.slice(12);
        const plaintext = await crypto.subtle.decrypt(
            { name: "AES-GCM", iv },
            key,
            ciphertext,
        );
        return new TextDecoder().decode(plaintext);
    } catch {
        return null; // decryption failed — possibly tampered
    }
}

const _SENSITIVE_KEYS = new Set([
    "auth.access_token",
    "auth.refresh_token",
]);

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
    const store = await getStore();
    if (store) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const val: any = await store.get(key);
        return (val ?? null) as T | null;
    }
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    try {
        let parsed: unknown;
        if (_SENSITIVE_KEYS.has(key)) {
            const decrypted = await _decrypt(raw);
            if (decrypted === null) {
                localStorage.removeItem(key);
                return null;
            }
            parsed = decrypted;
        } else {
            parsed = raw;
        }
        return JSON.parse(parsed as string) as T;
    } catch {
        return raw as unknown as T;
    }
}

async function storageSet(key: string, value: unknown): Promise<void> {
    const store = await getStore();
    if (store) {
        await store.set(key, value);
        return;
    }
    const serialized = JSON.stringify(value);
    if (_SENSITIVE_KEYS.has(key)) {
        const encrypted = await _encrypt(serialized);
        localStorage.setItem(key, encrypted);
    } else {
        localStorage.setItem(key, serialized);
    }
}

async function storageDel(key: string): Promise<void> {
    const store = await getStore();
    if (store) {
        await store.delete(key);
        return;
    }
    localStorage.removeItem(key);
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
