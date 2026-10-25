import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "./realDb";
import { contractsApi, type AvailableContract } from "@/api/contracts";
import { branchApi } from "@/api/branches";
import {
    getPriceContractCacheInfo,
    replacePriceContractAvailability,
} from "@/lib/priceContractStore";

const ORG = "org-cache-1";
const USER = "user-cache-1";
const OTHER_USER = "user-cache-2";
const BRANCH_A = "branch-cache-a";
const BRANCH_B = "branch-cache-b";

function contract(id: string, overrides: Partial<AvailableContract> = {}): AvailableContract {
    return {
        id,
        code: id.toUpperCase(),
        name: `Contract ${id}`,
        type: "standard",
        discount_percentage: 0,
        is_default: false,
        requires_verification: false,
        requires_approval: false,
        display: `Contract ${id}`,
        warning: null,
        copay_amount: null,
        copay_percentage: null,
        requires_preauthorization: false,
        insurance_provider_id: null,
        daily_usage_limit: null,
        per_customer_usage_limit: null,
        applies_to_prescription_only: false,
        applies_to_otc: true,
        minimum_purchase_amount: null,
        maximum_purchase_amount: null,
        ...overrides,
    };
}

function availabilityRows(userId = USER): Array<{ branch_id: string; contract_id: string }> {
    return rawDb()
        .prepare(
            "SELECT branch_id, contract_id FROM price_contract_availability WHERE user_id = ? ORDER BY branch_id, contract_id"
        )
        .all(userId) as Array<{ branch_id: string; contract_id: string }>;
}

function contractRow(id: string): Record<string, unknown> | undefined {
    return rawDb()
        .prepare("SELECT * FROM price_contracts WHERE id = ?")
        .get(id) as Record<string, unknown> | undefined;
}

describe("replacePriceContractAvailability", () => {
    beforeEach(async () => {
        await installRealDb();
        rawDb().exec("DELETE FROM price_contract_availability");
        rawDb().exec("DELETE FROM price_contracts");
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("branch scope replaces only that branch and upserts the contract", async () => {
        const db = await (await import("@/lib/localDb")).getDb();
        await replacePriceContractAvailability(db, {
            scope: "user",
            organizationId: ORG,
            userId: USER,
            branches: [
                { branchId: BRANCH_A, contracts: [contract("old-a")] },
                { branchId: BRANCH_B, contracts: [contract("old-b")] },
            ],
            cachedAt: "2026-10-25T00:00:00Z",
        });

        await replacePriceContractAvailability(db, {
            scope: "branch",
            organizationId: ORG,
            userId: USER,
            branchId: BRANCH_B,
            branches: [{ branchId: BRANCH_B, contracts: [contract("new-b")] }],
            cachedAt: "2026-10-25T01:00:00Z",
        });

        expect(availabilityRows()).toEqual([
            { branch_id: BRANCH_A, contract_id: "old-a" },
            { branch_id: BRANCH_B, contract_id: "new-b" },
        ]);
        expect(contractRow("old-a")).toBeTruthy();
        expect(contractRow("new-b")).toBeTruthy();
        expect(contractRow("old-b")).toBeTruthy();
        expect(String(contractRow("new-b")?.effective_from)).toHaveLength(10);
    });

    it("user scope replaces all branches for that user and leaves other users alone", async () => {
        const db = await (await import("@/lib/localDb")).getDb();
        await replacePriceContractAvailability(db, {
            scope: "user",
            organizationId: ORG,
            userId: OTHER_USER,
            branches: [{ branchId: BRANCH_A, contracts: [contract("other-user")] }],
        });
        await replacePriceContractAvailability(db, {
            scope: "user",
            organizationId: ORG,
            userId: USER,
            branches: [
                { branchId: BRANCH_A, contracts: [contract("a-1")] },
                { branchId: BRANCH_B, contracts: [contract("b-1")] },
            ],
        });

        await replacePriceContractAvailability(db, {
            scope: "user",
            organizationId: ORG,
            userId: USER,
            branches: [{ branchId: BRANCH_A, contracts: [contract("a-2")] }],
        });

        expect(availabilityRows(USER)).toEqual([
            { branch_id: BRANCH_A, contract_id: "a-2" },
        ]);
        expect(availabilityRows(OTHER_USER)).toEqual([
            { branch_id: BRANCH_A, contract_id: "other-user" },
        ]);
    });

    it("returns the count and newest cached_at for the user", async () => {
        const db = await (await import("@/lib/localDb")).getDb();
        await replacePriceContractAvailability(db, {
            scope: "user",
            organizationId: ORG,
            userId: USER,
            branches: [
                { branchId: BRANCH_A, contracts: [contract("a")] },
                { branchId: BRANCH_B, contracts: [contract("b")] },
            ],
            cachedAt: "2026-10-25T00:00:00Z",
        });

        expect(await getPriceContractCacheInfo(db, ORG, USER)).toEqual({
            count: 2,
            newest: "2026-10-25T00:00:00Z",
        });
    });
});

async function hydrate(context: Parameters<typeof import("@/lib/contractCache")["hydratePriceContractAvailability"]>[0]) {
    const { hydratePriceContractAvailability } = await import("@/lib/contractCache");
    return hydratePriceContractAvailability(context);
}

describe("hydratePriceContractAvailability", () => {
    beforeEach(async () => {
        await installRealDb();
        rawDb().exec("DELETE FROM price_contract_availability");
        rawDb().exec("DELETE FROM price_contracts");
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("hydrates the active and assigned branches, then honours the TTL", async () => {
        vi.spyOn(contractsApi, "getAvailableForPos").mockImplementation(async (branchId) => {
            if (branchId === BRANCH_A) return [contract("a")];
            return [contract("b")];
        });

        const first = await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A, BRANCH_B],
            activeBranchId: BRANCH_A,
        });
        expect(first).toMatchObject({ ok: true, skipped: false, branchCount: 2, contractCount: 2 });
        expect(availabilityRows()).toEqual([
            { branch_id: BRANCH_A, contract_id: "a" },
            { branch_id: BRANCH_B, contract_id: "b" },
        ]);

        const second = await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A, BRANCH_B],
            activeBranchId: BRANCH_A,
        });
        expect(second).toMatchObject({ ok: true, skipped: true, contractCount: 2 });
    });

    it("coalesces concurrent hydrations into one request per branch", async () => {
        const getAvailable = vi
            .spyOn(contractsApi, "getAvailableForPos")
            .mockResolvedValue([contract("a")]);

        const [first, second] = await Promise.all([
            hydrate({
                userId: USER,
                organizationId: ORG,
                assignedBranches: [BRANCH_A],
                activeBranchId: BRANCH_A,
            }),
            hydrate({
                userId: USER,
                organizationId: ORG,
                assignedBranches: [BRANCH_A],
                activeBranchId: BRANCH_A,
            }),
        ]);

        expect(first.ok).toBe(true);
        expect(second.ok).toBe(true);
        expect(getAvailable).toHaveBeenCalledTimes(1);
    });

    it("force refreshes and atomically replaces the user's mapping", async () => {
        const getAvailable = vi
            .spyOn(contractsApi, "getAvailableForPos")
            .mockResolvedValueOnce([contract("old")])
            .mockResolvedValueOnce([contract("new")]);

        await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A],
            activeBranchId: BRANCH_A,
        });
        await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A],
            activeBranchId: BRANCH_A,
            force: true,
        });

        expect(getAvailable).toHaveBeenCalledTimes(2);
        expect(availabilityRows()).toEqual([{ branch_id: BRANCH_A, contract_id: "new" }]);
    });

    it("skips a non-active branch that returns 403", async () => {
        vi.spyOn(contractsApi, "getAvailableForPos").mockImplementation(async (branchId) => {
            if (branchId === BRANCH_A) return [contract("a")];
            throw { response: { status: 403 } };
        });

        const result = await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A, BRANCH_B],
            activeBranchId: BRANCH_A,
        });

        expect(result).toMatchObject({ ok: true, branchCount: 1, contractCount: 1 });
        expect(availabilityRows()).toEqual([{ branch_id: BRANCH_A, contract_id: "a" }]);
    });

    it("keeps the previous cache when the active branch fails", async () => {
        const getAvailable = vi
            .spyOn(contractsApi, "getAvailableForPos")
            .mockResolvedValueOnce([contract("old")])
            .mockRejectedValueOnce({ response: { status: 500 } });

        await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A],
            activeBranchId: BRANCH_A,
        });
        const failed = await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A],
            activeBranchId: BRANCH_A,
            force: true,
        });

        expect(getAvailable).toHaveBeenCalledTimes(2);
        expect(failed.ok).toBe(false);
        expect(availabilityRows()).toEqual([{ branch_id: BRANCH_A, contract_id: "old" }]);
    });

    it("discovers branches with a paged list when no assignments are supplied", async () => {
        const list = vi.spyOn(branchApi, "list").mockResolvedValue({
            items: [
                { id: BRANCH_A } as never,
                { id: BRANCH_B } as never,
            ],
            total: 2,
            page: 1,
            page_size: 500,
            total_pages: 1,
            has_next: false,
            has_prev: false,
        });
        vi.spyOn(contractsApi, "getAvailableForPos").mockResolvedValue([contract("x")]);

        const result = await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [],
            activeBranchId: null,
        });

        expect(list).toHaveBeenCalledWith({ is_active: true, page: 1, page_size: 500 });
        expect(result.branchCount).toBe(2);
        expect(availabilityRows()).toHaveLength(2);
    });

    it("keeps a cached mapping when branch discovery fails", async () => {
        vi.spyOn(contractsApi, "getAvailableForPos").mockResolvedValueOnce([contract("cached")]);
        await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [BRANCH_A],
            activeBranchId: BRANCH_A,
        });

        vi.spyOn(branchApi, "list").mockRejectedValue(new Error("network down"));
        const failed = await hydrate({
            userId: USER,
            organizationId: ORG,
            assignedBranches: [],
            activeBranchId: BRANCH_A,
            force: true,
        });

        expect(failed.ok).toBe(false);
        expect(availabilityRows()).toEqual([{ branch_id: BRANCH_A, contract_id: "cached" }]);
    });
});
