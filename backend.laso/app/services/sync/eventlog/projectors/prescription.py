"""
PrescriptionProjector — handles prescription_* events.

Event types (schema_version=1):

- prescription_created   — INSERT into prescriptions (idempotent via ON CONFLICT).
- prescription_updated   — Partial-patch UPDATE with the payload's non-null fields.
- prescription_cancelled — Sets status='cancelled'. Idempotent (already-cancelled → no-op).
- prescription_refill_used — Decrements refills_remaining, sets last_refill_date,
                             auto-transitions status → 'filled' when refills_remaining=0.

Idempotency:
  - create: ON CONFLICT (id) DO NOTHING.
  - update/cancel/refill: WHERE clauses scope to org_id so cross-org replay is a no-op.
  - refill: GREATEST(0, refills_remaining - 1) prevents double-decrement below 0.

Dependencies:
  - prescription_created MUST declare its customer_created event as a dependency
    (customer_id FK with ondelete=RESTRICT). The router's dep check defers the
    prescription until the customer lands.
  - Mutation events (updated/cancelled/refill_used) MUST declare their
    prescription_created as a dependency.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import AggregateType, EventEnvelope
from app.services.sync.eventlog.projector import (
    Projector,
    ProjectorRegistry,
    ProjectorResult,
    ProjectorStatus,
)

logger = logging.getLogger(__name__)

_VALID_STATUSES = {"active", "filled", "expired", "cancelled"}

_UPDATABLE_FIELDS = {
    "prescriber_name",
    "prescriber_license",
    "prescriber_phone",
    "prescriber_address",
    "issue_date",
    "expiry_date",
    "medications",
    "diagnosis",
    "notes",
    "special_instructions",
    "refills_allowed",
    "refills_remaining",
    "status",
}


@ProjectorRegistry.register
class PrescriptionProjector(Projector):
    aggregate_type = AggregateType.PRESCRIPTION

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type
        if etype == "prescription_created":
            return _validate_created(event)
        if etype in ("prescription_updated", "prescription_cancelled",
                     "prescription_refill_used"):
            return await _validate_mutation(event, db)
        return ProjectorResult(
            status=ProjectorStatus.REJECTED_PERMANENT,
            error_code="unknown_event_type",
            error_message=(
                f"PrescriptionProjector does not handle event_type={etype!r}"
            ),
        )

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        etype = event.event_type
        if etype == "prescription_created":
            await _apply_created(event, db)
        elif etype == "prescription_updated":
            await _apply_updated(event, db)
        elif etype == "prescription_cancelled":
            await _apply_cancelled(event, db)
        elif etype == "prescription_refill_used":
            await _apply_refill_used(event, db)
        else:
            raise RuntimeError(
                f"PrescriptionProjector.apply reached with unhandled "
                f"event_type={etype!r}"
            )


# ── validate helpers ─────────────────────────────────────────────────────────


def _validate_created(event: EventEnvelope) -> ProjectorResult:
    p = event.payload

    org_id = p.get("organization_id")
    if not org_id:
        return _reject("missing_organization_id",
                       "prescription_created payload must include organization_id")
    if str(org_id) != str(event.org_id):
        return _reject(
            "org_scope_violation",
            f"payload.organization_id ({org_id}) != envelope.org_id ({event.org_id})",
        )

    for field in ("prescription_number", "customer_id", "prescriber_name",
                  "prescriber_license"):
        if not p.get(field):
            return _reject(f"missing_{field}", f"prescription_created must include {field}")

    for field in ("issue_date", "expiry_date"):
        if not p.get(field):
            return _reject(f"missing_{field}", f"prescription_created must include {field}")

    medications = p.get("medications")
    if not medications or not isinstance(medications, list):
        return _reject("missing_medications",
                       "prescription_created must include a non-empty medications list")

    status = p.get("status", "active")
    if status not in _VALID_STATUSES:
        return _reject("invalid_status",
                       f"status={status!r} not in {sorted(_VALID_STATUSES)}")

    refills_allowed = int(p.get("refills_allowed", 0))
    refills_remaining = int(p.get("refills_remaining", refills_allowed))
    if refills_remaining < 0 or refills_remaining > refills_allowed:
        return _reject(
            "invalid_refills",
            f"refills_remaining ({refills_remaining}) must be between 0 and "
            f"refills_allowed ({refills_allowed})",
        )

    return ProjectorResult(status=ProjectorStatus.OK)


async def _validate_mutation(
    event: EventEnvelope, db: AsyncSession
) -> ProjectorResult:
    row = (
        await db.execute(
            text(
                "SELECT id FROM prescriptions"
                " WHERE id = :rx_id"
                "   AND organization_id = :org_id"
            ),
            {
                "rx_id": str(event.aggregate_id),
                "org_id": str(event.org_id),
            },
        )
    ).first()
    if row is None:
        return _reject(
            "prescription_not_found",
            f"Prescription {event.aggregate_id} not found for org {event.org_id}. "
            "Client must declare prescription_created as a dependency.",
        )
    return ProjectorResult(status=ProjectorStatus.OK)


def _reject(code: str, message: str) -> ProjectorResult:
    return ProjectorResult(
        status=ProjectorStatus.REJECTED_PERMANENT,
        error_code=code,
        error_message=message,
    )


# ── apply helpers ─────────────────────────────────────────────────────────────


async def _apply_created(event: EventEnvelope, db: AsyncSession) -> None:
    p = event.payload
    refills_allowed = int(p.get("refills_allowed", 0))

    await db.execute(
        text("""
            INSERT INTO prescriptions (
                id, organization_id, branch_id,
                prescription_number, customer_id,
                prescriber_name, prescriber_license,
                prescriber_phone, prescriber_address,
                issue_date, expiry_date,
                medications,
                diagnosis, notes, special_instructions,
                refills_allowed, refills_remaining,
                last_refill_date, status,
                verified_by, verified_at,
                sync_version, sync_status,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:org_id AS UUID),
                CAST(:branch_id AS UUID),
                :prescription_number,
                CAST(:customer_id AS UUID),
                :prescriber_name, :prescriber_license,
                :prescriber_phone, :prescriber_address,
                :issue_date, :expiry_date,
                CAST(:medications AS JSONB),
                :diagnosis, :notes, :special_instructions,
                :refills_allowed, :refills_remaining,
                :last_refill_date, :status,
                CAST(:verified_by AS UUID), :verified_at,
                1, 'synced',
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
        """),
        {
            "id": str(event.aggregate_id),
            "org_id": str(event.org_id),
            "branch_id": str(p["branch_id"]),
            "prescription_number": p["prescription_number"],
            "customer_id": str(p["customer_id"]),
            "prescriber_name": p["prescriber_name"],
            "prescriber_license": p["prescriber_license"],
            "prescriber_phone": p.get("prescriber_phone"),
            "prescriber_address": p.get("prescriber_address"),
            "issue_date": _parse_date(p["issue_date"]),
            "expiry_date": _parse_date(p["expiry_date"]),
            "medications": _json_or_none(p.get("medications")),
            "diagnosis": p.get("diagnosis"),
            "notes": p.get("notes"),
            "special_instructions": p.get("special_instructions"),
            "refills_allowed": refills_allowed,
            "refills_remaining": int(p.get("refills_remaining", refills_allowed)),
            "last_refill_date": _parse_date(p.get("last_refill_date")),
            "status": p.get("status", "active"),
            "verified_by": _uuid_str_or_none(p.get("verified_by")),
            "verified_at": _parse_dt(p.get("verified_at")),
            "created_at": event.authored_at,
            "updated_at": event.authored_at,
        },
    )


async def _apply_updated(event: EventEnvelope, db: AsyncSession) -> None:
    p = event.payload
    updates: Dict[str, Any] = {}

    for field in _UPDATABLE_FIELDS:
        if field not in p:
            continue
        value = p[field]
        if field in ("issue_date", "expiry_date", "last_refill_date"):
            value = _parse_date(value)
        elif field == "medications":
            value = _json_or_none(value)
        updates[field] = value

    if not updates:
        return  # empty patch — nothing to write

    set_clauses = []
    params: Dict[str, Any] = {
        "rx_id": str(event.aggregate_id),
        "org_id": str(event.org_id),
        "updated_at": event.authored_at,
    }
    for field, value in updates.items():
        if field == "medications":
            set_clauses.append(f"{field} = CAST(:{field} AS JSONB)")
        elif field == "status" and value not in _VALID_STATUSES:
            # Skip invalid status updates silently; validate caught this.
            continue
        else:
            set_clauses.append(f"{field} = :{field}")
        params[field] = value
    set_clauses.append("updated_at = :updated_at")
    set_clauses.append("sync_version = sync_version + 1")
    set_clauses.append("sync_status = 'synced'")

    await db.execute(
        text(
            f"UPDATE prescriptions SET {', '.join(set_clauses)}"
            " WHERE id = :rx_id AND organization_id = :org_id"
        ),
        params,
    )


async def _apply_cancelled(event: EventEnvelope, db: AsyncSession) -> None:
    await db.execute(
        text("""
            UPDATE prescriptions
               SET status = 'cancelled',
                   updated_at = :updated_at,
                   sync_version = sync_version + 1,
                   sync_status = 'synced'
             WHERE id = :rx_id
               AND organization_id = :org_id
               AND status != 'cancelled'
        """),
        {
            "rx_id": str(event.aggregate_id),
            "org_id": str(event.org_id),
            "updated_at": event.authored_at,
        },
    )


async def _apply_refill_used(event: EventEnvelope, db: AsyncSession) -> None:
    p = event.payload
    verified_by = _uuid_str_or_none(p.get("verified_by"))
    now = datetime.now(timezone.utc)

    await db.execute(
        text("""
            UPDATE prescriptions
               SET refills_remaining = GREATEST(0, refills_remaining - 1),
                   last_refill_date  = :refill_date,
                   status = CASE
                               WHEN refills_remaining <= 1 THEN 'filled'
                               ELSE status
                            END,
                   verified_by = COALESCE(:verified_by, verified_by),
                   verified_at = COALESCE(:verified_at, verified_at),
                   updated_at  = :updated_at,
                   sync_version = sync_version + 1,
                   sync_status = 'synced'
             WHERE id = :rx_id
               AND organization_id = :org_id
               AND status IN ('active', 'filled')
               AND refills_remaining > 0
        """),
        {
            "rx_id": str(event.aggregate_id),
            "org_id": str(event.org_id),
            "refill_date": _parse_date(p.get("refill_date")) or now.date(),
            "verified_by": verified_by,
            "verified_at": _parse_dt(p.get("verified_at")),
            "updated_at": event.authored_at,
        },
    )


# ── type helpers ──────────────────────────────────────────────────────────────


def _uuid_str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _json_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v)


def _parse_date(v: Any) -> Optional[Any]:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        from datetime import date
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return v


def _parse_dt(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
