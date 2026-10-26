# Sync log investigation: `cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8`, seq 8-11

Date: 2026-10-25
Scope: investigation of the organization's event log, device outbox, and local read model, followed by fixes recorded in §7.

The investigation phase modified no code or data. The fixes and their test evidence are in §7.

## Short answer

- The hash chain breaks at **seq 10 only**.
- `af54356fce898b343f1ad44da5f14cc8c8daa68dd45038c1df9fa88884bb355c` is the **device outbox hash** of sale event `01M5SM2685NJVNJZ5NR2D32NM7`. It is not a hash stored on the server chain.
- The server accepted seq 10 because seq 9 and seq 10 were in the same push batch, and the router advanced its in-memory tail to the **client** hash instead of the **server-stored** hash.
- `authored_by` for customer and prescription is the organization ID, not a user ID. Nothing uses `authored_by` for authorization.
- All local `dependencies` arrays are hardcoded to `[]`. The prescription should have declared the customer event but did not. It worked only because the customer was applied first.
- `stock_adjustments` rows of type `correction` are the sale projector's audit trail. I found no report or query that reads them.
- No server event was missed at the cursor level. `applied_events` is empty because it only stores four replay-guarded event types, none of which are in this log.
- Additional issue: local stock is **97**, server stock is **94**. Three local deductions were skipped because `cacheSales` inserted the sale row before event projection, and `_saleCreated` returns early for any existing row. Local `items_json` is empty for five of six sales because the list API returns `SaleResponse` without items and `cacheSales` uses `INSERT OR REPLACE`. Both are separate from the hash-chain break.

---

## 1. `authored_by`: what it is and what reads it

Observed server values:

- Customer `01M5SM3RS0FQG79DBJFMA946K1`: `authored_by = cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8` (the organization).
- Prescription `01M5SM851QT6F2DB5GM1G9JN1E`: `authored_by = cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8`.
- Sales `01M5RYK...`, `01M5SM268...`, and the online sales use `authored_by = 1d2a0f2a-e174-4f30-b1ea-6bc0799e33af` (the cashier).

The device builders set it as follows in `ui.laso/src/lib/localWrite.ts`:

```ts
// sale, line 276
dependencies: [], authored_at: now, authored_by: sale.cashier_id,

// prescription, line 311
const authoredBy = prescription.verified_by ?? prescription.organization_id;

// customer, line 406
dependencies: [], authored_at: now, authored_by: customer.organization_id,

// stock adjustment, line 454
dependencies: [], authored_at: now, authored_by: adjustment.adjusted_by,

// drug batch, line 511
dependencies: [], authored_at: now, authored_by: batch.branch_id, // fallback

// branch inventory, line 562
dependencies: [], authored_at: now, authored_by: branchInventory.branch_id,

// purchase order, line 623
dependencies: [], authored_at: now, authored_by: po.ordered_by,
```

So the organization ID for customer and prescription is intentional in the current code, not a data corruption artifact. It is semantically wrong for an authorship field, but it does not grant permissions.

What actually reads it:

- `backend.laso/app/services/sync/eventlog/projectors/sale.py:682` passes `event.authored_by` into the derived `customer_loyalty_changed` event.
- `sale.py:832` uses it as the fallback author for a derived `prescription_refill_used` event when no pharmacist or cashier is available.
- `projectors/purchase_order.py:157` uses it as the fallback for `ordered_by`.
- Local sale projection uses `p.cashier_id ?? e.authored_by` at `ui.laso/src/lib/localProjectors.ts:508`.
- Local purchase order projection uses `p.ordered_by ?? e.authored_by` at `localProjectors.ts:1364`.

What does **not** read it:

- Pull authorship skip uses only `event_id`:

```ts
// ui.laso/src/lib/localDb.ts:2497
export async function isLocallyAuthored(eventId: string): Promise<boolean> {
  const db = await getDb();
  const rows = await db.select<{ event_id: string }[]>(
    "SELECT event_id FROM event_outbox WHERE event_id = $1 LIMIT 1",
    [eventId]
  );
  return rows.length > 0;
}
```

- The push endpoint checks only `org_id` and branch access, not `authored_by`:

```python
# backend.laso/app/api/v1/endpoints/event_sync_endpoints.py:105
for envelope in request.events:
    if str(envelope.org_id) != str(org_id):
        raise HTTPException(...)
```

There is no server-side check tying `authored_by` to the authenticated user. It is client-supplied provenance, not an authorization input.

---

## 2. Hash chain: exact cause

### Device side

The device chain is per-device outbox, not per-organization server log:

```ts
// ui.laso/src/lib/localDb.ts:2235
export async function getOutboxTailHash(): Promise<string> {
  const db = await getDb();
  const rows = await db.select<{ hash_self: string }[]>(
    "SELECT hash_self FROM event_outbox ORDER BY created_at DESC, event_id DESC LIMIT 1"
  );
  return rows?.[0]?.hash_self ?? "0".repeat(64);
}
```

```ts
// ui.laso/src/lib/localWrite.ts:202
export async function appendOutboxEvent(
    build: (hashPrev: string) => Promise<OutboxEvent>
): Promise<void> {
    _outboxLock = _outboxLock.then(async () => {
        const hashPrev = (await getOutboxTailHash()) ?? GENESIS_HASH;
        const envelope = await build(hashPrev);
        await appendToOutbox(envelope);
    });
    return _outboxLock;
}
```

`computeHashSelf` hashes the envelope body with `hash_prev` included:

```ts
// ui.laso/src/lib/eventEnvelope.ts:195
export async function computeHashSelf(
  envelope: Omit<EventEnvelope, "hash_self">,
  hashPrev: string,
): Promise<string> {
  const body: Record<string, unknown> = {};
  for (const field of _HASH_FIELDS) {
    body[field] = envelope[field];
  }
  body["hash_prev"] = hashPrev;
  ...
}
```

The first event in an empty outbox therefore gets `GENESIS_HASH`. That is why `01M5RYK...` has `hash_prev = 0000...` even though the server log already had seven events.

### Server side

The append service accepts a client hash computed against either the client's stated `hash_prev` or the real server tail, then stores the server-chain hash:

```python
# backend.laso/app/services/sync/eventlog/append_service.py:213
client_hash_prev = envelope.hash_prev or GENESIS_HASH
expected_client_hash = compute_hash_self(envelope, client_hash_prev)
expected_server_hash = compute_hash_self(envelope, current_tail_hash)
if envelope.hash_self not in (expected_client_hash, expected_server_hash):
    ...
    return AppendResult(
        ...
        status=AppendStatus.HASH_MISMATCH,
    )

seq = current_tail_seq + 1
server_hash_self = expected_server_hash
...
"hash_self": server_hash_self,
"hash_prev": current_tail_hash,
```

But after appending, the router advances the in-memory tail to the **client** hash:

```python
# backend.laso/app/services/sync/eventlog/router.py:136
results.append(push_result)
if append_result is not None and append_result.status == AppendStatus.APPENDED:
    # Advance in-memory tail so subsequent events chain onto
    # this one within the same batch.
    tail_hash = envelope.hash_self
    assert append_result.seq is not None
    tail_seq = append_result.seq
```

That line is the root cause. It should use the server-stored hash, not `envelope.hash_self`.

### What happened in this log

Device outbox:

- `01M5RYK...` sale, `0000... -> e53cf485...`, accepted after 187 attempts.
- `01M5SM268...` sale, `e53cf485... -> af54356f...`, accepted, 1 attempt.
- `01M5SM3RS...` customer, `af54356f... -> ad55238d...`, accepted, 1 attempt.
- `01M5SM851...` prescription, `ad55238d... -> 4f0876c2...`, accepted, 1 attempt.

Server `event_log`:

| seq | event | hash_prev | hash_self | chain |
|---|---|---|---|---|
| 8 | `01M5RYK...` sale | `5dabf00b...` | `24010885...` | true |
| 9 | `01M5SM268...` sale | `24010885...` | `8cd5390f...` | true |
| 10 | `01M5SM3RS...` customer | `af54356f...` | `ad55238d...` | **false** |
| 11 | `01M5SM851...` prescription | `ad55238d...` | `4f0876c2...` | true |
| 12 | `1B9FE032...` sale | `4f0876c2...` | `ff4477e0...` | true |
| 13 | `A09D762F...` sale | `ff4477e0...` | `3043cfe8...` | true |

Seq 10 is the only discontinuity. Seq 11 chains correctly to seq 10's stored hash, so the break is a single link, not a broken remainder.

Why seq 9 and seq 10 differ:

- Seq 9 was the first event in its batch. Server tail was `24010885...`; the client sent `hash_prev = e53cf485...`; the server recomputed `hash_self = 8cd5390f...` and stored `24010885... -> 8cd5390f...`.
- The router then set its in-memory tail to the client hash `af54356f...`.
- Seq 10 and seq 11 were later in the same batch. Seq 10 therefore used `af54356f...` as `hash_prev`, even though seq 9's stored hash was `8cd5390f...`.

So `af54356f...` is a phantom server-chain link. It exists only because the server used the client hash for the next event in the same batch.

### Does the server enforce `hash_prev` against the last stored `hash_self`?

No, not directly. It validates `hash_self` against either the client chain or the server chain, then stores the server hash. It does not require incoming `hash_prev == last stored hash_self`.

If seq 10 had arrived in a **new batch**, the server tail would have been `8cd5390f...`, not `af54356f...`. The expected server hash would then differ from the client's `ad55238d...`, and seq 10 would have been rejected as `HASH_MISMATCH`. It survived only because it was in the same batch as seq 9 and the in-memory tail had already been moved to `af54356f...`.

---

## 3. Why `dependencies` are empty

Every local builder hardcodes an empty array. For the prescription:

```ts
// ui.laso/src/lib/localWrite.ts:336
const hashSelf = await computeHashSelf(
  {
    event_id: eventId, aggregate_id: prescription.id, aggregate_type: "prescription",
    event_type: eventType, schema_version: 1, payload,
    dependencies: [], authored_at: now, authored_by: authoredBy,
    branch_id: prescription.branch_id, org_id: prescription.organization_id,
  },
  hashPrev,
);

return {
  ...
  dependencies: [],
  ...
};
```

The same pattern appears for sale, customer, stock, drug batch, branch inventory, and purchase order.

The server projector documentation says this is wrong for prescriptions:

```python
# backend.laso/app/services/sync/eventlog/projectors/prescription.py:18
Dependencies:
  - prescription_created MUST declare its customer_created event as a dependency
    (customer_id FK with ondelete=RESTRICT). The router's dep check defers the
    prescription until the customer lands.
```

The sale projector has the same rule when `customer_id` is present:

```python
# backend.laso/app/services/sync/eventlog/projectors/sale.py:21
- sale_created has no strict aggregate deps for walk-in sales. When
  customer_id is present the client MUST declare the matching
  customer_created event as a dependency so the FK is satisfied.
```

In this log:

- The sale events are walk-in sales. Their `customer_id` is null, so empty dependencies are correct.
- The prescription has `customer_id = 07bc55c0-fc21-48a6-9932-5156968d10ec`, but `dependencies = {}`. That is wrong under the projector contract.
- It succeeded because the customer event is seq 10 and the prescription is seq 11, so the customer row already existed when the prescription applied.

### What happens if the customer event is deferred or rejected?

Because the prescription declares no dependency, the router does not defer it.

- Customer deferred: the customer is in `event_log` and `pending_projections`, but not in `customers`.
- Prescription arrives: router sees empty dependencies, runs validation, appends, then applies.
- The `prescriptions.customer_id` foreign key is `ON DELETE RESTRICT` and non-null (`prescription_model.py:52`). The INSERT fails with a foreign-key violation.
- The router wraps the event in a savepoint, so the append and apply both roll back. The event is not persisted in `event_log`; the response is `REJECTED_TRANSIENT`.
- The device marks the outbox row `failed` and retries on the next sync cycle. There is no maximum attempt count in `event_outbox`; `getPendingOutboxEvents` includes `failed`, and `markOutboxResult` only increments attempts.
- If the customer later projects, the prescription retry succeeds.
- If the customer is permanently rejected, the prescription retries forever and never reaches the server. That is a real failure mode today.

For a sale with a non-null `customer_id`, the same foreign-key failure applies. The target sales are walk-in, so this log does not exercise it.

---

## 4. Why `stock_adjustments` rows of type `correction` exist

The sale projector writes one audit row per item/drug after it deducts branch inventory:

```python
# backend.laso/app/services/sync/eventlog/projectors/sale.py:1162
# ── 2e. INSERT stock_adjustment (one per item/drug) ───────────────────────
await db.execute(
    text("""
        INSERT INTO stock_adjustments (
            id, branch_id, drug_id,
            adjustment_type, quantity_change,
            previous_quantity, new_quantity,
            reason, adjusted_by,
            created_at, updated_at
        ) VALUES (
            :id,
            :branch_id,
            :drug_id,
            'correction', :qty_change,
            :prev_qty, :new_qty,
            :reason,
            :adjusted_by,
            :created_at, :updated_at
        )
    """),
    {
        ...
        "qty_change": -total_deducted,
        "prev_qty": inv_row.qty_before,
        "new_qty": inv_row.qty_after,
        "reason": f"Sale {sale_number}",
        "adjusted_by": cashier_id,
        "created_at": now,
        "updated_at": now,
    },
)
```

This mirrors the online sale path:

```python
# backend.laso/app/services/sales/sales_service.py:1059
# 17. StockAdjustment + system alerts
#     'correction' is the model-valid type for sale-driven deductions
db.add(
    StockAdjustment(
        ...
        adjustment_type="correction",
        quantity_change=-sale_item.quantity,
        previous_quantity=previous_qty,
        new_quantity=inventory.quantity,
        reason=f"Sale {sale_number}",
        adjusted_by=user.id,
    )
)
```

The model describes the table as:

```python
# backend.laso/app/models/inventory/branch_inventory.py:202
class StockAdjustment(Base, TimestampMixin):
    """
    Audit trail for inventory adjustments.
    Critical for accountability and fraud prevention.
    """
```

Observed rows for this branch:

| reason | qty change | previous | new | created |
|---|---:|---:|---:|---|
| Sale `BR001-20261024-0001` | -1 | 100 | 99 | 2026-10-24 20:17 |
| Sale `BR001-20261024-0002` | -1 | 99 | 98 | 2026-10-24 20:17 |
| Sale `OFFLINE-...47b9229d` | -1 | 98 | 97 | 2026-10-25 08:59 |
| Sale `OFFLINE-...4a0c5657` | -1 | 97 | 96 | 2026-10-25 13:34 |
| Sale `BR001-20261025-0001` | -1 | 96 | 95 | 2026-10-25 13:43 |
| Sale `BR001-20261025-0002` | -1 | 95 | 94 | 2026-10-25 13:44 |

All six are `adjustment_type = correction`, `adjusted_by = 1d2a0f2a...`, `drug_id = 5e195e51...`. They are audit rows, not the stock source of truth. The actual stock is in `branch_inventory` and `drug_batches`.

There is no `sale_id` column on `stock_adjustments`; the sale is identified only by the `reason` string. There is also a timestamp difference: the seq 8 `inventory_movements` row has `occurred_at = 07:15:01` (sale time), while its `stock_adjustments` row has `created_at = 08:59:10` (projection time).

### Do any reports or queries read them?

I found no application read path:

- `backend.laso/app/services/reports/reports_service.py:26` imports `StockAdjustment`, but there is no query using it in that file.
- The inventory API only creates adjustments: `POST /inventory/adjust` and the transfer endpoint. There is no GET/list endpoint for `stock_adjustments`.
- The UI has no `stock_adjustments` table in the local SQLite schema. `localWrite.ts:903` explicitly says it skips the local insert and goes straight to the outbox.
- The legacy sync service can push adjustments (`sync_service.py:3028`) and lists `stock_adjustments` as a synced table, but that is a write/sync path, not a report.
- The current device schema has no `stock_adjustments` table, so the event-sourced device does not consume them locally.

So these rows are effectively write-only audit records in the current application. If an external BI/reporting system reads the table, it is outside this repository.

---

## 5. Device outbox and server event reconciliation

Device outbox has exactly four rows, all `accepted`, no error:

| event | type | attempts | status |
|---|---|---:|---|
| `01M5RYK...` | `sale_created` | 187 | accepted |
| `01M5SM268...` | `sale_created` | 1 | accepted |
| `01M5SM3RS...` | `customer_created` | 1 | accepted |
| `01M5SM851...` | `prescription_created` | 1 | accepted |

`sync_event_failures` is empty. No outbox event is pending, failed, deferred, or permanently rejected. The 187 attempts on the first event are historical transient failures from the contract-resolution incident; the final status is accepted and the error fields are null.

Server `event_log` has 13 events, min seq 1, max seq 13. Device `sync_meta` has:

- `event_pull_seq:cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8 = 13`
- unscoped `event_pull_seq = 0`
- `last_sync_at:521ef6b1-... = 2026-10-25T13:54:03.366Z`

No server event was missed at the cursor level. Local rows exist for the target customer, prescription, and sales 12/13.

`applied_events` is empty, but that is expected. It is only written for four replay-sensitive event types:

```ts
// ui.laso/src/lib/localProjectors.ts:164
const REPLAY_GUARDED_TYPES = new Set([
  "sale_voided",
  "prescription_refill_used",
  "stock_adjusted",
  "stock_transfer",
]);
```

None of those types are in this 13-event log, so an empty `applied_events` table is correct.

`event_dead_letter` and `pending_projections` are both empty for this organization. Seq 8-11 were not deferred or dead-lettered.

---

## 6. Other things in this sync log worth knowing

### Local stock diverges from server stock: root cause found

- Server `branch_inventory.quantity = 94`.
- Local device `branch_inventory.quantity = 97`.
- Local `drug_batches.remaining_quantity = 97`.
- Initial batch quantity was `100` from seq 4 (`drug_batch_created`).
- Six sales exist on both sides: seq 6, 7, 8, 9, 12, 13.

The local stock path is not a single projector. It is the offline sales manager plus the event projector, and the two paths disagree about who owns the deduction.

The projector only deducts batches when the sale row does not already exist:

```ts
// ui.laso/src/lib/localProjectors.ts:409
const existing = await db.select<{ id: string }[]>(
  "SELECT id FROM sales WHERE id = $1",
  [String(e.aggregate_id)]
);
if (existing.length > 0) {
  // ... update contract metadata only ...
  return;
}

// ... later ...
const batchChanges = (p.batch_changes ?? []) as Array<Record<string, unknown>>;
for (const bc of batchChanges) {
  await db.execute(
    `UPDATE drug_batches
        SET remaining_quantity = MAX(0, remaining_quantity - $1)
      WHERE id = $2`,
    [Number(bc.quantity_used ?? 0), String(bc.batch_id)]
  );
}
```

`cacheSales` inserts the same rows before event projection:

```ts
// ui.laso/src/lib/localDb.ts:2101
export async function cacheSales(items: Sale[]): Promise<void> {
  ...
  `INSERT OR REPLACE INTO sales ...`
  ...
  JSON.stringify(item.items ?? []),
```

In this log the timing is visible in the timestamps:

| seq | sale | local `created_at` | event `authored_at` | local stock effect |
|---|---|---|---:|---|
| 6 | `BR001-20261024-0001` | `20:17:12.541` | `20:17:12.706` | skipped: cache row existed |
| 7 | `BR001-20261024-0002` | `20:17:22.523` | `20:17:22.606` | skipped: cache row existed |
| 8 | `OFFLINE-...47b9229d` | `07:15:01.067` | `07:15:01.067` | deducted by offline manager |
| 9 | `OFFLINE-...4a0c5657` | `13:30:08.321` | `13:30:08.321` | deducted by offline manager |
| 12 | `BR001-20261025-0001` | `13:43:50.835` | `13:43:51.294` | skipped: cache row existed |
| 13 | `BR001-20261025-0002` | `13:44:48.429` | `13:44:48.429` | deducted by projector |

For cached rows, `created_at` is the sale's original timestamp, not the cache insert time. The missing deduction is the evidence that the row already existed when the projector ran.

Local arithmetic:

```text
100 initial
-1 offline sale seq 8
-1 offline sale seq 9
-1 online sale seq 13 (projector inserted the row)
= 97
```

Server arithmetic: all six sales were projected and deducted, so `100 - 6 = 94`.

The three missing local deductions are seq 6, 7, and 12. The cause is the row-existence check at `localProjectors.ts:409-413`: it treats any existing `sales` row as proof that the event was already projected. `cacheSales` creates that row without applying stock, so the projector returns early and the deduction is lost. This is a real bug, not just a timing artifact.

### Local sale line items are missing: root cause found

Local `sales.items_json` is `[]` for five of six sales, including both offline sales. Only `BR001-20261025-0002` has its item.

The list API returns `SaleResponse`, which has no `items` field:

```python
# backend.laso/app/api/v1/endpoints/sales_endpoints.py:218
return await paginator.paginate(query, pagination, SaleResponse)
```

```python
# backend.laso/app/schemas/sales_schemas.py:387
class SaleResponse(SaleBase, TimestampSchema, SyncSchema):
    """Schema for sale API responses"""
    ...
    items_count: int = Field(default=0, ...)
```

Only `SaleWithDetails` adds the line items:

```python
# backend.laso/app/schemas/sales_schemas.py:555
class SaleWithDetails(SaleResponse):
    items: List[SaleItemWithDetails] = ...
```

`cacheSales` then replaces the local row with `item.items ?? []`:

```ts
// ui.laso/src/lib/localDb.ts:2106
`INSERT OR REPLACE INTO sales ...`
...
JSON.stringify(item.items ?? []),
```

`SalesHistoryPage.tsx:1004` calls `cacheSales(items)` after a non-cached online list fetch. Because the list response has no `items`, every cached sale gets `items_json = '[]'` while `items_count` stays at 1.

The data shows the same split as the stock issue:

- Seq 6, 7, and 12 were cached first, so their local rows have server `created_at` values and empty items.
- Seq 8 and 9 were inserted by the offline manager with items, then replaced by `cacheSales` with empty items.
- Seq 13 was projected by `_saleCreated` before any cache refresh, so it still has its item.

The fix needs to stop treating the local `sales` table as both a projection target and a cache. Either the list endpoint must return items (`SaleWithDetails`), or `cacheSales` must use an upsert that preserves `items_json` when the incoming `items` field is absent. The projector's row-existence check also needs a real projection marker so cached rows cannot suppress stock deductions.

### The chain break is a latent time bomb

Seq 10 is accepted only because it was in the same batch as seq 9. If the same event were replayed alone, or if the server restarted between seq 9 and seq 10, the server tail would be `8cd5390f...`, the client `hash_self` would be `ad55238d...`, and the event would be rejected as `HASH_MISMATCH`. The current log is therefore not safely replayable from seq 9 onward without preserving the exact batch boundary.

### `authored_by` is not authenticated

The server checks `org_id` and branch access, but never checks that `authored_by` is the current user or a user in the organization. A client can claim any UUID. That does not change projections for customer/prescription, but it does flow into derived loyalty/refill events and purchase-order fallbacks.

---

## 7. Fixes applied after the investigation

The investigation was read-only. The following fixes were implemented and verified afterwards.

### 7.1 Server hash-chain tail (seq 10 class of break)

- `AppendResult` now carries `hash_self`, the hash the server actually stored. On `APPENDED` it is the server-chain hash; on `ALREADY_APPENDED` it is the stored row's hash.
- `AppendService.append_batch` advances `tail_hash` from `result.hash_self`, not `envelope.hash_self`.
- `EventRouter.process_batch` reloads `_load_log_tail` before every envelope. Projectors can append nested events (`customer_loyalty_changed`, `prescription_refill_used`) through `StockEventEmitter`, so a cached outer tail was stale even without client/server hash divergence. Reloading prevents both the wrong `hash_prev` and `(org_id, seq)` collisions.
- `_append_one` wraps the INSERT in `db.begin_nested()`. The old `db.rollback()` on `IntegrityError` discarded every earlier accepted event in the same batch. The savepoint keeps the outer transaction and advisory lock intact.
- Dead-letter guard: `_process_one` checks `event_dead_letter` before projector lookup. A retry of a quarantined event now returns `REJECTED_PERMANENT` with `error_code="quarantined"` instead of re-parking it as `ACCEPTED_DEFERRED`.

Regression tests:

- `test_event_sync_spine.py::test_batch_chains_onto_stored_server_hash_when_client_chain_diverges`
- `test_event_sync_spine.py::test_append_batch_advances_to_stored_server_hash`
- `test_sale_sync_projector.py::test_batch_with_nested_loyalty_event_uses_persisted_tail`
- `test_event_dead_letter.py::test_repushing_a_quarantined_event_stays_rejected`

The test helpers in `test_event_sync_spine.py` and `test_sale_sync_projector.py` were also fixed to set `EventEnvelope.hash_prev` when computing `hash_self`. They previously computed chained hashes against `None`, which produced false `HASH_MISMATCH` failures.

### 7.2 Local dependency declarations

`ui.laso/src/lib/localWrite.ts` now resolves event IDs before building an envelope:

- `sale_created` -> `customer_created` and `prescription_created` when those IDs are present.
- `prescription_created` -> `customer_created`; `prescription_updated` -> `customer_created` + its own `prescription_created`.
- `customer_updated` -> its own `customer_created`.
- `drug_batch_created` -> `purchase_order_created`; `drug_batch_updated` -> its own `drug_batch_created` + `purchase_order_created`.
- `purchase_order_updated` -> its own `purchase_order_created`.

Resolver behavior:

- A queued or accepted create event: use its `event_id`; `event_outbox` is never pruned.
- A permanently rejected create: throw before writing.
- A synced local row or a missing local row: omit the dependency; the server owns the aggregate or validates the reference itself.
- A local row in any other status with no create event: throw before writing.

`writeLocal` resolves before `upsertLocal` because `upsertLocal` forces `sync_status='pending'`. `offlineSalesManager.recordSaleTransaction` resolves before its SQLite transaction, so a failed lookup aborts checkout before any write. `appendOutboxEvent` now recovers its lock after a builder failure; previously one rejected dependency poisoned every later append in the session.

Tests: `localWriteDependencies.test.ts` (10 cases on the real SQLite migration chain) and two new `offlineSalesManager` cases for pre-transaction resolution and fail-before-write.

### 7.3 Verification

| Suite | Result |
|---|---|
| `tests/integration/test_event_sync_spine.py` | 9 passed |
| `tests/integration/test_event_dead_letter.py` | 5 passed |
| `tests/integration/test_sale_sync_projector.py` | 20 passed |
| `ui.laso/src/lib/__tests__` | 50 files, 394 tests passed |
| `pnpm exec tsc --noEmit` | passed |

Backend tests used the disposable PostgreSQL cluster at `/tmp/com-pharma-phase2-pg` (socket `/tmp/com-pharma-phase2-pg/sock`, port `55998`, database `com_pharma_phase2`). The suite is slow because each test drops and recreates the schema; the full sale projector file took 9m47s.

### 7.4 Not fixed in this change

- Local stock still diverges when `cacheSales` creates a sale row before projection; `_saleCreated` treats the row as already projected and skips batch deduction. Server is 94, local is 97 for this branch.
- `cacheSales` still replaces `items_json` with `[]` because the list API returns `SaleResponse` without items.
- `authored_by` for customer and prescription is still the organization ID.
- The historical seq 10 link is untouched. Seq 11+ remain valid; replay from seq 9 must preserve the original batch boundary.
- `writeLocal.customer` with no `branchId` still writes a local row without an outbox event; a later dependent write fails the new resolver.

Residual risks:

- The cascade-versus-25-attempt quarantine policy is unchanged; a dependent chain can still consume attempts against a create that will never apply.
- Device handling of server `REJECTED_PERMANENT` / `quarantined` was not exercised end-to-end in the UI.
- The local projector's behavior for an orphan customer reference was not verified beyond the new resolver.

## Things I could not verify

- Whether any external BI, reporting, or analytics system reads `stock_adjustments`. No in-repo read path exists.
- Whether `authored_by = organization_id` for customer and prescription is an intentional convention or an unfixed builder bug. The code is consistent, but the field semantics say user UUID.
- Whether seq 12 and seq 13 were authored by another device or by the online API. They are not in this device's outbox, but they are present in `event_log` and locally projected.
- The exact UI navigation that triggered each `cacheSales` call. The cache-before-projection order for seq 6, 7, and 12 is inferred from row timestamps, not from runtime logs.
- Full test-suite behavior. No tests were run during the initial read-only phase; see §7.3 for the follow-up results.
