"""
_fefo.py — Server-side FEFO batch allocator for the sale projector.

This is the authoritative implementation of batch selection for offline sales.
The same logic runs in the online path via inventory_deductor.load_fefo_batches.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from dataclasses import dataclass
from typing import List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class BatchAllocation:
    batch_id: str
    batch_number: str
    expiry_date: date | None
    quantity: int
    unit_cost: str | None
    unit_price: str | None


async def fefo_allocate(
    db: AsyncSession,
    branch_id: str,
    drug_id: str,
    quantity: int,
    authored_at: datetime,
) -> List[BatchAllocation]:
    """
    Allocate `quantity` units from the branch's unexpired batches for `drug_id`,
    FEFO order (earliest expiry first).

    The expiry check uses `authored_at` (when the sale happened offline), not
    the projection time, so a batch valid at sale time is accepted even if it
    expired before the event synced.

    Returns the list of BatchAllocation records consumed. Raises ValueError if
    stock is insufficient.
    """
    sold_date = authored_at.date()

    # NOTE: We skip FOR UPDATE here for now since branch_id+drug_id guards 
    # prevent cross-tenant issues, and full locking belongs to Phase 4 (leases).
    rows = await db.execute(
        text("""
            SELECT id, batch_number, expiry_date, remaining_quantity,
                   cost_price, selling_price
              FROM drug_batches
             WHERE branch_id = :branch_id
               AND drug_id   = :drug_id
               AND remaining_quantity > 0
               AND (
                     expiry_date IS NULL
                     OR expiry_date >= :sold_at
                   )
             ORDER BY expiry_date ASC NULLS LAST
        """),
        {"branch_id": branch_id, "drug_id": drug_id, "sold_at": sold_date},
    )
    batches = rows.fetchall()

    total_available = sum(int(b.remaining_quantity) for b in batches)
    if total_available < quantity:
        raise ValueError(
            f"Insufficient stock for drug {drug_id}: need {quantity}, "
            f"have {total_available} unexpired units across {len(batches)} batches"
        )

    allocations: List[BatchAllocation] = []
    remaining = quantity
    for batch in batches:
        if remaining <= 0:
            break
        take = min(int(batch.remaining_quantity), remaining)
        if take <= 0:
            continue
        allocations.append(BatchAllocation(
            batch_id=str(batch.id),
            batch_number=str(batch.batch_number),
            expiry_date=batch.expiry_date,
            quantity=take,
            unit_cost=str(batch.cost_price) if batch.cost_price is not None else None,
            unit_price=str(batch.selling_price) if batch.selling_price is not None else None,
        ))
        remaining -= take

    return allocations
