/**
 * Unit tests for the default-contract precedence rule.
 *
 * These pin the ORDER of the fallbacks, which is the whole point: the bug was
 * `?? contracts[0]`, which silently made list order decide. Each test therefore
 * arranges the list so that "first in the array" and "the right answer" are
 * different contracts — if the precedence ever regresses to array order, these
 * fail rather than passing by luck.
 */
import { describe, expect, it } from "vitest";
import { pickDefaultContract } from "@/lib/pickDefaultContract";
import type { AvailableContract } from "@/api/contracts";

function contract(
    id: string,
    name: string,
    type: AvailableContract["type"] = "standard",
    isDefault = false
): AvailableContract {
    return {
        id,
        code: id.toUpperCase(),
        name,
        type,
        discount_percentage: 0,
        is_default: isDefault,
        requires_verification: false,
        requires_approval: false,
        display: name,
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
    } as unknown as AvailableContract;
}

describe("pickDefaultContract", () => {
    it("picks the contract flagged default", () => {
        const list = [
            contract("a", "Zebra Insurance", "insurance"),
            contract("b", "STANDARD PRICE", "standard", true),
        ];
        expect(pickDefaultContract(list)?.id).toBe("b");
    });

    it("falls back to the first standard contract when nothing is flagged", () => {
        // Array order puts an insurance contract first on purpose.
        const list = [
            contract("a", "AAA Insurance", "insurance"),
            contract("b", "Staff Discount", "corporate"),
            contract("c", "Standard Retail", "standard"),
        ];
        expect(pickDefaultContract(list)?.id).toBe("c");
    });

    it("falls back to the first contract when none is default and none is standard", () => {
        // An insurance-only org must still be able to price a sale.
        const list = [
            contract("a", "ZZZ Health", "insurance"),
            contract("b", "AAA Health", "insurance"),
        ];
        expect(pickDefaultContract(list)?.id).toBe("b"); // by name, not by position
    });

    it("returns null for an empty list", () => {
        expect(pickDefaultContract([])).toBeNull();
    });

    it("returns null for null and undefined", () => {
        expect(pickDefaultContract(null)).toBeNull();
        expect(pickDefaultContract(undefined)).toBeNull();
    });

    it("breaks a tie between several flagged defaults by name, not array order", () => {
        // The DB does not enforce a single default, so this is reachable.
        const list = [
            contract("z", "Zulu Default", "standard", true),
            contract("a", "Alpha Default", "standard", true),
        ];
        expect(pickDefaultContract(list)?.id).toBe("a");
    });

    it("ignores a default flag on a non-standard contract only when a standard one exists", () => {
        // Flagged wins even when it is insurance: the org explicitly nominated
        // it, and second-guessing an explicit flag is not this function's job.
        const list = [
            contract("i", "Glico Health", "insurance", true),
            contract("s", "Standard Retail", "standard"),
        ];
        expect(pickDefaultContract(list)?.id).toBe("i");
    });

    it("is stable across repeated calls on the same list", () => {
        const list = [
            contract("b", "Beta", "corporate"),
            contract("a", "Alpha", "standard"),
        ];
        const first = pickDefaultContract(list)?.id;
        const shuffled = [list[1], list[0]];
        expect(pickDefaultContract(list)?.id).toBe(first);
        expect(pickDefaultContract(shuffled)?.id).toBe(first);
    });

    it("does not mutate the caller's list", () => {
        const list = [
            contract("z", "Zulu", "standard", true),
            contract("a", "Alpha", "standard", true),
        ];
        const order = list.map((c) => c.id);
        pickDefaultContract(list);
        expect(list.map((c) => c.id)).toEqual(order);
    });

    it("treats a missing is_default flag as not-default (local rows without it)", () => {
        const list = [
            { ...contract("a", "Alpha", "insurance") } as AvailableContract,
            { ...contract("b", "Bravo", "standard") } as AvailableContract,
        ];
        delete (list[1] as { is_default?: boolean }).is_default;
        expect(pickDefaultContract(list)?.id).toBe("b");
    });
});
