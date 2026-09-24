# Offline-First Audit

**Audit date:** 2026-09-24
**Code baseline:** `a13d02b` plus local commit `1b9bccc`
**Phase:** 1, investigation only. No offline behavior was changed as part of this audit.

## 1. Executive findings

The application has a real offline architecture, but the reported “working” and “broken” split is not accurate at the feature level.

- **Sales history** is an offline read path. Sale creation belongs to POS, and refunds are online-only.
- **Reports** are not fully offline. Only the Daily Sales tab has a local fallback; the other active report tabs call the API directly.
- **Prescriptions** have local reads and offline create/update/cancel writes. Prescription-item selection works locally in POS, but the active sale event path does not preserve all prescription/refill semantics.
- **Inventory** has local tables and several local write paths, but the client emits invalid envelopes for several operations, existing server rows are not hydrated through the active event stream, and some UI entry points are network-only.
- **POS** has a real local transaction and FEFO allocation, but the outbox append is outside that transaction and the emitted sale event omits material sale, contract, insurance, prescription, and item fields.
- **Purchases** have a local table, but detail/workflow actions are network-only, suppliers and drugs cannot always be selected offline, and the purchase-order event drops important fields.
- **Customers** have a local table and create/edit fallback, but active status is online-only, fields are silently dropped, and no event is queued when no branch is selected.
- **Users** have no local table, projector, or mutation outbox. They use a six-hour generic cache containing only the most recently fetched page.

The common failure is not “offline code is missing everywhere.” It is that the local read model, local mutation transaction, event payload, server projector, authorization rules, and hydration path were implemented independently and now disagree.

## 2. Active architecture

### 2.1 Production local store

The production desktop app uses a custom Tauri IPC bridge over SQLCipher-backed SQLite.

- Native command selection and browser no-op behavior: `ui.laso/src/lib/localDb.ts:11-40`
- Tauri IPC commands `db_execute`, `db_select`, and `db_execute_transaction`: `ui.laso/src/lib/localDb.ts:119-164`
- SQLCipher dependency: `ui.laso/src-tauri/Cargo.toml:32`
- SQLCipher key handling: `ui.laso/src-tauri/src/lib.rs:6-60`, `ui.laso/src-tauri/src/db.rs:46-56`

In an ordinary browser, `MockDb` returns empty reads and successful zero-row writes. An offline customer or inventory write can therefore report success without persistence unless the app is running through Tauri.

### 2.2 Active sync path

The active frontend pushes and pulls event envelopes through `/api/v1/sync/events`.

- Sync engine startup and polling: `ui.laso/src/lib/syncEngine.ts:75-142`
- Push then pull cycle: `ui.laso/src/lib/syncEngine.ts:202-280`
- Outbox push: `ui.laso/src/lib/syncEngine.ts:288-361`
- Event pull and local projection: `ui.laso/src/lib/syncEngine.ts:363-437`
- API wrapper: `ui.laso/src/api/sync.ts:47-53`
- Backend POST/GET event endpoints: `backend.laso/app/api/v1/endpoints/event_sync_endpoints.py:68-264`

The legacy full-state `SyncService` remains in the repository but is not used by the current frontend event engine.

### 2.3 Local outbox and cursor

- `event_outbox`: `ui.laso/src/lib/localDb.ts:2151-2192`
- Pending outbox query: `ui.laso/src/lib/localDb.ts:2256-2289`
- Pull cursor: `ui.laso/src/lib/localDb.ts:2322-2339`

The outbox and cursor are global rather than scoped by organization and branch. Server event sequence numbers are organization-local, so a cursor from one organization can skip a later organization's lower sequence numbers.

### 2.4 App shell and authentication

- Startup waits for the backend probe before rendering: `ui.laso/src/main.tsx:7-18`
- Probe and heartbeat: `ui.laso/src/api/client.ts:319-357`
- Cached auth restoration: `ui.laso/src/stores/authStore.ts:105-150`
- Startup refresh failure currently clears cached auth: `ui.laso/src/stores/authStore.ts:117-133`
- Sync starts after a branch is selected: `ui.laso/src/stores/authStore.ts:135-150`
- Initial sync gate: `ui.laso/src/App.tsx:140-206`

The initial sync gate can pass immediately because the initial engine status is `idle`. Pages can mount before the first pull has hydrated local tables.

### 2.5 Service worker

No service worker, Workbox configuration, PWA manifest, or CacheStorage path exists. Browser app-shell reload while offline is not supported. The packaged Tauri frontend is bundled locally by Tauri and does not depend on a service worker for its shell.

## 3. Working-reference module traces

### 3.1 Sales history

**Read path**

1. Online list calls the sales API; any timeout/server failure falls back to `localRead.searchSales`: `ui.laso/src/pages/SalesHistoryPage.tsx:904-1005`.
2. Online responses are cached into the local `sales` table: `ui.laso/src/lib/localDb.ts:2086-2148`.
3. Offline list data is reconstructed from local sales: `ui.laso/src/lib/localRead.ts:704-785`.
4. Detail and receipt reads use the same local fallback: `ui.laso/src/pages/SalesHistoryPage.tsx:200-245`.

**Write path**

- The Sales History page has no offline create/update path.
- Refund is always a direct API mutation: `ui.laso/src/pages/SalesHistoryPage.tsx:248-307`.
- Sale creation is owned by POS and uses `OfflineSalesManager`.

**Important divergences inside the working path**

- Offline search only searches the currently loaded page.
- `cacheSales` writes list rows whose `items` are absent as `[]`, so a list refresh can replace cached receipt items.
- Local receipt reconstruction does not join organization/branch metadata.

### 3.2 Reports

**Read path**

- Daily Sales computes a local aggregate from cached sales when the API fails: `ui.laso/src/pages/ReportsPage.tsx:80-189`.
- The other active report tabs call the API directly: `ui.laso/src/pages/ReportsPage.tsx:192-224`.
- Reports are read-only; CSV export is browser-generated: `ui.laso/src/pages/ReportsPage.tsx:283-304`.

**Important divergences inside the working path**

- Local Daily Sales ignores server filters and completed-status semantics.
- Local gross and net revenue both use `total_amount`.
- The local query scans at most 1,000 sales and groups differently from the server.
- If no branch is selected, the local report can include all cached branches.

### 3.3 Prescriptions and prescription items

**Read path**

- Prescription list uses local SQLite when offline: `ui.laso/src/pages/PrescriptionsPage.tsx:144-183`.
- Local search reads medication JSON: `ui.laso/src/lib/localRead.ts:1308-1407`.
- POS prescription selection uses local medications when the API is unavailable: `ui.laso/src/components/pos/PrescriptionSelector.tsx:116-158`.

**Write path**

- Offline create persists a local prescription and appends an event: `ui.laso/src/pages/PrescriptionsPage.tsx:542-574`, `ui.laso/src/lib/localWrite.ts:985-1005`.
- Offline status update/delete uses the local prescription write path: `ui.laso/src/pages/PrescriptionsPage.tsx:190-250`.
- Server prescription projector handles create/update/cancel/refill events: `backend.laso/app/services/sync/eventlog/projectors/prescription.py:64-100`.

**Important divergences inside the working path**

- Prescription local-row commit and outbox append are separate operations.
- Accepted local events do not reconcile the local row's `sync_status` to `synced`.
- Offline POS checkout decrements a local prescription refill but emits no `prescription_refill_used` event.
- The active sale event omits item-level `prescription_id`, `requires_prescription`, and `prescription_verified` fields required by the server sale projector.

## 4. Broken module traces

### 4.1 Inventory

**Reads**

- Main list has an explicit offline SQLite path: `ui.laso/src/pages/InventoryPage.tsx:999-1018`.
- Low-stock, expiring, valuation, and batch reads have local paths: `ui.laso/src/pages/InventoryPage.tsx:1068-1166`.
- Local branch inventory query: `ui.laso/src/lib/localRead.ts:902-1003`.
- Local batch query: `ui.laso/src/lib/localRead.ts:1005-1063`.

**Writes**

- Add batch has an offline fallback: `ui.laso/src/components/inventory/AddBatchForm.tsx:171-228`.
- Stock adjustment queues a local event and updates local aggregate quantity: `ui.laso/src/pages/InventoryPage.tsx:329-357`, `ui.laso/src/lib/localWrite.ts:887-911`.
- Branch drug metadata has a local event/write path: `ui.laso/src/lib/localWrite.ts:795-815`.

**Confirmed divergences**

- Add-batch form setup and the Add Drugs catalogue picker call the API directly, blocking those workflows offline: `ui.laso/src/pages/InventoryPage.tsx:624-635`, `ui.laso/src/pages/InventoryPage.tsx:1200-1211`.
- Offline batch and branch-inventory event builders can emit an empty organization UUID: `ui.laso/src/lib/localWrite.ts:470-575`. The backend envelope schema rejects that request: `backend.laso/app/schemas/event_envelope.py:70-95`.
- Offline stock-adjustment events omit server-required `organization_id`, `previous_quantity`, and `new_quantity`: `ui.laso/src/lib/localWrite.ts:422-468`, `backend.laso/app/services/sync/eventlog/projectors/stock.py:118-179`.
- Local inventory write paths do not maintain batch quantity and sellable quantity consistently.
- Online inventory, batch, transfer, and adjustment endpoints do not emit corresponding events, and there is no bootstrap/backfill path for existing rows.

### 4.2 POS

**Reads**

- Drug/stock search has a local offline path: `ui.laso/src/components/pos/DrugSearchPanel.tsx:130-247`.
- Customer search reads local matches first: `ui.laso/src/components/pos/CartPanel.tsx:118-167`.
- Contracts and prescription medications have local paths: `ui.laso/src/pages/POSPage.tsx:162-194`, `ui.laso/src/components/pos/PrescriptionSelector.tsx:140-151`.

**Writes**

- `OfflineSalesManager` records a sale, batch deductions, aggregate inventory, prescription state, and an offline journal in one SQLite transaction: `ui.laso/src/lib/offlineSalesManager.ts:70-235`.
- The `sale_created` outbox event is appended only after that transaction commits: `ui.laso/src/lib/offlineSalesManager.ts:104-123`.

**Confirmed divergences**

- A crash or IPC failure after local commit but before outbox append can leave a successful local sale that is never queued. The retry path returns the existing sale without repairing the missing event.
- The sale event drops contract, split-payment, insurance, prescription, notes, tax, discount, and other item fields: `ui.laso/src/lib/localWrite.ts:219-291`.
- The server projector expects several omitted item fields and re-runs FEFO at push time: `backend.laso/app/services/sync/eventlog/projectors/sale.py:294-411`.
- The event push path does not enforce the direct `process_sales` permission used by the online sales endpoint.
- Local stock checks can use aggregate quantity without rejecting expired/empty batches consistently with the server.
- No service worker supports browser full reload while the Vite origin is offline.

### 4.3 Purchases

**Reads**

- Purchase-order list is local-first: `ui.laso/src/hooks/usePurchaseOrders.ts:68-132`.
- Detail is server-first and only falls back for classified network/404 errors: `ui.laso/src/hooks/usePurchaseOrderDetail.ts:31-57`.
- Offline suppliers are fabricated from supplier UUIDs stored in local purchase orders: `ui.laso/src/lib/localRead.ts:835-845`.

**Writes**

- Create has an offline local fallback: `ui.laso/src/hooks/usePurchaseOrders.ts:237-307`.
- Submit, approve, reject, cancel, receive, add item, and update item are direct API calls: `ui.laso/src/hooks/usePurchaseOrders.ts:193-235`, `ui.laso/src/hooks/usePurchaseOrderDetail.ts:63-103`.
- Offline create writes a local PO and event: `ui.laso/src/lib/localWrite.ts:919-931`.

**Confirmed divergences**

- PO drug selection always calls the API and has no local fallback: `ui.laso/src/components/purchases/CreatePOModal.tsx:87-105`.
- Supplier creation is online-only; there is no local supplier table.
- The PO event omits supplier ID, PO number, subtotal, tax, shipping, total, expected date, and approval fields: `ui.laso/src/lib/localWrite.ts:578-637`.
- The server projector substitutes the first organization supplier and generates a new PO number/zero financials when those fields are absent: `backend.laso/app/services/sync/eventlog/projectors/purchase_order.py:88-198`.
- Online PO reads are not cached locally, and online PO mutations do not emit events.

### 4.4 Customers

**Reads**

- List has an explicit offline local path: `ui.laso/src/pages/CustomersPage.tsx:79-152`.
- Local list/search query: `ui.laso/src/lib/localRead.ts:430-485`.
- Detail is not read from a dedicated local query; the drawer keeps the list-shaped row when API detail fails: `ui.laso/src/pages/CustomersPage.tsx:181-191`.

**Writes**

- Create/edit fall back to local persistence on classified network errors: `ui.laso/src/components/customers/CustomerForm.tsx:305-365`.
- Active/inactive status is a direct API mutation and is not queued offline: `ui.laso/src/pages/CustomersPage.tsx:156-168`.
- No customer delete action is wired in the page.

**Confirmed divergences**

- `writeLocal.customer` strips address, allergies, chronic conditions, contact preference, marketing consent, and insurance-card fields even though the local table has columns: `ui.laso/src/lib/localWrite.ts:1019-1064`.
- With no selected branch, the local customer write reports success but appends no event: `ui.laso/src/lib/localWrite.ts:1047-1051`.
- The event uses the organization UUID as `authored_by` rather than the current user: `ui.laso/src/lib/localWrite.ts:391-417`.
- Corporate contracts and insurance relationships are selected through network-only paths.
- HTTP 5xx is classified as offline; a committed server create followed by 5xx can fall back with a new UUID and create a duplicate.

### 4.5 Users

**Reads**

- Online list uses `usersApi.list`: `ui.laso/src/pages/UsersPage.tsx:849-861`.
- Offline uses a generic six-hour cache, not the local database: `ui.laso/src/pages/UsersPage.tsx:823-886`, `ui.laso/src/lib/storage.ts:139-205`.
- Only the most recently fetched page is cached. Without a cache, only the authenticated user is shown.

**Writes**

- Create, edit, activate/deactivate, unlock, delete, and password reset are all direct API calls: `ui.laso/src/pages/UsersPage.tsx:158-363`, `ui.laso/src/pages/UsersPage.tsx:910-948`, `ui.laso/src/pages/UsersPage.tsx:667-704`.

**Confirmed divergences**

- There is no local `users` table.
- There is no user aggregate type, backend projector, client outbox builder, server event emitter, or hydration path.
- There is no local role table; offline create/edit sees only roles embedded in the current user.
- `/users` has no effective `manage_users` guard. Actions can render for users the server will reject.
- No user mutation updates durable state or queues an offline operation.

## 5. Module-by-layer divergence matrix

Legend: **OK** matches the working offline read model; **Partial** works on some paths; **Network** is API-only; **Missing** has no implementation; **Diverged** has an implementation whose contract disagrees with another layer.

| Module | UI -> read | Local schema | Initial hydration | Local write | Outbox | Server projector/event emitter | Reconnect/conflict | Auth/offline shell |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Sales history | Partial | OK: `sales` with `items_json` | Diverged: online cache can erase items | Refund Network | Missing for refund | Sale create event only | Diverged: accepted local row may remain pending | Shared startup auth defect |
| Reports | Partial | Uses `sales` only | Diverged: only Daily Sales | Read-only | N/A | No report events | N/A | Shared startup auth defect |
| Prescriptions/items | Partial | `prescriptions.medications` JSON | Diverged: no bootstrap/backfill | Partial create/update/cancel | Diverged, non-atomic | Diverged: no direct mutation emitter; refill event missing | Diverged: accepted status and dependency gaps | Shared startup auth defect |
| Inventory | Partial | Tables exist | Missing for existing server rows | Partial | Invalid payloads/non-atomic | Diverged: no direct mutation emitter | Diverged: request-level poison event | Shared startup auth defect |
| POS | Partial | Tables exist | Diverged: dependent reference data not fully hydrated | Real local transaction | Diverged: outside transaction | Diverged: payload/side effects incomplete | Conflict/failure paths incomplete | Route permission not enforced; no browser SW |
| Purchases | Partial | POs exist; suppliers absent | Missing for existing server rows | Create only | Diverged/lossy | Direct mutations do not emit | Workflow mutations online-only | Shared startup auth defect |
| Customers | Partial | Table exists | Missing for pre-event rows | Partial create/edit | Missing without branch; lossy payload | Events exist but direct fields/dependencies diverge | Conflicts only for customers; 5xx duplicate risk | Shared startup auth defect |
| Users | Network/cache | Missing | Missing | Missing | Missing | Missing | Missing | Expired-token startup can log out; route guard weak |

## 6. Cross-cutting root causes

1. **No canonical event contract per aggregate.** Client builders, backend validators, projectors, and direct REST writers disagree on required fields.
2. **No bootstrap snapshot.** Fresh clients can only hydrate rows already represented by events. Existing inventory, purchases, prescriptions, users, and direct-seeded records are absent.
3. **Non-atomic local mutation plus outbox.** Local read-model changes and event creation are separate IPC calls, so crashes can silently strand state.
4. **Global cursor and outbox.** Organization-local server sequences are paired with one device-global cursor and unscoped pending-event query.
5. **Branch and permission checks live in the wrong layer.** Direct endpoints enforce permissions, but the generic event push path bypasses aggregate permissions and can project unauthorized offline mutations.
6. **No applied-event ledger on the client.** Pulled side effects are not transactionally or idempotently applied, so cursor recovery/replay can double-apply stock/refill effects.
7. **HTTP failure classification is unsafe.** Treating all 5xx as offline can create local duplicates after a committed server mutation; direct timeouts are not consistently classified as offline.
8. **Auth refresh is not offline-aware at startup.** A failed proactive refresh clears cached auth even when the network is unavailable.
9. **Browser and desktop semantics differ.** Browser mode uses a no-op local database, and there is no service worker. Browser full-reload offline cannot work without the dev server.
10. **Tests validate helpers, not contracts.** Unit tests mock the local write layer, backend projector tests use richer hand-built payloads, and E2E fixtures often insert log rows or read models directly instead of using the active event endpoint.

## 7. E2E harness audit

The existing Playwright lane is useful but does not satisfy the requested real-stack proof.

- Chromium runs against Vite: `ui.laso/playwright.config.ts:3-22`.
- The Tauri bridge is replaced with Playwright IPC backed by `node:sqlite`: `ui.laso/tests/e2e/helpers/tauri-bridge.ts:22-203`.
- That bridge is in-memory by default and does not exercise Tauri IPC, SQLCipher, OS keyring, native WAL behavior, or the Rust transaction implementation.
- Backend fixtures insert directly into PostgreSQL `event_log` and manually project selected customer rows: `ui.laso/tests/e2e/helpers/backend-db.ts:147-241`.
- Fixtures therefore bypass `/sync/events`, Pydantic envelope validation, `EventRouter`, backend authorization, dependencies, dead-letter behavior, and projectors.
- Existing tests generally log browser errors but do not fail on every console error or unhandled rejection.
- Existing tests do not use `BrowserContext.setOffline(true)` for the required cold/warm offline reload scenarios.
- No service worker exists to assert.
- `tauri-driver` and `WebKitWebDriver` are not installed on this machine, although a graphical session, PostgreSQL, Docker, and WebKitGTK runtime are available.

A compliant baseline therefore needs two lanes:

1. A native Tauri/WebKit lane for real SQLCipher/local transaction evidence.
2. A Chromium lane for real FastAPI/PostgreSQL/API/UI navigation and deterministic browser-context offline tests.

Until the native lane is enabled, local-store evidence must be labeled as Node SQLite adapter evidence, not production Tauri evidence.

## 8. Baseline evidence collected

Static evidence:

- Full active client event trace from UI to SQLite/outbox/API.
- Local schema and migration inventory through schema version 31.
- Event-to-projector inventory and backend mutation emitter inventory.
- Existing unit, integration, and E2E coverage inspection.

Runtime baseline will be captured in Phase 2 and recorded in `docs/offline-report.md`. It must include the exact command, test matrix, database evidence, and failures before any Phase 3 fix.
