"""
Inventory Deductor
==================
Handles the read side of FEFO (First Expire, First Out) batch management.

Responsibility
--------------
Load all eligible DrugBatch rows for a set of drugs at a branch in a single
query, grouped and ordered by expiry date ascending (earliest first).

The actual write-side deduction (FOR UPDATE locks, remaining_quantity
decrements, BranchInventory updates, StockAdjustment records) stays inside
the main sales transaction in ``SalesService.process_sale`` because it needs
to share the same savepoint and session.  Separating the read query here keeps
the deductor focused and independently testable.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import DrugBatch


async def load_fefo_batches(
    db: AsyncSession,
    branch_id: uuid.UUID,
    drug_ids: List[uuid.UUID],
) -> Dict[uuid.UUID, List[DrugBatch]]:
    """
    Load all non-expired, non-empty DrugBatch rows for the given drugs at a
    branch in a single query, ordered by ``expiry_date ASC`` (FEFO).

    This snapshot is used for two purposes in ``process_sale``:

    1. **Price resolution** — ``pricing_calculator.resolve_unit_price`` reads
       ``batches[0].selling_price`` (the earliest-expiry batch's price) to
       determine what to charge the customer.

    2. **Pre-flight availability check** — step 9 sums ``remaining_quantity``
       across all batches for each drug to verify the requested quantity can be
       fulfilled before any reservation is made.

    The actual deduction in step 15 re-queries the same batches under
    ``SELECT ... FOR UPDATE`` locks to prevent phantom reads in concurrent
    transactions, so this snapshot does not need to be locked.

    Args:
        db:        Async SQLAlchemy session.
        branch_id: Branch where the sale is being processed.
        drug_ids:  Drug IDs from the sale request.

    Returns:
        Dict keyed by ``drug_id``.  Each value is a list of ``DrugBatch``
        objects ordered earliest-expiry first.  Drugs with no valid batches
        are absent from the dict (callers should use ``.get(drug_id, [])``)
    """
    res = await db.execute(
        select(DrugBatch)
        .where(
            DrugBatch.branch_id == branch_id,
            DrugBatch.drug_id.in_(drug_ids),
            DrugBatch.remaining_quantity > 0,
            DrugBatch.expiry_date > date.today(),
        )
        .order_by(DrugBatch.expiry_date.asc())
    )

    batches: Dict[uuid.UUID, List[DrugBatch]] = {}
    for batch in res.scalars().all():
        batches.setdefault(batch.drug_id, []).append(batch)

    return batches