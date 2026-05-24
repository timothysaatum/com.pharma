import { get, post } from "./client";
import type {
    LoginRequest,
    TokenResponse,
    UserResponse,
    UserRole,
    PasswordChange,
} from "@/types";
import { authStorage } from "@/lib/storage";

// ─────────────────────────────────────────────────────────────────────────────
// Response shapes for auth-specific endpoints
// ─────────────────────────────────────────────────────────────────────────────

export interface VerifyResponse {
    valid: boolean;
    /** UUID of the authenticated user */
    user_id: string;
    username: string;
    role: UserRole;
}

export interface SessionInfo {
    id: string;
    ip_address: string | null;
    user_agent: string | null;
    created_at: string;
    expires_at: string;
}

export interface SessionsResponse {
    total: number;
    sessions: SessionInfo[];
}

export interface PermissionsResponse {
    role: UserRole;
    permissions: string[];
    branches: string[];
}

// ─────────────────────────────────────────────────────────────────────────────
// API
// ─────────────────────────────────────────────────────────────────────────────

export const authApi = {
    /**
     * POST /auth/login
     * Accepts: { username, password, device_info? }
     * Returns: { access_token, refresh_token, expires_in, user }
     */
    async login(data: LoginRequest): Promise<TokenResponse> {
        const result = await post<TokenResponse>("/auth/login", {
            username: data.username,
            password: data.password,
            device_info: navigator.userAgent.slice(0, 500),
        });
        await authStorage.setTokens(result.access_token, result.refresh_token);
        return result;
    },

    /**
     * POST /auth/logout
     * Revokes the current session token.
     */
    async logout(): Promise<void> {
        try {
            await post("/auth/logout");
        } finally {
            // Always clear local tokens even if the API call fails
            await authStorage.clearTokens();
        }
    },

    /**
     * POST /auth/logout-all
     * Revokes all sessions for the current user across all devices.
     */
    logoutAll(): Promise<{ message: string; sessions_revoked: number }> {
        return post("/auth/logout-all");
    },

    /**
     * POST /auth/refresh
     * Exchanges a refresh token for a new token pair.
     */
    async refresh(refreshToken: string): Promise<TokenResponse> {
        const result = await post<TokenResponse>("/auth/refresh", {
            refresh_token: refreshToken,
        });
        await authStorage.setTokens(result.access_token, result.refresh_token);
        return result;
    },

    /**
     * GET /auth/me
     * Returns the current user from a valid access token.
     * Pass an AbortSignal to cancel (e.g. during initialize with a timeout).
     */
    me(signal?: AbortSignal): Promise<UserResponse> {
        return get<UserResponse>("/auth/me", { signal });
    },

    /**
     * POST /auth/change-password
     * Updates the password and revokes all sessions.
     * The user must re-login after this call.
     */
    changePassword(data: PasswordChange): Promise<{ message: string }> {
        return post("/auth/change-password", data);
    },

    /**
     * GET /auth/sessions
     * Lists all active sessions for the current user.
     */
    getSessions(signal?: AbortSignal): Promise<SessionsResponse> {
        return get<SessionsResponse>("/auth/sessions", { signal });
    },

    /**
     * GET /auth/verify
     * Lightweight token validity check.
     * Returns user_id, username, and role — does not return full user data.
     * Use authApi.me() when you need the full User object.
     */
    verify(signal?: AbortSignal): Promise<VerifyResponse> {
        return get<VerifyResponse>("/auth/verify", { signal });
    },

    /**
     * GET /auth/permissions
     * Returns the effective permission list for the current user,
     * combining role-based permissions with any additional/denied overrides.
     */
    getPermissions(signal?: AbortSignal): Promise<PermissionsResponse> {
        return get<PermissionsResponse>("/auth/permissions", { signal });
    },
};