# Pharmacy Inventory System - Offline-First Capabilities Assessment

**Assessment Date:** May 24, 2026  
**Status:** ✅ COMPREHENSIVE OFFLINE-FIRST ARCHITECTURE IMPLEMENTED

---

## Executive Summary

The LASO pharmacy inventory system has a **well-architected offline-first architecture** built on Tauri + local SQLite with a sophisticated sync engine. The system can operate completely offline and reconcile changes when reconnected.

### Key Findings:
- ✅ **Local SQLite database** for offline storage via Tauri plugin
- ✅ **Sync engine** with push-first, then pull strategy
- ✅ **Conflict resolution** with server-wins and manual-required modes
- ✅ **Idempotent operations** for network resilience
- ✅ **Network event listeners** for online/offline state
- ✅ **Tauri storage layer** with localStorage fallback
- ⚠️ **NO service workers** (not needed due to Tauri architecture)
- ⚠️ **Limited UI conflict resolution** (conflicts tracked but no UI dialog yet)

---

## 1. FRONTEND OFFLINE SUPPORT

### 1.1 Local Database Storage ✅

**Implementation:** [ui.laso/src/lib/localDb.ts](ui.laso/src/lib/localDb.ts)

- **Database:** SQLite via `@tauri-apps/plugin-sql`
- **Database File:** `sqlite:laso.db` (persisted locally)
- **Schema Version:** 4 migrations (tracks with `PRAGMA user_version`)

#### Schema Design

| Table | Scope | Access | Sync Tracking |
|-------|-------|--------|---------------|
| `drugs` | Org-level | Read-only pull | `sync_status`, `sync_version` |
| `drug_categories` | Org-level | Read-only pull | `sync_status`, `sync_version` |
| `price_contracts` | Org-level | Read-only pull | `sync_status`, `sync_version` |
| `customers` | Org-level | Pull + push | `sync_status`, `sync_version` |
| `branch_inventory` | Branch-level | Read/write | `sync_status`, `sync_version` |
| `drug_batches` | Branch-level | Read/write | `sync_status`, `sync_version` |
| `sales` | Branch-level | Read/write | `sync_status`, `sync_version` |
| `stock_adjustments` | Branch-level | Read/write | `sync_status`, `sync_version` |
| `purchase_orders` | Branch-level | Read/write | `sync_status`, `sync_version` |
| `sync_queue` | Local metadata | Append-only | Tracks pending operations |
| `sync_meta` | Local metadata | Append-only | Stores `last_sync_at` |

#### Migrations

**V1 - Initial Schema:**
```
- Org-level tables (drugs, drug_categories, price_contracts, customers)
- Branch-level tables (branch_inventory, drug_batches, sales, stock_adjustments, purchase_orders)
- sync_queue for pending operations tracking
- sync_meta for sync timestamp tracking
```

**V2-V4 - Schema Evolution:**
- Adds prescription fields (`prescription_number`, `prescriber_name`)
- Adds receipt tracking (`receipt_printed`, `receipt_emailed`)
- Consolidates discount handling (`discount_amount`)

**Indexes for Performance:**
```
idx_drugs_org, idx_drugs_active, idx_drugs_type,
idx_inv_branch, idx_batch_branch, idx_batch_expiry,
idx_sales_branch, idx_sales_status, idx_queue_table,
idx_customer_phone, idx_customer_email
```

### 1.2 Sync Queue Mechanism ✅

**Location:** [ui.laso/src/lib/localDb.ts](ui.laso/src/lib/localDb.ts#L311)

```typescript
// Tracks pending operations for push when online
CREATE TABLE sync_queue (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name      TEXT NOT NULL,
  record_id       TEXT NOT NULL,
  operation       TEXT NOT NULL DEFAULT 'create',  // create|update|delete
  sync_version    INTEGER NOT NULL DEFAULT 1,
  payload_json    TEXT NOT NULL,
  created_offline_at TEXT NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  error           TEXT,
  UNIQUE(table_name, record_id)
);
```

**Operations Tracked:**
- `sales`: Create sales offline, push when online
- `drug_batches`: Add stock receipts offline
- `stock_adjustments`: Record inventory changes offline
- `branch_inventory`: Update local quantities offline
- `purchase_orders`: Create POs offline
- `customers`: Create customers offline with phone/email dedup

### 1.3 Sync Engine Architecture ✅

**Location:** [ui.laso/src/lib/syncEngine.ts](ui.laso/src/lib/syncEngine.ts)

#### Sync Flow

```
┌─────────────────────────────────────┐
│  App Goes Online/Offline            │
│  navigator.onLine event fires       │
└────────────────┬────────────────────┘
                 │
    ┌────────────┴────────────┐
    │                         │
    ▼ Online                  ▼ Offline
  PUSH                      "offline" state
  PULL                      Queue grows
  Mark synced               No network calls
    │
    └─────► PULL again
            Apply deltas
            Mark synced
```

#### Key Features

1. **Push-First Strategy**
   - Sends pending local changes to server first
   - Prevents conflicts from client changes being overwritten by server
   - Handles: sales, batches, adjustments, inventory, POs, customers

2. **Pull Strategy** 
   - Fetches all records changed since `last_sync_at`
   - Pagination via `has_more` flag
   - REPEATABLE READ transaction isolation (prevents phantom reads)

3. **Network State Management**
   - Listens to `window.online` and `window.offline` events
   - Automatic sync on reconnection
   - Periodic sync on timer (default: 30 seconds)

4. **Status Tracking**
   ```typescript
   type SyncStatus = "idle" | "syncing" | "error" | "offline";
   ```

5. **Conflict Handling**
   ```typescript
   interface PushConflict {
     local_id: string;
     table_name: string;
     local_version: number;
     server_version: number;
     server_record: Record<string, unknown>;
     resolution: "server_wins" | "local_wins" | "manual_required";
   }
   ```

**File:** [ui.laso/src/lib/syncEngine.ts](ui.laso/src/lib/syncEngine.ts#L1)

### 1.4 Offline Write Operations ✅

**Location:** [ui.laso/src/lib/localWrite.ts](ui.laso/src/lib/localWrite.ts)

Public API for offline writes:

```typescript
export const writeLocal = {
  sale: async (sale) => {
    // Destructures items array (not a DB column)
    // Writes to sales table + sync_queue
  },
  
  drugBatch: async (batch) => {
    // Strips computed fields (days_until_expiry, is_expired, is_expiring_soon)
    // Writes to drug_batches table + sync_queue
  },
  
  inventory: async (branchId, drugId, quantityDelta) => {
    // Updates or creates branch_inventory
    // Auto-creates row if it doesn't exist
    // Enqueues for sync
  },
  
  stockAdjustment: async (adjustment) => {
    // Records adjustments with reason
    // Enqueues for sync
  },
  
  purchaseOrder: async (po) => {
    // Creates POs locally
    // Enqueues for sync
  },
  
  customer: async (customer) => {
    // Creates customers with phone/email dedup on server
    // Enqueues for sync
  }
}
```

#### Offline Write Pattern

```typescript
// When offline, operations like this work seamlessly:
const sale = { id: uuid(), customer_id: "...", items: [...], ... };
await writeLocal.sale(sale);
// → Written to local DB immediately
// → Added to sync_queue
// → Available for offline receipt printing
// → Will push to server when online
```

### 1.5 Network State Management ✅

**Location:** [ui.laso/src/lib/syncEngine.ts](ui.laso/src/lib/syncEngine.ts#L74)

```typescript
// Bound function references (prevent memory leaks)
private readonly _onOnline = () => this.onOnline();
private readonly _onOffline = () => this.onOffline();

// In start():
window.addEventListener("online", this._onOnline);
window.addEventListener("offline", this._onOffline);

// Check current state:
if (navigator.onLine) {
  this.sync();  // Trigger sync immediately
} else {
  this.setStatus("offline");
}

// Periodic sync check (only when online):
this.intervalId = setInterval(() => {
  if (navigator.onLine && !this._isSyncing) {
    this.sync();
  }
}, intervalMs);  // Default: 30 seconds
```

### 1.6 Tauri Storage Layer ✅

**Location:** [ui.laso/src/lib/storage.ts](ui.laso/src/lib/storage.ts)

**Dual-Stack Storage:**

```typescript
// Priority 1: Tauri plugin-store (native persistence)
// - Used in packaged Tauri app
// - Auto-save enabled
const store = await load("laso.bin", { autoSave: true });

// Priority 2: localStorage (fallback)
// - Used in browser/dev mode
// - Falls back if Tauri plugin unavailable

// Auth tokens stored in:
export const authStorage = {
  ACCESS_TOKEN: "auth.access_token",
  REFRESH_TOKEN: "auth.refresh_token",
  USER: "auth.user",
  BRANCH: "session.branch_id"
}
```

### 1.7 API Client Architecture ✅

**Location:** [ui.laso/src/api/client.ts](ui.laso/src/api/client.ts)

#### Error Handling

```typescript
// Network errors detected:
if (err.code === "ECONNREFUSED" || err.code === "ERR_NETWORK")
  return "Cannot connect to server. Make sure the backend is running.";
if (err.code === "ECONNABORTED")
  return "Request timed out. Try again.";
if (err.code === "ERR_CANCELED")
  return "";  // Aborted requests ignored

// Token refresh queue for 401 responses:
- Prevents thundering herd on token expiry
- Queues pending requests during refresh
- Prevents retry loops on refresh endpoint itself
```

#### Timeout Configuration
- `timeout: 15_000` (15 seconds) per request

### 1.8 React Query Integration ✅

**Location:** [ui.laso/src/App.tsx](ui.laso/src/App.tsx#L24)

```typescript
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { 
      staleTime: 1000 * 60 * 5,  // 5 minutes
      retry: 1                     // Single retry on failure
    },
  },
});
```

### 1.9 Sync Status Hook ✅

**Location:** [ui.laso/src/hooks/useSyncStatus.ts](ui.laso/src/hooks/useSyncStatus.ts)

Exposes sync state to React components:

```typescript
export interface SyncState {
  status: SyncStatus;           // idle|syncing|error|offline
  pendingCount: number;         // Records waiting to push
  lastSyncAt: string | null;    // Timestamp of last sync
  conflicts: PushConflict[];    // Conflicts requiring resolution
  syncNow: () => Promise<void>; // Manual sync trigger
  dismissConflict: (localId: string) => void;  // Conflict dismissal
}
```

### 1.10 Service Workers ⚠️

**Status:** NOT IMPLEMENTED

**Reasoning:**
- Tauri apps don't use service workers (not web-based)
- Local SQLite provides offline storage
- Sync engine handles all cache coordination
- Web workers unnecessary for this architecture

---

## 2. BACKEND SYNC CAPABILITIES

### 2.1 Sync Endpoints ✅

**Location:** [backend.laso/app/api/v1/endpoints/sync_endpoints.py](backend.laso/app/api/v1/endpoints/sync_endpoints.py)

#### Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/sync/pull` | POST | Pull delta changes from server |
| `/sync/push` | POST | Push pending records to server |
| `/sync/status` | GET | Server timestamp for clock calibration |

#### POST /sync/pull

```python
@router.post("/pull", response_model=PullResponse)
async def pull(
  request: PullRequest,
  current_user: User = Depends(get_current_active_user),
  db: AsyncSession = Depends(get_db),
) -> PullResponse:
```

**Request:**
```typescript
interface PullRequest {
  branch_id: string;
  last_sync_at: string | null;  // null = full sync
  tables?: string[];
}
```

**Response:**
```typescript
interface PullResponse {
  drugs: Drug[];
  drug_categories: DrugCategory[];
  price_contracts: PriceContract[];
  customers: Customer[];
  branch_inventory: BranchInventory[];
  drug_batches: DrugBatch[];
  sales: Sale[];
  purchase_orders: PurchaseOrder[];
  sync_timestamp: string;       // Server "now" at transaction open
  has_more: boolean;            // Pagination flag
  total_records: number;
}
```

**Behavior:**
- First sync: omit `last_sync_at` → full dataset
- Subsequent syncs: pass previous `sync_timestamp` as `last_sync_at`
- Only records with `updated_at > last_sync_at` returned
- Org-level data (drugs, contracts) returned for entire org
- Branch-level data (sales, inventory) filtered to requesting branch
- Only synced records returned (pending records excluded to prevent loops)

#### POST /sync/push

```python
@router.post("/push", response_model=PushResponse)
async def push(
  request: PushRequest,
  current_user: User = Depends(get_current_active_user),
  db: AsyncSession = Depends(get_db),
) -> PushResponse:
```

**Request:**
```typescript
interface PushRequest {
  branch_id: string;
  records: PushRecord[];  // Each record: {table_name, local_id, operation, sync_version, data, created_offline_at}
}
```

**Response:**
```typescript
interface PushResponse {
  accepted: PushResult[];        // Successfully synced
  conflicts: PushConflict[];     // Conflict detected
  failed: PushResult[];          // Failed to process
  total_received: number;
  total_accepted: number;
  total_conflicts: number;
  total_failed: number;
  sync_timestamp: string;
  next_pull_timestamp: string;   // Timestamp to use for next pull
}
```

#### GET /sync/status

```python
@router.get("/sync/status")
async def sync_status(
  current_user: User = Depends(get_current_active_user),
) -> dict:
  return {
    "server_time": datetime.now(timezone.utc).isoformat(),
    "organization_id": str(current_user.organization_id),
    "user_id": str(current_user.id),
  }
```

### 2.2 Sync Service Implementation ✅

**Location:** [backend.laso/app/services/sync/sync_service.py](backend.laso/app/services/sync/sync_service.py)

#### Pull Strategy

**Transaction Isolation:**
```python
# All queries within REPEATABLE READ transaction
await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
# SQLite: Already SERIALIZABLE (stronger), SET ignored safely
```

**Prevents Phantom Reads:**
- Captures server timestamp BEFORE opening transaction
- All subsequent queries see consistent DB snapshot
- Records committed between queries won't be missed by next pull

**Sync Filtering:**
```python
# For org-level (pull-only):
select(Drug).where(
  Drug.organization_id == organization_id,
  Drug.updated_at > since,  # if since is not None
)

# For branch-level (read/write):
select(Sale).where(
  Sale.branch_id == branch_id,
  Sale.sync_status == "synced",  # CRITICAL: exclude pending
  Sale.updated_at > since,
)
```

#### Push Strategy

**Savepoint-Based Error Isolation:**
```python
for record in request.records:
  try:
    async with db.begin_nested():  # savepoint per record
      push_result, conflict = await SyncService._handle_record(...)
  except Exception as exc:
    # Error on record N doesn't roll back records 1..N-1
    failed.append(PushResult(...))

# Single commit for entire batch
await db.commit()
```

#### Conflict Resolution Rules

```python
CONFLICT_RESOLUTION: Dict[str, str] = {
  "sales":             "server_wins",
  "branch_inventory":  "server_wins",
  "drug_batches":      "server_wins",
  "stock_adjustments": "server_wins",
  "purchase_orders":   "server_wins",
  "customers":         "manual_required",
}
```

**Server-Wins Logic:**
```python
if conflict.resolution == "server_wins":
  # Apply server record, remove from client queue
  await this.applyServerRecord(conflict.table_name, conflict.server_record)
  await dequeue(conflict.table_name, conflict.local_id)
  await markSynced(...)
```

**Manual-Required Logic (Customers):**
```python
else:
  # Surface to user via pendingConflicts array
  this.pendingConflicts.push(conflict)
  await markQueueError(
    conflict.table_name,
    conflict.local_id,
    "Conflict: manual resolution required"
  )
```

### 2.3 Idempotency ✅

**Implemented for all push handlers:**

```python
# Sales idempotency check:
existing = await db.execute(
  select(Sale).where(
    Sale.organization_id == organization_id,
    or_(
      Sale.id == record.local_id,
      Sale.sale_number == record.data.get("sale_number"),
    ),
  )
).scalar_one_or_none()

if existing:
  return PushResult(
    local_id=record.local_id,
    table_name="sales",
    server_id=str(existing.id),
    success=True,  # Duplicate ignored gracefully
  ), None
```

**Network Retry Safe:**
- Re-pushing the same record returns success
- No duplicate entries created
- Organization scope prevents cross-org collisions

### 2.4 Field Whitelisting ✅

**Per-table whitelists prevent unauthorized writes:**

```python
_SALE_WRITABLE: frozenset[str] = frozenset({
  "id", "sale_number", "customer_id", "customer_name",
  "subtotal", "discount_amount", "tax_amount", "total_amount",
  # ... 30+ fields explicitly listed
})

_BATCH_WRITABLE: frozenset[str] = frozenset({
  "id", "drug_id", "batch_number",
  "quantity", "remaining_quantity",
  "manufacturing_date", "expiry_date",
  # ... fields
})

# Client attempts to write organization_id, branch_id, etc. are silently ignored
safe_data = _whitelist(record.data, _SALE_WRITABLE)
safe_data["organization_id"] = str(organization_id)  # Server-set
safe_data["branch_id"] = str(branch_id)              # Server-set
```

### 2.5 Sync Metadata Stripping ✅

```python
_SYNC_META_KEYS: frozenset[str] = frozenset({
  "sync_status",      # Server-managed
  "sync_hash",        # Server-managed
  "last_synced_at",   # Server-managed
})

def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
  """Strip sync-metadata keys. Preserve intentional None values."""
  return {k: v for k, v in data.items() if k not in _SYNC_META_KEYS}
```

### 2.6 Database Schema Tracking ✅

Each record includes sync metadata:

```python
# Client-side schema:
class SyncFields:
  sync_status: str        # "synced" | "pending" | "conflict" | "deleted"
  sync_version: int       # Version number for conflict detection
  synced_at: str | None   # Last sync timestamp

# Server maintains:
- last_synced_at
- created_at, updated_at
- is_deleted (soft deletes)
```

---

## 3. STORAGE STRATEGY

### 3.1 Frontend Storage ✅

**Multi-Tier Approach:**

```
Tier 1: Tauri SQLite Database (primary)
│
├─ Connection: @tauri-apps/plugin-sql
├─ Path: sqlite:laso.db
├─ Scope: Full pharmacy database
├─ Persistence: File system (persistent across restarts)
└─ Size: No hard limit (device storage dependent)

Tier 2: Tauri Store (auth + settings)
│
├─ Plugin: @tauri-apps/plugin-store
├─ Path: laso.bin
├─ Scope: Auth tokens, session state
├─ Auto-save: True
└─ Fallback: localStorage if plugin unavailable

Tier 3: Browser localStorage (fallback)
│
├─ Scope: Auth tokens, user preferences
├─ Persistence: Browser-dependent
└─ Used when: Tauri plugins unavailable
```

### 3.2 Response Caching ✅

**Implicit via local database:**
- `getDb()` returns lazy-loaded singleton connection
- Queries against local DB are always cached
- No additional caching layer needed

**API responses cached by:**
- React Query (5-minute staleTime default)
- Local SQLite sync pulls
- Manual cache invalidation via app events

### 3.3 Backend Storage ✅

**Database:** PostgreSQL with SQLAlchemy ORM

**Async Driver:** `psycopg2-binary` (from requirements.txt)

**Key Tables:**
- All sync data persisted to PostgreSQL
- Soft deletes via `is_deleted` flag
- Audit timestamps: `created_at`, `updated_at`
- Branch-scoped data isolation

### 3.4 Offline Library Dependencies ⚠️

**Status:** NOT USED

**Why:** Not needed due to Tauri architecture

**Analysis of package.json:**
```json
{
  "@tauri-apps/plugin-sql": "^2.3.2",      // Direct SQLite support
  "@tauri-apps/plugin-store": "^2.4.2",    // Persistent key-value store
  "zustand": "^5.0.11",                    // State management
  "@tanstack/react-query": "^5.90.21",     // Server state caching
}
```

**Not present:**
- No `dexie` (IndexedDB wrapper)
- No `pouchdb` (offline-first DB)
- No `rxdb` (reactive DB)
- No `idb` (IndexedDB async wrapper)
- No service worker libraries

**Reasoning:** Tauri's native SQLite is superior for this use case.

---

## 4. CURRENT NETWORK HANDLING

### 4.1 Network Listeners ✅

**Location:** [ui.laso/src/lib/syncEngine.ts](ui.laso/src/lib/syncEngine.ts#L74)

```typescript
// Lifecycle: start() and stop()

start(branchId: string, intervalMs = 30_000): void {
  window.addEventListener("online", this._onOnline);
  window.addEventListener("offline", this._onOffline);
  
  if (navigator.onLine) {
    this.sync();
  } else {
    this.setStatus("offline");
  }
  
  // Periodic polling
  this.intervalId = setInterval(() => {
    if (navigator.onLine && !this._isSyncing) {
      this.sync();
    }
  }, intervalMs);
}

stop(): void {
  clearInterval(this.intervalId);
  window.removeEventListener("online", this._onOnline);
  window.removeEventListener("offline", this._onOffline);
}
```

### 4.2 Error Handling ✅

**Location:** [ui.laso/src/api/client.ts](ui.laso/src/api/client.ts#L116)

```typescript
export function parseApiError(err: unknown): string {
  if (axios.isAxiosError(err)) {
    // Network errors
    if (err.code === "ECONNREFUSED" || err.code === "ERR_NETWORK")
      return "Cannot connect to server. Make sure the backend is running.";
    if (err.code === "ECONNABORTED")
      return "Request timed out. Try again.";
    if (err.code === "ERR_CANCELED")
      return "";  // Aborted silently
    
    // FastAPI errors
    if (typeof data.detail === "string")
      return data.detail;
    if (Array.isArray(data.detail))
      return data.detail.map((d) => d.msg).join(", ");
  }
  return "An unexpected error occurred";
}
```

### 4.3 Retry Logic ✅

**In Sync Engine:**
```typescript
// On push/pull error:
catch (err) {
  console.warn("[SyncEngine] Pull failed:", err);
  // Error state set, but no automatic retry
  // Next periodic cycle (30s) will retry
}
```

**Per-record retry tracking (backend):**
```python
# sync_queue table:
attempts        INTEGER NOT NULL DEFAULT 0,
last_attempt_at TEXT,
error           TEXT,
```

**React Query:**
```typescript
queries: { retry: 1 }  // Single automatic retry on failure
```

### 4.4 Graceful Offline Degradation ✅

**App Initialization:**
```typescript
// In App.tsx:
function SyncGate({ children }) {
  if (!initialSyncDone) {
    return <LoadingScreen />;  // Wait for initial sync to settle
  }
  return <>{children}</>;
}
```

**Sync Status Indicator:**
Users can see via `useSyncStatus()` hook:
- Current sync status (idle/syncing/error/offline)
- Pending record count
- Last sync timestamp
- Pending conflicts

---

## 5. SYNC ARCHITECTURE

### 5.1 Sync Endpoints ✅

Comprehensive endpoints defined in:
- **Frontend:** [ui.laso/src/api/sync.ts](ui.laso/src/api/sync.ts)
- **Backend:** [backend.laso/app/api/v1/endpoints/sync_endpoints.py](backend.laso/app/api/v1/endpoints/sync_endpoints.py)

### 5.2 Conflict Detection ✅

**Version-Based Detection:**

```python
# Server tracks versions:
local_version = record.sync_version      # Client sent
server_version = existing_record.sync_version

if server_version > local_version:
  # Server record is newer
  conflict = PushConflict(
    resolution="server_wins",
    server_record=existing_record,
  )
```

**Phone/Email Deduplication (Customers):**

```python
# When pushing customer:
existing_customer = await db.execute(
  select(Customer).where(
    Customer.organization_id == org_id,
    or_(
      Customer.phone == safe_data.get("phone"),
      Customer.email == safe_data.get("email"),
    ),
  )
).scalar_one_or_none()

if existing_customer and existing_customer.id != local_id:
  # Duplicate found
  conflict = PushConflict(resolution="manual_required")
```

### 5.3 Conflict Resolution Strategies ✅

| Strategy | Tables | Behavior |
|----------|--------|----------|
| **server_wins** | sales, inventory, batches, adjustments, POs | Server version overwrites client |
| **manual_required** | customers | Surface to UI for user resolution |

**Implementation:**

```typescript
// Client-side handling (syncEngine.ts):
if (conflict.resolution === "server_wins") {
  // Apply server record immediately
  await this.applyServerRecord(
    conflict.table_name,
    conflict.server_record
  );
  await dequeue(conflict.table_name, conflict.local_id);
  await this.markSynced(...);
} else if (resolution === "manual_required") {
  // Add to pending conflicts
  this.pendingConflicts.push(conflict);
  await markQueueError(..., "Conflict: manual resolution required");
}
```

### 5.4 Sync Flow Diagram

```
┌─── OFFLINE ───┐
│               │
│ User creates  │
│ sale → write  │
│ to localDB +  │
│ sync_queue    │
└───────┬───────┘
        │
        ▼
    [ONLINE]
        │
        ▼
    ┌─────────────────────────┐
    │  PUSH Phase             │
    │  (500 records at a time)│
    ├─────────────────────────┤
    │ 1. Get pending queue    │
    │ 2. Serialize to POST    │
    │ 3. Send to /sync/push   │
    │ 4. Process response:    │
    │    - accepted → remove  │
    │    - conflicts → mark   │
    │    - failed → retry     │
    └────────────┬────────────┘
                 │
                 ▼
    ┌─────────────────────────┐
    │  PULL Phase             │
    │  (delta from server)    │
    ├─────────────────────────┤
    │ 1. GET /sync/status     │
    │ 2. POST /sync/pull      │
    │ 3. Receive records      │
    │ 4. Upsert to localDB    │
    │ 5. Check has_more →     │
    │    loop if true         │
    │ 6. Save last_sync_at    │
    └────────────┬────────────┘
                 │
                 ▼
            [SYNCED]
```

### 5.5 Last Sync Tracking ✅

**Storage:** [ui.laso/src/lib/localDb.ts](ui.laso/src/lib/localDb.ts#L341)

```typescript
// sync_meta table:
key   TEXT PRIMARY KEY,    // "last_sync_at"
value TEXT NOT NULL        // ISO8601 timestamp

// Helpers:
export async function getLastSyncAt(): Promise<string | null> {
  // Returns ISO timestamp or null (first sync)
}

export async function setLastSyncAt(timestamp: string): Promise<void> {
  // Stores timestamp for next pull
}
```

**Sync Timestamp Workflow:**

1. Client pulls: `GET /sync/pull` with `last_sync_at` from local storage
2. Server responds with `sync_timestamp` (server's "now" at transaction open)
3. Client stores `sync_timestamp` as new `last_sync_at`
4. Next pull uses the stored timestamp
5. **Prevents:** Records falling between two pulls

---

## 6. INTEGRATION POINTS

### 6.1 App Initialization Flow ✅

**File:** [ui.laso/src/stores/authStore.ts](ui.laso/src/stores/authStore.ts)

```typescript
// Step 1: Login
login: async (username, password) => {
  const data = await authApi.login({ username, password });
  // ... store user + derive setup state ...
  
  if (setupState === "ready" && branchId) {
    syncEngine.start(branchId);  // ← START SYNC
  }
}

// Step 2: Persistent session recovery
initialize: async () => {
  const [token, user, branchId] = await Promise.all([
    authStorage.getAccessToken(),
    authStorage.getUser(),
    authStorage.getActiveBranch(),
  ]);
  
  if (token && user && activeBranchId) {
    syncEngine.start(activeBranchId);  // ← RESTORE SYNC
  }
}

// Step 3: Logout cleanup
logout: async () => {
  syncEngine.stop();  // ← STOP SYNC
  await authApi.logout();
  await authStorage.clearTokens();
}
```

### 6.2 React Components Integration ✅

**Hook Usage Pattern:**

```typescript
// In any component:
import { useSyncStatus } from "@/hooks/useSyncStatus";

export function MyComponent() {
  const { 
    status,         // "idle" | "syncing" | "error" | "offline"
    pendingCount,   // Number of unsync'd records
    lastSyncAt,     // ISO timestamp
    conflicts,      // PushConflict[]
    syncNow,        // Manual trigger
    dismissConflict,
  } = useSyncStatus();
  
  return (
    <>
      {status === "offline" && <OfflineAlert />}
      {pendingCount > 0 && <PendingBadge count={pendingCount} />}
      {conflicts.length > 0 && <ConflictPanel conflicts={conflicts} />}
    </>
  );
}
```

### 6.3 Event Bus for Cache Invalidation ✅

**File:** [ui.laso/src/lib/events.ts](ui.laso/src/lib/events.ts)

```typescript
type AppEventType =
  | "drugs:changed"
  | "inventory:changed"
  | "sales:changed"
  | "purchases:changed"
  | "customers:changed";

// Usage:
appEvents.emit("sales:changed");  // After offline sale saved

// Listeners (in hooks):
useAppEvent("sales:changed", () => {
  refetchSalesData();  // Re-fetch from localDB or server
});
```

---

## 7. TAURI-SPECIFIC FEATURES

### 7.1 Tauri Plugins Installed ✅

**From package.json:**
```json
"@tauri-apps/api": "^2",
"@tauri-apps/plugin-sql": "^2.3.2",
"@tauri-apps/plugin-store": "^2.4.2",
```

**Capabilities:**
- ✅ Local SQLite database
- ✅ Persistent key-value store
- ✅ File system access
- ✅ Native OS dialogs
- ✅ App window management

### 7.2 Build Configuration ✅

**File:** [ui.laso/src-tauri/Cargo.toml](ui.laso/src-tauri/Cargo.toml)

Tauri dependencies configured, SQLite plugin registered in `src-tauri/src/lib.rs`.

---

## 8. KNOWN LIMITATIONS & GAPS

### 8.1 Conflict Resolution UI ⚠️

**Current State:**
- ✅ Conflicts detected and tracked
- ✅ Available via `useSyncStatus().conflicts`
- ⚠️ No UI component to resolve "manual_required" conflicts
- ⚠️ User cannot manually choose local vs server version

**Impact:** Customers with duplicate phone/email cannot resolve conflicts.

**Recommendation:** Implement conflict resolution dialog:
```typescript
// Missing component:
<ConflictResolutionDialog
  conflict={conflict}
  onResolution={(choice) => {
    if (choice === "local") { /* keep local */ }
    if (choice === "server") { /* accept server */ }
  }}
/>
```

### 8.2 Offline Cache Warming ⚠️

**Current State:**
- ✅ Automatic on first sync
- ⚠️ No manual "download all data" option
- ⚠️ User waits for SyncGate on slow connections

**Impact:** Slow initial load on high-latency networks.

**Recommendation:** Add background download button or pre-fetch on login.

### 8.3 Partial Sync ⚠️

**Current State:**
- ✅ Supports `tables` parameter in PullRequest
- ⚠️ Frontend always syncs all tables
- ⚠️ No selective table sync UI

**Impact:** Bandwidth waste if only need to update drugs.

**Recommendation:** Add table selector if bandwidth is a constraint.

### 8.4 Compression ⚠️

**Current State:**
- ⚠️ No compression on sync payloads
- ⚠️ No gzip in axios config

**Impact:** Bandwidth usage on slow networks.

**Recommendation:** Add gzip compression to apiClient or nginx reverse proxy.

### 8.5 Sync Scheduling ⚠️

**Current State:**
- ✅ Fixed 30-second interval
- ⚠️ No exponential backoff on repeated failures
- ⚠️ No user-configurable sync frequency

**Impact:** High server load during outages, then immediate retry storm.

**Recommendation:** Exponential backoff: 5s → 10s → 30s → 60s → 5m.

### 8.6 Storage Quota Warnings ⚠️

**Current State:**
- ⚠️ No monitoring of local SQLite size
- ⚠️ No cleanup/archival of old data

**Impact:** Disk space exhaustion possible on devices with small storage.

**Recommendation:** Implement data retention policy (e.g., archive sales older than 90 days).

---

## 9. VERIFICATION CHECKLIST

| Component | Status | File |
|-----------|--------|------|
| Local SQLite DB | ✅ | [localDb.ts](ui.laso/src/lib/localDb.ts) |
| Sync Queue | ✅ | [localDb.ts](ui.laso/src/lib/localDb.ts#L311) |
| Sync Engine | ✅ | [syncEngine.ts](ui.laso/src/lib/syncEngine.ts) |
| Network Listeners | ✅ | [syncEngine.ts](ui.laso/src/lib/syncEngine.ts#L74) |
| API Client | ✅ | [client.ts](ui.laso/src/api/client.ts) |
| Offline Writes | ✅ | [localWrite.ts](ui.laso/src/lib/localWrite.ts) |
| Tauri Storage | ✅ | [storage.ts](ui.laso/src/lib/storage.ts) |
| Backend Endpoints | ✅ | [sync_endpoints.py](backend.laso/app/api/v1/endpoints/sync_endpoints.py) |
| Conflict Resolution | ⚠️ | [sync_service.py](backend.laso/app/services/sync/sync_service.py) - No UI |
| Idempotency | ✅ | [sync_service.py](backend.laso/app/services/sync/sync_service.py#L459) |
| Error Handling | ✅ | [client.ts](ui.laso/src/api/client.ts#L116) |
| Service Workers | ❌ | Not needed (Tauri architecture) |
| IndexedDB | ❌ | Not needed (Tauri SQLite) |

---

## 10. RECOMMENDATIONS

### Immediate Priorities

1. **Implement Conflict Resolution UI**
   - Add dialog for manual customer duplicate resolution
   - Test with duplicate phone/email scenarios

2. **Add Sync Status Indicator**
   - Display in AppShell header
   - Show pending count + last sync time
   - Manual "Sync Now" button

3. **Error Boundaries**
   - Catch sync errors gracefully
   - Don't break app on sync failure

### Medium-Term Enhancements

4. **Exponential Backoff**
   - Reduce server load on failures
   - Prevent retry storms

5. **Data Retention Policy**
   - Archive old transactions
   - Monitor disk space
   - Implement cleanup tasks

6. **Selective Sync**
   - Allow users to disable specific table syncs
   - Reduce bandwidth for low-connectivity areas

### Advanced Features

7. **P2P Sync**
   - Sync between multiple branches via local network
   - Tauri bridges could enable this

8. **Encryption at Rest**
   - SQLite encryption for sensitive data
   - Pharmacy compliance requirement

9. **Sync Analytics**
   - Track sync success rates
   - Monitor conflict patterns
   - Identify problematic users/branches

---

## Appendix: Key File Summary

| File | Purpose | Lines |
|------|---------|-------|
| [ui.laso/src/lib/localDb.ts](ui.laso/src/lib/localDb.ts) | SQLite schema + migrations | ~500 |
| [ui.laso/src/lib/syncEngine.ts](ui.laso/src/lib/syncEngine.ts) | Push/pull orchestration | ~600 |
| [ui.laso/src/lib/localWrite.ts](ui.laso/src/lib/localWrite.ts) | Offline write helpers | ~250 |
| [ui.laso/src/api/sync.ts](ui.laso/src/api/sync.ts) | Sync HTTP layer | ~50 |
| [backend.laso/app/services/sync/sync_service.py](backend.laso/app/services/sync/sync_service.py) | Sync business logic | ~900 |
| [backend.laso/app/api/v1/endpoints/sync_endpoints.py](backend.laso/app/api/v1/endpoints/sync_endpoints.py) | Sync REST API | ~120 |

---

## Conclusion

The LASO pharmacy inventory system has a **sophisticated, production-grade offline-first architecture**. The implementation demonstrates:

- ✅ Comprehensive local data storage via Tauri SQLite
- ✅ Robust sync engine with push-first strategy
- ✅ Server-side conflict detection and resolution
- ✅ Idempotent operations for network resilience
- ✅ Clean separation of concerns (client/server)
- ✅ Extensible event-driven cache invalidation

**Primary gaps:** Conflict resolution UI and bandwidth optimization. These should be addressed before high-volume deployment in low-bandwidth regions.

**Overall Assessment:** 8.5/10 - Excellent architecture with minor polish needed on edge cases.
