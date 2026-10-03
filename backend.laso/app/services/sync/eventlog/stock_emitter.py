"""
Stock event emission that participates in the caller's transaction.

Why this exists separately from ``ServerEventEmitter``
------------------------------------------------------
``ServerEventEmitter.emit`` runs AFTER the caller's commit and swallows every
exception, on purpose: for reference data (drugs, categories) a failed
notification must never roll back the primary write.

Stock is different. A device's stock figures are derived entirely from these
events, so an event that is silently dropped is not a cosmetic problem: the
device keeps serving its old numbers and no error surfaces anywhere. Worse, the
event and the stock row must agree, or the device and the server diverge
permanently.

So ``StockEventEmitter`` does two things differently:

1. It is called BEFORE the caller commits, so the append and the stock change
   land in the same transaction. Either both commit or neither does.
2. It does NOT swallow exceptions. A failed append must abort the stock change,
   because a stock change that cannot be published has not happened as far as
   any device is concerned.

It goes through the same ``AppendService`` primitives as the reference emitter,
so the per-org advisory lock, the seq assignment and the SHA-256 hash chain are
identical, and the two emitters share one chain.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import (
    AggregateType,
    EventEnvelope,
    ULID_LENGTH,
    compute_hash_self,
)
from app.services.sync.eventlog.append_service import (
    AppendResult,
    AppendService,
    AppendStatus,
)

logger = logging.getLogger(__name__)

# Nil UUID for org-level events that are not scoped to a branch.
ORG_LEVEL_BRANCH = uuid.UUID(int=0)


def deterministic_event_id(
    event_type: str,
    aggregate_id: Any,
    version_tag: str,
) -> str:
    """A stable 26-character event id derived from the event's identity.

    Backfills must be re-runnable. The append path already returns
    ALREADY_APPENDED for a repeated ``event_id``, so deriving the id from
    (event_type, aggregate_id, version_tag) makes a second run a no-op instead of
    a duplicate set of events. Derived from the SHA-256 of those three parts and
    rendered as ULID_LENGTH uppercase hex characters, which is what
    EventEnvelope accepts and what ``ServerEventEmitter._generate_event_id``
    already produces.
    """
    seed = f"{event_type}|{aggregate_id}|{version_tag}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:ULID_LENGTH].upper()


def _coerce_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


class StockEventEmitter:
    """Append stock events inside the caller's open transaction.

    Every method is a classmethod and none of them commit. The caller owns the
    transaction, which is what makes the stock change and its event atomic.
    """

    @classmethod
    async def emit_in_transaction(
        cls,
        db: AsyncSession,
        *,
        org_id: uuid.UUID,
        event_type: str,
        aggregate_type: AggregateType,
        aggregate_id: uuid.UUID,
        payload: Dict[str, Any],
        authored_by: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        dependencies: Optional[List[str]] = None,
        event_id: Optional[str] = None,
    ) -> AppendResult:
        """Append one stock event. Does not commit. Does not swallow errors.

        Raises:
            Exception: whatever the append path raises. Callers must let this
                propagate so the surrounding transaction rolls back.
        """
        now = datetime.now(timezone.utc)
        eid = event_id or uuid.uuid4().hex[:ULID_LENGTH].upper()
        resolved_branch = branch_id or ORG_LEVEL_BRANCH

        org_id_str = str(_coerce_uuid(org_id))
        # Transaction-scoped: released at commit or rollback, and re-entrant, so
        # emitting several events in one transaction chain is correct.
        await AppendService._acquire_org_lock(db, org_id_str)
        tail_hash, tail_seq = await AppendService._load_log_tail(db, org_id_str)

        placeholder = EventEnvelope(
            event_id=eid,
            aggregate_id=_coerce_uuid(aggregate_id),
            aggregate_type=aggregate_type,
            event_type=event_type,
            schema_version=1,
            payload=payload,
            dependencies=dependencies or [],
            authored_at=now,
            authored_by=_coerce_uuid(authored_by),
            branch_id=_coerce_uuid(resolved_branch),
            org_id=_coerce_uuid(org_id),
            hash_self="0" * 64,
        )
        real_hash = compute_hash_self(placeholder, tail_hash)

        envelope = EventEnvelope(
            event_id=eid,
            aggregate_id=_coerce_uuid(aggregate_id),
            aggregate_type=aggregate_type,
            event_type=event_type,
            schema_version=1,
            payload=payload,
            dependencies=dependencies or [],
            authored_at=now,
            authored_by=_coerce_uuid(authored_by),
            branch_id=_coerce_uuid(resolved_branch),
            org_id=_coerce_uuid(org_id),
            hash_self=real_hash,
        )

        result = await AppendService._append_one(
            db,
            org_id_str,
            envelope,
            current_tail_hash=tail_hash,
            current_tail_seq=tail_seq,
        )

        if result.status not in (AppendStatus.APPENDED, AppendStatus.ALREADY_APPENDED):
            # Deliberately an error: the caller must roll back rather than commit
            # a stock change that has no event.
            raise RuntimeError(
                f"StockEventEmitter: append returned {result.status} for "
                f"{event_type}/{aggregate_id}; rolling back the stock change"
            )

        logger.debug(
            "StockEventEmitter: appended %s for %s (seq=%s, status=%s)",
            event_type,
            aggregate_id,
            result.seq,
            result.status,
        )
        return result


def branch_inventory_payload(
    *,
    inventory_id: uuid.UUID,
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    quantity: int,
    reserved_quantity: int = 0,
    location: Optional[str] = None,
    selling_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Payload for branch_inventory_created / branch_inventory_updated.

    Carries an EXPLICIT ABSOLUTE quantity. The device's _branchInventoryUpserted
    projector assigns quantity directly, so it must never be handed an event with
    no quantity: it used to default that to 0 and zeroed real stock. It also
    carries branch_id and drug_id so the projector can key the row, and
    selling_price so the local POS shows the branch price.
    """
    return {
        "branch_inventory_id": str(inventory_id),
        "branch_id": str(branch_id),
        "drug_id": str(drug_id),
        "quantity": int(quantity),
        "reserved_quantity": int(reserved_quantity),
        "location": location,
        "selling_price": float(selling_price) if selling_price is not None else None,
    }


def drug_batch_payload(
    *,
    batch_id: uuid.UUID,
    drug_id: uuid.UUID,
    branch_id: uuid.UUID,
    batch_number: str,
    quantity: int,
    remaining_quantity: int,
    cost_price: Optional[float] = None,
    selling_price: Optional[float] = None,
    expiry_date: Optional[str] = None,
    supplier: Optional[str] = None,
    purchase_order_id: Optional[uuid.UUID] = None,
    location: Optional[str] = None,
) -> Dict[str, Any]:
    """Payload for drug_batch_created / drug_batch_updated.

    The device's _drugBatchUpserted projector upserts by batch id and assigns
    remaining_quantity directly, so it needs every field it will write rather
    than a delta.
    """
    return {
        "batch_id": str(batch_id),
        "id": str(batch_id),
        "drug_id": str(drug_id),
        "branch_id": str(branch_id),
        "batch_number": batch_number,
        "quantity": int(quantity),
        "remaining_quantity": int(remaining_quantity),
        "cost_price": float(cost_price) if cost_price is not None else None,
        "selling_price": float(selling_price) if selling_price is not None else None,
        "expiry_date": expiry_date,
        "supplier": supplier,
        "purchase_order_id": str(purchase_order_id) if purchase_order_id else None,
        "location": location,
    }