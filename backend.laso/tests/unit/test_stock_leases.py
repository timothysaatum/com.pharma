import pytest
import uuid
from datetime import datetime, timedelta, timezone
from app.services.inventory.lease_service import LeaseService
from app.models.inventory.stock_lease import StockLease
from sqlalchemy import select

@pytest.mark.asyncio
async def test_grant_lease_up_to_available(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    test_branch, test_drug = branch, drugs[0]
    
    # Set up some inventory for test_drug in test_branch
    from app.services.inventory.inventory_service import InventoryService
    from app.schemas.inventory_schemas import DrugBatchCreate
    from app.models.inventory.branch_inventory import BranchInventory

    inventory = BranchInventory(
        branch_id=test_branch.id,
        drug_id=test_drug.id,
        quantity=0,
        reserved_quantity=0
    )
    db.add(inventory)
    await db.flush()

    batch_data = DrugBatchCreate(
        branch_id=test_branch.id,
        drug_id=test_drug.id,
        batch_number="BATCH-LEASE-1",
        quantity=100,
        expiry_date=datetime.now().date() + timedelta(days=30),
        cost_price=10.0,
        selling_price=15.0
    )
    await InventoryService.create_batch(db, batch_data, test_branch.organization_id)

    # Now we have 100 available stock. Request 50.
    leases = await LeaseService.grant_or_renew_lease(
        db,
        branch_id=test_branch.id,
        terminal_id="TERM-1",
        items=[(test_drug.id, 50)],
        ttl_seconds=3600
    )
    
    assert len(leases) == 1
    assert leases[0].leased_quantity == 50
    assert leases[0].terminal_id == "TERM-1"
    
    # Request 60 more (only 50 remaining pool, so it should cap at 50, but since we're the same terminal it sees 100 available pool)
    leases = await LeaseService.grant_or_renew_lease(
        db,
        branch_id=test_branch.id,
        terminal_id="TERM-1",
        items=[(test_drug.id, 110)],
        ttl_seconds=3600
    )
    
    assert leases[0].leased_quantity == 100

@pytest.mark.asyncio
async def test_lease_reduces_available_for_others(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    test_branch, test_drug = branch, drugs[0]
    from app.services.inventory.inventory_service import InventoryService
    from app.schemas.inventory_schemas import DrugBatchCreate
    from app.models.inventory.branch_inventory import BranchInventory

    inventory = BranchInventory(
        branch_id=test_branch.id,
        drug_id=test_drug.id,
        quantity=0,
        reserved_quantity=0
    )
    db.add(inventory)
    await db.flush()

    batch_data = DrugBatchCreate(
        branch_id=test_branch.id,
        drug_id=test_drug.id,
        batch_number="BATCH-LEASE-2",
        quantity=100,
        expiry_date=datetime.now().date() + timedelta(days=30),
        cost_price=10.0,
        selling_price=15.0
    )
    await InventoryService.create_batch(db, batch_data, test_branch.organization_id)

    # Terminal A gets 70
    await LeaseService.grant_or_renew_lease(
        db,
        branch_id=test_branch.id,
        terminal_id="TERM-A",
        items=[(test_drug.id, 70)]
    )

    # Terminal B requests 50, but only 30 available
    leases = await LeaseService.grant_or_renew_lease(
        db,
        branch_id=test_branch.id,
        terminal_id="TERM-B",
        items=[(test_drug.id, 50)]
    )
    
    assert leases[0].leased_quantity == 30

@pytest.mark.asyncio
async def test_lease_extension_and_expiry(db, setup_test_data):
    org, branch, user, drugs, customer = setup_test_data
    test_branch, test_drug = branch, drugs[0]
    from app.services.inventory.inventory_service import InventoryService
    from app.schemas.inventory_schemas import DrugBatchCreate
    from app.models.inventory.branch_inventory import BranchInventory
    from app.services.sync._sellable_qty import compute_sellable_quantities

    inventory = BranchInventory(
        branch_id=test_branch.id,
        drug_id=test_drug.id,
        quantity=0,
        reserved_quantity=0
    )
    db.add(inventory)
    await db.flush()

    batch_data = DrugBatchCreate(
        branch_id=test_branch.id,
        drug_id=test_drug.id,
        batch_number="BATCH-LEASE-3",
        quantity=50,
        expiry_date=datetime.now().date() + timedelta(days=30),
        cost_price=10.0,
        selling_price=15.0
    )
    await InventoryService.create_batch(db, batch_data, test_branch.organization_id)

    leases = await LeaseService.grant_or_renew_lease(
        db,
        branch_id=test_branch.id,
        terminal_id="TERM-EXP",
        items=[(test_drug.id, 50)],
        ttl_seconds=3600
    )
    
    lease_id = leases[0].id
    
    # Available for another terminal should be 0
    avail = await compute_sellable_quantities(db, test_branch.id, [test_drug.id], terminal_id="OTHER")
    assert avail.get(test_drug.id, 0) == 0

    # Manually expire the lease by changing expires_at to past
    leases[0].expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db.flush()

    # Now it should be 50, even though status is still 'active', because of expires_at > now_utc
    avail2 = await compute_sellable_quantities(db, test_branch.id, [test_drug.id], terminal_id="OTHER")
    assert avail2.get(test_drug.id, 0) == 50

    # Expire stale leases (updates status to 'expired')
    expired_count = await LeaseService.expire_stale_leases(db)
    assert expired_count == 1
    
    avail3 = await compute_sellable_quantities(db, test_branch.id, [test_drug.id], terminal_id="OTHER")
    assert avail3.get(test_drug.id, 0) == 50
