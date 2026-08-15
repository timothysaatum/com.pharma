import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from sqlalchemy import select, update, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.stock_lease import StockLease
from app.services.sync._sellable_qty import compute_sellable_quantities

class LeaseService:
    @staticmethod
    async def grant_or_renew_lease(
        db: AsyncSession,
        branch_id: uuid.UUID,
        terminal_id: str,
        items: List[Tuple[uuid.UUID, int]],
        ttl_seconds: int = 3600
    ) -> List[StockLease]:
        """
        Grants or renews leases for the given terminal and items.
        Items is a list of (drug_id, requested_quantity).
        """
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(seconds=ttl_seconds)
        
        drug_ids = [item[0] for item in items]
        
        # 1. Calculate available unleased stock for THIS terminal (which excludes OTHER terminals' leases)
        available_stock = await compute_sellable_quantities(db, branch_id, drug_ids, terminal_id=terminal_id)
        
        # 2. Get existing active leases for this terminal and these drugs
        existing_leases_res = await db.execute(
            select(StockLease)
            .where(
                StockLease.branch_id == branch_id,
                StockLease.terminal_id == terminal_id,
                StockLease.drug_id.in_(drug_ids),
                StockLease.status == 'active'
            )
            .with_for_update()
        )
        existing_leases = {lease.drug_id: lease for lease in existing_leases_res.scalars().all()}
        
        result_leases = []
        for drug_id, requested_qty in items:
            available = available_stock.get(drug_id, 0)
            # Grant up to requested qty, capped at available stock.
            # Available stock already ignores THIS terminal's lease, so `available` is the TOTAL pool we can draw from.
            granted_qty = min(requested_qty, available)
            
            lease = existing_leases.get(drug_id)
            if lease:
                lease.leased_quantity = granted_qty
                lease.expires_at = expires_at
                # If we've already consumed more than the newly granted qty, something is weird, but we leave consumed alone.
            else:
                lease = StockLease(
                    branch_id=branch_id,
                    terminal_id=terminal_id,
                    drug_id=drug_id,
                    leased_quantity=granted_qty,
                    consumed_quantity=0,
                    expires_at=expires_at,
                    status='active'
                )
                db.add(lease)
            result_leases.append(lease)
            
        await db.flush()
        return result_leases
        
    @staticmethod
    async def release_lease(
        db: AsyncSession,
        lease_id: uuid.UUID
    ) -> None:
        """
        Releases a lease back to the pool.
        """
        await db.execute(
            update(StockLease)
            .where(StockLease.id == lease_id)
            .values(status='released', updated_at=datetime.now(timezone.utc))
        )
        await db.flush()

    @staticmethod
    async def expire_stale_leases(db: AsyncSession) -> int:
        """
        Finds all active leases that have passed their expires_at time and marks them expired.
        """
        now_utc = datetime.now(timezone.utc)
        result = await db.execute(
            update(StockLease)
            .where(
                StockLease.status == 'active',
                StockLease.expires_at < now_utc
            )
            .values(status='expired', updated_at=now_utc)
        )
        await db.flush()
        return result.rowcount
