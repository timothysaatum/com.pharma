from __future__ import annotations
import uuid
from datetime import date, datetime, timezone
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.inventory.branch_inventory import DrugBatch
from app.models.inventory.stock_lease import StockLease

async def compute_sellable_quantities(
    db: AsyncSession,
    branch_id: uuid.UUID,
    drug_ids: list[uuid.UUID],
    terminal_id: str | None = None,
) -> dict[uuid.UUID, int]:
    """
    Returns {drug_id: sellable_quantity} for the given branch.
    sellable_quantity = SUM(unexpired batch remaining) - SUM(active unexpired leased quantity across all OTHER terminals).
    """
    today = date.today()
    now_utc = datetime.now(timezone.utc)
    
    # 1. Base sellable: sum of remaining unexpired batches
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
    base_sellable = {row.drug_id: int(row.sellable) for row in rows}
    
    # 2. Subtract leased quantities for OTHER terminals
    # leased_quantity - consumed_quantity represents the remaining locked stock.
    # We subtract this so that the requesting terminal only sees stock that isn't promised elsewhere.
    lease_query = select(
        StockLease.drug_id,
        func.coalesce(func.sum(StockLease.leased_quantity - StockLease.consumed_quantity), 0).label("leased_locked"),
    ).where(
        StockLease.branch_id == branch_id,
        StockLease.drug_id.in_(drug_ids),
        StockLease.status == 'active',
        StockLease.expires_at > now_utc,
    )
    
    if terminal_id:
        lease_query = lease_query.where(StockLease.terminal_id != terminal_id)
        
    lease_query = lease_query.group_by(StockLease.drug_id)
    
    lease_rows = await db.execute(lease_query)
    locked_stock = {row.drug_id: int(row.leased_locked) for row in lease_rows}
    
    final_sellable = {}
    for did in drug_ids:
        base = base_sellable.get(did, 0)
        locked = locked_stock.get(did, 0)
        final_sellable[did] = max(0, base - locked)
        
    return final_sellable
