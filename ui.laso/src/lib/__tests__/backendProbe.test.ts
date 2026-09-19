/** @vitest-environment jsdom */
/**
 * backendProbe.test.ts
 * ====================
 * Gate tests for the startup backend health probe and periodic heartbeat.
 *
 * Root cause these tests cover:
 *   1. `navigator.onLine` stays true even when the local/remote backend is down
 *      (the OS has a NIC; that doesn't mean the FastAPI server is reachable).
 *   2. `backendReachable` defaults to `true` at module init, so the very first
 *      render of every page incorrectly assumes the backend is up — resulting in
 *      blank POS / Customers / Inventory / Contracts / Categories pages offline.
 *
 * The probe fixes both: it runs before React renders and accurately sets the
 * flag via `markBackendOffline()` / `markBackendOnline()` before any page
 * mounts and calls `isBackendKnownUnreachable()`.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import axios from "axios";
import {
    isBackendKnownUnreachable,
    isBackendReachable,
    markBackendOnline,
    probeBackendNow,
    startBackendHeartbeat,
} from "@/api/client";

// Mock axios so no real HTTP calls are made in tests.
vi.mock("axios", async (importOriginal) => {
    const actual = await importOriginal<typeof import("axios")>();
    return {
        ...actual,
        default: {
            ...actual.default,
            get: vi.fn(),
            isAxiosError: actual.default.isAxiosError,
        },
    };
});

const mockedAxiosGet = vi.mocked(axios.get);

describe("probeBackendNow", () => {
    beforeEach(() => {
        markBackendOnline(); // reset to known state before each test
        vi.clearAllMocks();
    });

    it("marks backend ONLINE when /health returns 200", async () => {
        mockedAxiosGet.mockResolvedValueOnce({ status: 200, data: { status: "ok" } });

        const result = await probeBackendNow();

        expect(result).toBe(true);
        expect(isBackendReachable()).toBe(true);
        expect(isBackendKnownUnreachable()).toBe(false);
    });

    it("marks backend ONLINE when /health returns 4xx (server is up, just unauthenticated)", async () => {
        // A 401 or 404 means the server responded — it IS reachable.
        const axiosError = Object.assign(new Error("Request failed with status code 401"), {
            isAxiosError: true,
            response: { status: 401, data: { detail: "Not authenticated" } },
        });
        mockedAxiosGet.mockRejectedValueOnce(axiosError);
        // Make axios.isAxiosError return true for this error
        vi.spyOn(axios, "isAxiosError").mockReturnValueOnce(true);

        const result = await probeBackendNow();

        expect(result).toBe(true);
        expect(isBackendReachable()).toBe(true);
    });

    it("marks backend OFFLINE on ECONNREFUSED (backend process not running)", async () => {
        const networkError = Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:8000"), {
            isAxiosError: true,
            code: "ECONNREFUSED",
            response: undefined, // no response = real network failure
        });
        mockedAxiosGet.mockRejectedValueOnce(networkError);
        vi.spyOn(axios, "isAxiosError").mockReturnValueOnce(true);

        const result = await probeBackendNow();

        expect(result).toBe(false);
        expect(isBackendReachable()).toBe(false);
        expect(isBackendKnownUnreachable()).toBe(true);
    });

    it("marks backend OFFLINE on timeout (backend process hanging)", async () => {
        const timeoutError = Object.assign(new Error("timeout of 5000ms exceeded"), {
            isAxiosError: true,
            code: "ECONNABORTED",
            response: undefined,
        });
        mockedAxiosGet.mockRejectedValueOnce(timeoutError);
        vi.spyOn(axios, "isAxiosError").mockReturnValueOnce(true);

        const result = await probeBackendNow();

        expect(result).toBe(false);
        expect(isBackendKnownUnreachable()).toBe(true);
    });

    it("marks backend OFFLINE on ERR_NETWORK (internet down, remote backend)", async () => {
        const netErr = Object.assign(new Error("Network Error"), {
            isAxiosError: true,
            code: "ERR_NETWORK",
            response: undefined,
        });
        mockedAxiosGet.mockRejectedValueOnce(netErr);
        vi.spyOn(axios, "isAxiosError").mockReturnValueOnce(true);

        const result = await probeBackendNow();

        expect(result).toBe(false);
        expect(isBackendKnownUnreachable()).toBe(true);
    });

    it("transitions back to ONLINE when probe succeeds after being offline", async () => {
        // First probe: backend is down
        const networkError = Object.assign(new Error("ECONNREFUSED"), {
            isAxiosError: true,
            response: undefined,
        });
        mockedAxiosGet.mockRejectedValueOnce(networkError);
        vi.spyOn(axios, "isAxiosError").mockReturnValueOnce(true);
        await probeBackendNow();
        expect(isBackendKnownUnreachable()).toBe(true);

        // Second probe: backend is back
        mockedAxiosGet.mockResolvedValueOnce({ status: 200, data: { status: "ok" } });
        await probeBackendNow();

        expect(isBackendReachable()).toBe(true);
        expect(isBackendKnownUnreachable()).toBe(false);
    });
});

describe("startBackendHeartbeat", () => {
    beforeEach(() => {
        markBackendOnline();
        vi.clearAllMocks();
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it("calls probeBackendNow on each interval tick", async () => {
        mockedAxiosGet.mockResolvedValue({ status: 200, data: { status: "ok" } });

        const cleanup = startBackendHeartbeat(5_000);

        // Advance time past 3 intervals
        await vi.advanceTimersByTimeAsync(15_001);

        // Should have been called at 5s, 10s, 15s = 3 times
        expect(mockedAxiosGet).toHaveBeenCalledTimes(3);

        cleanup();
    });

    it("stops calling probe after cleanup is called", async () => {
        mockedAxiosGet.mockResolvedValue({ status: 200, data: { status: "ok" } });

        const cleanup = startBackendHeartbeat(5_000);
        await vi.advanceTimersByTimeAsync(5_001); // 1 tick
        cleanup();
        await vi.advanceTimersByTimeAsync(15_000); // 3 more ticks (skipped)

        expect(mockedAxiosGet).toHaveBeenCalledTimes(1);
    });

    it("detects reconnection during heartbeat and marks backend online", async () => {
        // Start offline
        const networkError = Object.assign(new Error("ECONNREFUSED"), {
            isAxiosError: true,
            response: undefined,
        });
        mockedAxiosGet
            .mockRejectedValueOnce(networkError)      // tick 1: still down
            .mockResolvedValueOnce({ status: 200 });   // tick 2: back online

        vi.spyOn(axios, "isAxiosError")
            .mockReturnValueOnce(true)
            .mockReturnValueOnce(false);

        const cleanup = startBackendHeartbeat(5_000);

        await vi.advanceTimersByTimeAsync(5_001); // tick 1 — offline
        expect(isBackendKnownUnreachable()).toBe(true);

        await vi.advanceTimersByTimeAsync(5_001); // tick 2 — back online
        expect(isBackendReachable()).toBe(true);

        cleanup();
    });
});
