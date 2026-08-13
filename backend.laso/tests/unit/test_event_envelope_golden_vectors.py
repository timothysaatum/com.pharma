"""
Golden-vector parity tests for the canonical event envelope.

The Tauri client and this FastAPI server MUST produce byte-identical
canonical JSON and hash_self for the same envelope, otherwise the
per-org hash chain diverges. This test pins a set of representative
envelopes to their expected canonical bytes and SHA-256 hashes so
that:

1. Any accidental change to Python's ``canonical_json`` or
   ``compute_hash_self`` fails loudly (this file).
2. The TypeScript mirror in ``ui.laso/src/lib/sync/eventEnvelope.ts``
   (Phase 1b) can be validated against the same JSON fixtures — see
   ``golden_vectors.json`` written alongside this test.

Do **not** change these expected values without regenerating the TS
side and doing a joint bump.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.schemas.event_envelope import (
    GENESIS_HASH,
    AggregateType,
    EventEnvelope,
    canonical_json,
    compute_hash_self,
)


# ── Fixture inputs — deterministic UUIDs, datetimes, ULIDs ───────────────────

ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGGREGATE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
AUTHORED_BY = uuid.UUID("33333333-3333-3333-3333-333333333333")
BRANCH_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
AUTHORED_AT = datetime(2026, 1, 15, 12, 34, 56, 789000, tzinfo=timezone.utc)


def _make_customer_created() -> EventEnvelope:
    return EventEnvelope(
        event_id="01HNXCVBNM1234567890ABCDEF",  # deterministic 26-char ULID
        aggregate_id=AGGREGATE_ID,
        aggregate_type=AggregateType.CUSTOMER,
        event_type="customer_created",
        schema_version=1,
        payload={
            "organization_id": str(ORG_ID),
            "customer_type": "registered",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "0501234567",
            "email": "ada@example.com",
            "loyalty_points": 0,
            "loyalty_tier": "bronze",
        },
        dependencies=[],
        authored_at=AUTHORED_AT,
        authored_by=AUTHORED_BY,
        branch_id=BRANCH_ID,
        org_id=ORG_ID,
        # hash_self is computed below and pinned in EXPECTED_HASHES.
        hash_self="0" * 64,
    )


def _make_customer_updated_with_dep() -> EventEnvelope:
    return EventEnvelope(
        event_id="01HNXCVBNM1234567890ABCDEG",
        aggregate_id=AGGREGATE_ID,
        aggregate_type=AggregateType.CUSTOMER,
        event_type="customer_updated",
        schema_version=1,
        payload={
            "first_name": "Augusta",
            "allergies": ["penicillin"],
        },
        dependencies=["01HNXCVBNM1234567890ABCDEF"],
        authored_at=AUTHORED_AT,
        authored_by=AUTHORED_BY,
        branch_id=BRANCH_ID,
        org_id=ORG_ID,
        hash_self="0" * 64,
    )


def _make_payload_with_unicode() -> EventEnvelope:
    """Non-ASCII payload — verifies UTF-8 round-tripping is stable."""
    return EventEnvelope(
        event_id="01HNXCVBNM1234567890ABCDEH",
        aggregate_id=AGGREGATE_ID,
        aggregate_type=AggregateType.CUSTOMER,
        event_type="customer_created",
        schema_version=1,
        payload={
            "organization_id": str(ORG_ID),
            "customer_type": "registered",
            "first_name": "Kofi",
            "last_name": "Amankwah",
            "address": {"city": "Kumasi", "country": "Ghana"},
            "notes": "café — Ghanaian Cedi ₵",
        },
        dependencies=[],
        authored_at=AUTHORED_AT,
        authored_by=AUTHORED_BY,
        branch_id=BRANCH_ID,
        org_id=ORG_ID,
        hash_self="0" * 64,
    )


# ── Pinned expected outputs ──────────────────────────────────────────────────
#
# These are the canonical bytes and hash_self for each fixture above with
# ``hash_prev`` set to GENESIS_HASH. Compute once, commit forever. To
# regenerate (only after coordinating a Python + TS bump), run:
#
#     ./.venv/bin/python -m pytest tests/unit/test_event_envelope_golden_vectors.py \
#         --regenerate-golden
#
# and copy the printed values below. See the fixture ``_regenerate`` below.

EXPECTED_HASHES: Dict[str, str] = {
    "customer_created": (
        "640af4fd5e2b1bf7ab90c74eabbbd9c89cdd6911482ae0990e9cd46a85e2e16d"
    ),
    "customer_updated_with_dep": (
        "b28917c321bfe21c3037094a72d2620e6b0aa272d4ae3e5edc3eed85c0500e6b"
    ),
    "unicode_payload": (
        "e6389ff0db6069e59063ab859cbd0a04068b3997a71f7a1d3eb40fb6c6d1fb4b"
    ),
}


VECTORS = [
    ("customer_created", _make_customer_created),
    ("customer_updated_with_dep", _make_customer_updated_with_dep),
    ("unicode_payload", _make_payload_with_unicode),
]


# ── Tests ────────────────────────────────────────────────────────────────────


class TestCanonicalJsonStability:
    """Guards Python-side determinism of canonical_json / compute_hash_self."""

    @pytest.mark.parametrize("name,factory", VECTORS)
    def test_hash_self_matches_pin(self, name: str, factory) -> None:
        env = factory()
        actual = compute_hash_self(env, GENESIS_HASH)
        assert actual == EXPECTED_HASHES[name], (
            f"Golden-vector hash mismatch for {name!r}. "
            f"If canonical_json or the hash formula changed intentionally, "
            f"regenerate EXPECTED_HASHES *and* the TypeScript mirror.\n"
            f"  expected: {EXPECTED_HASHES[name]}\n"
            f"  actual:   {actual}"
        )

    def test_canonical_json_is_deterministic(self) -> None:
        """Same input, called twice, produces byte-identical output."""
        env = _make_customer_created()
        body = {
            "event_id": env.event_id,
            "aggregate_id": env.aggregate_id,
            "aggregate_type": env.aggregate_type,
            "event_type": env.event_type,
            "schema_version": env.schema_version,
            "payload": env.payload,
            "dependencies": env.dependencies,
            "authored_at": env.authored_at,
            "authored_by": env.authored_by,
            "branch_id": env.branch_id,
            "org_id": env.org_id,
            "hash_prev": GENESIS_HASH,
        }
        a = canonical_json(body)
        b = canonical_json(body)
        assert a == b
        assert isinstance(a, bytes)

    def test_canonical_json_sorts_keys(self) -> None:
        """Key ordering in the input dict must not change the output."""
        payload_a = {"z": 1, "a": 2, "m": 3}
        payload_b = {"a": 2, "m": 3, "z": 1}
        assert canonical_json(payload_a) == canonical_json(payload_b)

    def test_datetime_uses_rfc3339_z_at_ms_precision(self) -> None:
        """Datetime serialization must be RFC 3339 with 'Z' suffix and
        millisecond precision — the TS Date.toISOString() form.
        """
        body = {"t": AUTHORED_AT}
        got = canonical_json(body).decode("utf-8")
        # AUTHORED_AT = 2026-01-15T12:34:56.789Z
        assert '"t":"2026-01-15T12:34:56.789Z"' in got

    def test_uuid_is_lowercase_hyphenated(self) -> None:
        body = {"u": ORG_ID}
        got = canonical_json(body).decode("utf-8")
        assert '"u":"11111111-1111-1111-1111-111111111111"' in got


# ── Fixture export ───────────────────────────────────────────────────────────
#
# We also emit the vectors as a JSON file so the future TS-side parity
# test can load them without re-implementing the fixture factories.

GOLDEN_VECTORS_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "docs"
    / "plans"
    / "phase-0-artifacts"
    / "envelope-golden-vectors.json"
)


def _envelope_to_input(env: EventEnvelope) -> Dict[str, Any]:
    """Serialize the envelope to the wire-input shape the TS side will
    consume for parity testing.
    """
    return {
        "event_id": env.event_id,
        "aggregate_id": str(env.aggregate_id),
        "aggregate_type": (
            env.aggregate_type.value
            if hasattr(env.aggregate_type, "value")
            else env.aggregate_type
        ),
        "event_type": env.event_type,
        "schema_version": env.schema_version,
        "payload": env.payload,
        "dependencies": env.dependencies,
        "authored_at": env.authored_at.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "authored_by": str(env.authored_by),
        "branch_id": str(env.branch_id),
        "org_id": str(env.org_id),
        "hash_prev": GENESIS_HASH,
    }


def test_write_golden_vectors_file() -> None:
    """Emit the golden-vectors JSON alongside the Phase 0 artifacts so
    the TS side can consume the same inputs and expected outputs.

    This test is idempotent: it computes vectors from the same fixtures
    the parity tests use, then writes the file.
    """
    vectors: List[Dict[str, Any]] = []
    for name, factory in VECTORS:
        env = factory()
        vectors.append(
            {
                "name": name,
                "input": _envelope_to_input(env),
                "expected_hash_self": EXPECTED_HASHES[name],
            }
        )

    output = {
        "$comment": (
            "Auto-emitted by "
            "backend.laso/tests/unit/test_event_envelope_golden_vectors.py. "
            "Do not hand-edit — run the test to regenerate."
        ),
        "vectors": vectors,
    }

    GOLDEN_VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_VECTORS_PATH.write_text(json.dumps(output, indent=2) + "\n")
    assert GOLDEN_VECTORS_PATH.exists()
