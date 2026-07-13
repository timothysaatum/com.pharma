import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";
import { authStorage } from "@/lib/storage";

const configuredBaseUrl = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
if (import.meta.env.PROD && !configuredBaseUrl.startsWith("https://")) {
    throw new Error("Production VITE_API_URL must use HTTPS.");
}
const BASE_URL = configuredBaseUrl.replace(/^http:\/\/127\.0\.0\.1:8000\/?$/, "http://localhost:8000");
let backendReachable = true;
let backendOfflineSince: number | null = null;
// Until at least one API call succeeds, assume the backend is offline.
// This avoids CORS / connection-refused noise on initial searches
// before the sync engine has had a chance to confirm reachability.
let _backendEverConfirmed = false;

export const BACKEND_CONNECTIVITY_EVENT = "backend:connectivity-change";

type BackendConnectivityDetail = {
    reachable: boolean;
    offlineSince: number | null;
};

function emitBackendConnectivityChange(): void {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new CustomEvent<BackendConnectivityDetail>(
        BACKEND_CONNECTIVITY_EVENT,
        {
            detail: {
                reachable: backendReachable,
                offlineSince: backendOfflineSince,
            },
        },
    ));
}

export function isBackendReachable(): boolean {
    if (!_backendEverConfirmed) return false;
    if (!backendReachable && navigator.onLine && backendOfflineSince !== null) {
        return Date.now() - backendOfflineSince > 15_000;
    }
    return backendReachable;
}

export function markBackendOffline(): void {
    const wasReachable = backendReachable;
    backendReachable = false;
    backendOfflineSince = Date.now();
    if (wasReachable) {
        emitBackendConnectivityChange();
    }
}

export function markBackendOnline(): void {
    _backendEverConfirmed = true;
    const wasOffline = !backendReachable;
    backendReachable = true;
    backendOfflineSince = null;
    if (wasOffline) {
        emitBackendConnectivityChange();
    }
}

export const apiClient = axios.create({
    baseURL: `${BASE_URL}/api/v1`,
    headers: { "Content-Type": "application/json" },
    timeout: 30_000,
});

// ── Token refresh state ───────────────────────────────────
let isRefreshing = false;
let refreshPromise: Promise<void> | null = null;
let pendingQueue: Array<{
    resolve: (token: string) => void;
    reject: (err: unknown) => void;
}> = [];

function flushQueue(token: string | null, error: unknown = null) {
    const queue = pendingQueue.slice();
    pendingQueue = [];
    queue.forEach(({ resolve, reject }) =>
        token ? resolve(token) : reject(error)
    );
}

// ── Request interceptor: attach access token ─────────────
apiClient.interceptors.request.use(async (config: InternalAxiosRequestConfig) => {
    const token = await authStorage.getAccessToken();
    if (token && config.headers) {
        config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
});

// ── Response interceptor: handle 401 → refresh → retry ───
apiClient.interceptors.response.use(
    (response) => {
        markBackendOnline();
        return response;
    },
    async (error: AxiosError) => {
        if (isOfflineError(error)) {
            markBackendOffline();
        }
        const original = error.config as InternalAxiosRequestConfig & {
            _retry?: boolean;
        };

        // Only attempt refresh on 401, and not on the refresh endpoint itself
        if (
            error.response?.status !== 401 ||
            original._retry ||
            original.url?.includes("/auth/refresh") ||
            original.url?.includes("/auth/login")
        ) {
            return Promise.reject(error);
        }

        if (isRefreshing) {
            // Queue this request until refresh completes
            return new Promise((resolve, reject) => {
                pendingQueue.push({
                    resolve: (token) => {
                        original.headers!.Authorization = `Bearer ${token}`;
                        resolve(apiClient(original));
                    },
                    reject,
                });
            });
        }

        original._retry = true;
        isRefreshing = true;

        // Use a shared promise so concurrent failures don't each trigger a refresh
        if (!refreshPromise) {
            refreshPromise = (async () => {
                const refreshToken = await authStorage.getRefreshToken();
                if (!refreshToken) throw new Error("No refresh token");

                const { data } = await axios.post(
                    `${BASE_URL}/api/v1/auth/refresh`,
                    { refresh_token: refreshToken },
                    { headers: { "Content-Type": "application/json" }, timeout: 15_000 }
                );

                const newAccessToken: string = data.access_token;
                await authStorage.setTokens(newAccessToken, data.refresh_token);
                flushQueue(newAccessToken);
            })();
        }

        try {
            await refreshPromise;
            const storedToken = await authStorage.getAccessToken();
            if (storedToken) {
                original.headers!.Authorization = `Bearer ${storedToken}`;
            }
            return apiClient(original);
        } catch (refreshError) {
            refreshPromise = null;
            if (isOfflineError(refreshError)) {
                flushQueue(null, refreshError);
                return Promise.reject(refreshError);
            }
            flushQueue(null, refreshError);
            await authStorage.clearTokens();
            window.dispatchEvent(new Event("auth:logout"));
            return Promise.reject(refreshError);
        } finally {
            isRefreshing = false;
            refreshPromise = null;
        }
    }
);

// ── Typed helpers ─────────────────────────────────────────
export async function get<T>(url: string, config?: { signal?: AbortSignal; params?: Record<string, unknown> }): Promise<T> {
    const { data } = await apiClient.get<T>(url, { signal: config?.signal, params: config?.params });
    return data;
}

export async function post<T>(url: string, body?: unknown, config?: { signal?: AbortSignal }): Promise<T> {
    const { data } = await apiClient.post<T>(url, body, { signal: config?.signal });
    return data;
}

export async function put<T>(url: string, body?: unknown, config?: { signal?: AbortSignal }): Promise<T> {
    const { data } = await apiClient.put<T>(url, body, { signal: config?.signal });
    return data;
}

export async function patch<T>(url: string, body?: unknown, config?: { signal?: AbortSignal }): Promise<T> {
    const { data } = await apiClient.patch<T>(url, body, { signal: config?.signal });
    return data;
}

export async function del<T>(url: string, config?: { signal?: AbortSignal }): Promise<T> {
    const { data } = await apiClient.delete<T>(url, { signal: config?.signal });
    return data;
}

/**
 * Extracts a human-readable error message from any API error shape.
 * Handles FastAPI's { detail } and { detail: [{msg}] } formats.
 */
export function parseApiError(err: unknown): string {
    if (axios.isAxiosError(err)) {
        const data = err.response?.data;
        if (!data) {
            if (err.code === "ECONNREFUSED" || err.code === "ERR_NETWORK")
                return "Cannot connect to server. Make sure the backend is running.";
            if (err.code === "ECONNABORTED") return "Request timed out. Try again.";
            
            if (err.code === "ERR_CANCELED") return "";
            return err.message;
        }
        // Never render server-side exception details. Besides being confusing
        // to users, database and framework errors can disclose implementation
        // details. The backend logs retain the specific cause for diagnosis.
        if (err.response?.status && err.response.status >= 500) {
            return "Something went wrong on our side. Please try again.";
        }
        // Project validation responses use a stable top-level detail plus a
        // field-level errors array. Surface the actionable messages first.
        if (Array.isArray(data.errors)) {
            const messages = data.errors
                .map((item: unknown) => {
                    if (!item || typeof item !== "object") return null;
                    const error = item as {
                        loc?: unknown[];
                        msg?: string;
                    };
                    if (typeof error.msg !== "string") return null;
                    const field = Array.isArray(error.loc)
                        ? error.loc.filter((part) => part !== "body").join(".")
                        : "";
                    return field ? `${field}: ${error.msg}` : error.msg;
                })
                .filter((message: string | null): message is string => Boolean(message));
            if (messages.length > 0) return messages.join(", ");
        }
        // FastAPI string detail
        if (typeof data.detail === "string") return data.detail;
        // FastAPI validation error array
        if (Array.isArray(data.detail)) {
            return data.detail.map((d: { msg: string }) => d.msg).join(", ");
        }
        if (typeof data.message === "string") return data.message;
        if (typeof data.error === "string") return data.error;
    }
    if (err instanceof Error) return err.message;
    return "An unexpected error occurred";
}

export function isOfflineError(err: unknown): boolean {
    // Axios errors without a response usually indicate network problems.
    if (axios.isAxiosError(err)) {
        if (!err.response) {
            if (err.code === "ERR_CANCELED" || err.code === "ECONNABORTED") return false;
            return true;
        }
        return false;
    }

    // Non-axios errors — inspect message and name for common network-related phrases.
    if (err instanceof Error) {
        const msg = (err.message || "").toLowerCase();
        const name = ((err as any).name || "").toLowerCase();
        if (name === "aborterror") return false; // user cancelled or abort
        const networkPatterns = [
            "network error",
            "failed to fetch",
            "network request failed",
            "econnrefused",
            "econnreset",
            "enotfound",
            "ehostunreach",
            "socket",
            "connect",
            "timeout",
            "timed out",
            "networkrequestfailed",
        ];
        for (const p of networkPatterns) {
            if (msg.includes(p) || name.includes(p)) return true;
        }
    }

    return false;
}
