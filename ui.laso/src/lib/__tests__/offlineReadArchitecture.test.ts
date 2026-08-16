/** @vitest-environment jsdom */
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
    isBackendReachable,
    isBackendKnownUnreachable,
    isOfflineOrUnreachable,
    markBackendOffline,
    markBackendOnline,
} from "@/api/client";
import { withTimeout } from "@/lib/withTimeout";

describe("Offline Read Architecture & Reachability Guard", () => {
    beforeEach(() => {
        markBackendOnline();
    });

    it("defaults to online and transitions to offline upon markBackendOffline()", () => {
        expect(isBackendReachable()).toBe(true);
        expect(isBackendKnownUnreachable()).toBe(false);
        expect(isOfflineOrUnreachable()).toBe(false);

        markBackendOffline();

        expect(isBackendReachable()).toBe(false);
        expect(isBackendKnownUnreachable()).toBe(true);
        expect(isOfflineOrUnreachable()).toBe(true);

        markBackendOnline();
        expect(isBackendReachable()).toBe(true);
        expect(isBackendKnownUnreachable()).toBe(false);
    });

    it("withTimeout immediately resolves cacheFn without calling serverFn when offline", async () => {
        markBackendOffline();

        const serverFn = vi.fn().mockResolvedValue("remote data");
        const cacheFn = vi.fn().mockResolvedValue("local sqlite data");

        const result = await withTimeout(serverFn, cacheFn, {
            timeoutMs: 5000,
            dataKey: "test:key",
        });

        expect(serverFn).not.toHaveBeenCalled();
        expect(cacheFn).toHaveBeenCalledTimes(1);
        expect(result.data).toBe("local sqlite data");
        expect(result.isFromCache).toBe(true);
    });

    it("withTimeout calls serverFn when online and returns remote data", async () => {
        markBackendOnline();

        const serverFn = vi.fn().mockResolvedValue("remote fresh data");
        const cacheFn = vi.fn().mockResolvedValue("local stale data");

        const result = await withTimeout(serverFn, cacheFn, {
            timeoutMs: 5000,
            dataKey: "test:key:2",
        });

        expect(serverFn).toHaveBeenCalledTimes(1);
        expect(cacheFn).not.toHaveBeenCalled();
        expect(result.data).toBe("remote fresh data");
        expect(result.isFromCache).toBe(false);
    });

    it("withTimeout falls back to cacheFn when serverFn fails", async () => {
        markBackendOnline();

        const serverFn = vi.fn().mockRejectedValue(new Error("Network Error"));
        const cacheFn = vi.fn().mockResolvedValue("local fallback data");

        const result = await withTimeout(serverFn, cacheFn, {
            timeoutMs: 5000,
            dataKey: "test:key:3",
        });

        expect(serverFn).toHaveBeenCalledTimes(1);
        expect(cacheFn).toHaveBeenCalledTimes(1);
        expect(result.data).toBe("local fallback data");
        expect(result.isFromCache).toBe(true);
    });
});
