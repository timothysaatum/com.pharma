"""
Conflict Routes
===============
Endpoints for listing and resolving unresolved_conflicts — concurrent edits
to reference data (customers, drugs, drug_categories) detected by the
vector-clock comparison in the event projectors.

Permission map:
  view_customers / manage_customers — list conflicts for their org
  manage_customers                  — resolve conflicts
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.models.user.user_model import User
from app.schemas.conflict_schemas import (
    ConflictListResponse,
    ConflictResponse,
    ResolveConflictRequest,
)
from app.services.sync.eventlog.vector_clock import merge

logger = logging.getLogger(__name__)

def _parse_json_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


router = APIRouter(prefix="/conflicts", tags=["Conflicts"])


# ── LIST ─────────────────────────────────────────────────────────────────────


@router.get("", response_model=ConflictListResponse)
async def list_conflicts(
    conflict_status: Optional[str] = Query(
        default="pending",
        alias="status",
        pattern="^(pending|resolved_keep_local|resolved_keep_incoming|all)$",
        description="Filter by resolution status (default: pending)",
    ),
    aggregate_type: Optional[str] = Query(
        default=None,
        description="Filter by aggregate type (customer, drug, drug_category)",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current_user: User = Depends(require_permission("manage_customers")),
    db: AsyncSession = Depends(get_db),
):
    """
    List conflicts for this organisation.

    **Returns**: paginated list of conflicts with local and incoming
    payload snapshots for comparison.
    """
    where_clauses = ["org_id = :org_id"]
    params: dict = {"org_id": str(current_user.organization_id)}

    if conflict_status and conflict_status != "all":
        where_clauses.append("status = :status")
        params["status"] = conflict_status

    if aggregate_type:
        where_clauses.append("aggregate_type = :aggregate_type")
        params["aggregate_type"] = aggregate_type

    where_sql = " AND ".join(where_clauses)

    total_row = (
        await db.execute(
            text(f"SELECT COUNT(*) FROM unresolved_conflicts WHERE {where_sql}"),
            params,
        )
    ).scalar_one()

    pending_row = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM unresolved_conflicts"
                " WHERE org_id = :org_id AND status = 'pending'"
            ),
            {"org_id": str(current_user.organization_id)},
        )
    ).scalar_one()

    offset = (page - 1) * page_size
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, org_id, aggregate_type, aggregate_id, event_id,
                       local_vector, local_snapshot, incoming_vector, incoming_payload,
                       status, resolved_at, resolved_by, created_at
                  FROM unresolved_conflicts
                 WHERE {where_sql}
                 ORDER BY created_at DESC
                 LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": page_size, "offset": offset},
        )
    ).mappings().all()

    conflicts = [
        ConflictResponse(
            id=r["id"],
            org_id=r["org_id"],
            aggregate_type=r["aggregate_type"],
            aggregate_id=r["aggregate_id"],
            event_id=r["event_id"],
            local_vector=_parse_json_dict(r["local_vector"]),
            local_snapshot=_parse_json_dict(r["local_snapshot"]),
            incoming_vector=_parse_json_dict(r["incoming_vector"]),
            incoming_payload=_parse_json_dict(r["incoming_payload"]),
            status=r["status"],
            resolved_at=r["resolved_at"],
            resolved_by=r["resolved_by"],
            created_at=r["created_at"],
        )
        for r in rows
    ]

    return ConflictListResponse(
        conflicts=conflicts,
        total=total_row,
        pending=pending_row,
    )


# ── RESOLVE ───────────────────────────────────────────────────────────────────


@router.post("/{conflict_id}/resolve", response_model=ConflictResponse)
async def resolve_conflict(
    conflict_id: uuid.UUID,
    body: ResolveConflictRequest,
    current_user: User = Depends(require_permission("manage_customers")),
    db: AsyncSession = Depends(get_db),
):
    """
    Resolve a conflict.

    **resolution = keep_local**: discard the incoming event payload; the
    current server row is authoritative. The version vector is left unchanged.

    **resolution = keep_incoming**: apply the incoming payload to the row
    and merge the two vectors so this edit will dominate future replays.

    **Required Permission**: manage_customers
    """
    row = (
        await db.execute(
            text(
                """
                SELECT id, org_id, aggregate_type, aggregate_id, event_id,
                       local_vector, local_snapshot, incoming_vector, incoming_payload,
                       status, resolved_at, resolved_by, created_at
                  FROM unresolved_conflicts
                 WHERE id = :id AND org_id = :org_id
                """
            ),
            {"id": str(conflict_id), "org_id": str(current_user.organization_id)},
        )
    ).mappings().first()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conflict not found")

    if row["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflict is already resolved ({row['status']})",
        )

    new_status = (
        "resolved_keep_local"
        if body.resolution == "keep_local"
        else "resolved_keep_incoming"
    )

    if body.resolution == "keep_incoming":
        await _apply_incoming(
            aggregate_type=row["aggregate_type"],
            aggregate_id=str(row["aggregate_id"]),
            org_id=str(row["org_id"]),
            incoming_payload=row["incoming_payload"],
            local_vector=row["local_vector"] or {},
            incoming_vector=row["incoming_vector"] or {},
            db=db,
        )

    updated = (
        await db.execute(
            text(
                """
                UPDATE unresolved_conflicts
                   SET status = :status,
                       resolved_at = now(),
                       resolved_by = CAST(:resolved_by AS UUID)
                 WHERE id = :id
                 RETURNING id, org_id, aggregate_type, aggregate_id, event_id,
                           local_vector, local_snapshot, incoming_vector, incoming_payload,
                           status, resolved_at, resolved_by, created_at
                """
            ),
            {
                "id": str(conflict_id),
                "status": new_status,
                "resolved_by": str(current_user.id),
            },
        )
    ).mappings().first()

    await db.commit()

    return ConflictResponse(
        id=updated["id"],
        org_id=updated["org_id"],
        aggregate_type=updated["aggregate_type"],
        aggregate_id=updated["aggregate_id"],
        event_id=updated["event_id"],
        local_vector=updated["local_vector"] or {},
        local_snapshot=updated["local_snapshot"] or {},
        incoming_vector=updated["incoming_vector"] or {},
        incoming_payload=updated["incoming_payload"] or {},
        status=updated["status"],
        resolved_at=updated["resolved_at"],
        resolved_by=updated["resolved_by"],
        created_at=updated["created_at"],
    )


# ── helpers ────────────────────────────────────────────────────────────────────


async def _apply_incoming(
    aggregate_type: str,
    aggregate_id: str,
    org_id: str,
    incoming_payload: dict,
    local_vector: dict,
    incoming_vector: dict,
    db: AsyncSession,
) -> None:
    """Apply the incoming payload fields to the aggregate row and merge vectors."""
    merged_vector = merge(local_vector, incoming_vector)
    merged_json = json.dumps(merged_vector)

    if aggregate_type == "customer":
        from app.services.sync.eventlog.projectors.customer import (
            UPDATABLE_FIELDS,
            _json_or_none,
            _parse_date,
            _uuid_str_or_none,
        )

        updates: dict = {}
        for field in UPDATABLE_FIELDS:
            if field not in incoming_payload:
                continue
            value = incoming_payload[field]
            if field == "date_of_birth":
                value = _parse_date(value)
            updates[field] = value

        if not updates:
            return

        set_clauses = []
        params: dict = {
            "customer_id": aggregate_id,
            "org_id": org_id,
            "new_vector": merged_json,
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
        set_clauses.append("updated_at = now()")

        await db.execute(
            text(
                f"UPDATE customers SET {', '.join(set_clauses)}"
                " WHERE id = CAST(:customer_id AS UUID)"
                " AND organization_id = CAST(:org_id AS UUID)"
            ),
            params,
        )

    elif aggregate_type in ("drug", "drug_category"):
        # For future: apply drug/category updates on resolve.
        # Currently drugs are server-first and conflicts are unlikely.
        logger.warning(
            "Conflict resolution for %s not yet implemented; "
            "only version_vector merged.",
            aggregate_type,
        )
        table = "drugs" if aggregate_type == "drug" else "drug_categories"
        await db.execute(
            text(
                f"UPDATE {table} SET version_vector = CAST(:vec AS JSONB)"
                " WHERE id = CAST(:id AS UUID)"
            ),
            {"vec": merged_json, "id": aggregate_id},
        )
