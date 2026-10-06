/**
 * Deterministic event id for a loyalty change — the device half.
 *
 * MUST stay identical to the Python implementation at
 * `backend.laso/app/services/sync/eventlog/loyalty_event_id.py`. The two are
 * pinned to the same vectors by `test_loyalty_event_id_vectors.py` and
 * `loyaltyEventId.spec.ts`.
 *
 * Formula (both runtimes):
 *   sha256("customer_loyalty_changed|<customer_id>|<sale_id>|<direction>")
 *     -> first 26 characters, uppercased
 *
 * 26 uppercase hex characters is what `EventEnvelope` accepts (`ULID_LENGTH`).
 *
 * The device needs this because an offline sale earns its points on the SERVER
 * when it syncs: the server derives the id and the device must be able to
 * recognise the resulting event as the server's answer to its own sale, rather
 * than applying it a second time on top of whatever it guessed locally.
 *
 * `direction` is in the seed so a sale's earn and its refund cannot collide. If
 * they did, the refund would be swallowed as a duplicate earn and the customer
 * would keep the points they just returned.
 */

/** Mirrors ULID_LENGTH in the backend's `app/schemas/event_envelope.py`. */
const ULID_LENGTH = 26;

/** Exported for the cross-runtime vector test. */
export const ULID_LENGTH_SHOULD_BE = ULID_LENGTH;

export const LOYALTY_EVENT_TYPE = "customer_loyalty_changed";

export const LOYALTY_DIRECTION_EARN = "earn";
export const LOYALTY_DIRECTION_REFUND = "refund";
export const LOYALTY_DIRECTIONS = [
    LOYALTY_DIRECTION_EARN,
    LOYALTY_DIRECTION_REFUND,
] as const;

export type LoyaltyDirection =
    (typeof LOYALTY_DIRECTIONS)[number];

/** The one event id for the loyalty change `saleId` made to `customerId`. */
export async function customerLoyaltyChangedEventId(
    customerId: string,
    saleId: string,
    direction: LoyaltyDirection,
): Promise<string> {
    if (!LOYALTY_DIRECTIONS.includes(direction)) {
        throw new Error(
            `direction must be one of ${LOYALTY_DIRECTIONS.join(", ")}, got ${direction}`,
        );
    }
    const seed = `${LOYALTY_EVENT_TYPE}|${customerId}|${saleId}|${direction}`;
    const digest = await sha256Hex(seed);
    return digest.slice(0, ULID_LENGTH).toUpperCase();
}

async function sha256Hex(input: string): Promise<string> {
    const subtle = globalThis.crypto?.subtle;
    if (subtle) {
        const bytes = new TextEncoder().encode(input);
        const buf = await subtle.digest("SHA-256", bytes);
        return [...new Uint8Array(buf)]
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");
    }
    // Node fallback (tests, scripts). `node:crypto` is a builtin, so this does
    // not add a dependency or pull anything into the browser bundle unless the
    // branch is actually reached — and in the browser it never is.
    const { createHash } = await import("node:crypto");
    return createHash("sha256").update(input, "utf8").digest("hex");
}
