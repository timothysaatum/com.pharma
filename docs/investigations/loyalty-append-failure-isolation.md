# Loyalty append isolation — what a failed `customer_loyalty_changed` does to a sale

**Branch:** `fix/customers-sync` · **Fix commit:** `4135439`
**Date:** 2026-10-06 · Not pushed.

Two outcomes, measured and then fixed:

1. **Before:** a failed loyalty event append **failed the whole sale** — HTTP 500, sale row, line items, stock deduction and points all rolled back.
2. **After:** a failed loyalty event append is **isolated**. The sale commits, the balance commits, the event does not.

Everything below was measured on a disposable PostgreSQL cluster (initdb in `/tmp`, unix socket, destroyed afterwards), not inferred. `laso.db` was never opened; no AtlasDB write was performed.

---

## 1. Outcome one — the sale failed

### 1.1 Why, from the code

`StockEventEmitter.emit_in_transaction` is **deliberately non-swallowing** (`stock_emitter.py:91-108`):

> *"Does not commit. Does not swallow errors. Raises: Exception: whatever the append path raises. Callers must let this propagate so the surrounding transaction rolls back."*

That is correct for an event which describes a mutation made in the same transaction — the event and the row it describes must commit or roll back together. It becomes a problem only if the mutation is something we no longer want coupled to the sale.

Pre-fix, both call sites sat **inside** their function's outer savepoint:

```
process_sale
  :379    async with db.begin_nested():          indent 8    <- outer savepoint
  :1337   event_type="customer_loyalty_changed"  indent 16   <- INSIDE it
  :1415   await db.commit()                      indent 8    <- outside
refund_sale
  :1501   async with db.begin_nested():          indent 8
  :1933   event_type="customer_loyalty_changed"  indent 20   <- INSIDE
  :1955   await db.commit()                      indent 8
```

So a raised append escaped `async with db.begin_nested()`, whose `__aexit__` rolled the savepoint back — and the savepoint covered the entire sale. Nothing in between could intercept it:

- the only `except` in `sales_service.py:379-1337` is `except HTTPException: ... raise` at `:692`, which re-raises;
- the endpoint is a bare `await` — `sales_endpoints.py:92`: `return await SalesService.process_sale(db, sale_data, current_user)`;
- `core/exception_handlers.py:350` registers `@app.exception_handler(Exception)` and returns **500**.

### 1.2 What was measured

The append was made to fail **only** for `event_type == "customer_loyalty_changed"`, so `sale_created` still appended normally and the loyalty event was the single variable.

**`process_sale`, online sale of 30.00**

| Measured | Result |
|---|---|
| HTTP status (real endpoint, `TestClient`) | **500** |
| `sales` rows | **0** — rolled back |
| `sale_items` | **0** — rolled back |
| `branch_inventory.quantity` | **900** — unchanged, deduction rolled back |
| `drug_batches.remaining_quantity` | **900** — rolled back |
| `customers.loyalty_points` | **0** — unchanged |
| `customers.total_orders` / `total_value` | unchanged |
| `event_log` rows for the customer | **0** — even the `sale_created` event rolled back |

A customer had paid for medicine that the till recorded as never sold.

**`refund_sale`, refund of that sale**

| Measured | Result |
|---|---|
| `sales` row | still present |
| `sales.refund_amount` | **0.0** — rolled back |
| `sales.refunded_at` | **NULL** — rolled back |
| stock restock | **rolled back** |
| `customers.loyalty_points` | **unchanged** — the deduction rolled back |
| `event_log` loyalty events | only the original earn; no reversal |

So a refund could be undone entirely because its *notification* failed, while the money had already been returned at the till.

---

## 2. Outcome two — the sale survives

### 2.1 The change

Small on purpose, and applied to both paths.

```python
await db.flush()                      # points + counters -> OUTER transaction
try:
    async with db.begin_nested():     # the append, alone
        await StockEventEmitter.emit_in_transaction(...)
except Exception:
    logger.exception(...)             # one loud line, sale continues
else:
    customer.mark_as_synced()
```

Post-fix structure — `process_sale` and `refund_sale` are now identically shaped:

```
process_sale
  :379    async with db.begin_nested():          outer savepoint
  :1238   # 19a. Loyalty event, isolated in its own savepoint
  :1264     async with db.begin_nested():        <- the append only
  :1268       event_type="customer_loyalty_changed"
  :1433   await db.commit()
refund_sale
  :1519   async with db.begin_nested():          outer savepoint
  :1936   # 7b. Loyalty reversal event, isolated in its own savepoint.
  :1958     async with db.begin_nested():        <- the append only
  :1962       event_type="customer_loyalty_changed"
  :1995   await db.commit()
```

### 2.2 The flush is load-bearing

`await db.flush()` before `begin_nested()` is not tidiness — it is what makes this correct.

SQLAlchemy autoflushes pending ORM state when raw SQL runs. The emitter executes `text(...)`, so without the explicit flush the `UPDATE customers SET loyalty_points = …` would be emitted **after** `SAVEPOINT`, inside it. The rollback would then take the points with it and reintroduce the original bug in a harder-to-spot form: the sale would survive but the balance would silently vanish.

Flushing first puts the points and the denormalized counters in the outer transaction, where the savepoint cannot reach them.

### 2.3 What is measured after the fix

Same forced failure, same disposable cluster.

**`process_sale`**

| Measured | Result |
|---|---|
| HTTP status | **201** |
| `sales` rows | **1** — kept |
| `sale_items` | **1** — kept |
| `branch_inventory.quantity` | **899** — deduction kept |
| `drug_batches.remaining_quantity` | **899** — kept |
| `customers.loyalty_points` | **30** — kept |
| `customers.total_orders` / `total_value` | **1 / 30.00** — kept |
| `customer_loyalty_changed` events | **0** — not published |
| `sale_created` event | appended normally |

**`refund_sale`**: refund succeeds, `refund_amount` 30.00, `refunded_at` set, points 30 → 0 kept, and the log still holds only the original earn.

Exactly one `logger.exception` line per failure, naming the sale, the customer and the points involved.

---

## 3. Trade-offs

### 3.1 A missed publish is bounded, not cumulative

This is why the absolute-state payload (ADR 0010) is doing real work rather than being tidiness. Each event carries the **whole** balance, so the next one republishes the truth regardless of what was missed. Both directions are pinned by tests:

| Scenario | Server balance | Next event publishes | Device effect |
|---|---|---|---|
| Earn publish fails, then another sale | 30 + 30 = **60** | **60** | jumps straight to 60, no drift |
| Reversal publish fails, then another sale | 30 − 30 + 30 = **30** | **30** | converges on the post-refund balance |

A missed *delta* would have been unrecoverable drift; a missed *absolute assignment* is a gap that closes.

### 3.2 What a failure genuinely costs

- **The device stays on the previous balance** until the next loyalty event — and if the customer never visits again, that is forever. This is the real cost, and it is a staleness cost, not a correctness one: the server's books are right the whole time.
- **The customer row stays `sync_status='pending'`**, because there genuinely is now nothing behind it. That is the honest state, and the `repairStalePendingMarkers` sweep from `3d8a7c1` will not touch it while an event is genuinely in flight.
- **The tier-upgrade `SystemAlert` is inside the savepoint**, so a failed publish also drops that alert. Both describe the same unpublished change, so this is consistent rather than an extra loss.

### 3.3 What did not change

- `sale_created` still aborts the sale if its own append fails. That coupling is deliberate and correct: the sale *is* the event. Only loyalty is decoupled.
- The offline projector path (`SaleProjector._apply_offline_loyalty`) already wrapped its whole award-and-emit block in `try/except` and logged a warning, so an offline sale was never at risk. The two paths are now consistent in outcome, if not in shape.

---

## 4. Tests

`backend.laso/tests/integration/test_loyalty_failure_isolated.py` — 5 tests, all passing.

| Test | Pins |
|---|---|
| `test_sale_survives_a_failed_loyalty_append` | sale, items, stock and points all kept; no loyalty event |
| `test_sale_endpoint_returns_201_not_500` | the real endpoint returns **201** |
| `test_the_next_sale_republishes_the_correct_absolute_balance` | a missed earn is republished as 60 |
| `test_refund_survives_a_failed_loyalty_append` | refund, deduction and `refund_amount` kept |
| `test_a_failed_reversal_is_corrected_by_the_next_event` | a missed reversal is republished as 30 |

Suite counts, on the disposable cluster:

| Suite | Count |
|---|---|
| Backend unit | **240 passed** |
| Backend integration | **208 passed** (was 203; +5) |

The pre-fix suite that asserted the 500 was **deleted rather than left failing**. Its measurements are recorded in §1.2 above and in the `4135439` commit body; the inverted suite is the regression protection.

---

## 5. Two mistakes I made, recorded because they cost time and will recur

**5.1 An unanchored edit put the online block inside `refund_sale`.**
The replacement matched `customer.mark_as_pending_sync()` in *both* functions. The result landed the earn-side block in the refund path — where `points_earned` does not exist — and left `process_sale` with **no loyalty emit at all**. The new tests caught it on the first run as `NameError: name 'points_earned' is not defined`. Fixed by deleting the misplaced block and inserting at an anchor derived from the file (`elif customer:` preceded by the step-19 comment) rather than from a hardcoded line number, which is what made my first two attempts miss as well.

**5.2 The catalogue prices the sale, not the payload.**
A test sale of 20.00 was rejected with `400 Insufficient payment. Required: 30.00, Paid: 20.0` because `process_sale` resolves the unit price from `BranchInventory.selling_price → DrugBatch.selling_price → Drug.unit_price` and ignores `unit_price` in the payload. Same trap as the online-vs-offline drift test in `test_loyalty_convergence.py`. The payload is a request; the catalogue is the source of truth for money.

---

## 6. Open questions this raises

1. **Should `customers:changed` be emitted on the device for a loyalty event that failed server-side?** Nothing arrives, so nothing is emitted — correct, but it means a failed publish is invisible on the device until the next event. A sync-health signal ("the server has a balance you have not seen") would need a different mechanism; nothing in the current design surfaces it.
2. **How long is too long?** §3.2's "until the next visit" is unbounded. If a customer stops coming, the last known device balance is wrong indefinitely. The backfill covers the *initial* convergence but not this class of later drift.
3. **Should the refund's tier-upgrade-style alerts be treated the same way?** No alert exists for a downgrade today, so there is nothing to reason about yet.
