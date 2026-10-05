/**
 * Pins the device's refill event id to the SAME vectors as the server.
 *
 * The backend twin is `backend.laso/tests/unit/test_refill_event_id_vectors.py`
 * and the formula is documented in both implementations. If either changes, one
 * of these two suites fails — which is the only reason the device can trust the
 * id it writes into `applied_events`.
 */
import { describe, expect, it } from "vitest";
import { prescriptionRefillUsedEventId, ULID_LENGTH_SHOULD_BE } from "../refillEventId";
import { ULID_LENGTH } from "../eventEnvelope";

const RX = "11111111-2222-3333-4444-555555555555";
const RX2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const SALE = "99999999-8888-7777-6666-555555555555";
const SALE2 = "12345678-1234-1234-1234-123456789abc";

/** Copied verbatim from tests/unit/test_refill_event_id_vectors.py. */
const VECTORS: Array<[string, string, string]> = [
    [RX, SALE, "FA5BB38695815707FDFE0DB5EA"],
    [RX2, SALE, "FFB35FB9C51ED6BA229D70C1B9"],
    [RX, SALE2, "F2E19894F0591A5DF84F072999"],
];

describe("prescriptionRefillUsedEventId", () => {
    it("matches the server vectors exactly", async () => {
        for (const [rx, sale, expected] of VECTORS) {
            expect(await prescriptionRefillUsedEventId(rx, sale)).toBe(expected);
        }
    });

    it("produces a ULID-length id, matching the envelope's own constant", async () => {
        const id = await prescriptionRefillUsedEventId(RX, SALE);
        expect(id).toHaveLength(ULID_LENGTH_SHOULD_BE);
        expect(ULID_LENGTH).toBe(ULID_LENGTH_SHOULD_BE);
    });

    it("is deterministic for the same sale", async () => {
        expect(await prescriptionRefillUsedEventId(RX, SALE)).toBe(
            await prescriptionRefillUsedEventId(RX, SALE)
        );
    });

    it("differs for a different sale on the same prescription", async () => {
        expect(await prescriptionRefillUsedEventId(RX, SALE)).not.toBe(
            await prescriptionRefillUsedEventId(RX, SALE2)
        );
    });

    it("differs for a different prescription on the same sale", async () => {
        expect(await prescriptionRefillUsedEventId(RX, SALE)).not.toBe(
            await prescriptionRefillUsedEventId(RX2, SALE)
        );
    });

    it("is uppercase hex", async () => {
        const id = await prescriptionRefillUsedEventId(RX, SALE);
        expect(id).toBe(id.toUpperCase());
        expect(/^[0-9A-F]+$/.test(id)).toBe(true);
    });
});
