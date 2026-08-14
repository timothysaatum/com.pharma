"""
CustomerProjector — reads customer_* events, writes to ``customers``.

This is the simplest projector in the system and serves as the
canonical shape for others (Sale, Prescription, Stock). No cross-
aggregate joins, no denormalized counters, no downstream effects: a
customer event affects exactly one row in ``customers``.

Event types handled (schema_version=1):

- ``customer_created`` — INSERT into customers. Payload carries all
  fields the client saw when the customer was created offline.
- ``customer_updated`` — UPDATE customers with the payload's non-null
  fields (partial patch, matches ``CustomerUpdate`` semantics).
- ``customer_deleted`` — soft-delete via SoftDeleteMixin columns.

Idempotency:

- Create uses ``ON CONFLICT (id) DO NOTHING``. A duplicate create
  (same aggregate_id) is a no-op at the SQL layer.
- Update writes only the fields present in the payload; a replay of
  the same update produces the same row.
- Delete is idempotent: setting is_deleted=TRUE twice is a no-op.

Dependencies:

- ``customer_created`` has no dependencies (customer is a root
  aggregate).
- ``customer_updated`` / ``customer_deleted`` MUST declare their
  originating ``customer_created`` in ``envelope.dependencies``. The
  event router's coarse dep check handles the deferred case; when
  ``validate`` is called here, the router has already confirmed the
  create is projected — so a missing row is a client bug
  (REJECTED_PERMANENT).
"""

from __future__ import annotations

import json
import logging
import uuid
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
from app.services.sync.eventlog.vector_clock import concurrent, dominates, merge

logger = logging.getLogger(__name__)

# Fields captured in local_snapshot for conflict display.
_SNAPSHOT_COLS = (
    "first_name", "last_name", "phone", "email", "date_of_birth",
    "address", "allergies", "chronic_conditions",
    "loyalty_points", "loyalty_tier",
    "insurance_provider_id", "insurance_member_id",
    "preferred_contact_method", "marketing_consent", "is_active",
    "version_vector", "updated_at",
)


VALID_CUSTOMER_TYPES = {"walk_in", "registered", "insurance", "corporate"}
VALID_LOYALTY_TIERS = {"bronze", "silver", "gold", "platinum"}
VALID_CONTACT_METHODS = {"email", "phone", "sms"}

EVENT_CREATED = "customer_created"
EVENT_UPDATED = "customer_updated"
EVENT_DELETED = "customer_deleted"

UPDATABLE_FIELDS = {
    "first_name",
    "last_name",
    "phone",
    "email",
    "date_of_birth",
    "address",
    "allergies",
    "chronic_conditions",
    "loyalty_points",
    "loyalty_tier",
    "preferred_contact_method",
    "marketing_consent",
    "is_active",
    "insurance_provider_id",
    "insurance_member_id",
    "insurance_card_image_url",
    "preferred_contract_id",
}


@ProjectorRegistry.register
class CustomerProjector(Projector):
    aggregate_type = AggregateType.CUSTOMER

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type

        if etype == EVENT_CREATED:
            return _validate_created(event)

        if etype == EVENT_UPDATED:
            return await _validate_mutation(event, db, allow_missing=False)

        if etype == EVENT_DELETED:
            return await _validate_mutation(event, db, allow_missing=False)

        return ProjectorResult(
            status=ProjectorStatus.REJECTED_PERMANENT,
            error_code="unknown_event_type",
            error_message=(
                f"CustomerProjector does not handle event_type={etype!r}"
            ),
        )

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        etype = event.event_type
        if etype == EVENT_CREATED:
            await _apply_created(event, db)
            return
        if etype == EVENT_UPDATED:
            await _apply_updated(event, db)
            return
        if etype == EVENT_DELETED:
            await _apply_deleted(event, db)
            return
        # Unreachable — validate would have rejected.
        raise RuntimeError(
            f"CustomerProjector.apply reached with unhandled event_type={etype!r}"
        )


# ── validate helpers ─────────────────────────────────────────────────────────

def _validate_created(event: EventEnvelope) -> ProjectorResult:
    payload = event.payload

    organization_id = payload.get("organization_id")
    if not organization_id:
        return _reject("missing_organization_id", "customer_created payload must include organization_id")
    try:
        uuid.UUID(str(organization_id))
    except (ValueError, TypeError):
        return _reject("invalid_organization_id", f"organization_id={organization_id!r} is not a UUID")

    # Enforce org scope: the envelope's org_id must match the payload's
    # organization_id. Cross-org events are a permanent client bug.
    if str(organization_id) != str(event.org_id):
        return _reject(
            "org_scope_violation",
            f"payload.organization_id ({organization_id}) does not match "
            f"envelope.org_id ({event.org_id})",
        )

    customer_type = payload.get("customer_type", "walk_in")
    if customer_type not in VALID_CUSTOMER_TYPES:
        return _reject(
            "invalid_customer_type",
            f"customer_type={customer_type!r} not in {sorted(VALID_CUSTOMER_TYPES)}",
        )

    tier = payload.get("loyalty_tier", "bronze")
    if tier not in VALID_LOYALTY_TIERS:
        return _reject(
            "invalid_loyalty_tier",
            f"loyalty_tier={tier!r} not in {sorted(VALID_LOYALTY_TIERS)}",
        )

    contact = payload.get("preferred_contact_method", "email")
    if contact not in VALID_CONTACT_METHODS:
        return _reject(
            "invalid_contact_method",
            f"preferred_contact_method={contact!r} not in {sorted(VALID_CONTACT_METHODS)}",
        )

    return ProjectorResult(status=ProjectorStatus.OK)


async def _validate_mutation(
    event: EventEnvelope,
    db: AsyncSession,
    allow_missing: bool,
) -> ProjectorResult:
    """Shared validate for update/delete: check the customer row exists
    in the read model. ``allow_missing`` is False for Phase 1a because
    the router's dep check has already confirmed the create was
    projected — a missing row here means the client failed to declare
    the create as a dependency.
    """
    row = (
        await db.execute(
            text(
                """
                SELECT 1
                  FROM customers
                 WHERE id = :customer_id
                   AND organization_id = :org_id
                """
            ),
            {
                "customer_id": str(event.aggregate_id),
                "org_id": str(event.org_id),
            },
        )
    ).first()
    if row is None:
        if allow_missing:
            return ProjectorResult(
                status=ProjectorStatus.DEFERRED,
                unresolved_deps=list(event.dependencies),
            )
        return _reject(
            "customer_not_found",
            f"Customer {event.aggregate_id} not found for org {event.org_id}. "
            "Client must declare the customer_created event as a dependency.",
        )

    if event.event_type == EVENT_UPDATED:
        payload = event.payload

        tier = payload.get("loyalty_tier")
        if tier is not None and tier not in VALID_LOYALTY_TIERS:
            return _reject(
                "invalid_loyalty_tier",
                f"loyalty_tier={tier!r} not in {sorted(VALID_LOYALTY_TIERS)}",
            )

        contact = payload.get("preferred_contact_method")
        if contact is not None and contact not in VALID_CONTACT_METHODS:
            return _reject(
                "invalid_contact_method",
                f"preferred_contact_method={contact!r} not in "
                f"{sorted(VALID_CONTACT_METHODS)}",
            )

    return ProjectorResult(status=ProjectorStatus.OK)


def _reject(code: str, message: str) -> ProjectorResult:
    return ProjectorResult(
        status=ProjectorStatus.REJECTED_PERMANENT,
        error_code=code,
        error_message=message,
    )


# ── apply helpers ────────────────────────────────────────────────────────────

async def _apply_created(event: EventEnvelope, db: AsyncSession) -> None:
    """INSERT ... ON CONFLICT DO NOTHING so a replay of the same create
    is a no-op. Fields absent from the payload fall back to table
    defaults.
    """
    payload = event.payload
    incoming_vector: Dict[str, int] = payload.get("version_vector") or {}
    params: Dict[str, Any] = {
        "id": str(event.aggregate_id),
        "organization_id": str(payload["organization_id"]),
        "customer_type": payload.get("customer_type", "walk_in"),
        "first_name": payload.get("first_name"),
        "last_name": payload.get("last_name"),
        "phone": payload.get("phone"),
        "email": payload.get("email"),
        "date_of_birth": _parse_date(payload.get("date_of_birth")),
        "address": _json_or_none(payload.get("address")),
        "allergies": list(payload.get("allergies") or []),
        "chronic_conditions": list(payload.get("chronic_conditions") or []),
        "loyalty_points": payload.get("loyalty_points", 0),
        "loyalty_tier": payload.get("loyalty_tier", "bronze"),
        "total_orders": payload.get("total_orders", 0),
        "total_value": payload.get("total_value", 0.0),
        "preferred_contact_method": payload.get(
            "preferred_contact_method", "email"
        ),
        "marketing_consent": payload.get("marketing_consent", False),
        "is_active": payload.get("is_active", True),
        "insurance_provider_id": _uuid_str_or_none(
            payload.get("insurance_provider_id")
        ),
        "insurance_member_id": payload.get("insurance_member_id"),
        "insurance_card_image_url": payload.get("insurance_card_image_url"),
        "preferred_contract_id": _uuid_str_or_none(
            payload.get("preferred_contract_id")
        ),
        "medical_data_encrypted": payload.get("medical_data_encrypted", False),
        "version_vector": json.dumps(incoming_vector),
        "created_at": event.authored_at,
        "updated_at": event.authored_at,
    }
    await db.execute(
        text(
            """
            INSERT INTO customers (
                id, organization_id, customer_type,
                first_name, last_name, phone, email, date_of_birth,
                address, allergies, chronic_conditions,
                loyalty_points, loyalty_tier, total_orders, total_value,
                preferred_contact_method, marketing_consent, is_active,
                insurance_provider_id, insurance_member_id,
                insurance_card_image_url, preferred_contract_id,
                medical_data_encrypted,
                version_vector,
                sync_version, sync_status, is_deleted,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:organization_id AS UUID),
                :customer_type,
                :first_name, :last_name, :phone, :email, :date_of_birth,
                CAST(:address AS JSONB),
                CAST(:allergies AS TEXT[]),
                CAST(:chronic_conditions AS TEXT[]),
                :loyalty_points, :loyalty_tier, :total_orders, :total_value,
                :preferred_contact_method, :marketing_consent, :is_active,
                CAST(:insurance_provider_id AS UUID),
                :insurance_member_id,
                :insurance_card_image_url,
                CAST(:preferred_contract_id AS UUID),
                :medical_data_encrypted,
                CAST(:version_vector AS JSONB),
                1, 'synced', FALSE,
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        params,
    )


async def _apply_updated(event: EventEnvelope, db: AsyncSession) -> None:
    """Patch the customer row with the payload's non-null fields.

    Before applying, compare version vectors:
      - concurrent → write to unresolved_conflicts, skip apply.
      - incoming dominated by current (stale) → skip silently.
      - incoming dominates or no vector info → apply + update vector.
    """
    payload = event.payload
    incoming_vector: Dict[str, int] = payload.get("version_vector") or {}

    # Read current row for vector comparison + snapshot.
    snapshot_cols_sql = ", ".join(_SNAPSHOT_COLS)
    row = (
        await db.execute(
            text(
                f"SELECT {snapshot_cols_sql} FROM customers"
                " WHERE id = :customer_id AND organization_id = :org_id"
            ),
            {"customer_id": str(event.aggregate_id), "org_id": str(event.org_id)},
        )
    ).first()

    if row is None:
        return  # validate() already confirmed existence; defensive guard only.

    current_vector: Dict[str, int] = row.version_vector or {}

    if incoming_vector:
        if concurrent(current_vector, incoming_vector):
            local_snapshot = {c: getattr(row, c) for c in _SNAPSHOT_COLS}
            await _write_conflict(event, db, current_vector, local_snapshot, incoming_vector)
            return

        if dominates(current_vector, incoming_vector):
            # Stale event — a newer version of this customer is already committed.
            return

    # Incoming dominates or no vector info → apply the update.
    new_vector = merge(current_vector, incoming_vector) if incoming_vector else current_vector

    updates: Dict[str, Any] = {}
    for field in UPDATABLE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if field == "date_of_birth":
            value = _parse_date(value)
        updates[field] = value

    if not updates:
        return

    set_clauses = []
    params: Dict[str, Any] = {
        "customer_id": str(event.aggregate_id),
        "org_id": str(event.org_id),
        "updated_at": event.authored_at,
        "new_vector": json.dumps(new_vector),
    }
    for field, value in updates.items():
        if field == "address":
            set_clauses.append(f"{field} = CAST(:{field} AS JSONB)")
            params[field] = _json_or_none(value)
        elif field in ("insurance_provider_id", "preferred_contract_id"):
            set_clauses.append(f"{field} = CAST(:{field} AS UUID)")
            params[field] = _uuid_str_or_none(value)
        elif field in ("allergies", "chronic_conditions"):
            set_clauses.append(f"{field} = CAST(:{field} AS TEXT[])")
            params[field] = list(value or [])
        else:
            set_clauses.append(f"{field} = :{field}")
            params[field] = value
    set_clauses.append("version_vector = CAST(:new_vector AS JSONB)")
    set_clauses.append("updated_at = :updated_at")

    sql = f"""
        UPDATE customers
           SET {", ".join(set_clauses)}
         WHERE id = :customer_id
           AND organization_id = :org_id
    """
    await db.execute(text(sql), params)


async def _write_conflict(
    event: EventEnvelope,
    db: AsyncSession,
    current_vector: Dict[str, int],
    local_snapshot: Dict[str, Any],
    incoming_vector: Dict[str, int],
) -> None:
    """Park a concurrent edit in unresolved_conflicts."""
    await db.execute(
        text(
            """
            INSERT INTO unresolved_conflicts
                (org_id, aggregate_type, aggregate_id, event_id,
                 local_vector, local_snapshot, incoming_vector, incoming_payload,
                 status, created_at)
            VALUES
                (CAST(:org_id AS UUID), :aggregate_type, CAST(:aggregate_id AS UUID),
                 :event_id,
                 CAST(:local_vector AS JSONB), CAST(:local_snapshot AS JSONB),
                 CAST(:incoming_vector AS JSONB), CAST(:incoming_payload AS JSONB),
                 'pending', now())
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "org_id": str(event.org_id),
            "aggregate_type": event.aggregate_type.value,
            "aggregate_id": str(event.aggregate_id),
            "event_id": event.event_id,
            "local_vector": json.dumps(current_vector),
            "local_snapshot": json.dumps(
                {k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
                 for k, v in local_snapshot.items()}
            ),
            "incoming_vector": json.dumps(incoming_vector),
            "incoming_payload": json.dumps(event.payload),
        },
    )


async def _apply_deleted(event: EventEnvelope, db: AsyncSession) -> None:
    """Soft-delete via SoftDeleteMixin columns."""
    await db.execute(
        text(
            """
            UPDATE customers
               SET is_deleted = TRUE,
                   deleted_at = :deleted_at,
                   updated_at = :deleted_at
             WHERE id = :customer_id
               AND organization_id = :org_id
               AND is_deleted = FALSE
            """
        ),
        {
            "customer_id": str(event.aggregate_id),
            "org_id": str(event.org_id),
            "deleted_at": event.authored_at,
        },
    )


# ── small conversions ────────────────────────────────────────────────────────

def _parse_date(v: Any) -> Optional[Any]:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        # Accept ISO date strings; leave datetime parsing to the DB
        # driver otherwise.
        from datetime import date

        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return v


def _json_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    import json

    return json.dumps(v)


def _uuid_str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


