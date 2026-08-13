# 0008: Sync Push/Pull Endpoint Contracts (REST Batched)

**Status:** Accepted
**Date:** 2026-08-12
**Related:** [0006 — Event-Sourced Sync Spine](0006-event-sourced-sync-spine.md), [0007 — Event Schema, Hash Chain, and Dependency Semantics](0007-event-schema-hash-chain-dependencies.md)

## Context

The event-sourced spine (ADR 0006) needs a transport for the client's
outbox to reach the server and for the client to receive events other
branches have committed. The two live options are REST batched (single
request/response per push or pull cycle) and WebSocket streaming
(persistent bidirectional channel with continuous event flow).

Deployment target is Ghanaian pharmacies on intermittent mobile data.

## Decision

Use REST batched endpoints for both push and pull. No WebSocket in
Phase 1.

Two endpoints:

- `POST /api/v1/sync/events`  — client pushes a batch of outbox events
- `GET  /api/v1/sync/events`  — client pulls events by server sequence

Both endpoints authenticate with the existing JWT scheme. The user's
`organization_id` scopes every access; branch scoping is enforced
per-event by the projectors, not by the endpoint.

### Push contract

**Request (`POST /api/v1/sync/events`):**

```json
{
  "branch_id": "uuid",
  "client_clock": "2026-08-12T14:03:11Z",
  "events": [
    {
      "event_id": "01H8XABC...",
      "aggregate_id": "uuid",
      "aggregate_type": "sale",
      "event_type": "SaleRecorded",
      "schema_version": 1,
      "payload": { ... },
      "dependencies": ["01H8XABB..."],
      "authored_at": "2026-08-12T13:59:47Z",
      "authored_by": "uuid",
      "branch_id": "uuid",
      "org_id": "uuid",
      "hash_self": "sha256hex..."
    }
  ]
}
```

- Events must be ordered by `event_id` ascending (also chronological
  by ULID guarantee).
- Batch size cap: **500 events per request** (defensive; typical shift
  push is far smaller).
- `hash_prev` is not sent by the client — the server computes it at
  accept-time against its own log tail.

**Response:**

```json
{
  "server_clock": "2026-08-12T14:03:12Z",
  "results": [
    {
      "event_id": "01H8XABC...",
      "status": "accepted" | "accepted_deferred" | "rejected_permanent" | "rejected_transient",
      "seq": 12847,
      "received_at": "2026-08-12T14:03:12Z",
      "error_code": "INVALID_ORG_SCOPE" | null,
      "error_message": "human-readable" | null,
      "pending_on": ["01H8XABB..."] | null
    }
  ],
  "next_pull_seq": 12849
}
```

- Result order matches request order.
- `seq` is present for `accepted` and `accepted_deferred`; null for
  rejections.
- `pending_on` is populated only for `accepted_deferred` — the specific
  dependency ids the server is waiting for.
- `next_pull_seq` is the current head of the org's log — a hint to the
  client that a pull cycle is worthwhile if this value has advanced
  past its cursor.

Per-event failure is the norm — one rejected event does not fail the
whole batch. This is the antithesis of the current CRR batch-cursor
stall behavior and is the primary design point of this endpoint.

**Idempotency:** the server rejects duplicate `event_id`s with
`{"status": "accepted", "seq": <original_seq>}` — same as first-time
acceptance from the client's perspective. Resends are structurally
safe.

**Rate limit:** 60 push requests / minute / branch. Sized so a
recovering-from-offline client can burst-drain its outbox (multiple
500-event batches) without throttling in normal operation.

### Pull contract

**Request (`GET /api/v1/sync/events`):**

```
?after_seq=12800
&limit=500
&aggregate_types=sale,prescription,customer,stock
&branch_ids=<uuid>,<uuid>   (optional; defaults to all branches in org)
```

- `after_seq` is exclusive; client's cursor is the highest `seq`
  successfully projected locally.
- `aggregate_types` is required — clients declare exactly which streams
  they care about (a POS terminal may not need audit_log events, for
  example).
- `branch_ids` is optional; omitted means "all branches in my org I
  have access to." Access is enforced against the user's assigned
  branches (see ADR 0005 for the branch-authorization model).

**Response:**

```json
{
  "server_clock": "2026-08-12T14:04:00Z",
  "events": [ ... full envelopes with seq populated ... ],
  "has_more": true,
  "next_after_seq": 13300
}
```

- `has_more: true` means the server truncated at `limit`; the client
  should call again with `after_seq = next_after_seq` immediately
  (no backoff needed — this is normal drain).
- `has_more: false` and `events: []` means fully caught up; client
  schedules the next pull per its polling policy.
- Events are ordered by `seq` ascending. Applying them in order
  respects the causal ordering established at accept-time.

**Cache validators:** response carries `ETag: W/"<max_seq>"`, and the
client sends `If-None-Match` on next poll. A 304 with no body is the
common "nothing new" response and costs almost no bandwidth.

### Client polling policy

- **Foreground (app open):** pull every 30 seconds if online. Immediate
  pull after any successful push (uses `next_pull_seq` hint to skip if
  no new events).
- **Background (app closed or backgrounded):** pull every 5 minutes.
- **Offline detection:** any request failure with a network-level error
  suspends polling; resume when connectivity is regained (existing
  `isOnline` hook).

### Auth failure handling

- **401 Unauthorized:** trigger token refresh via existing refresh
  flow; if refresh fails, sign the user out of the sync loop (they can
  still work offline) and surface a login-required banner.
- **403 Forbidden on `branch_ids`:** the user's assignments have
  changed since login; refresh assigned-branches from `/auth/me` and
  retry with the intersection.

## Rationale

- **REST beats WebSocket for this workload.** WebSocket wins when you
  need sub-second push-to-many-clients — trading floors, chat, live
  dashboards. Pharmacies want their sale to eventually land on the
  server; the difference between "arrives in 200ms" and "arrives in
  30s" is invisible at the counter. Meanwhile, WebSocket adds a
  persistent-connection lifecycle to a codebase whose users routinely
  have intermittent connectivity — every reconnect is a re-auth, a
  cursor resync, and a new source of edge cases.
- **`curl` is a debugging tool.** Every REST call in this system can
  be reproduced from a terminal. That property is worth more than any
  latency win a persistent socket would give us.
- **Per-event results, not batch success/failure.** The single biggest
  design lesson from the current CRR sync: batch all-or-nothing is
  where mystery bugs live. Every event gets its own verdict, and one
  poison event never blocks the rest of the batch.
- **Idempotency by `event_id`, not by request.** The client cannot
  always tell whether a network failure happened before or after the
  server committed. Resending the same `event_id` and getting back
  the original `seq` closes that ambiguity.
- **ETag + 304 on pull.** Polling is chatty by design; making the
  "nothing new" case cost a few hundred bytes preserves the bandwidth
  budget for pharmacies actually pushing/pulling meaningful events.
- **Client declares `aggregate_types`.** Different clients (POS
  terminal vs. admin dashboard vs. mobile) need different subsets of
  the event stream. Sending only what a client asks for keeps mobile
  payloads small.

## Consequences

- Two endpoints, no persistent connection state, straightforward
  operational model (same rate limits, same load balancer, same
  observability as the rest of the API).
- Cross-branch propagation latency is bounded by the pull interval
  (up to 30 s in foreground). If a workflow ever needs faster
  cross-branch visibility, add a lightweight server-sent-events (SSE)
  push-notification endpoint later — SSE fits the "one-way server-to-
  client hint" role better than WebSocket and can be added without
  changing the REST contract.
- Client outbox drain is bounded by `500 events * 60 requests/min =
  30,000 events/min/branch`. This is well above any realistic
  offline-catchup scenario for a single pharmacy branch (a full week
  offline for a busy branch is on the order of thousands of events,
  not tens of thousands).
- The pull cursor is a single `BIGINT` per client per org; simple to
  persist, simple to reason about, no vector-clock bookkeeping on the
  cursor.
- REST batching means the client is responsible for retry policy on
  transient failures. Existing `RetryBackoff` utility already covers
  this.

## Trigger for Revisit

Revisit and consider adding an SSE (server-sent events) push channel
if either:

- Cross-branch propagation latency of up to 30 s becomes a real
  workflow blocker (e.g. a customer walks between branches faster
  than the pull cycle can propagate their new prescription).
- Client polling load becomes a measurable server-cost item at scale
  (many idle terminals polling every 30 s in aggregate).

Consider WebSocket only if bidirectional real-time (server needs to
initiate commands to client, not just notifications) becomes a
requirement — which is not on any current roadmap.
