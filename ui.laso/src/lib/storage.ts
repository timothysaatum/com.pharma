/**
 * Tauri-safe storage layer.
 * Uses @tauri-apps/plugin-store in native Tauri context,
 * falls back to localStorage for browser / dev mode.
 *
 * plugin-store v2 changed API: use load() instead of new Store()
 */

const IS_TAURI =
    typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

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
        // Fix: call get without type argument, then cast
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const val: any = await store.get(key);
        return (val ?? null) as T | null;
    }
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    try {
        return JSON.parse(raw) as T;
    } catch {
        return raw as unknown as T;
    }
}

async function storageSet(key: string, value: unknown): Promise<void> {
    const store = await getStore();
    if (store) {
        await store.set(key, value);
        // No manual save() needed — autoSave: true handles it
        return;
    }
    localStorage.setItem(key, JSON.stringify(value));
}

async function storageDel(key: string): Promise<void> {
    const store = await getStore();
    if (store) {
        await store.delete(key);
        // No manual save() needed — autoSave: true handles it
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
        ]);
    },

    setUser: (user: unknown) => storageSet(KEYS.USER, user),
    getUser: <T>() => storageGet<T>(KEYS.USER),
    setActiveBranch: (id: string) => storageSet(KEYS.BRANCH, id),
    getActiveBranch: () => storageGet<string>(KEYS.BRANCH),
};