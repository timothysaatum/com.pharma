/**
 * Cross-runtime vectors for the loyalty event id — the device half.
 *
 * These four vectors are mirrored verbatim in
 * `backend.laso/tests/unit/test_loyalty_event_id_vectors.py`. If either side
 * changes the formula, BOTH suites fail.
 */
import { describe, expect, it } from "vitest";
import {
    LOYALTY_DIRECTION_EARN,
    LOYALTY_DIRECTION_REFUND,
    LOYALTY_EVENT_TYPE,
    ULID_LENGTH_SHOULD_BE,
    customerLoyaltyChangedEventId,
} from "@/lib/loyaltyEventId";

const CUST = "5823ef27-51ce-4431-9adc-e81f9b3f949f";
const CUST2 = "0615fb0e-416f-4ecb-bca1-d4839446cb36";
const SALE = "APO1-20261005-0001";
const SALE2 = "99999999-8888-7777-6666-555555555555";

const VECTORS: Array<[string, string, "earn" | "refund", string]> = [
    ["5823ef27-51ce-4431-9adc-e81f9b3f949f", "APO1-20261005-0001", "earn",
        "EB25262A7317448712B80292AA"],
    ["5823ef27-51ce-4431-9adc-e81f9b3f949f", "APO1-20261005-0001", "refund",
        "DB76F8DFBAA917956EB45A8074"],
    ["0615fb0e-416f-4ecb-bca1-d4839446cb36", "APO1-20261005-0001", "earn",
        "831785162E51C425BCD33CB87F"],
    ["5823ef27-51ce-4431-9adc-e81f9b3f949f", "99999999-8888-7777-6666-555555555555", "earn",
        "69F3506D0779E344454F6CEF75"],
];

describe("customerLoyaltyChangedEventId", () => {
    it("is a 26-character ULID, as EventEnvelope requires", async () => {
        expect(ULID_LENGTH_SHOULD_BE).toBe(26);
        for (const [cust, sale, direction] of VECTORS) {
            expect((await customerLoyaltyChangedEventId(cust, sale, direction)).length).toBe(26);
        }
    });

    it("gives the same id for the same loyalty change, forever", async () => {
        const a = await customerLoyaltyChangedEventId(CUST, SALE, LOYALTY_DIRECTION_EARN);
        const b = await customerLoyaltyChangedEventId(CUST, SALE, LOYALTY_DIRECTION_EARN);
        expect(a).toBe(b);
    });

    it("never lets a sale's earn collide with its refund", async () => {
        const earn = await customerLoyaltyChangedEventId(CUST, SALE, LOYALTY_DIRECTION_EARN);
        const refund = await customerLoyaltyChangedEventId(CUST, SALE, LOYALTY_DIRECTION_REFUND);
        expect(earn).not.toBe(refund);
    });

    it("separates different sales and different customers", async () => {
        const base = await customerLoyaltyChangedEventId(CUST, SALE, LOYALTY_DIRECTION_EARN);
        expect(await customerLoyaltyChangedEventId(CUST, SALE2, LOYALTY_DIRECTION_EARN))
            .not.toBe(base);
        expect(await customerLoyaltyChangedEventId(CUST2, SALE, LOYALTY_DIRECTION_EARN))
            .not.toBe(base);
    });

    it("produces uppercase hex", async () => {
        for (const [cust, sale, direction] of VECTORS) {
            const eid = await customerLoyaltyChangedEventId(cust, sale, direction);
            expect(eid).toBe(eid.toUpperCase());
            expect(/^[0-9A-F]{26}$/.test(eid)).toBe(true);
        }
    });

    it("rejects an unknown direction rather than risking a collision", async () => {
        await expect(
            customerLoyaltyChangedEventId(CUST, SALE, "adjust" as never),
        ).rejects.toThrow(/direction must be one of/);
    });

    it("matches the pinned cross-runtime vectors", async () => {
        for (const [cust, sale, direction, expected] of VECTORS) {
            expect(await customerLoyaltyChangedEventId(cust, sale, direction)).toBe(expected);
        }
    });

    it("uses the event type the backend projector dispatches on", () => {
        expect(LOYALTY_EVENT_TYPE).toBe("customer_loyalty_changed");
    });
});
