"""Cross-runtime vectors for the loyalty event id.

The server (Python) and the device (TypeScript) MUST derive the same string for
the same (customer, sale, direction), because the device uses it to recognise
the server's answer to its own offline sale. If the two drift, an offline device
applies the loyalty change twice.

The expected values here are the contract. They are produced by
``sha256("customer_loyalty_changed|<customer_id>|<sale_id>|<direction>")``
truncated to 26 uppercase hex characters, and the TypeScript mirror is asserted
against the same four vectors by
``ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts``.
"""

import uuid as _uuid

import pytest

from app.services.sync.eventlog.loyalty_event_id import (
    DIRECTION_EARN,
    DIRECTION_REFUND,
    customer_loyalty_changed_event_id,
)

CUST = "5823ef27-51ce-4431-9adc-e81f9b3f949f"
CUST2 = "0615fb0e-416f-4ecb-bca1-d4839446cb36"
SALE = "APO1-20261005-0001"
SALE2 = "99999999-8888-7777-6666-555555555555"

# Mirrored verbatim in ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts.
VECTORS = [
    ("5823ef27-51ce-4431-9adc-e81f9b3f949f", "APO1-20261005-0001", "earn",
     "EB25262A7317448712B80292AA"),
    ("5823ef27-51ce-4431-9adc-e81f9b3f949f", "APO1-20261005-0001", "refund",
     "DB76F8DFBAA917956EB45A8074"),
    ("0615fb0e-416f-4ecb-bca1-d4839446cb36", "APO1-20261005-0001", "earn",
     "831785162E51C425BCD33CB87F"),
    ("5823ef27-51ce-4431-9adc-e81f9b3f949f", "99999999-8888-7777-6666-555555555555", "earn",
     "69F3506D0779E344454F6CEF75"),
]


def test_vector_length_is_a_ulid():
    """EventEnvelope requires exactly ULID_LENGTH characters."""
    for cust, sale, direction, _ in VECTORS:
        assert len(customer_loyalty_changed_event_id(cust, sale, direction)) == 26


def test_same_sale_direction_gives_the_same_id():
    """The whole point: one loyalty change is one id, forever."""
    assert customer_loyalty_changed_event_id(
        CUST, SALE, DIRECTION_EARN
    ) == customer_loyalty_changed_event_id(CUST, SALE, DIRECTION_EARN)


def test_earn_and_refund_of_one_sale_never_collide():
    """The single most important property.

    If these collided, the refund event would be swallowed as a duplicate of the
    earn and the customer would keep the points they just gave back.
    """
    assert customer_loyalty_changed_event_id(
        CUST, SALE, DIRECTION_EARN
    ) != customer_loyalty_changed_event_id(CUST, SALE, DIRECTION_REFUND)


def test_different_sale_gives_a_different_id():
    assert customer_loyalty_changed_event_id(
        CUST, SALE, DIRECTION_EARN
    ) != customer_loyalty_changed_event_id(CUST, SALE2, DIRECTION_EARN)


def test_different_customer_gives_a_different_id():
    assert customer_loyalty_changed_event_id(
        CUST, SALE, DIRECTION_EARN
    ) != customer_loyalty_changed_event_id(CUST2, SALE, DIRECTION_EARN)


def test_id_is_uppercase_hex():
    for cust, sale, direction, _ in VECTORS:
        eid = customer_loyalty_changed_event_id(cust, sale, direction)
        assert eid == eid.upper()
        int(eid, 16)  # raises if not hex


def test_accepts_uuid_objects_and_strings():
    """The offline path only has the string form from the payload.

    Note SALE is a human sale NUMBER here, not a UUID: online sales identify
    themselves by number in some paths, and the helper must not care which.
    """
    assert customer_loyalty_changed_event_id(
        _uuid.UUID(CUST), _uuid.UUID(SALE2), DIRECTION_EARN
    ) == customer_loyalty_changed_event_id(CUST, SALE2, DIRECTION_EARN)


def test_rejects_an_unknown_direction():
    """Better to raise than derive an id that could collide with the other side."""
    with pytest.raises(ValueError):
        customer_loyalty_changed_event_id(CUST, SALE, "adjust")


def test_shared_vectors_are_stable():
    """Pinned values. A change here is a cross-runtime break, not a refactor."""
    for cust, sale, direction, expected in VECTORS:
        assert customer_loyalty_changed_event_id(cust, sale, direction) == expected
