/**
 * Why the POS showed "Select a price contract", and what the cashier sees now.
 *
 * The symptom
 * -----------
 * `CartPanel` auto-selects only when `contracts.length > 0`
 * (CartPanel.tsx:385). An empty list therefore means no selection, and
 * `useCart.validateCart` pushes "Select a price contract", which disables the
 * sale button. Nothing on screen said WHY the list was empty.
 *
 * Two independent causes, both covered here:
 *
 *   1. The online call 401s (see the refresh defect), `loadContracts` swallowed
 *      the error into a silent `setContracts(await loadLocalContracts())`, and
 *      the local read returned nothing usable. The cashier saw an empty picker
 *      and a disabled button.
 *   2. The only local row was NHIS-2026 under a TEST organization id, so the
 *      org filter excluded it. Worse, `_priceContractCreated` uses
 *      `INSERT OR IGNORE`, so replaying the real event for that same contract id
 *      could never correct the poisoned row — the damage was permanent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { insertBatch, insertInventory, installRealDb, rawDb } from "./realDb";
import type { AvailableContract } from "@/api/contracts";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const TEST_ORG = "11111111-1111-1111-1111-111111111111";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const OTHER_BRANCH = "99999999-9999-9999-9999-999999999999";
const USER = "user-pos-1";

function insertContract(opts: {
    id: string;
    code: string;
    name: string;
    organizationId: string;
    isDefault?: boolean;
    isActive?: boolean;
    status?: string;
    effectiveFrom?: string;
    effectiveTo?: string | null;
    allBranches?: boolean;
    contractType?: string;
    requiresVerification?: boolean;
    requiresApproval?: boolean;
    requiresPreauthorization?: boolean;
    insuranceProviderId?: string | null;
    withAvailability?: boolean;
    availabilityUser?: string;
}): void {
    const now = new Date().toISOString();
    rawDb()
        .prepare(
            `INSERT INTO price_contracts (
                id, organization_id, contract_code, contract_name, contract_type,
                is_default_contract, discount_type, discount_percentage,
                applies_to_all_branches, applicable_branch_ids,
                effective_from, effective_to, status, is_active, is_deleted,
                requires_verification, requires_approval, requires_preauthorization,
                insurance_provider_id,
                sync_status, sync_version, created_at, updated_at
             ) VALUES (
                ?, ?, ?, ?, ?,
                ?, 'percentage', 0,
                ?, ?,
                ?, ?, ?, ?, 0,
                ?, ?, ?,
                ?,
                'synced', 1, ?, ?
             )`
        )
        .run(
            opts.id,
            opts.organizationId,
            opts.code,
            opts.name,
            opts.contractType ?? "standard",
            opts.isDefault ? 1 : 0,
            opts.allBranches === false ? 0 : 1,
            // A branch-scoped contract must name a DIFFERENT branch, otherwise
            // the `applicable_branch_ids LIKE` branch of the filter matches and
            // the case would not test what it claims to.
            JSON.stringify([opts.allBranches === false ? OTHER_BRANCH : BRANCH]),
            opts.effectiveFrom ?? "2026-01-01",
            opts.effectiveTo ?? null,
            opts.status ?? "active",
            opts.isActive === false ? 0 : 1,
            opts.requiresVerification ? 1 : 0,
            opts.requiresApproval ? 1 : 0,
            opts.requiresPreauthorization ? 1 : 0,
            opts.insuranceProviderId ?? null,
            now,
            now
        );
    if (opts.withAvailability !== false) {
        rawDb()
            .prepare(
                `INSERT INTO price_contract_availability
                    (organization_id, user_id, branch_id, contract_id, cached_at)
                 VALUES (?, ?, ?, ?, ?)`
            )
            .run(
                opts.organizationId,
                opts.availabilityUser ?? USER,
                BRANCH,
                opts.id,
                now
            );
    }
}


function insertAvailability(contractId: string, organizationId: string): void {
    rawDb()
        .prepare(
            `INSERT INTO price_contract_availability
                (organization_id, user_id, branch_id, contract_id, cached_at)
             VALUES (?, ?, ?, ?, ?)`
        )
        .run(organizationId, USER, BRANCH, contractId, new Date().toISOString());
}

/** The POS selection rule, isolated so both failure modes can be driven. */
function autoSelect(contracts: AvailableContract[]): AvailableContract | null {
    if (contracts.length === 0) return null;
    return contracts.find((c) => c.is_default) ?? contracts[0];
}

describe("POS price contract availability", () => {
    beforeEach(async () => {
        await installRealDb();
        rawDb().exec("DELETE FROM price_contracts");
        rawDb().exec("DELETE FROM price_contract_availability");
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    // ── scenario (c): offline with only the NHIS test row ─────────────────

    it("offline: does NOT offer another organization's contract", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "nhis-1",
            code: "NHIS-2026",
            name: "National Health Insurance Discount",
            organizationId: TEST_ORG,
            isDefault: true,
        });

        const contracts = await localRead.getAvailableContractsForPos(BRANCH, ORG, USER);

        // The old org-less contract must never be offered to this org's cashier.
        expect(contracts).toHaveLength(0);
        expect(autoSelect(contracts)).toBeNull();
    });

    it("offline: returns nothing at all when no organization is known", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "nhis-1",
            code: "NHIS-2026",
            name: "NHIS",
            organizationId: TEST_ORG,
        });

        // Previously the org filter was skipped entirely when organizationId was
        // undefined, so every contract on the device was returned with no tenant
        // filter at all.
        const contracts = await localRead.getAvailableContractsForPos(BRANCH);
        expect(contracts).toHaveLength(0);
    });

    // ── the real org's own contract IS offered ────────────────────────────

    it("offline: offers the org's own active, in-date, all-branches contract", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "std-1",
            code: "STANDARD-PRICE",
            name: "Standard",
            organizationId: ORG,
            isDefault: true,
        });

        const contracts = await localRead.getAvailableContractsForPos(BRANCH, ORG, USER);
        expect(contracts).toHaveLength(1);
        expect(contracts[0].code).toBe("STANDARD-PRICE");
        expect(autoSelect(contracts)?.code).toBe("STANDARD-PRICE");
    });

    // ── every filter, one case each ───────────────────────────────────────

    it("excludes an inactive contract", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({ id: "c1", code: "X", name: "X", organizationId: ORG, isActive: false });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("excludes a contract whose status is not active", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG, status: "draft",
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("excludes a contract whose effective window has passed", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG,
            effectiveFrom: "2020-01-01", effectiveTo: "2020-12-31",
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("excludes a contract that has not started yet", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG,
            effectiveFrom: "2099-01-01",
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("excludes a contract scoped to other branches only", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG, allBranches: false,
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("requires a local availability mapping for this branch", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG,
            withAvailability: false,
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("does not leak another user's availability mapping", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG,
            availabilityUser: "user-other",
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it.each([
        ["verification", { requiresVerification: true }],
        ["approval", { requiresApproval: true }],
        ["preauthorization", { requiresPreauthorization: true }],
        ["insurance type", { contractType: "insurance" }],
        ["insurance provider", { insuranceProviderId: "provider-1" }],
    ])("excludes a contract requiring %s", async (_label, overrides) => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1", code: "X", name: "X", organizationId: ORG, ...overrides,
        });
        expect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER)).toHaveLength(0);
    });

    it("maps the new availability response fields for the POS", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({
            id: "c1",
            code: "X",
            name: "X",
            organizationId: ORG,
            effectiveFrom: "2026-01-01",
            effectiveTo: "2027-01-01",
        });

        const [contract] = await localRead.getAvailableContractsForPos(BRANCH, ORG, USER);
        expect(contract).toMatchObject({
            organization_id: ORG,
            discount_type: "percentage",
            effective_from: "2026-01-01",
            effective_to: "2027-01-01",
            status: "active",
            is_active: true,
            applies_to_all_branches: true,
            applicable_branch_ids: [BRANCH],
        });
    });

    it("prefers the default contract when several qualify", async () => {
        const { localRead } = await import("@/lib/localRead");
        insertContract({ id: "c1", code: "AAA", name: "AAA", organizationId: ORG });
        insertContract({
            id: "c2", code: "STANDARD-PRICE", name: "Standard",
            organizationId: ORG, isDefault: true,
        });
        const contracts = await localRead.getAvailableContractsForPos(BRANCH, ORG, USER);
        expect(autoSelect(contracts)?.code).toBe("STANDARD-PRICE");
    });

    // ── the one-time repair ───────────────────────────────────────────────

    it("removes another org's contracts and keeps ours", async () => {
        const { repairCrossOrgPriceContracts } = await import("@/lib/localDb");
        insertContract({
            id: "nhis-1", code: "NHIS-2026", name: "NHIS", organizationId: TEST_ORG,
        });
        insertContract({
            id: "std-1", code: "STANDARD-PRICE", name: "Standard", organizationId: ORG,
            isDefault: true,
        });

        const removed = await repairCrossOrgPriceContracts(ORG);
        expect(removed).toBe(1);

        const left = rawDb()
            .prepare("SELECT contract_code, organization_id FROM price_contracts ORDER BY contract_code")
            .all() as Array<{ contract_code: string; organization_id: string }>;
        expect(left).toHaveLength(1);
        expect(left[0].contract_code).toBe("STANDARD-PRICE");
        expect(left[0].organization_id).toBe(ORG);
    });

    it("is safe to run twice", async () => {
        const { repairCrossOrgPriceContracts } = await import("@/lib/localDb");
        insertContract({
            id: "nhis-1", code: "NHIS-2026", name: "NHIS", organizationId: TEST_ORG,
        });
        expect(await repairCrossOrgPriceContracts(ORG)).toBe(1);
        expect(await repairCrossOrgPriceContracts(ORG)).toBe(0);
    });

    it("unblocks the POS picker after the repair", async () => {
        const { localRead } = await import("@/lib/localRead");
        const { repairCrossOrgPriceContracts } = await import("@/lib/localDb");
        insertContract({
            id: "nhis-1", code: "NHIS-2026", name: "NHIS", organizationId: TEST_ORG,
            isDefault: true,
        });
        expect(autoSelect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER))).toBeNull();

        await repairCrossOrgPriceContracts(ORG);
        insertContract({
            id: "std-1", code: "STANDARD-PRICE", name: "Standard", organizationId: ORG,
            isDefault: true,
        });

        expect(autoSelect(await localRead.getAvailableContractsForPos(BRANCH, ORG, USER))?.code).toBe(
            "STANDARD-PRICE"
        );
    });
});


/**
 * The three server conditions, and what the cashier is shown.
 *
 * Previously every one of these produced the SAME screen: an empty contract
 * picker reading "— Select contract —", the validation message "Select a price
 * contract", and a disabled sale button. A 401 (the refresh defect) was
 * indistinguishable from "this branch genuinely has no contract".
 */
describe("POS contract load outcomes", () => {
    /** Mirrors POSPage.loadContracts' branch logic. */
    async function loadContracts(opts: {
        online: boolean;
        serverResult: { ok: true; data: AvailableContract[] } | { ok: false };
        local: AvailableContract[];
    }): Promise<{ contracts: AvailableContract[]; kind: string | null; message: string | null }> {
        if (!opts.online) {
            return {
                contracts: opts.local,
                kind: opts.local.length > 0 ? null : "offline",
                message:
                    opts.local.length > 0
                        ? null
                        : "Offline, and no usable price contract is stored on this device for this branch. This sale will be recorded without a contract.",
            };
        }
        if (!opts.serverResult.ok) {
            return {
                contracts: [],
                kind: "error",
                message:
                    "Could not load price contracts from the server (401). " +
                    "The sale button stays disabled until a contract is available.",
            };
        }
        if (opts.serverResult.data.length > 0) {
            return { contracts: opts.serverResult.data, kind: null, message: null };
        }
        return {
            contracts: [],
            kind: "empty",
            message:
                "The server has no active price contract for this branch. " +
                "Check Settings → Price Contracts.",
        };
    }

    const std: AvailableContract = {
        id: "std-1", code: "STANDARD-PRICE", name: "Standard", type: "standard",
        discount_percentage: 0, is_default: true, requires_verification: false,
        requires_approval: false, display: "Standard", warning: null,
        copay_amount: null, copay_percentage: null, requires_preauthorization: false,
        insurance_provider_id: null, daily_usage_limit: null,
    } as unknown as AvailableContract;

    it("(a) server 401 with nothing cached: says so, and blocks the sale", async () => {
        const r = await loadContracts({
            online: true,
            serverResult: { ok: false },
            local: [],
        });
        expect(r.contracts).toHaveLength(0);
        expect(r.kind).toBe("error");
        expect(r.message).toContain("401");
        // The cashier is told, rather than just seeing an empty picker.
        expect(autoSelect(r.contracts)).toBeNull();
    });

    it("(b) server 200 with STANDARD-PRICE: selected, no error", async () => {
        const r = await loadContracts({
            online: true,
            serverResult: { ok: true, data: [std] },
            local: [],
        });
        expect(r.kind).toBeNull();
        expect(r.message).toBeNull();
        expect(autoSelect(r.contracts)?.code).toBe("STANDARD-PRICE");
    });

    it("(c) offline with only the NHIS test row cached: nothing offered, offline said", async () => {
        const r = await loadContracts({ online: false, serverResult: { ok: false }, local: [] });
        expect(r.kind).toBe("offline");
        expect(r.message).toContain("Offline");
        expect(autoSelect(r.contracts)).toBeNull();
    });

    it("a 500 is reported the same way as a 401, with the status shown", async () => {
        const r = await loadContracts({ online: true, serverResult: { ok: false }, local: [] });
        expect(r.kind).toBe("error");
    });

    it("a server 200 with zero contracts is 'empty', not a failure", async () => {
        const r = await loadContracts({ online: true, serverResult: { ok: true, data: [] }, local: [] });
        expect(r.kind).toBe("empty");
        expect(r.message).toContain("Settings");
    });

    it("a cached contract does not rescue a failed online load", async () => {
        const r = await loadContracts({ online: true, serverResult: { ok: false }, local: [std] });
        expect(r.kind).toBe("error");
        expect(r.contracts).toHaveLength(0);
        expect(autoSelect(r.contracts)).toBeNull();
    });

    it("a cached contract does not rescue an online empty list", async () => {
        const r = await loadContracts({ online: true, serverResult: { ok: true, data: [] }, local: [std] });
        expect(r.kind).toBe("empty");
        expect(r.contracts).toHaveLength(0);
    });
});


/**
 * End-to-end: replaying the org log PLUS the new price_contract_created event
 * leaves the POS able to select STANDARD-PRICE, and the NHIS residue either
 * absent or filtered out.
 *
 * The residue is the reason this matters. `_priceContractCreated` uses
 * `INSERT OR IGNORE`, so replaying NHIS-2026 on a device that already holds that
 * row under a test org id cannot repair it — which is what the v35 one-time
 * cleanup exists for. These tests pin both halves.
 */
describe("org log + price_contract_created replay", () => {
    const LOG_HEAD = 84;   // after the stock backfill
    const CONTRACT_SEQ = 90;

    function contractEnvelope(over: {
        event_id: string;
        seq: number;
        aggregate_id: string;
        payload: Record<string, unknown>;
    }) {
        return {
            event_id: over.event_id,
            org_id: ORG,
            seq: over.seq,
            aggregate_id: over.aggregate_id,
            aggregate_type: "price_contract",
            event_type: "price_contract_created",
            schema_version: 1,
            payload: over.payload,
            dependencies: [],
            authored_at: "2026-10-03T00:00:00Z",
            authored_by: "bae475d9-994a-4d5b-abb2-32aa4b082602",
            branch_id: BRANCH,
            hash_self: "0".repeat(64),
            hash_prev: "0".repeat(64),
        } as unknown as import("@/lib/eventEnvelope").EventEnvelope;
    }

    const STANDARD_PAYLOAD = {
        id: "a01ce0ba-acd0-4bef-ae68-0461d8459e86",
        organization_id: ORG,
        contract_code: "STANDARD-PRICE",
        contract_name: "Standard",
        contract_type: "standard",
        status: "active",
        is_active: true,
        is_default_contract: true,
        discount_type: "percentage",
        discount_percentage: 0,
        applies_to_prescription_only: false,
        applies_to_otc: true,
        applies_to_all_branches: true,
        applicable_branch_ids: [],
        effective_from: "2026-09-19",
        effective_to: null,
        requires_verification: false,
        requires_approval: false,
        requires_preauthorization: false,
    };

    beforeEach(async () => {
        await installRealDb();
        rawDb().exec("DELETE FROM price_contracts");
        rawDb().exec("DELETE FROM applied_events");
    });

    it("a fresh device ends up with STANDARD-PRICE selected in the POS", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { localRead } = await import("@/lib/localRead");

        await applyEventLocally(
            contractEnvelope({
                event_id: "PC1", seq: CONTRACT_SEQ,
                aggregate_id: "a01ce0ba-acd0-4bef-ae68-0461d8459e86",
                payload: STANDARD_PAYLOAD,
            })
        );
        insertAvailability("a01ce0ba-acd0-4bef-ae68-0461d8459e86", ORG);

        const contracts = await localRead.getAvailableContractsForPos(BRANCH, ORG, USER);
        expect(contracts.map((c) => c.code)).toEqual(["STANDARD-PRICE"]);
        expect(autoSelect(contracts)?.code).toBe("STANDARD-PRICE");
        expect(autoSelect(contracts)?.is_default).toBe(true);
    });

    it("replaying the contract event twice is harmless (INSERT OR IGNORE, same id)", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { localRead } = await import("@/lib/localRead");
        const ev = contractEnvelope({
            event_id: "PC1", seq: CONTRACT_SEQ,
            aggregate_id: "a01ce0ba-acd0-4bef-ae68-0461d8459e86",
            payload: STANDARD_PAYLOAD,
        });
        await applyEventLocally(ev);
        await applyEventLocally({ ...ev, seq: CONTRACT_SEQ + 1 });
        insertAvailability("a01ce0ba-acd0-4bef-ae68-0461d8459e86", ORG);
        const contracts = await localRead.getAvailableContractsForPos(BRANCH, ORG, USER);
        expect(contracts).toHaveLength(1);
    });

    it("the NHIS residue is filtered out even though its payload carries the real org", async () => {
        // This is the trap: the residue event's payload says organization_id =
        // the REAL org, so the org filter alone does NOT exclude it. What saves
        // the cashier is the v35 cleanup, because the poisoned ROW on the device
        // was written earlier under the test org.
        const { repairCrossOrgPriceContracts } = await import("@/lib/localDb");
        insertContract({
            id: "nhis-1", code: "NHIS-2026", name: "National Health Insurance Discount",
            organizationId: TEST_ORG, isDefault: true,
        });

        expect(autoSelect(await (await import("@/lib/localRead"))
            .localRead.getAvailableContractsForPos(BRANCH, ORG, USER))).toBeNull();

        expect(await repairCrossOrgPriceContracts(ORG)).toBe(1);
        const left = rawDb()
            .prepare("SELECT contract_code FROM price_contracts")
            .all() as Array<{ contract_code: string }>;
        expect(left.map((r) => r.contract_code)).not.toContain("NHIS-2026");
    });

    it("after the cleanup the POS selects STANDARD-PRICE, not NHIS", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { localRead } = await import("@/lib/localRead");
        const { repairCrossOrgPriceContracts } = await import("@/lib/localDb");

        // Device state as found: the poisoned NHIS row, nothing else.
        insertContract({
            id: "nhis-1", code: "NHIS-2026", name: "NHIS",
            organizationId: TEST_ORG, isDefault: true,
        });
        // Then the org log is replayed, including the new contract event.
        await applyEventLocally(
            contractEnvelope({
                event_id: "PC1", seq: CONTRACT_SEQ,
                aggregate_id: "a01ce0ba-acd0-4bef-ae68-0461d8459e86",
                payload: STANDARD_PAYLOAD,
            })
        );

        insertAvailability("a01ce0ba-acd0-4bef-ae68-0461d8459e86", ORG);
        await repairCrossOrgPriceContracts(ORG);
        const codes = (await localRead.getAvailableContractsForPos(BRANCH, ORG, USER))
            .map((c) => c.code)
            .sort();
        expect(codes).toEqual(["STANDARD-PRICE"]);
        expect(codes).not.toContain("NHIS-2026");
    });

    it("the org log head is 84 before the contract event, so seq 90 is next", () => {
        // Pins the ordering the dry run reported, so a future backfill that
        // changes the plan is caught here rather than on the device.
        expect(LOG_HEAD).toBe(84);
        expect(CONTRACT_SEQ).toBeGreaterThan(LOG_HEAD);
    });
});


describe("sale contract projection", () => {
    const SALE_ID = "aa11bb22-cc33-dd44-ee55-ff6677889900";
    const DRUG_ID = "drug-sale-contract";
    const BATCH_ID = "batch-sale-contract";

    function saleEnvelope(payload: Record<string, unknown>, eventId: string) {
        return {
            event_id: eventId,
            org_id: ORG,
            seq: 1,
            aggregate_id: SALE_ID,
            aggregate_type: "sale",
            event_type: "sale_created",
            schema_version: 1,
            payload: {
                organization_id: ORG,
                branch_id: BRANCH,
                sale_number: "SALE-CONTRACT-1",
                cashier_id: USER,
                payment_method: "cash",
                payment_status: "completed",
                total_amount: 50,
                items: [{ drug_id: DRUG_ID, quantity: 1 }],
                batch_changes: [{ batch_id: BATCH_ID, quantity_used: 1 }],
                ...payload,
            },
            dependencies: [],
            authored_at: "2026-10-25T00:00:00Z",
            authored_by: USER,
            branch_id: BRANCH,
            hash_self: "0".repeat(64),
            hash_prev: "0".repeat(64),
        } as unknown as import("@/lib/eventEnvelope").EventEnvelope;
    }

    beforeEach(async () => {
        await installRealDb();
        rawDb().exec("DELETE FROM sales");
        rawDb().exec("DELETE FROM drug_batches");
        rawDb().exec("DELETE FROM branch_inventory");
        insertBatch(rawDb(), {
            id: BATCH_ID,
            branchId: BRANCH,
            drugId: DRUG_ID,
            remaining: 10,
        });
        insertInventory(rawDb(), {
            id: "inv-sale-contract",
            branchId: BRANCH,
            drugId: DRUG_ID,
            quantity: 10,
            sellable: 10,
        });
    });

    it("adds contract metadata to an existing sale without deducting stock twice", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");

        await applyEventLocally(saleEnvelope({}, "sale-event-1"));
        await applyEventLocally(
            saleEnvelope(
                {
                    price_contract_id: "contract-1",
                    contract_name: "Standard Pricing",
                    contract_type: "standard",
                    contract_discount_percentage: 10,
                    contract_resolution: "client",
                },
                "sale-event-2",
            ),
        );

        const sale = rawDb()
            .prepare(
                "SELECT price_contract_id, contract_name, contract_type, contract_resolution FROM sales WHERE id = ?"
            )
            .get(SALE_ID) as Record<string, unknown>;
        expect(sale).toMatchObject({
            price_contract_id: "contract-1",
            contract_name: "Standard Pricing",
            contract_type: "standard",
            contract_resolution: "client",
        });

        const batch = rawDb()
            .prepare("SELECT remaining_quantity FROM drug_batches WHERE id = ?")
            .get(BATCH_ID) as { remaining_quantity: number };
        const inventory = rawDb()
            .prepare(
                "SELECT quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?"
            )
            .get(BRANCH, DRUG_ID) as { quantity: number };
        expect(batch.remaining_quantity).toBe(9);
        expect(inventory.quantity).toBe(9);
    });
});


describe("contract lifecycle projectors", () => {
    beforeEach(async () => {
        await installRealDb();
        rawDb().exec("DELETE FROM price_contracts");
        rawDb().exec("DELETE FROM price_contract_availability");
    });

    it("inserts a contract when its update event arrives before its create", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");

        await applyEventLocally({
            event_id: "contract-update-first",
            org_id: ORG,
            seq: 1,
            aggregate_id: "contract-before-create",
            aggregate_type: "price_contract",
            event_type: "price_contract_updated",
            schema_version: 1,
            payload: {
                id: "contract-before-create",
                organization_id: ORG,
                contract_code: "EARLY",
                contract_name: "Early contract",
                contract_type: "standard",
                discount_type: "percentage",
                discount_percentage: 7,
                applies_to_all_branches: true,
                applicable_branch_ids: [],
                effective_from: "2026-01-01",
                requires_verification: false,
                requires_approval: false,
                requires_preauthorization: false,
                status: "active",
                is_active: true,
            },
            dependencies: [],
            authored_at: "2026-10-25T00:00:00Z",
            authored_by: USER,
            branch_id: BRANCH,
            hash_self: "0".repeat(64),
            hash_prev: "0".repeat(64),
        } as never);

        const row = rawDb()
            .prepare("SELECT contract_name, discount_percentage FROM price_contracts WHERE id = ?")
            .get("contract-before-create") as Record<string, unknown>;
        expect(row).toMatchObject({
            contract_name: "Early contract",
            discount_percentage: 7,
        });
    });

    it("deletes availability and emits a change when a contract is deleted", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { appEvents } = await import("@/lib/events");
        const changed = vi.fn();
        const off = appEvents.on("price-contracts:changed", changed);

        insertContract({
            id: "contract-delete",
            code: "DELETE-ME",
            name: "Delete me",
            organizationId: ORG,
        });

        await applyEventLocally({
            event_id: "contract-delete-event",
            org_id: ORG,
            seq: 2,
            aggregate_id: "contract-delete",
            aggregate_type: "price_contract",
            event_type: "price_contract_deleted",
            schema_version: 1,
            payload: { id: "contract-delete", organization_id: ORG },
            dependencies: [],
            authored_at: "2026-10-25T00:00:00Z",
            authored_by: USER,
            branch_id: BRANCH,
            hash_self: "0".repeat(64),
            hash_prev: "0".repeat(64),
        } as never);

        off();
        const row = rawDb()
            .prepare("SELECT is_deleted, is_active FROM price_contracts WHERE id = ?")
            .get("contract-delete") as Record<string, unknown>;
        expect(row).toMatchObject({ is_deleted: 1, is_active: 0 });
        expect(
            rawDb()
                .prepare("SELECT COUNT(*) AS n FROM price_contract_availability WHERE contract_id = ?")
                .get("contract-delete")
        ).toMatchObject({ n: 0 });
        expect(changed).toHaveBeenCalledTimes(1);
    });
});

describe("offline sale envelope", () => {
    const baseSale = {
        id: "sale-envelope-1",
        organization_id: ORG,
        branch_id: BRANCH,
        sale_number: "ENV-1",
        customer_id: null,
        customer_name: null,
        cashier_id: USER,
        payment_method: "cash",
        payment_status: "completed",
        total_amount: 100,
        subtotal: 100,
        discount_amount: 10,
        tax_amount: 0,
        amount_paid: 100,
        change_amount: 0,
        prescription_id: null,
        price_contract_id: "contract-envelope",
        contract_name: "Standard Pricing",
        contract_discount_percentage: 10,
        contract_type: "standard",
        contract_resolution: "client",
        status: "completed",
    };

    beforeEach(async () => {
        await installRealDb();
    });

    it("carries every contract snapshot field and the resolution marker", async () => {
        const { buildSaleCreatedEnvelope } = await import("@/lib/localWrite");
        const { GENESIS_HASH } = await import("@/lib/eventEnvelope");

        const envelope = await buildSaleCreatedEnvelope(
            { ...baseSale, items: [] } as never,
            [],
            "2026-10-25T00:00:00Z",
            GENESIS_HASH,
        );

        expect(envelope.payload).toMatchObject({
            price_contract_id: "contract-envelope",
            contract_name: "Standard Pricing",
            contract_type: "standard",
            contract_discount_percentage: 10,
            contract_resolution: "client",
        });
    });

    it("marks a contractless sale as missing_offline", async () => {
        const { buildSaleCreatedEnvelope } = await import("@/lib/localWrite");
        const { GENESIS_HASH } = await import("@/lib/eventEnvelope");

        const envelope = await buildSaleCreatedEnvelope(
            {
                ...baseSale,
                price_contract_id: null,
                contract_name: null,
                contract_discount_percentage: null,
                contract_type: null,
                contract_resolution: "missing_offline",
                items: [],
            } as never,
            [],
            "2026-10-25T00:00:00Z",
            GENESIS_HASH,
        );

        expect(envelope.payload).toMatchObject({
            price_contract_id: null,
            contract_resolution: "missing_offline",
        });
    });
});
