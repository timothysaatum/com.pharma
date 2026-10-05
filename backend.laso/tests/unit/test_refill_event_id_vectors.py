"""Cross-runtime vectors for the refill event id.

The server (Python) and the device (TypeScript) MUST derive the same string for
the same dispense, because the device records that string in ``applied_events``
to skip the echo of its own offline sale. If the two implementations drift, an
offline device double-decrements.

The expected values here are the contract. They were produced by the formula
``sha256("prescription_refill_used|<prescription_id>|<sale_id>")`` truncated to
26 uppercase hex characters, and the TypeScript mirror is asserted against them
by ``ui.laso/src/lib/__tests__/refillEventId.spec.ts``. If either side changes
the formula, both suites fail.
"""

from app.services.sync.eventlog.refill_event_id import (
    prescription_refill_used_event_id,
)

RX = "11111111-2222-3333-4444-555555555555"
RX2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SALE = "99999999-8888-7777-6666-555555555555"
SALE2 = "12345678-1234-1234-1234-123456789abc"

# Mirrored verbatim in ui.laso/src/lib/__tests__/refillEventId.spec.ts.
VECTORS = [
    ("11111111-2222-3333-4444-555555555555", "99999999-8888-7777-6666-555555555555", "FA5BB38695815707FDFE0DB5EA"),
    ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "99999999-8888-7777-6666-555555555555", "FFB35FB9C51ED6BA229D70C1B9"),
    ("11111111-2222-3333-4444-555555555555", "12345678-1234-1234-1234-123456789abc", "F2E19894F0591A5DF84F072999"),
]


def test_vector_length_is_a_ulid():
    """EventEnvelope requires exactly ULID_LENGTH characters."""
    for rx, sale, expected in VECTORS:
        assert len(prescription_refill_used_event_id(rx, sale)) == 26


def test_same_sale_gives_the_same_id():
    """The whole point: one dispense is one id, forever."""
    a = prescription_refill_used_event_id(RX, SALE)
    b = prescription_refill_used_event_id(RX, SALE)
    assert a == b


def test_different_sale_gives_a_different_id():
    """Two dispenses of the same prescription must not collide."""
    assert prescription_refill_used_event_id(RX, SALE) != prescription_refill_used_event_id(
        RX, SALE2
    )


def test_different_prescription_gives_a_different_id():
    """Same sale id, different prescription: cannot collide either."""
    assert prescription_refill_used_event_id(RX, SALE) != prescription_refill_used_event_id(
        RX2, SALE
    )


def test_id_is_uppercase_hex():
    for rx, sale, _ in VECTORS:
        eid = prescription_refill_used_event_id(rx, sale)
        assert eid == eid.upper()
        int(eid, 16)  # raises if not hex


def test_accepts_uuid_objects_and_strings():
    """The offline path only has the string form from the payload."""
    import uuid as _uuid

    assert prescription_refill_used_event_id(
        _uuid.UUID(RX), _uuid.UUID(SALE)
    ) == prescription_refill_used_event_id(RX, SALE)


def test_shared_vectors_are_stable():
    """Pinned values. A change here is a cross-runtime break, not a refactor."""
    for rx, sale, expected in VECTORS:
        assert prescription_refill_used_event_id(rx, sale) == expected
