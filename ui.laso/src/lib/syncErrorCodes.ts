/**
 * Shared sync error codes
 * ========================
 * Structured error codes on `PushResult.error_code`, used to classify a
 * push failure as retryable-without-penalty (e.g. a dependency row — a
 * prescription or customer — hasn't synced yet) versus a genuine failure
 * that should count against the record's retry budget and eventually
 * dead-letter.
 *
 * These values are the actual wire contract with the backend — kept in
 * sync manually with backend.laso/app/schemas/sync_error_codes.py. Do not
 * rename or remove a value without updating both sides.
 *
 * Replaces matching prose substrings out of `PushResult.error`: the old
 * code checked for a string the backend never emitted ("Prescription not
 * yet synced"), and the one substring that did work ("is not yet synced")
 * was an untyped, unversioned contract that any backend wording change
 * could silently break in either direction. See
 * docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md,
 * finding S4.
 */

/**
 * A record this push depends on (e.g. the prescription a sale references,
 * or the customer a prescription references) hasn't reached the server
 * yet. The client should retry without incrementing its attempt counter.
 */
export const SYNC_ERROR_DEPENDENCY_NOT_SYNCED = "dependency_not_synced";

/**
 * This row's CRR merge was rejected by validation on an earlier push, with
 * no authoritative Postgres row to restore in its place. Resending the
 * identical local crsql_changes bytes can never resolve differently — the
 * client must stop resending this specific row (suppress it from all
 * future push batches) rather than let it block every other row forever,
 * and should surface it to the user as needing manual review.
 */
export const SYNC_ERROR_PERMANENTLY_REJECTED = "permanently_rejected";
