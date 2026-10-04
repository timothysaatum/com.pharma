/**
 * Deterministic event id for a prescription refill — the device half.
 *
 * MUST stay identical to the Python implementation at
 * `backend.laso/app/services/sync/eventlog/refill_event_id.py`. The server
 * derives the id for the `prescription_refill_used` it emits when an offline
 * sale syncs, and this device records that same string in `applied_events` so
 * `_prescriptionRefillUsed` skips the echo of its own dispense. If the two
 * formulas drift, an offline device decrements twice.
 *
 * Formula (both runtimes):
 *   sha256("prescription_refill_used|<prescription_id>|<sale_id>")
 *     -> first 26 characters, uppercased
 *
 * 26 uppercase hex characters is what `EventEnvelope` accepts
 * (`ULID_LENGTH`, `eventEnvelope.ts`), and what the server's
 * `deterministic_event_id` already produces.
 *
 * Why derived and not random: idempotency on the server rests solely on
 * `event_log`'s primary key `(org_id, event_id)`. One dispense must therefore
 * produce exactly one id, forever.
 */

/** Mirrors ULID_LENGTH in the backend's `app/schemas/event_envelope.py`. */
const ULID_LENGTH = 26;

/** Exported for the cross-runtime vector test. */
export const ULID_LENGTH_SHOULD_BE = ULID_LENGTH;

export const RX_REFILL_EVENT_TYPE = "prescription_refill_used";

/**
 * The one event id for the refill consumed by `saleId` on `prescriptionId`.
 *
 * Async because it needs a SHA-256 implementation. In a Tauri build
 * `crypto.subtle` is available; under Node/vitest we fall back to `node:crypto`
 * so the same function is testable in both places.
 */
export async function prescriptionRefillUsedEventId(
    prescriptionId: string,
    saleId: string
): Promise<string> {
    const seed = `${RX_REFILL_EVENT_TYPE}|${prescriptionId}|${saleId}`;
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
