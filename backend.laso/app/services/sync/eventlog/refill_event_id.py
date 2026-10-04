"""Deterministic event id for a prescription refill.

Why this exists
---------------
`prescription_refill_used` is a raw decrement. If the same dispense ever
produces two events with two different ids, the counter drops twice and a refill
disappears. The report's fact 9 is exactly that: idempotency on the server rests
solely on ``event_log``'s primary key ``(org_id, event_id)``, so two ids means
two decrements.

So the id is a pure function of the dispense, never random:

    prescription_refill_used | <prescription_id> | <sale_id>

One dispense is one sale, so one sale id yields one event id forever. That gives
three properties for free:

  * A replay of the same ``sale_created`` re-derives the same id, and the append
    path returns ALREADY_APPENDED, so the re-emit is a no-op. This is what lets
    SaleProjector emit from inside ``apply`` despite ADR 0007 asking projectors
    to be pure functions of (event, current_read_model).
  * An offline device can compute the id the server will use, record it in
    ``applied_events``, and skip the echo of its own dispense.
  * Server and device derive the same string, so the two never disagree about
    which event is "the" refill event for a sale.

`deterministic_event_id` is the existing helper (stock_emitter.py:55) and is
already used by the backfills for the same reason. The formula is mirrored in
`ui.laso/src/lib/refillEventId.ts`; the two are pinned to the same vectors by
`test_refill_event_id_vectors.py` so they cannot drift.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.sync.eventlog.stock_emitter import deterministic_event_id

EVENT_TYPE = "prescription_refill_used"


def prescription_refill_used_event_id(
    prescription_id: Any, sale_id: Any
) -> str:
    """The one event id for the refill consumed by ``sale_id``.

    Accepts UUIDs or strings, because the offline path has only the string form
    from the event payload.
    """
    return deterministic_event_id(
        EVENT_TYPE, str(prescription_id), str(sale_id)
    )


def as_uuid(value: Any) -> uuid.UUID | None:
    """Best-effort UUID parse. Returns None rather than raising on junk."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
