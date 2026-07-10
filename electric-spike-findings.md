# SPIKE: ElectricSQL + Tauri Feasibility Report

## Step 1 — Official Client Options

| SDK | Source | Status | Notes |
|---|---|---|---|
| **TypeScript client** (`@electric-sql/client`) | Official — ElectricSQL monorepo | **GA (v1.0, Mar 2025)** | Primary SDK. HTTP-based Shape sync protocol. Works in any JS runtime. |
| **PGlite + sync plugin** (`@electric-sql/pglite-sync`) | Official — `@electric-sql/pglite` | **GA (v0.5.5, Apr 2026)** | Postgres compiled to WASM (~3 MB gzipped). The recommended embeddable local DB for Electric. 49 versions since Aug 2024. |
| **wa-sqlite driver** | Official — merged in PR #891 (Feb 2024) | **Abandoned / unknown** | A Tauri SQLite driver was added to the TypeScript client, but it's unclear if it ships in the current npm package or is kept compatible with the latest client API. No dedicated npm package. |
| **Rust client** (`electric-sql-client` on crates.io) | Third-party by `jihchi` | **Abandoned (v0.2.3, Dec 2024)** | 0 stars, 0 dependents, 0% docs. Not affiliated with ElectricSQL. Not usable. |
| **Elixir client** | Official | GA | Not relevant for Tauri. |

**Official SDK page:** https://electric-sql.com/docs/api/clients/typescript — lists TypeScript and Elixir only. No Rust.

---

## Step 2 — Option A: Electric Client in the Webview

### Architecture

```
┌─────────────────────────────────────────────────┐
│  Tauri App                                       │
│  ┌─────────────────────────────┐                  │
│  │  Webview (TypeScript)       │                  │
│  │  ┌───────────────────────┐  │                  │
│  │  │  PGlite (WASM Postgres)│  │  invoke/listen  │
│  │  │  + electric sync plugin│◄───────────────────│─── Rust backend
│  │  └───────┬───────────────┘  │                  │
│  │          │ Shape subscription│                  │
│  └──────────┼──────────────────┘                  │
└─────────────┼────────────────────────────────────┘
              │ HTTP/SSE
     ┌────────▼────────┐     ┌──────────────────┐
     │  Electric Sync   │◄────│  Postgres (server) │
     │  Service (Docker) │     │  + logical repl   │
     └─────────────────┘     └──────────────────┘
              ▲
              │ HTTP POST
     ┌────────┴────────┐
     │  Write API       │
     │  (FastAPI or     │
     │   write server)  │
     └─────────────────┘
```

### Key Findings

1. **PGlite runs entirely in the webview's WASM memory.** The Rust backend CANNOT directly access it — all reads/writes must go through `invoke`/`listen` IPC. This is not necessarily a blocker (the current app already routes all DB access through the webview via `@tauri-apps/plugin-sql`), but it means the Rust backend has zero direct data access unless you maintain a parallel connection to Postgres.

2. **Electric is a read-path sync engine only.** Writes go through a separate write path — typically a write-through-database pattern (as demonstrated by Linearlite):
   - Local mutation → local PGlite → trigger detects `synced=false` → live query fires → JavaScript POSTs to a write server → write server applies changes to Postgres → Electric syncs the change back as confirmation.
   - This adds **round-trip latency** (seconds) for write confirmation.

3. **Hard cutover is required.** PGlite is Postgres (not SQLite). All local SQL schema and queries would need to be rewritten in Postgres dialect. The 14 SQLite migrations, `sync_queue` table, and per-table sync columns would be replaced by Electric's generated client schema.

4. **Code change estimate:**
   - Remove: `syncEngine.ts` (852 lines), `localDb.ts` sync portions (~600 lines), `sync_endpoints.py`, `sync_service.py` (~3026 lines)
   - Add: Electric sync service Docker Compose, PGlite init + electric sync plugin setup, write path handler, `@electric-sql/pglite` + `@electric-sql/pglite-react` dependencies
   - Rewrite: All local SQL queries to Postgres dialect
   - Total: ~3000–5000 lines removed, ~500–1000 lines added, plus Docker infrastructure.

5. **wa-sqlite alternative:** Could use the existing Tauri SQLite plugin + the Electric wa-sqlite driver, keeping SQLite as the local engine. But:
   - The driver's current state is unknown (not an officially shipped npm package in the Electric monorepo).
   - No examples exist for this path — the Electric docs focus on PGlite.
   - The same write path complexity applies.

---

## Step 3 — Option B: Embedded Postgres via pg_embed

**Verdict: NOT viable.** Abandoned experiment.

| Factor | Assessment |
|---|---|
| Repo state | 8 commits, 0 releases, 24 stars, last touched Feb 2024 |
| Official stance | Blog post carries a **WARNING**: "This post was written for a previous version of Electric that is no longer active." |
| `pg_embed` capability | Does NOT embed Postgres in-process. Spawns a full Postgres process as a sidecar. |
| Startup time | Multiple seconds |
| Platform support | Tested on Ubuntu only. Windows/macOS support unknown. |
| Binary size | Massive (full Postgres distribution must be bundled) |
| Current relevance | PGlite (WASM Postgres) has since made this approach obsolete |

**Reference:** https://github.com/electric-sql/electric-tauri-postgres — README says "An experiment."

---

## Step 4 — Comparison Against Current Custom Engine

| Dimension | Current Custom Engine | ElectricSQL + PGlite (Option A) |
|---|---|---|
| **Real-time sync** | No (30s polling interval) | Yes (SSE/long-polling Shape subscription) |
| **Offline support** | Yes (sync_queue + retry) | Yes (PGlite persistence + sync plugin) |
| **Conflict resolution** | Custom (per-table `server_wins`/`manual_required`) | Server-side via Electric (limited) |
| **Field-level merge** | No (all-or-nothing per record) | No (same limitation) |
| **Custom logic (FK validation, etc.)** | 17 per-table handlers | Must be implemented in write path |
| **Write latency** | Immediate (writes go directly to server API) | Delayed (local → trigger → POST → server → sync back) |
| **Infrastructure** | FastAPI (existing) | FastAPI + Electric sync service (Docker) |
| **Lines of sync code** | ~5000 lines (TS + Python) | ~0 (Electric handles sync) |
| **Migration path** | N/A (current state) | Hard cutover only |
| **Rust backend data access** | Via `tauri-plugin-sql` (SQLite file) | Via `invoke` to webview (PGlite is WASM) |
| **Maturity** | Production, battle-tested | Electric v1.0 GA; PGlite-sync v0.5.x (pre-1.0) |
| **Bundle size impact** | Baseline | +3 MB (PGlite WASM) + Electric sync service |

---

## Recommendation

### Do NOT proceed with ElectricSQL at this time.

**Three specific blockers drove this recommendation:**

1. **Write path complexity.** Electric is a read-path sync engine. The current app has a sophisticated write path (17 per-table handlers with FK validation, idempotency via operation receipts, field whitelisting). Replacing this with the write-through-database pattern would lose most of this logic — or require rebuilding it in a write server, negating the simplification benefit.

2. **Hard cutover required.** No gradual migration is possible. PGlite is Postgres, not SQLite. All 14 local DB migrations, the `sync_queue` table, and every SQL query in the app would need to be rewritten simultaneously. This is a multi-week effort with significant risk.

3. **No official Tauri path.** Electric's docs and examples target browsers and web apps. The Tauri SQLite driver (PR #891, Feb 2024) is of unknown maintenance status. The `electric-tauri-postgres` repo is explicitly an abandoned experiment. There is no recommended, documented, or supported path for integrating Electric with a Tauri app today. PGlite in a webview works technically, but the lack of Rust backend data access and the undeveloped Tauri integration story make this a risky bet.

### What to do instead

| Recommendation | Rationale |
|---|---|
| **Keep the custom sync engine** | It works, it's production-tested, and it covers all the app's specific needs (FK validation, per-table conflicts, operation idempotency). |
| **Add a WebSocket push channel** to the server (`POST /sync/pull` → notify clients) | Eliminates the 30s polling latency — biggest user-facing gap. |
| **Add a service worker / background sync** | Keeps sync running when the app is backgrounded/second window closed. |
| **Revisit Electric after ~12 months** | If/when Electric ships a first-class Tauri integration (Rust SDK, documented PGlite-in-Tauri pattern, better write path support), re-evaluate. Until then, the custom engine provides more control with less integration risk. |

### Relevant links for future re-evaluation

- Electric main repo: https://github.com/electric-sql/electric (10.3k stars, very active)
- PGlite: https://github.com/electric-sql/pglite
- PGlite sync plugin (npm): https://www.npmjs.com/package/@electric-sql/pglite-sync
- Linearlite example (write-through-database pattern): https://github.com/electric-sql/electric/tree/main/examples/linearlite
- Electric docs — official clients: https://electric-sql.com/docs/api/clients/typescript
- Tauri plugin SQL: https://v2.tauri.app/plugin/sql/
