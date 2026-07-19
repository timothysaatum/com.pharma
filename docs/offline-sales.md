# Offline sales continuity

The desktop POS can complete standard cash, card, mobile-money, credit, and
split-payment sales while the browser network is unavailable or FastAPI cannot
be reached. Reconnection sends the sale through sync protocol v2 and refreshes
the local projection from the server.

## Correctness guarantees

- One checkout uses one UUID in the online request, offline journal, local sale,
  sync operation, and server sale.
- A lost online response can fall back locally with that UUID. When sync finds
  the server sale, it acknowledges it without repeating stock, batch, ledger,
  loyalty, or prescription effects.
- Local checkout is one SQLite transaction. The sale row, queue envelope,
  inventory deductions, prescription refill update, and offline journal either
  all commit or all roll back.
- Stock checks use available quantity, `quantity - reserved_quantity`, not only
  physical quantity.
- Sale commands never use generic CRR row merging. Protocol v2 is the only
  client-to-server sale creation path because it applies FEFO, inventory ledger,
  alerts, and prescription side effects under one server transaction.
- Immediate local inventory and prescription changes are projections. Their
  exact CRR database versions are suppressed from upload, then discarded after
  the CRR cursor passes them. This prevents a second server-side deduction.
- The sync queue is repaired before push after startup. Recovery preserves the
  sale UUID as the operation UUID and restores the complete item payload.
- Rapid repeated clicks are blocked synchronously before React rerenders.

## Connectivity and failure matrix

| Scenario | POS result | Reconnection result |
| --- | --- | --- |
| Browser reports offline | Sale commits to local SQLite | Protocol-v2 sale creates one server sale |
| FastAPI is down | Network error falls back to local SQLite | Queue retries with backoff, then creates one server sale |
| Server commits but response is lost | Same sale UUID commits locally | Existing server sale is acknowledged, with no repeated effects |
| Sync response is lost | Queue retains the operation UUID | Server receipt replays the original result |
| App exits after checkout | Atomic transaction contains sale and queue | Queue is pushed on the first sync cycle after restart |
| Queue envelope is missing on an upgraded device | Offline journal remains authoritative | Envelope is rebuilt before push with full items and protocol v2 |
| Local stock is missing or insufficient | Checkout fails | No local sale, stock change, prescription change, or queue row exists |
| Stock is reserved by another workflow | Checkout fails when available stock is insufficient | No partial write exists |
| Prescription is missing, inactive, filled, or from another branch | Checkout fails | No partial write exists |
| Same checkout UUID is retried with the same payload | Original sale is returned | No effects repeat |
| Same checkout UUID is reused with a different cart | Server returns HTTP 409 | Cashier must start a new sale |
| Insurance or approval contract needs live verification | Offline checkout is blocked | Retry when verification service is online |

## Evidence and simulation

Run the deterministic gate tests:

```bash
(cd ui.laso && pnpm exec vitest run \
  src/lib/__tests__/offlineSalesManager.test.ts \
  src/lib/__tests__/crrSaleProjectionRouting.test.ts \
  src/lib/__tests__/localDbMigrationV19.test.ts \
  src/lib/__tests__/localDbMigrationV20.test.ts \
  src/lib/__tests__/syncTableRouting.test.ts \
  src/pages/__tests__/POSPage.checkoutResilience.test.tsx)

(cd ui.laso/src-tauri && cargo test guarded_transaction --lib)

(cd backend.laso && .venv/bin/python -m pytest -q \
  tests/unit/test_sales_service.py -k client_identity)
```

Run the end-to-end ambiguous-response eval:

```bash
(cd backend.laso && \
  .venv/bin/python -m pytest -q evals/test_offline_sales_resilience.py)
```

Successful local capture logs `offline_sale_recorded` with `sale_id`, item
count, and operation ID. An online retry logs `sale_idempotent_replay` with sale
and branch IDs. These lines let support verify whether recovery created a sale
or replayed an existing result.
