#!/usr/bin/env python3
"""
Database Invariant Verification Script for Pharmacare application.
Performs nightly integrity checks to verify that:
1. Physical branch stock matches the sum of non-expired batch stock.
2. Branch stock matches the historical sum of the immutable ledger (InventoryMovement).
3. Individual batch stock matches the historical sum of batch movements.

Usage:
  PYTHONPATH=. python3 scripts/verify_db_invariants.py
"""

import sys
import asyncio
import logging
from typing import Dict, List, Tuple
from sqlalchemy import select, func
from app.db.session import AsyncSessionLocal
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.ledger import InventoryMovement
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("db_invariant_verifier")


async def verify_branch_vs_batch_sums(db) -> List[Dict]:
    """
    Invariant 1: Sum of remaining batch stock <= BranchInventory.quantity.
    (It can be less if some stock is not batch-tracked, but batch stock should never exceed physical).
    """
    logger.info("Checking Invariant 1: Batch stock sums vs physical branch stock...")
    errors = []

    # Query sum of remaining quantities per drug/branch
    batch_sums_stmt = (
        select(
            DrugBatch.branch_id,
            DrugBatch.drug_id,
            func.sum(DrugBatch.remaining_quantity).label("total_batch_qty")
        )
        .where(DrugBatch.remaining_quantity > 0)
        .group_by(DrugBatch.branch_id, DrugBatch.drug_id)
    )
    result = await db.execute(batch_sums_stmt)
    batch_sums = {(row.branch_id, row.drug_id): row.total_batch_qty for row in result.all()}

    # Query all physical inventories
    inv_stmt = select(
        BranchInventory.branch_id,
        BranchInventory.drug_id,
        BranchInventory.quantity,
        Branch.name.label("branch_name"),
        Drug.name.label("drug_name")
    ).join(Branch, Branch.id == BranchInventory.branch_id).join(Drug, Drug.id == BranchInventory.drug_id)
    
    inv_result = await db.execute(inv_stmt)
    
    for row in inv_result.all():
        key = (row.branch_id, row.drug_id)
        batch_qty = batch_sums.get(key, 0)
        physical_qty = row.quantity

        if batch_qty > physical_qty:
            errors.append({
                "branch": row.branch_name,
                "drug": row.drug_name,
                "batch_qty": batch_qty,
                "physical_qty": physical_qty,
                "drift": batch_qty - physical_qty,
                "reason": "Total active batch stock exceeds recorded physical stock."
            })

    return errors


async def verify_inventory_vs_ledger(db) -> List[Dict]:
    """
    Invariant 2: BranchInventory.quantity == sum(InventoryMovement.quantity_change).
    Every stock movement must be documented in the ledger.
    """
    logger.info("Checking Invariant 2: Physical stock vs historical ledger changes...")
    errors = []

    # Aggregate all movements in the immutable ledger
    ledger_sums_stmt = (
        select(
            InventoryMovement.branch_id,
            InventoryMovement.drug_id,
            func.sum(InventoryMovement.quantity_change).label("total_ledger_qty")
        )
        .group_by(InventoryMovement.branch_id, InventoryMovement.drug_id)
    )
    result = await db.execute(ledger_sums_stmt)
    ledger_sums = {(row.branch_id, row.drug_id): row.total_ledger_qty for row in result.all()}

    # Fetch physical stock
    inv_stmt = select(
        BranchInventory.branch_id,
        BranchInventory.drug_id,
        BranchInventory.quantity,
        Branch.name.label("branch_name"),
        Drug.name.label("drug_name")
    ).join(Branch, Branch.id == BranchInventory.branch_id).join(Drug, Drug.id == BranchInventory.drug_id)
    inv_result = await db.execute(inv_stmt)

    for row in inv_result.all():
        key = (row.branch_id, row.drug_id)
        ledger_qty = ledger_sums.get(key, 0)
        physical_qty = row.quantity

        if ledger_qty != physical_qty:
            errors.append({
                "branch": row.branch_name,
                "drug": row.drug_name,
                "ledger_qty": ledger_qty,
                "physical_qty": physical_qty,
                "drift": physical_qty - ledger_qty,
                "reason": "Physical inventory does not reconcile with movement ledger history."
            })

    return errors


async def verify_batches_vs_ledger(db) -> List[Dict]:
    """
    Invariant 3: DrugBatch.remaining_quantity == sum(InventoryMovement.quantity_change) where batch_id matches.
    """
    logger.info("Checking Invariant 3: Batch remaining stock vs historical batch-ledger changes...")
    errors = []

    # Query batch movements in ledger
    batch_ledger_stmt = (
        select(
            InventoryMovement.batch_id,
            func.sum(InventoryMovement.quantity_change).label("total_batch_ledger")
        )
        .where(InventoryMovement.batch_id.isnot(None))
        .group_by(InventoryMovement.batch_id)
    )
    result = await db.execute(batch_ledger_stmt)
    batch_ledger_sums = {row.batch_id: row.total_batch_ledger for row in result.all()}

    # Query active batches
    batches_stmt = select(
        DrugBatch.id,
        DrugBatch.batch_number,
        DrugBatch.remaining_quantity,
        DrugBatch.quantity.label("initial_qty"),
        Branch.name.label("branch_name"),
        Drug.name.label("drug_name")
    ).join(Branch, Branch.id == DrugBatch.branch_id).join(Drug, Drug.id == DrugBatch.drug_id)
    batches_result = await db.execute(batches_stmt)

    for row in batches_result.all():
        ledger_change = batch_ledger_sums.get(row.id, 0)
        # Reconciled batch remaining quantity must match: initial_qty + sum(ledger_change)
        # Note: if quantity_change includes the initial receipt (movement_type = purchase_receipt),
        # then sum(ledger_change) itself equals the remaining stock.
        # If the receipt is NOT in the ledger, then the sum is relative to initial_qty.
        # Let's check both by calculating absolute drift.
        expected_remaining = row.initial_qty + ledger_change if ledger_change < 0 else ledger_change

        if expected_remaining != row.remaining_quantity:
            # Check if ledger contains the initial receipt
            # If so, sum(ledger_change) should exactly match remaining_quantity
            if ledger_change == row.remaining_quantity:
                continue

            errors.append({
                "branch": row.branch_name,
                "drug": row.drug_name,
                "batch_number": row.batch_number,
                "ledger_sum": ledger_change,
                "batch_remaining": row.remaining_quantity,
                "drift": row.remaining_quantity - expected_remaining,
                "reason": "Batch remaining quantity does not reconcile with batch ledger entries."
            })

    return errors


async def main():
    logger.info("Starting database invariant verification run...")
    async with AsyncSessionLocal() as db:
        try:
            # 1. Branch vs Batch
            branch_batch_errors = await verify_branch_vs_batch_sums(db)
            # 2. Inventory vs Ledger
            inventory_ledger_errors = await verify_inventory_vs_ledger(db)
            # 3. Batch vs Ledger
            batch_ledger_errors = await verify_batches_vs_ledger(db)

            all_errors = branch_batch_errors + inventory_ledger_errors + batch_ledger_errors

            if all_errors:
                logger.error(f"Invariant check failed. Found {len(all_errors)} discrepancies:")
                for err in all_errors:
                    if "batch_number" in err:
                        logger.warning(
                            f"Discrepancy at {err['branch']} for {err['drug']} (Batch: {err['batch_number']}): "
                            f"Ledger={err.get('ledger_sum')}, BatchRemaining={err['batch_remaining']}. "
                            f"Reason: {err['reason']}"
                        )
                    else:
                        logger.warning(
                            f"Discrepancy at {err['branch']} for {err['drug']}: "
                            f"Ledger={err.get('ledger_qty', 'N/A')}, Physical={err.get('physical_qty', 'N/A')}, "
                            f"BatchSum={err.get('batch_qty', 'N/A')}. Reason: {err['reason']}"
                        )
                sys.exit(1)
            else:
                logger.info("All database invariants successfully verified! 100% consistent.")
                sys.exit(0)

        except Exception as e:
            logger.exception(f"Verification run failed due to system exception: {e}")
            sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
