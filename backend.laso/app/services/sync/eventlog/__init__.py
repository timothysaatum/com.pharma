"""
Event-Sourced Sync Spine
========================

Layer-1 of the sync architecture per ADR 0006. Client-generated events
land in ``event_log`` (append-only, hash-chained per org) and dispatch
to per-aggregate projectors that write to existing Postgres read
models.

Public entry points:

- :class:`~app.services.sync.eventlog.append_service.AppendService`
  handles hash-chain verification, per-org monotonic sequence
  assignment, and idempotent inserts.
- :class:`~app.services.sync.eventlog.router.EventRouter` orchestrates
  per-event flow (dependency resolution, projector dispatch, deferred
  parking) and returns the wire-level per-event verdicts.
- Concrete projectors live under
  :mod:`app.services.sync.eventlog.projectors`.

The push and pull FastAPI endpoints are in
``app/api/v1/endpoints/event_sync_endpoints.py``.
"""

from app.services.sync.eventlog.append_service import (
    AppendResult,
    AppendService,
    AppendStatus,
    HashChainMismatch,
)
from app.services.sync.eventlog.projector import (
    Projector,
    ProjectorRegistry,
    ProjectorResult,
    ProjectorStatus,
)
from app.services.sync.eventlog.router import EventRouter

__all__ = [
    "AppendResult",
    "AppendService",
    "AppendStatus",
    "EventRouter",
    "HashChainMismatch",
    "Projector",
    "ProjectorRegistry",
    "ProjectorResult",
    "ProjectorStatus",
]
