import { describe, expect, it } from "vitest";
import {
  shouldFallbackToOfflineSaleAfterError,
  shouldUseOfflineSalePath,
} from "@/lib/offlineFallback";

describe("offline sale fallback decisions", () => {
  it("uses the offline sale path when the backend is unreachable but the browser is online", () => {
    expect(shouldUseOfflineSalePath({
      browserOnline: true,
      appOffline: false,
      backendReachable: false,
    })).toBe(true);
  });

  it("falls back after a network-class sale error updates backend reachability", () => {
    expect(shouldFallbackToOfflineSaleAfterError({
      offlineError: true,
      browserOnline: true,
      appOffline: false,
      backendReachable: false,
      backendReachableBeforeAttempt: true,
    })).toBe(true);
  });

  it("does not fall back for validation errors while online", () => {
    expect(shouldFallbackToOfflineSaleAfterError({
      offlineError: false,
      browserOnline: true,
      appOffline: false,
      backendReachable: true,
      backendReachableBeforeAttempt: true,
    })).toBe(false);
  });
});
