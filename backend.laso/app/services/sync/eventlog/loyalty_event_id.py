"""Deterministic event id for a loyalty change.

The device half of this pair is ``ui.laso/src/lib/loyaltyEventId.ts``. The two
implementations are pinned to the same vectors by
``tests/unit/test_loyalty_event_id_vectors.py`` and
``ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts`` so they cannot drift.

Why this exists
---------------
`customer_loyalty_changed` carries an ABSOLUTE post-state, not a delta: after the
sale the payload says loyalty_points=30, loyalty_tier='bronze'; after the refund
of the same sale it says loyalty_points=0. That shape is what makes duplicates
harmless, and it is only true if the duplicate carries the same absolute value —
which it does, because the id is a pure function of the change.

    customer_loyalty_changed | <customer_id> | <sale_id> | <direction>

The direction ('earn' | 'refund') is part of the seed so one sale and its refund
produce two different ids. Without it the refund would collide with the earn and
be swallowed as a duplicate — the customer would keep the points they just gave
back. That is the single most important property of this file.

Idempotency on the server rests solely on ``event_log``'s primary key
``(org_id, event_id)``. One sale, one earn id, forever; one sale, one refund id,
forever.

Reuses `deterministic_event_id` (stock_emitter.py), the same helper the refill id
and the backfills already use, so the 26-character ULID_LENGTH rule and the
uppercase-hex rendering are identical across every deterministic event in the
system.
"""

from __future__ import annotations

from typing import Any

from app.services.sync.eventlog.stock_emitter import deterministic_event_id

EVENT_TYPE = "customer_loyalty_changed"

#: The only two directions. A refund and its earn must not share an id.
DIRECTION_EARN = "earn"
DIRECTION_REFUND = "refund"
DIRECTIONS = (DIRECTION_EARN, DIRECTION_REFUND)


def customer_loyalty_changed_event_id(
    customer_id: Any, sale_id: Any, direction: str
) -> str:
    """The one event id for the loyalty change ``sale_id`` made to a customer.

    Accepts UUIDs or strings, because the offline path has only the string form
    from the event payload. Raises on an unknown direction rather than silently
    deriving an id that could collide with the other direction's.
    """
    if direction not in DIRECTIONS:
        raise ValueError(
            f"direction must be one of {DIRECTIONS}, got {direction!r}"
        )
    return deterministic_event_id(
        EVENT_TYPE, str(customer_id), f"{sale_id}|{direction}"
    )
