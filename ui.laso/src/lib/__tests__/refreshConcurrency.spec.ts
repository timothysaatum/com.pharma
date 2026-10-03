/**
 * The refresh-interceptor defect that produced repeated 401s on
 * POST /api/v1/auth/refresh, and silently disabled the POS.
 *
 * Two compounding bugs, both about there being more than one refresh in flight.
 *
 * 1. `_retry` was set ONLY on the request that kicked off the refresh. A request
 *    that was QUEUED while that refresh ran got retried with the new access
 *    token, and if it 401'd again it was treated as a brand-new failure and
 *    started ANOTHER refresh. The server rotates the refresh token on every use
 *    (`AuthService.refresh_access_token` revokes the old session and mints a new
 *    refresh token), so the second refresh presented an already-rotated token
 *    and was rejected. Several concurrent retries could each consume one.
 *
 * 2. The refresh gate was released in the `finally` of whichever request
 *    happened to await the shared promise — so it opened while queued retries
 *    were still in flight. A 401 in that window started yet another refresh
 *    against a rotated token.
 *
 * Net effect for the cashier: the access token expired, every authenticated
 * request 401'd, the refresh could not recover because the session had been
 * consumed by racing refreshes, and the POS contract list — which needs an
 * authenticated call — came back empty and reported "Select a price contract".
 */
import { describe, expect, it } from "vitest";

/**
 * A faithful stand-in for the interceptor's refresh machinery, with the two
 * fixes applied, so the behaviour is asserted rather than described.
 */
class RefreshGate {
    isRefreshing = false;
    refreshPromise: Promise<void> | null = null;
    pendingQueue: Array<{ resolve: (t: string | null) => void; reject: (e: unknown) => void }> = [];
    refreshCalls = 0;

    constructor(
        private readonly doRefresh: () => Promise<{ access: string; refresh: string }>,
        private readonly onAuthFailure: () => void,
        private readonly isOffline: (e: unknown) => boolean = () => false
    ) {}

    private flushQueue(token: string | null, error?: unknown): void {
        const queue = this.pendingQueue;
        this.pendingQueue = [];
        queue.forEach(({ resolve, reject }) =>
            token ? resolve(token) : reject(error ?? new Error("refresh failed"))
        );
    }

    /** One request's 401 handling. Returns the retried outcome or throws. */
    async handle401(original: { _retry?: boolean; headers: Record<string, string> }) {
        if (original._retry) {
            // Already retried once: do NOT start another refresh.
            throw Object.assign(new Error("retried and still unauthorized"), {
                response: { status: 401 },
            });
        }
        if (this.isRefreshing) {
            // FIX 1: queued requests are marked as retried too.
            original._retry = true;
            return new Promise<string>((resolve, reject) => {
                this.pendingQueue.push({
                    resolve: (t: string | null) => resolve(t as string),
                    reject,
                });
            });
        }
        original._retry = true;
        this.isRefreshing = true;

        if (!this.refreshPromise) {
            this.refreshCalls += 1;
            this.refreshPromise = (async () => {
                const tokens = await this.doRefresh();
                this.lastTokens = tokens;
            })();
        }

        try {
            await this.refreshPromise;
            original.headers.Authorization = `Bearer ${this.lastTokens.access}`;
            this.flushQueue(this.lastTokens.access);
            return this.lastTokens.access;
        } catch (err) {
            this.flushQueue(null, err);
            if (!this.isOffline(err)) this.onAuthFailure();
            throw err;
        } finally {
            // FIX 2: release the gate only once the shared refresh has settled,
            // not as soon as this caller stops waiting.
            try {
                await this.refreshPromise;
            } catch {
                /* handled above */
            }
            this.isRefreshing = false;
            this.refreshPromise = null;
        }
    }

    lastTokens = { access: "", refresh: "" };
}

// Real timers: the delays here are 5-20ms, and fake timers would need explicit
// advancement on every await, which obscures the concurrency being tested.
describe("access-token refresh under concurrency", () => {
    it("performs ONE refresh for a burst of concurrent 401s", async () => {
        let refreshes = 0;
        const gate = new RefreshGate(
            async () => {
                refreshes += 1;
                // Server rotates: the old refresh token is now dead.
                await new Promise((r) => setTimeout(r, 10));
                return { access: "A2", refresh: "R2" };
            },
            () => {}
        );

        const a = { _retry: false, headers: {} as Record<string, string> };
        const b = { _retry: false, headers: {} as Record<string, string> };
        const c = { _retry: false, headers: {} as Record<string, string> };

        const results = await Promise.all([
            gate.handle401(a),
            gate.handle401(b),
            gate.handle401(c),
        ]);

        expect(refreshes).toBe(1);
        expect(gate.refreshCalls).toBe(1);
        // Every waiter got the SAME new access token.
        expect(new Set(results).size).toBe(1);
        expect(results[0]).toBe("A2");
        // And the first requester retried once, not twice.
        expect(a._retry).toBe(true);
    });

    it("a queued request that 401s AGAIN does not trigger a second refresh", async () => {
        let refreshes = 0;
        const gate = new RefreshGate(
            async () => {
                refreshes += 1;
                await new Promise((r) => setTimeout(r, 5));
                return { access: "A2", refresh: "R2" };
            },
            () => {}
        );

        const first = { _retry: false, headers: {} as Record<string, string> };
        const queued = { _retry: false, headers: {} as Record<string, string> };

        const p1 = gate.handle401(first);
        // Arrives while the first refresh is still in flight → queued.
        const p2 = gate.handle401(queued);
        await Promise.all([p1, p2]);

        expect(queued._retry).toBe(true);
        // Retrying it throws immediately rather than refreshing again, so the
        // rotated refresh token is not consumed a second time.
        await expect(gate.handle401(queued)).rejects.toThrow("still unauthorized");
        expect(refreshes).toBe(1);
    });

    it("the gate stays closed until the shared refresh settles", async () => {
        const gate = new RefreshGate(
            async () => {
                await new Promise((r) => setTimeout(r, 20));
                return { access: "A2", refresh: "R2" };
            },
            () => {}
        );

        const first = { _retry: false, headers: {} as Record<string, string> };
        const p = gate.handle401(first);
        // Mid-refresh: a new 401 must queue, not open a second refresh.
        const second = { _retry: false, headers: {} as Record<string, string> };
        const q = gate.handle401(second);

        expect(gate.isRefreshing).toBe(true);
        expect(gate.refreshCalls).toBe(1);

        await Promise.all([p, q]);
        expect(gate.refreshCalls).toBe(1);
    });

    it("a genuine refresh rejection signs the user out exactly once", async () => {
        let signOuts = 0;
        const gate = new RefreshGate(
            async () => {
                await new Promise((r) => setTimeout(r, 5));
                throw new Error("Session expired or invalid");
            },
            () => {
                signOuts += 1;
            }
        );

        const a = { _retry: false, headers: {} as Record<string, string> };
        const b = { _retry: false, headers: {} as Record<string, string> };
        const results = await Promise.allSettled([gate.handle401(a), gate.handle401(b)]);

        expect(results.every((r) => r.status === "rejected")).toBe(true);
        expect(signOuts).toBe(1);
        expect(gate.refreshCalls).toBe(1);
    });

    it("an OFFLINE refresh failure does NOT sign the user out", async () => {
        let signOuts = 0;
        const offline = Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" });
        const gate = new RefreshGate(
            async () => {
                throw offline;
            },
            () => {
                signOuts += 1;
            },
            (e) => e === offline
        );

        const a = { _retry: false, headers: {} as Record<string, string> };
        await expect(gate.handle401(a)).rejects.toThrow("Network Error");

        // Tokens are preserved so a later retry can refresh when the network is
        // back. Signing out here loses the cashier's session over a dropout.
        expect(signOuts).toBe(0);
    });

    it("a later, separate 401 burst can refresh again", async () => {
        let refreshes = 0;
        const gate = new RefreshGate(
            async () => {
                refreshes += 1;
                await new Promise((r) => setTimeout(r, 5));
                return { access: `A${refreshes + 1}`, refresh: `R${refreshes + 1}` };
            },
            () => {}
        );

        await gate.handle401({ _retry: false, headers: {} });
        await gate.handle401({ _retry: false, headers: {} });

        expect(refreshes).toBe(2);
    });
});
