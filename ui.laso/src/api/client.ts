import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";
import { authStorage } from "@/lib/storage";

const BASE_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

export const apiClient = axios.create({
    baseURL: `${BASE_URL}/api/v1`,
    headers: { "Content-Type": "application/json" },
    timeout: 15_000,
});

// ── Token refresh state ───────────────────────────────────
let isRefreshing = false;
let pendingQueue: Array<{
    resolve: (token: string) => void;
    reject: (err: unknown) => void;
}> = [];

function flushQueue(token: string | null, error: unknown = null) {
    pendingQueue.forEach(({ resolve, reject }) =>
        token ? resolve(token) : reject(error)
    );
    pendingQueue = [];
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
    (response) => response,
    async (error: AxiosError) => {
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

        try {
            const refreshToken = await authStorage.getRefreshToken();
            if (!refreshToken) throw new Error("No refresh token");

            // Call refresh directly to avoid interceptor loop
            const { data } = await axios.post(
                `${BASE_URL}/api/v1/auth/refresh`,
                { refresh_token: refreshToken },
                { headers: { "Content-Type": "application/json" } }
            );

            const newAccessToken: string = data.access_token;
            await authStorage.setTokens(newAccessToken, data.refresh_token);

            flushQueue(newAccessToken);
            original.headers!.Authorization = `Bearer ${newAccessToken}`;
            return apiClient(original);
        } catch (refreshError) {
            flushQueue(null, refreshError);
            await authStorage.clearTokens();
            // Broadcast logout event so App.tsx can redirect
            window.dispatchEvent(new Event("auth:logout"));
            return Promise.reject(refreshError);
        } finally {
            isRefreshing = false;
        }
    }
);

// ── Typed helpers ─────────────────────────────────────────
export async function get<T>(url: string, config?: { signal?: AbortSignal; params?: Record<string, unknown> }): Promise<T> {
    const { data } = await apiClient.get<T>(url, { signal: config?.signal, params: config?.params });
    return data;
}

export async function post<T>(url: string, body?: unknown): Promise<T> {
    const { data } = await apiClient.post<T>(url, body);
    return data;
}

export async function patch<T>(url: string, body?: unknown): Promise<T> {
    const { data } = await apiClient.patch<T>(url, body);
    return data;
}

export async function del<T>(url: string): Promise<T> {
    const { data } = await apiClient.delete<T>(url);
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