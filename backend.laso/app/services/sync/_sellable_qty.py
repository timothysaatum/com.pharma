from __future__ import annotations
import uuid
from datetime import date
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.inventory.branch_inventory import DrugBatch

async def compute_sellable_quantities(
    db: AsyncSession,
    branch_id: uuid.UUID,
    drug_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """
    Returns {drug_id: sellable_quantity} for the given branch.
    sellable_quantity = SUM of remaining_quantity across unexpired, non-zero batches.
    This is the single canonical server-side implementation (Phase 1).
    """
    today = date.today()
    rows = await db.execute(
        select(
            DrugBatch.drug_id,
            func.coalesce(func.sum(DrugBatch.remaining_quantity), 0).label("sellable"),
        )
        .where(
            DrugBatch.branch_id == branch_id,
            DrugBatch.drug_id.in_(drug_ids),
            DrugBatch.remaining_quantity > 0,
            or_(
                DrugBatch.expiry_date.is_(None),
                DrugBatch.expiry_date >= today,
            ),
        )
        .group_by(DrugBatch.drug_id)
    )
    return {row.drug_id: int(row.sellable) for row in rows}
