/**
 * Phase 0 artifact: canonical event-envelope types (TypeScript).
 *
 * Reference implementation for the event-sourced sync spine defined in
 * ADRs 0006, 0007, 0008. Phase 2 relocates this file to
 * `ui.laso/src/lib/sync/eventEnvelope.ts` and wires it into the new
 * sync engine.
 *
 * The types here MUST be kept in lock-step with `event_envelope.py` in
 * this directory. Canonical hashing (bottom of file) must produce
 * byte-identical output to the Python side for the same input — a
 * shared golden-vector test suite enforces this.
 *
 * Related ADRs:
 *   0006 — Event-Sourced Sync Spine
 *   0007 — Event Schema, Hash Chain, and Dependency Semantics
 *   0008 — Sync Push/Pull Endpoint Contracts
 */

// ── Constants ────────────────────────────────────────────────────────────────

export const ULID_LENGTH = 26;
export const SHA256_HEX_LENGTH = 64;
export const GENESIS_HASH = "0".repeat(SHA256_HEX_LENGTH);


// ── Enums / string unions ────────────────────────────────────────────────────

export type AggregateType =
    | "sale"
    | "prescription"
    | "customer"
    | "stock"
    | "stock_transfer";

export type EventStatus =
    | "accepted"
    | "accepted_deferred"
    | "rejected_permanent"
    | "rejected_transient";


// ── Envelope ─────────────────────────────────────────────────────────────────

/**
 * Immutable event envelope. Client sets all fields except `seq`,
 * `received_at`, and `hash_prev`; server sets those at accept-time.
 *
 * See ADR 0007 for field semantics and hash-chain rules.
 */
export interface EventEnvelope {
    event_id: string;              // ULID, 26 chars
    aggregate_id: string;          // UUID string
    aggregate_type: AggregateType;
    event_type: string;
    schema_version: number;
    payload: Record<string, unknown>;
    dependencies: string[];        // ULIDs
    authored_at: string;           // ISO 8601 with 'Z' suffix
    authored_by: string;           // UUID string
    branch_id: string;             // UUID string
    org_id: string;                // UUID string
    hash_self: string;             // SHA-256 hex, 64 chars

    // Server-assigned. Undefined on client-outbound; populated on
    // server-inbound / pull.
    hash_prev?: string;
    seq?: number;
    received_at?: string;
}


// ── Push endpoint contracts (ADR 0008) ───────────────────────────────────────

export interface EventPushRequest {
    branch_id: string;
    client_clock: string;
    events: EventEnvelope[];       // ≤ 500 per request
}

export interface EventPushResult {
    event_id: string;
    status: EventStatus;
    seq?: number;
    received_at?: string;
    error_code?: string;
    error_message?: string;
    pending_on?: string[];         // only set when status === "accepted_deferred"
}

export interface EventPushResponse {
    server_clock: string;
    results: EventPushResult[];
    next_pull_seq: number;
}


// ── Pull endpoint contracts (ADR 0008) ───────────────────────────────────────

export interface EventPullRequest {
    after_seq: number;
    limit: number;                 // ≤ 500
    aggregate_types: AggregateType[];
    branch_ids?: string[];
}

export interface EventPullResponse {
    server_clock: string;
    events: EventEnvelope[];
    has_more: boolean;
    next_after_seq: number;
}


// ── Canonical hashing (ADR 0007) ─────────────────────────────────────────────

const HASH_FIELDS = [
    "event_id",
    "aggregate_id",
    "aggregate_type",
    "event_type",
    "schema_version",
    "payload",
    "dependencies",
    "authored_at",
    "authored_by",
    "branch_id",
    "org_id",
    "hash_prev",
] as const;

/**
 * Byte-identical canonical JSON encoder shared with the Python server.
 * Keys sorted, no whitespace, UTF-8, numbers in shortest form. Strings
 * pass through as-is — envelope fields that carry datetime/UUID values
 * MUST already be in canonical string form before reaching this function
 * (see the note in `computeHashSelf` below).
 *
 * The Python-side implementation in event_envelope.py must produce
 * byte-identical output for the same input. A shared golden-vector test
 * suite enforces this.
 */
export function canonicalJson(value: unknown): Uint8Array {
    return new TextEncoder().encode(stableStringify(value));
}

function stableStringify(value: unknown): string {
    if (value === null) return "null";
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") {
        if (!Number.isFinite(value)) {
            throw new TypeError(`canonicalJson: non-finite number ${value}`);
        }
        // JSON.stringify emits shortest form for integers and finite floats.
        return JSON.stringify(value);
    }
    if (typeof value === "string") return JSON.stringify(value);
    if (Array.isArray(value)) {
        return "[" + value.map(stableStringify).join(",") + "]";
    }
    if (typeof value === "object") {
        const keys = Object.keys(value as Record<string, unknown>).sort();
        return "{" + keys
            .map(k => JSON.stringify(k) + ":" + stableStringify((value as Record<string, unknown>)[k]))
            .join(",") + "}";
    }
    throw new TypeError(`canonicalJson: cannot serialize ${typeof value}`);
}

/**
 * SHA-256 over the canonical serialization of the envelope with the
 * given hash_prev substituted in. Used client-side before outbox write;
 * the server re-runs the same computation with the real hash_prev from
 * its log tail to verify.
 *
 * IMPORTANT: `envelope.authored_at` MUST already be an ISO 8601 string
 * with 'Z' suffix (millisecond precision). `authored_by`, `branch_id`,
 * `org_id`, `aggregate_id` MUST be lower-case UUID hex-with-hyphens.
 * These are the on-the-wire forms; the Python side's canonical_json()
 * default handler enforces the same normalization.
 */
export async function computeHashSelf(
    envelope: Omit<EventEnvelope, "hash_self" | "seq" | "received_at">,
    hashPrev: string,
): Promise<string> {
    const body: Record<string, unknown> = {};
    for (const field of HASH_FIELDS) {
        body[field] = field === "hash_prev"
            ? hashPrev
            : (envelope as unknown as Record<string, unknown>)[field];
    }
    const bytes = canonicalJson(body);
    // Copy into a fresh ArrayBuffer-backed Uint8Array so the type is
    // unambiguously BufferSource (not SharedArrayBuffer-backed).
    const buf = new Uint8Array(bytes.length);
    buf.set(bytes);
    const digest = await crypto.subtle.digest("SHA-256", buf.buffer);
    return bufferToHex(digest);
}

function bufferToHex(buf: ArrayBuffer): string {
    const bytes = new Uint8Array(buf);
    let out = "";
    for (let i = 0; i < bytes.length; i++) {
        const h = bytes[i].toString(16);
        out += h.length === 1 ? "0" + h : h;
    }
    return out;
}
