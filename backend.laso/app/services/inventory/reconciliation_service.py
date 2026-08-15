import uuid
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.stock_lease import StockLease
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch
from app.schemas.reconciliation_schemas import ReconciliationReportResponse, DrugReconciliationItem

async def generate_reconciliation_report(
    db: AsyncSession, branch_id: uuid.UUID, report_date: date
) -> ReconciliationReportResponse:
    # 1. Fetch branch and org
    branch_result = await db.execute(select(Branch).where(Branch.id == branch_id))
    branch = branch_result.scalar_one_or_none()
    if not branch:
        raise ValueError("Branch not found")

    org_id = branch.organization_id

    # 2. Get inventory quantities
    inventory_stmt = (
        select(
            BranchInventory.drug_id,
            Drug.name.label("drug_name"),
            func.sum(BranchInventory.quantity).label("total_quantity"),
            func.sum(BranchInventory.reserved_quantity).label("total_reserved")
        )
        .join(Drug, Drug.id == BranchInventory.drug_id)
        .where(BranchInventory.branch_id == branch_id)
        .group_by(BranchInventory.drug_id, Drug.name)
    )
    inventory_res = await db.execute(inventory_stmt)
    inventory_data = inventory_res.all()

    # 3. Get batch sums
    batch_stmt = (
        select(
            DrugBatch.drug_id,
            func.sum(DrugBatch.remaining_quantity).label("batch_sum")
        )
        .where(DrugBatch.branch_id == branch_id)
        .group_by(DrugBatch.drug_id)
    )
    batch_res = await db.execute(batch_stmt)
    batch_sums = {row.drug_id: row.batch_sum for row in batch_res.all()}

    # 4. Get active lease sums
    lease_stmt = (
        select(
            StockLease.drug_id,
            func.sum(StockLease.leased_quantity - StockLease.consumed_quantity).label("active_lease_qty")
        )
        .where(StockLease.branch_id == branch_id)
        .where(StockLease.status == 'active')
        .group_by(StockLease.drug_id)
    )
    lease_res = await db.execute(lease_stmt)
    lease_sums = {row.drug_id: row.active_lease_qty for row in lease_res.all()}

    # 5. Get dead letter count
    dlq_res = await db.execute(
        text("SELECT COUNT(*) FROM event_dead_letter WHERE org_id = :org_id"),
        {"org_id": str(org_id)}
    )
    dead_letter_count = dlq_res.scalar_one()

    # 6. Reconcile
    items = []
    balanced_count = 0
    drift_count = 0

    for row in inventory_data:
        d_id = row.drug_id
        d_name = row.drug_name
        inv_qty = row.total_quantity or 0
        inv_reserved = row.total_reserved or 0
        
        batch_sum_qty = batch_sums.get(d_id, 0)
        lease_unconsumed = lease_sums.get(d_id, 0)

        sellable_quantity = inv_qty - inv_reserved
        unleased_sellable = sellable_quantity - lease_unconsumed

        drift = inv_qty - batch_sum_qty
        
        status = 'balanced'
        if drift != 0:
            status = 'batch_mismatch'
        elif unleased_sellable < 0:
            status = 'sellable_mismatch'
            
        if status == 'balanced':
            balanced_count += 1
        else:
            drift_count += 1

        items.append(DrugReconciliationItem(
            drug_id=d_id,
            drug_name=d_name,
            inventory_quantity=inv_qty,
            batch_sum_quantity=batch_sum_qty,
            sellable_quantity=sellable_quantity,
            unleased_sellable=unleased_sellable,
            drift=drift,
            status=status
        ))

    return ReconciliationReportResponse(
        branch_id=branch_id,
        report_date=report_date.isoformat(),
        total_drugs_checked=len(items),
        balanced_count=balanced_count,
        drift_count=drift_count,
        dead_letter_count=dead_letter_count,
        items=items,
        has_drift=(drift_count > 0)
    )
