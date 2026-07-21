# Offline Sync Architecture Improvement Plan
> Target Project: `com.pharma`  
> Prepared by: Senior Systems Architect  
> Date: July 20, 2026

---

## 1. Executive Summary

The current offline sync architecture in the Pharmacare application utilizes a hybrid state-sync model where both the client (Tauri/SQLite) and the server (FastAPI/PostgreSQL) maintain duplicate validation engines. While functional, this design is prone to logic drift, clock-skew vulnerabilities, and race conditions under concurrent offline updates.

This document outlines a transition plan from the current **State-Sync** model to an **Intent-Based (Command-Sourced) Sync** model, standardizing validation rules under a single engine and ensuring mathematical consistency across all distributed nodes.

---

## 2. Architectural Critique & Current Vulnerabilities

```
CURRENT STATE:
[Client Local SQLite] ──(Pushes State Deltas)──► [Server PostgreSQL]
   - Evaluates Expiry via UTC                       - Evaluates Expiry via Local OS Time
   - Evaluates FEFO locally                         - Re-evaluates FEFO at Sync Time
   * Risk: Rules can drift.                         * Risk: Expiry / concurrency collisions.
```

### 2.1 Logic Duplication (TypeScript vs. Python)
*   **Vulnerability**: Business rules for First-Expired, First-Out (FEFO) batch allocation and expiry validation are implemented independently in TypeScript (`offlineSalesManager.ts`) and Python (`sync_service.py`).
*   **Impact**: Any update to business requirements (e.g., tax calculation, pricing rules, batch reservation) requires double-implementation, creating a high probability of logic drift.

### 2.2 State-Sync vs. Event-Sourcing
*   **Vulnerability**: The sync protocol pushes final state snapshots (e.g., "reduce drug quantity by X").
*   **Impact**: If two offline terminals perform transactions on the same batch, syncing the resulting inventory states will overwrite updates, resulting in phantom stock or negative quantities.

### 2.3 Temporal and Clock Drift
*   **Vulnerability**: The client relies on SQLite's UTC date logic (`DATE('now')`), while the backend relies on server-local date evaluations (`date.today()`).
*   **Impact**: Edge-hour transactions around midnight/expiry boundaries will be inconsistently accepted or rejected depending on server timezone configurations and client clock drift.

---

## 3. Target Architecture: Intent-Based Sync

The proposed architecture converts the sync pipeline into an **Append-Only Event Stream of Cashier Intents**.

```
PROPOSED TARGET STATE:
[POS Client UI] 
       │
       ▼ (Record Local Action)
[Intent: "Sell Drug X Qty Y at Timestamp T"]
       │
       ▼ (Append to Local Outbox)
[Sync Queue (Network Connection)]
       │
       ▼ (Replay Stream on Server)
[Server Event Sequencer] ──► [Saga Orchestrator]
                                    │
                  ┌─────────────────┴─────────────────┐
                  ▼                                   ▼
        [Apply State Changes]               [Trigger Reconciliation]
        - Atomic batch allocation           - Flag warning if stock depleted
        - Update ledger                     - Audit log entry
```

### 3.1 Intent-Based Command Logs
Instead of syncing state modifications, the client appends user actions to a local log:
```json
{
  "command_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "action": "RECORD_SALE",
  "timestamp": "2026-07-20T22:22:48.123Z",
  "payload": {
    "drug_id": "uuid",
    "quantity": 5,
    "price_contract_id": "uuid"
  }
}
```
The server processes this log sequentially, acting as the absolute source of truth.

### 3.2 Shared WebAssembly (WASM) Validation Core
To eliminate logic duplication, critical validation logic (FEFO allocations, tax structures, contract rules) will be implemented in a single codebase (e.g., Rust or clean TypeScript) and compiled to WebAssembly.
*   **Client**: Runs the WASM binary locally inside Tauri for instant offline feedback.
*   **Server**: Runs the same WASM binary inside the Python API layer (using `wasmer` or `wasmtime`) to validate incoming sync events.

### 3.3 Hybrid Logical Clocks (HLC)
To resolve clock-drift issues without relying on NTP servers:
*   Equip the client and server with a **Hybrid Logical Clock (HLC)**.
*   HLC timestamps guarantee causal ordering (e.g., ensuring a "restock" event is processed before a "sale" event, even if the client's physical system clock is set in the past).

---

## 4. Implementation Roadmap

```
Phase 1: Standardization ──► Phase 2: Command Outbox ──► Phase 3: Shared Core (WASM)
  - UTC Expiry Alignment       - Local Event Outbox        - Extract FEFO Logic
  - DB Schema Hardening        - Server Idempotency Keys   - Integrate with Tauri/Python
```

### Phase 1: Standardization & Immediate Hardening
1.  **Date Unification**: Convert all database and logic layers on both frontend and backend to use ISO 8601 UTC timestamps instead of local dates.
2.  **Concurrency Checks**: Introduce row-level optimistic locking (`version_id` columns) on the PostgreSQL server for inventory and drug batch tables to prevent race conditions during bulk sync processing.

### Phase 2: Command-Log Outbox Integration
1.  Add an `event_outbox` table to the client's SQLite schema.
2.  Refactor `offlineSalesManager.ts` to write to the `event_outbox` in the same local database transaction that records the sale projection.
3.  Modify the sync client to push payloads sequentially from the `event_outbox`.

### Phase 3: Shared Core Extraction
1.  Isolate the FEFO allocation algorithm and expiry evaluation rules.
2.  Pack this logic into a modular package and compile to WebAssembly.
3.  Integrate the WASM module into the Tauri frontend runtime and the Python backend service.

---

## 5. Verification & Testing Strategy

To ensure system correctness, three layers of automated verification must be deployed:

### 5.1 Invariant Assertion Suite
A nightly integrity script runs on the production database, verifying that the physical stock always equals receipt allocations minus sales and adjustments:
$$\text{Branch Inventory} = \sum(\text{Stock Receipts}) - \sum(\text{Sales}) + \sum(\text{Adjustments})$$

### 5.2 Network & Clock skew Emulator (Chaos Testing)
Integrate a network chaos framework (such as `Toxiproxy`) into the local CI/CD environment to run the following test suites:
*   **Clock Skew**: Simulate client tablets with clock drifts of up to $\pm 24\text{ hours}$ and verify that the server resolves temporal conflicts correctly.
*   **Interrupted Sync**: Terminate the network connection mid-payload transmission and assert that no duplicate entries or partial stock deductions are persisted on the server.

### 5.3 Shadow Execution
Before shifting the production sync over to the event-sourced pipeline, route production payloads to both the legacy state-sync pipeline and the new event-sourced staging pipeline. Verify that both systems produce identical states across 100,000 transactions before deprecating the legacy code.
