/**
 * eventEnvelopeGoldenVectors.test.ts
 * ====================================
 * Parity test: TypeScript canonicalJson + computeHashSelf MUST produce
 * byte-identical output to the Python side for the same inputs.
 *
 * Vectors are loaded from the JSON file emitted by:
 *   backend.laso/tests/unit/test_event_envelope_golden_vectors.py
 *
 * If any hash mismatches, the Python and TypeScript implementations have
 * diverged — fix canonical_json/_canonicalValue before merging.
 *
 * NOTE: computeHashSelf uses crypto.subtle which is async. Vitest runs in
 * Node which provides globalThis.crypto.subtle from Node 18+.
 */

import { describe, expect, it } from "vitest";
import { canonicalJson, computeHashSelf } from "@/lib/eventEnvelope";
import type { EventEnvelope } from "@/lib/eventEnvelope";

// Load the golden vectors emitted by the Python test suite.
import vectors from "../../../../docs/plans/phase-0-artifacts/envelope-golden-vectors.json";

// ── Canonical JSON unit tests ──────────────────────────────────────────────

describe("canonicalJson", () => {
  it("sorts object keys alphabetically", () => {
    const a = canonicalJson({ z: 1, a: 2, m: 3 });
    const b = canonicalJson({ a: 2, m: 3, z: 1 });
    expect(a).toBe(b);
    expect(a).toBe('{"a":2,"m":3,"z":1}');
  });

  it("sorts nested object keys", () => {
    const result = canonicalJson({ outer: { z: 1, a: 2 } });
    expect(result).toBe('{"outer":{"a":2,"z":1}}');
  });

  it("preserves array element order (does not sort arrays)", () => {
    const result = canonicalJson({ arr: [3, 1, 2] });
    expect(result).toBe('{"arr":[3,1,2]}');
  });

  it("serializes null and undefined as null", () => {
    expect(canonicalJson({ x: null })).toBe('{"x":null}');
    expect(canonicalJson({ x: undefined })).toBe('{"x":null}');
  });

  it("serializes booleans", () => {
    expect(canonicalJson({ t: true, f: false })).toBe('{"f":false,"t":true}');
  });

  it("serializes numbers without trailing zeros", () => {
    expect(canonicalJson({ n: 1, m: 1.5 })).toBe('{"m":1.5,"n":1}');
  });

  it("preserves unicode characters without escaping (ensure_ascii=False parity)", () => {
    const result = canonicalJson({ s: "café — ₵" });
    // Keys and string values should be JSON-escaped for special chars but
    // unicode code points are preserved as-is (not \\uXXXX escaped).
    expect(result).toContain("café");
    expect(result).toContain("₵");
  });
});

// ── Golden vector parity tests ─────────────────────────────────────────────

describe("computeHashSelf — Python/TypeScript byte-identical parity", () => {
  for (const vector of vectors.vectors) {
    it(`matches Python hash for vector "${vector.name}"`, async () => {
      const input = vector.input as Record<string, unknown>;
      const hashPrev = input.hash_prev as string;

      // Build an envelope-like object from the vector input.
      const envelope: Omit<EventEnvelope, "hash_self"> = {
        event_id: input.event_id as string,
        aggregate_id: input.aggregate_id as string,
        aggregate_type: input.aggregate_type as EventEnvelope["aggregate_type"],
        event_type: input.event_type as string,
        schema_version: input.schema_version as number,
        payload: input.payload as Record<string, unknown>,
        dependencies: input.dependencies as string[],
        authored_at: input.authored_at as string,
        authored_by: input.authored_by as string,
        branch_id: input.branch_id as string,
        org_id: input.org_id as string,
      };

      const actual = await computeHashSelf(envelope, hashPrev);
      expect(actual).toBe(vector.expected_hash_self);
    });
  }
});
