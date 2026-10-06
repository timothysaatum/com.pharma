# 0010: Customer Loyalty Event Contract

**Status:** Accepted
**Date:** 2026-10-06
**Related:** [0006 — Event-Sourced Sync Spine](0006-event-sourced-sync-spine.md), [0007 — Event Schema, Hash Chain, and Dependency Semantics](0007-event-schema-hash-chain-dependencies.md), `app/services/sync/eventlog/refill_event_id.py`

## Context

Loyalty points were awarded on exactly one code path: `SalesService.process_sale`,
the online sale. Three gaps followed from that, all confirmed by execution against
a disposable cluster and by reading `atlasdb`:

1. The offline path never awarded anything. `SaleProjector` creates the `Sale` and
   its items and stops; it does not touch `customers`. A sale taken offline and
   synced later produced a sale row with no points behind it.
2. No event was ever emitted for a loyalty change. `customers.loyalty_points` is a
   bare counter mutated in place, so the device had nothing to apply and no way to
   learn the new balance. Joe's server row sat at `sync_status='pending'`,
   `sync_version=5`, with no event behind it and a device row still showing 0.
3. The device had no loyalty read or write path at all: the `sale_created` payload
   carries no loyalty keys, and no projector touches `customers`.

A fourth, adjacent fact shaped the design: a refund reverses `loyalty_points` and
re-resolves the tier, but leaves `total_orders` and `total_value` untouched. That
inconsistency is reported, not fixed, here.

## Decision

### 1. The event

A new event type, `customer_loyalty_changed`, carrying the customer's **absolute**
post-state:

```json
{
  "customer_id": "<uuid>",
  "loyalty_points": 125,
  "loyalty_tier": "silver",
  "sale_id": "<sale id or number>",
  "direction": "earn" | "refund"
}
```

`aggregate_type` is `customer` and `aggregate_id` is the customer id, so the event
flows through the existing customer projector and the existing
`AGGREGATE_TABLES` reconciliation.

**Why absolute rather than a delta.** An absolute value makes a duplicate
assignment rather than an addition: applying the same event twice lands on the
same number. A delta would need the device to be exactly-once, which is precisely
the property we do not have. Absolute also means a device that missed an event
converges on the next one, instead of drifting.

### 2. The event id

```
sha256("customer_loyalty_changed|<customer_id>|<sale_id>|<direction>")
  -> first 26 characters, uppercased
```

Implemented once in `app/services/sync/eventlog/loyalty_event_id.py`, mirroring
`ui.laso/src/lib/loyaltyEventId.ts`. Both sides reuse
`deterministic_event_id` (`stock_emitter.py:55`), the same helper the refill id and
the backfills use, so the 26-character `ULID_LENGTH` rule is identical across every
deterministic event in the system.

**`direction` is in the seed, and that is the load-bearing part.** One sale's earn
and that sale's refund must not share an id. If they did, the refund would be
swallowed by `event_log`'s `(org_id, event_id)` primary key as a duplicate of the
earn, and the customer would keep the points they had just given back. The id
helper raises on an unknown direction rather than deriving an id that could
collide.

The two implementations are pinned to the same four vectors by
`tests/unit/test_loyalty_event_id_vectors.py` and
`ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts`. A change to either formula
fails both suites.

### 3. Ordering

The event is emitted **inside the same transaction** as the points update, after
it, through the non-swallowing server emitter. If the points commit, the event is
in the log; if the transaction rolls back, neither happened. Emitting before the
update would let a device observe a balance the server then failed to write.

The offline path follows the precedent set by `prescription_refill_used`: the
projector emits from inside `apply`. ADR 0007 asks projectors to be pure functions
of (event, read model); that is already bent for the refill event, and the
deterministic id is what makes it safe — a replay of the same `sale_created`
re-derives the same id, the append returns `ALREADY_APPENDED`, and the re-emit is a
no-op. Loyalty does not bend it further than that.

### 4. Replay behaviour

| Situation | Result |
|---|---|
| Same event delivered twice | Second `apply` writes the same absolute value. No drift. |
| `sale_created` re-projected | `ON CONFLICT (id) DO NOTHING` on the sale, and the same derived event id, so no second award. |
| Earn then refund | Two distinct ids, two events, balance returns to where it started. |
| Out-of-order (refund seen before its earn) | Both are absolute, so the last one applied wins and the row holds a real server value. There is no counter to go negative. |
| Event for a customer this device does not have | Recorded as a projection failure; **not** silently inserted. |

### 5. Why duplicates are safe

Three independent reasons, so no single one has to hold:

1. The payload is absolute, so a duplicate is idempotent by construction.
2. The id is derived from the change's identity, so the server's append path
   refuses the duplicate before a projector ever runs.
3. The server's read model is the only writer of `loyalty_points`. The device
   never computes a balance; it copies one.

### 6. What this ADR does not decide

- **No points ledger.** `loyalty_points` remains a counter on the customer row. A
  ledger makes every award reconstructible and is the right long-term answer; it is
  a separate project with its own migration.
- **Refund and `total_orders` / `total_value`.** A refund currently reverses points
  and the tier but not the order count or the spend total. Options are in the
  investigation report; changing it here would alter historical aggregates on data
  nobody has audited.
- **Redemption.** Not implemented. The thresholds exist, the ledger does not.
