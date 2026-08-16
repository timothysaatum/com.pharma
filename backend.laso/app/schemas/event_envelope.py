"""
Event Envelope — Sync Wire Contract
====================================

Canonical envelope types for the event-sourced sync spine. Single
source of truth for the wire contract between the Tauri client and this
FastAPI server. The TypeScript mirror lives at
``ui.laso/src/lib/sync/eventEnvelope.ts`` and MUST be kept in
lock-step — a shared golden-vector test suite enforces byte-identical
canonical serialization on both sides.

Related ADRs:
    0006 — Event-Sourced Sync Spine
    0007 — Event Schema, Hash Chain, and Dependency Semantics
    0008 — Sync Push/Pull Endpoint Contracts
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Constants ────────────────────────────────────────────────────────────────

ULID_LENGTH = 26
SHA256_HEX_LENGTH = 64
GENESIS_HASH = "0" * SHA256_HEX_LENGTH
"""``hash_prev`` value for the very first event in an organization's log."""

MAX_PUSH_BATCH = 500
"""Maximum events per POST /sync/events request (ADR 0008)."""


# ── Enums ────────────────────────────────────────────────────────────────────

class AggregateType(str, Enum):
    """Domain aggregates carried by the event-sourced spine (Layer 1)."""

    SALE = "sale"
    PRESCRIPTION = "prescription"
    CUSTOMER = "customer"
    STOCK = "stock"
    STOCK_TRANSFER = "stock_transfer"
    DRUG = "drug"
    DRUG_CATEGORY = "drug_category"
    DRUG_BATCH = "drug_batch"
    BRANCH_INVENTORY = "branch_inventory"
    PURCHASE_ORDER = "purchase_order"
    PRICE_CONTRACT = "price_contract"


class EventStatus(str, Enum):
    """Per-event verdict returned by POST /api/v1/sync/events."""

    ACCEPTED = "accepted"
    ACCEPTED_DEFERRED = "accepted_deferred"
    REJECTED_PERMANENT = "rejected_permanent"
    REJECTED_TRANSIENT = "rejected_transient"


# ── Envelope ─────────────────────────────────────────────────────────────────

class EventEnvelope(BaseModel):
    """Immutable event envelope. Client sets every field except ``seq``,
    ``received_at`` and ``hash_prev``; the server populates those at
    accept-time. See ADR 0007 for field semantics and hash-chain rules.
    """

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    event_id: str = Field(..., min_length=ULID_LENGTH, max_length=ULID_LENGTH)
    aggregate_id: uuid.UUID
    aggregate_type: AggregateType
    event_type: str = Field(..., min_length=1, max_length=128)
    schema_version: int = Field(default=1, ge=1)
    payload: Dict[str, Any]
    dependencies: List[str] = Field(default_factory=list)
    authored_at: datetime
    authored_by: uuid.UUID
    branch_id: uuid.UUID
    org_id: uuid.UUID
    hash_self: str = Field(
        ..., min_length=SHA256_HEX_LENGTH, max_length=SHA256_HEX_LENGTH
    )

    hash_prev: Optional[str] = Field(default=None)
    seq: Optional[int] = Field(default=None, ge=0)
    received_at: Optional[datetime] = Field(default=None)

    @field_validator("dependencies")
    @classmethod
    def _validate_dep_ulids(cls, deps: List[str]) -> List[str]:
        for dep in deps:
            if len(dep) != ULID_LENGTH:
                raise ValueError(
                    f"dependencies entry {dep!r} is not a ULID "
                    f"(expected length {ULID_LENGTH})"
                )
        return deps

    @field_validator("hash_self", "hash_prev")
    @classmethod
    def _validate_hex(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        int(v, 16)  # raises ValueError if not hex
        return v.lower()


# ── Push endpoint contracts (ADR 0008) ───────────────────────────────────────

class EventPushRequest(BaseModel):
    """POST /api/v1/sync/events request body."""

    branch_id: uuid.UUID
    client_clock: datetime
    events: List[EventEnvelope] = Field(..., max_length=MAX_PUSH_BATCH)


class EventPushResult(BaseModel):
    """Per-event verdict inside EventPushResponse.results."""

    event_id: str
    status: EventStatus
    seq: Optional[int] = None
    received_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pending_on: Optional[List[str]] = None
    """ULIDs of unresolved dependencies. Only set when status=ACCEPTED_DEFERRED."""


class EventPushResponse(BaseModel):
    server_clock: datetime
    results: List[EventPushResult]
    next_pull_seq: int


# ── Pull endpoint contracts (ADR 0008) ───────────────────────────────────────

class EventPullResponse(BaseModel):
    server_clock: datetime
    events: List[EventEnvelope]
    has_more: bool
    next_after_seq: int


# ── Canonical hashing (ADR 0007) ─────────────────────────────────────────────

_HASH_FIELDS = (
    "event_id",
    "aggregate_id",
    "aggregate_type",
    "event_type",
    "schema_version",
    "payload",
    "dependencies",
    "authored_at",
    "authored_by",
    "branch_id",
    "org_id",
    "hash_prev",
)


def canonical_json(obj: Any) -> bytes:
    """Byte-identical canonical JSON encoder shared with the TypeScript
    client. Keys sorted, no whitespace, UTF-8. Datetimes serialized as
    RFC 3339 with ``Z`` suffix at millisecond precision. UUIDs as
    lower-case hex-with-hyphens.

    The TypeScript implementation in ``eventEnvelope.ts`` must produce
    byte-identical output for the same input. A shared golden-vector
    test suite enforces this — do not change this function without
    updating the TS mirror and re-generating vectors.
    """
    return json.dumps(
        obj,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _json_default(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise TypeError(
                f"naive datetime {v!r} is not permitted in envelope"
            )
        s = v.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s
    if isinstance(v, Enum):
        return v.value
    raise TypeError(
        f"canonical_json cannot serialize {type(v).__name__}"
    )


def compute_hash_self(envelope: EventEnvelope, hash_prev: str) -> str:
    """SHA-256 over the canonical serialization of ``envelope`` with
    ``hash_prev`` substituted in. Called client-side before outbox
    write (with a placeholder ``hash_prev``) and server-side at
    accept-time (with the real ``hash_prev`` from the org's log tail).
    """
    body = {name: getattr(envelope, name) for name in _HASH_FIELDS}
    body["hash_prev"] = hash_prev
    return hashlib.sha256(canonical_json(body)).hexdigest()
