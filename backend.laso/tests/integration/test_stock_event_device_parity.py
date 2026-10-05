"""
End-to-end stock-event parity: real service call -> real device replay.

THE CONTRACT
------------
A device only learns stock from events, so "the service committed the right
number" is not enough. This file proves the events it appended actually drive a
device to the server's state.

It cannot replay on a device directly — the device is TypeScript. So this file
does half the work and hands the other half over a JSON contract:

  1. Call the REAL service function against the disposable PostgreSQL.
  2. Read every event it appended to event_log (org-scoped, in seq order).
  3. Read the server's resulting branch_inventory + drug_batches rows.
  4. Write both to ui.laso/src/lib/__tests__/fixtures/stock-parity.json.

  5. vitest (stockEventDeviceParity.test.ts) replays those events through the
     REAL projectors onto a fresh in-memory device and onto a "partial" device
     that already holds some lots, then compares local rows against the server
     rows this file captured.

Both halves must agree or the feature is not finished, so the vitest half is
where the verdict is produced.

Requires TEST_DATABASE_URL pointing at a DISPOSABLE cluster.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)

from app.models.inventory.branch_inventory import DrugBatch  # noqa: E402
from app.services.inventory.inventory_service import InventoryService  # noqa: E402
from app.services.sales.purchase_order_service import PurchaseOrderService  # noqa: E402
from app.services.sales.sales_service import SalesService  # noqa: E402

# Only usable where there is no FK: stock_adjustments.adjusted_by references
# users, so service calls must pass the fixture's real user id, not this.
AUTHOR = uuid.UUID("bae475d9-994a-4d5b-abb2-32aa4b082602")

FIXTURE_PATH = Path(__file__).resolve().parents[3] / (
    "ui.laso/src/lib/__tests__/fixtures/stock-parity.json"
)

SCENARIOS: dict[str, dict[str, Any]] = {}


# ── Infrastructure ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def event_log_table(db):
    """event_log is Alembic-owned with no ORM model; the suite builds it by DDL."""
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.execute(text("""
        CREATE TABLE event_log (
            event_id TEXT NOT NULL,
            org_id UUID NOT NULL,
            seq BIGINT NOT NULL,
            aggregate_id UUID NOT NULL,
            aggregate_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            schema_version SMALLINT NOT NULL DEFAULT 1,
            payload JSONB NOT NULL,
            dependencies TEXT[] NOT NULL DEFAULT '{}',
            authored_at TIMESTAMPTZ NOT NULL,
            authored_by UUID NOT NULL,
            branch_id UUID NOT NULL,
            hash_self TEXT NOT NULL,
            hash_prev TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (org_id, event_id),
            UNIQUE (org_id, seq)
        )
    """))
    await db.commit()
    yield
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


async def _append_events(db, org_id) -> list[dict[str, Any]]:
    """Every event in the org's log, in the order a device would pull them."""
    rows = (
        (
            await db.execute(
                text(
                    "SELECT event_id, seq, aggregate_id, aggregate_type, "
                    "event_type, payload, authored_at, authored_by, branch_id "
                    "FROM event_log WHERE org_id = :o ORDER BY seq"
                ),
                {"o": str(org_id)},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "event_id": r["event_id"],
            "seq": int(r["seq"]),
            "org_id": str(org_id),
            "aggregate_id": str(r["aggregate_id"]),
            "aggregate_type": r["aggregate_type"],
            "event_type": r["event_type"],
            "schema_version": 1,
            "payload": r["payload"],
            "dependencies": [],
            "authored_at": r["authored_at"].isoformat(),
            "authored_by": str(r["authored_by"]),
            "branch_id": str(r["branch_id"]),
            "hash_self": "0" * 64,
            "hash_prev": "0" * 64,
        }
        for r in rows
    ]


async def _server_state(db, branch_id, drug_id) -> dict[str, Any]:
    """The rows a device must end up matching, exactly as PostgreSQL holds them."""
    inv = (
        await db.execute(
            text(
                "SELECT quantity, reserved_quantity FROM branch_inventory "
                "WHERE branch_id = :b AND drug_id = :d"
            ),
            {"b": str(branch_id), "d": str(drug_id)},
        )
    ).first()

    batches = (
        await db.execute(
            text(
                "SELECT id, batch_number, quantity, remaining_quantity, expiry_date "
                "FROM drug_batches WHERE branch_id = :b AND drug_id = :d "
                "ORDER BY batch_number"
            ),
            {"b": str(branch_id), "d": str(drug_id)},
        )
    ).mappings().all()

    return {
        "branch_inventory": (
            None
            if inv is None
            else {
                "quantity": int(inv[0]),
                "reserved_quantity": int(inv[1]),
            }
        ),
        "drug_batches": [
            {
                "id": str(r["id"]),
                "batch_number": r["batch_number"],
                "quantity": int(r["quantity"]),
                "remaining_quantity": int(r["remaining_quantity"]),
                "expiry_date": (
                    r["expiry_date"].isoformat() if r["expiry_date"] else None
                ),
            }
            for r in batches
        ],
    }


def _record(name: str, *, events, server, branch_id, drug_id, note: str = "") -> None:
    SCENARIOS[name] = {
        "branch_id": str(branch_id),
        "drug_id": str(drug_id),
        "events": events,
        "server": server,
        "note": note,
    }


async def _batch_sum(db, branch_id, drug_id) -> int:
    """SUM(remaining_quantity > 0) for a pair — the quantity the device derives."""
    from sqlalchemy import func

    from app.models.inventory.branch_inventory import DrugBatch as _DB

    total = await db.scalar(
        select(func.coalesce(func.sum(_DB.remaining_quantity), 0)).where(
            _DB.branch_id == branch_id,
            _DB.drug_id == drug_id,
            _DB.remaining_quantity > 0,
        )
    )
    return int(total or 0)


async def _first_po_item_id(db, po_id):
    """The PO's first line id.

    Not via `po.items`: create_purchase_order leaves that relationship
    PASSIVE_OFF, so touching it is lazy IO outside a greenlet and raises
    MissingGreenlet.
    """
    from app.models.sales.sales_model import PurchaseOrderItem

    return await db.scalar(
        select(PurchaseOrderItem.id)
        .where(PurchaseOrderItem.purchase_order_id == po_id)
        .order_by(PurchaseOrderItem.id)
        .limit(1)
    )


async def _flush_contract() -> None:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(SCENARIOS, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


# ── Fixtures for the domain objects the services need ────────────────────────


@pytest_asyncio.fixture
async def stock_scenario(db, setup_test_data, event_log_table):
    """(org, branch, user, drug) plus helpers to seed lots directly."""
    org, branch, user, drugs, _customer = setup_test_data

    async def seed_batch(branch_id, drug_id, *, batch_number, quantity, expiry_days=365):
        batch = DrugBatch(
            id=uuid.uuid4(),
            branch_id=branch_id,
            drug_id=drug_id,
            batch_number=batch_number,
            quantity=quantity,
            remaining_quantity=quantity,
            manufacturing_date=date(2020, 1, 1),
            expiry_date=date.today() + timedelta(days=expiry_days),
            cost_price=Decimal("8.00"),
            supplier="Test Supplier",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(batch)
        await db.flush()
        return batch

    async def make_lot(branch_id, drug_id, batch_number: str, quantity: int):
        """Create a lot the way the application does — via create_batch.

        NOT seed_batch: that writes the row straight to the database, so no event
        is published and a replaying device never hears about the lot. Any lot a
        device is expected to know must arrive through an event, which means it
        must be created through the emitting path.
        """
        return await InventoryService.create_batch(
            db=db,
            batch_data=_batch_payload(branch_id, drug_id, batch_number, quantity),
            created_by=user.id,
        )

    async def seed_price_contract():
        """process_sale refuses to price a sale with no active contract."""
        from app.models.pricing.pricing_model import PriceContract

        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add(contract)
        await db.flush()
        return contract

    return org, branch, user, drugs, seed_batch, make_lot, seed_price_contract


# ── Path 1: receive_goods (the real delivery path) ───────────────────────────


@pytest.mark.asyncio
async def test_receive_goods_publishes_batch_detail(db, stock_scenario):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[0]

    # The Amoxicilin shape: two lots already on the branch (147 + 100 = 247),
    # then a receipt adds a third (+50 = 297).
    #
    # The inventory row must exist BEFORE the lots are seeded:
    # _recalculate_inventory_quantity only updates an existing row
    # (`if inventory:`), so seeding a lot against no row leaves the aggregate at
    # nothing and quietly breaks quantity == SUM(lots). Worth knowing.
    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    await make_lot(branch.id, drug.id, "LOT-1", 147)
    await make_lot(branch.id, drug.id, "LOT-2", 100)
    assert await _batch_sum(db, branch.id, drug.id) == 247

    supplier = await PurchaseOrderService.create_supplier(
        db=db, supplier_data=_supplier_payload(org.id), user=user
    )
    po = await PurchaseOrderService.create_purchase_order(
        db=db, po_data=_po_payload(supplier.id, branch.id, drug.id), user=user
    )
    po_item_id = await _first_po_item_id(db, po.id)
    await PurchaseOrderService.submit_for_approval(db=db, po_id=po.id, user=user)
    await PurchaseOrderService.approve_purchase_order(
        db=db, po_id=po.id, user=user
    )

    result = await PurchaseOrderService.receive_goods(
        db=db,
        po_id=po.id,
        receive_data=_receive_payload(po_item_id, "LOT-3", 50),
        user=user,
    )
    assert result.batches_created == 1

    events = await _append_events(db, org.id)
    # The ruling: batch detail is mandatory, and the inventory event carries the
    # server's absolute quantity AFTER the change.
    # The whole history must be replayable, so the baseline lots are themselves
    # event-published (via create_batch). add_drug_to_branch opens the log, each
    # lot adds a drug_batch_created + branch_inventory_updated pair, and the
    # receipt adds the third lot.
    assert [e["event_type"] for e in events] == [
        "branch_inventory_created",
        "drug_batch_created",
        "branch_inventory_updated",
        "drug_batch_created",
        "branch_inventory_updated",
        "drug_batch_created",
        "branch_inventory_updated",
    ]
    assert events[5]["payload"]["remaining_quantity"] == 50
    assert events[5]["payload"]["batch_number"] == "LOT-3"
    # The inventory event carries the server's absolute quantity AFTER the
    # change: 247 + 50 = 297, not the 50 that arrived.
    assert events[6]["payload"]["quantity"] == 297
    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 297
    assert len(server["drug_batches"]) == 3
    # The invariant the whole C-hybrid design rests on.
    assert server["branch_inventory"]["quantity"] == sum(
        b["remaining_quantity"] for b in server["drug_batches"]
    )

    _record(
        "receive_goods adds a second lot",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="PO receipt appending a new lot to an existing one",
    )
    await _flush_contract()


# ── Path 2: create_batch (manual add stock) ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_batch_publishes_batch_detail(db, stock_scenario):
    org, branch, user, drugs, _seed, make_lot, seed_contract = stock_scenario
    drug = drugs[1]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )

    batch = await InventoryService.create_batch(
        db=db,
        batch_data=_batch_payload(branch.id, drug.id, "MANUAL-1", 80),
        created_by=user.id,
    )

    events = await _append_events(db, org.id)
    assert [e["event_type"] for e in events] == [
        "branch_inventory_created",
        "drug_batch_created",
        "branch_inventory_updated",
    ]
    assert events[1]["payload"]["remaining_quantity"] == 80
    assert events[2]["payload"]["quantity"] == 80

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 80

    _record(
        "create_batch manual add stock",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="add_drug_to_branch at 0, then a manual lot",
    )
    await _flush_contract()


@pytest.mark.asyncio
async def test_update_batch_publishes_corrected_remaining(db, stock_scenario):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[2]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    batch = await seed_batch(branch.id, drug.id, batch_number="FIX-1", quantity=100)
    await InventoryService._recalculate_inventory_quantity(
        db=db, branch_id=branch.id, drug_id=drug.id
    )
    await db.commit()

    await InventoryService.update_batch(
        db=db,
        batch_id=batch.id,
        batch_data=_batch_update_payload(remaining_quantity=35),
    )

    events = await _append_events(db, org.id)
    assert "drug_batch_updated" in [e["event_type"] for e in events]
    upd = next(e for e in events if e["event_type"] == "drug_batch_updated")
    # Absolute, not a delta: this is what the ruling requires.
    assert upd["payload"]["remaining_quantity"] == 35

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 35

    _record(
        "update_batch corrects a lot",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="absolute corrected remaining_quantity, not a delta",
    )
    await _flush_contract()


# ── Path 3: _apply_adjustment (all types) ────────────────────────────────────


@pytest.mark.asyncio
async def test_adjustment_drains_a_lot_to_zero(db, stock_scenario):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[0]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    b1 = await seed_batch(branch.id, drug.id, batch_number="ADJ-A", quantity=60)
    b2 = await seed_batch(branch.id, drug.id, batch_number="ADJ-B", quantity=40)
    await InventoryService._recalculate_inventory_quantity(
        db=db, branch_id=branch.id, drug_id=drug.id
    )
    await db.commit()

    # -70 draws down ADJ-A (60) then 10 from ADJ-B, so ADJ-A hits zero.
    await InventoryService.adjust_inventory(
        db=db,
        branch_id=branch.id,
        drug_id=drug.id,
        quantity_change=-70,
        adjustment_type="damage",
        reason="water damage",
        adjusted_by=user.id,
    )

    events = await _append_events(db, org.id)
    types = [e["event_type"] for e in events]
    # Both touched lots are published, each with an absolute remaining.
    assert types.count("drug_batch_updated") == 2
    remainings = {
        e["payload"]["batch_number"]: e["payload"]["remaining_quantity"]
        for e in events
        if e["event_type"] == "drug_batch_updated"
    }
    assert remainings == {"ADJ-A": 0, "ADJ-B": 30}

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 30
    assert {b["batch_number"]: b["remaining_quantity"] for b in server["drug_batches"]} == {
        "ADJ-A": 0,
        "ADJ-B": 30,
    }

    _record(
        "damage adjustment drains a lot to zero",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="FEFO draw-down across two lots; ADJ-A reaches 0",
    )
    await _flush_contract()


@pytest.mark.asyncio
async def test_adjustment_on_a_drug_with_no_lots(db, stock_scenario):
    org, branch, user, drugs, _seed, make_lot, seed_contract = stock_scenario
    drug = drugs[1]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )

    await InventoryService.adjust_inventory(
        db=db,
        branch_id=branch.id,
        drug_id=drug.id,
        quantity_change=25,
        adjustment_type="correction",
        reason="opening balance",
        adjusted_by=user.id,
    )

    events = await _append_events(db, org.id)
    created = [e for e in events if e["event_type"] == "drug_batch_created"]
    assert len(created) == 1
    # The server fabricates an ADJ- lot; the event must mirror it exactly,
    # otherwise the device derives from a lot the server does not have.
    assert created[0]["payload"]["batch_number"].startswith("ADJ-")
    assert created[0]["payload"]["remaining_quantity"] == 25
    assert created[0]["payload"]["expiry_date"] == (
        date.today() + timedelta(days=365 * 10)
    ).isoformat()

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 25

    _record(
        "correction on a drug with no lots",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="server fabricates the ADJ- lot; the event mirrors it exactly",
    )
    await _flush_contract()


# ── Path 4: consume_from_batch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_from_batch_publishes_batch_detail(db, stock_scenario):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[2]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    batch = await seed_batch(branch.id, drug.id, batch_number="CONS-1", quantity=90)
    await InventoryService._recalculate_inventory_quantity(
        db=db, branch_id=branch.id, drug_id=drug.id
    )
    await db.commit()

    await InventoryService.consume_from_batch(
        db=db, batch_id=batch.id, quantity=30
    )

    events = await _append_events(db, org.id)
    upd = next(e for e in events if e["event_type"] == "drug_batch_updated")
    assert upd["payload"]["remaining_quantity"] == 60

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 60

    _record(
        "consume_from_batch",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="direct lot consumption",
    )
    await _flush_contract()


# ── Path 5: refund_sale (restock into the original lots) ─────────────────────


@pytest.mark.asyncio
async def test_refund_restocks_into_the_original_lot(db, stock_scenario):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[0]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    contract = await seed_contract()
    # Two lots so FEFO has to choose, and so the refund's original lot is
    # distinguishable from the untouched one.
    near = await make_lot(branch.id, drug.id, "LOT-NEAR", 50)
    far = await make_lot(branch.id, drug.id, "LOT-FAR", 70)

    result = await SalesService.process_sale(
        db=db,
        sale_data=_sale_payload(branch.id, contract.id, drug.id, quantity=20),
        user=user,
    )
    sale = result.sale
    # FEFO drew from the near lot.
    assert near.remaining_quantity == 30

    await SalesService.refund_sale(
        db=db,
        sale_id=sale.id,
        refund_data=_refund_payload(sale, user, 20),
        user=user,
    )

    events = await _append_events(db, org.id)
    ups = [e for e in events if e["event_type"] == "drug_batch_updated"]
    # The refund must put stock back into the ORIGINAL lot, not a new one.
    restocked = {
        e["payload"]["batch_number"]: e["payload"]["remaining_quantity"] for e in ups
    }
    assert restocked["LOT-NEAR"] == 50
    assert "RETURN-" not in " ".join(restocked)

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 120
    assert {b["batch_number"]: b["remaining_quantity"] for b in server["drug_batches"]} == {
        "LOT-NEAR": 50,
        "LOT-FAR": 70,
    }

    _record(
        "refund restocks the original lot",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="FEFO sale across two lots, then a refund into the exact original lot",
    )
    await _flush_contract()


# ── Item 6: the sale path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sale_created_payload_carries_batch_allocations(db, stock_scenario):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[1]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    contract = await seed_contract()
    # 30 in the near lot, 70 in the far one: a 50-unit sale must spill.
    await make_lot(branch.id, drug.id, "SPILL-NEAR", 30)
    await make_lot(branch.id, drug.id, "SPILL-FAR", 70)

    await SalesService.process_sale(
        db=db,
        sale_data=_sale_payload(branch.id, contract.id, drug.id, quantity=50),
        user=user,
    )

    events = await _append_events(db, org.id)
    sale_ev = next(e for e in events if e["event_type"] == "sale_created")
    allocations = sale_ev["payload"]["batch_changes"]

    # Two allocations = the multi-lot spill is described, not just the total.
    assert len(allocations) == 2, allocations
    assert {a["quantity_used"] for a in allocations} == {30, 20}
    for a in allocations:
        assert set(a) == {"batch_id", "quantity_used"}

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 50
    assert {b["batch_number"]: b["remaining_quantity"] for b in server["drug_batches"]} == {
        "SPILL-NEAR": 0,
        "SPILL-FAR": 50,
    }

    _record(
        "sale_created multi-lot spill",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="50 units across a 30 and a 70 lot; FEFO spill in batch_changes",
    )
    await _flush_contract()


# ── Rollback: a forced append failure must undo the stock change ─────────────


@pytest.mark.asyncio
async def test_forced_append_failure_rolls_back_the_receipt(
    db, stock_scenario, monkeypatch
):
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[0]

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drug.id,
        organization_id=org.id, authored_by=user.id,
    )
    await seed_batch(branch.id, drug.id, batch_number="RB-1", quantity=100)
    await InventoryService._recalculate_inventory_quantity(
        db=db, branch_id=branch.id, drug_id=drug.id
    )
    await db.commit()

    before = await _server_state(db, branch.id, drug.id)

    supplier = await PurchaseOrderService.create_supplier(
        db=db, supplier_data=_supplier_payload(org.id), user=user
    )
    po = await PurchaseOrderService.create_purchase_order(
        db=db, po_data=_po_payload(supplier.id, branch.id, drug.id), user=user
    )
    po_item_id = await _first_po_item_id(db, po.id)
    await PurchaseOrderService.submit_for_approval(db=db, po_id=po.id, user=user)
    await PurchaseOrderService.approve_purchase_order(db=db, po_id=po.id, user=user)

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated append failure")

    from app.services.sync.eventlog import append_service
    from app.services.sync.eventlog.stock_emitter import StockEventEmitter

    monkeypatch.setattr(StockEventEmitter, "_append_one", _boom, raising=False)
    monkeypatch.setattr(
        append_service.AppendService, "_append_one", _boom, raising=False
    )

    with pytest.raises(RuntimeError):
        try:
            await PurchaseOrderService.receive_goods(
                db=db, po_id=po.id,
                receive_data=_receive_payload(po_item_id, "RB-2", 40),
                user=user,
            )
        finally:
            await db.execute(text("ROLLBACK"))

    after = await _server_state(db, branch.id, drug.id)
    # The delivery is entirely undone: no new lot, quantity unchanged.
    assert after["drug_batches"] == before["drug_batches"]
    assert after["branch_inventory"]["quantity"] == before["branch_inventory"]["quantity"]
    assert {b["batch_number"] for b in after["drug_batches"]} == {"RB-1"}


@pytest.mark.asyncio
async def test_operation_that_raises_publishes_nothing(db, stock_scenario):
    """No emit may survive an operation that raises before committing.

    The events are appended inside the caller's transaction, so a raise must
    leave event_log byte-for-byte unchanged. This is the mirror of the forced
    append failure: there the append breaks the stock change, here the stock
    change breaks and must take no event with it.
    """
    org, branch, user, drugs, seed_batch, make_lot, _contract = stock_scenario
    drug = drugs[0]

    await make_lot(branch.id, drug.id, "RAISE-1", 10)
    before = await _append_events(db, org.id)
    assert before, "fixture should have published the baseline lot"

    # 999 units cannot be allocated from a 10-unit lot, so _apply_adjustment
    # raises HTTPException(400) from inside the savepoint, after it has already
    # mutated rows.
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        try:
            await InventoryService.adjust_inventory(
                db=db,
                branch_id=branch.id,
                drug_id=drug.id,
                quantity_change=-999,
                adjustment_type="damage",
                reason="impossible",
                adjusted_by=user.id,
            )
        finally:
            await db.execute(text("ROLLBACK"))

    after = await _append_events(db, org.id)
    assert [e["event_id"] for e in after] == [e["event_id"] for e in before]

    # And the stock itself is untouched.
    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 10
    assert [b["remaining_quantity"] for b in server["drug_batches"]] == [10]


# ── A lot must never exist without its aggregate row ─────────────────────────


@pytest.mark.asyncio
async def test_a_lot_never_ends_up_without_an_aggregate_row(db, stock_scenario):
    """quantity == SUM(lots) must hold for every pair, always.

    _recalculate_inventory_quantity used to do nothing at all when the
    BranchInventory row was missing, so a pair could hold lots while the
    aggregate tracked nothing. That breaks the invariant the device's C-hybrid
    derivation relies on, and it surfaced far from its cause: the next caller
    raised NoResultFound or a 404 instead.
    """
    org, branch, user, drugs, seed_batch, _make_lot, _contract = stock_scenario
    drug = drugs[0]

    # A lot written straight to the database, with NO aggregate row: the state
    # that used to break silently.
    await seed_batch(branch.id, drug.id, batch_number="ORPHAN-1", quantity=64)
    orphan = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM branch_inventory "
                "WHERE branch_id = :b AND drug_id = :d"
            ),
            {"b": str(branch.id), "d": str(drug.id)},
        )
    ).scalar_one()
    assert orphan == 0, "precondition: no aggregate row yet"

    qty = await InventoryService._recalculate_inventory_quantity(
        db=db, branch_id=branch.id, drug_id=drug.id
    )
    await db.commit()

    # The aggregate now exists and equals the lot sum.
    assert qty == 64
    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"] is not None
    assert server["branch_inventory"]["quantity"] == 64
    assert await _batch_sum(db, branch.id, drug.id) == 64

    # And it is idempotent: a second call changes nothing.
    again = await InventoryService._recalculate_inventory_quantity(
        db=db, branch_id=branch.id, drug_id=drug.id
    )
    await db.commit()
    assert again == 64
    rows = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM branch_inventory "
                "WHERE branch_id = :b AND drug_id = :d"
            ),
            {"b": str(branch.id), "d": str(drug.id)},
        )
    ).scalar_one()
    assert rows == 1, "must not create a second aggregate row"


@pytest.mark.asyncio
async def test_every_pair_with_lots_has_an_aggregate(db, stock_scenario):
    """Sweep the seeded pairs: no lot anywhere without its aggregate."""
    org, branch, user, drugs, seed_batch, _make_lot, _contract = stock_scenario

    for i, drug in enumerate(drugs):
        await seed_batch(branch.id, drug.id, batch_number=f"SWEEP-{i}", quantity=10 + i)
        await InventoryService._recalculate_inventory_quantity(
            db=db, branch_id=branch.id, drug_id=drug.id
        )
    await db.commit()

    orphans = (
        await db.execute(
            text(
                """
                SELECT db.drug_id, SUM(db.remaining_quantity) AS lot_sum
                  FROM drug_batches db
                  LEFT JOIN branch_inventory bi
                    ON bi.branch_id = db.branch_id AND bi.drug_id = db.drug_id
                 WHERE db.branch_id = :b
                 GROUP BY db.drug_id, bi.id
                HAVING bi.id IS NULL OR bi.quantity <> SUM(db.remaining_quantity)
                """
            ),
            {"b": str(branch.id)},
        )
    ).mappings().all()
    assert orphans == [], f"lots without a matching aggregate: {orphans}"


# ── process_sale: the emit must be inside the sale's transaction ──────────────


@pytest.mark.asyncio
async def test_forced_append_failure_rolls_back_the_whole_sale(
    db, stock_scenario, monkeypatch
):
    """A failed append must undo the sale: rows, stock and allocations alike.

    The emit used to run AFTER the commit through ServerEventEmitter, which
    swallows exceptions, so a sale could commit with no event and every device
    would keep showing the pre-sale stock with nothing logged.
    """
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[0]
    contract = await seed_contract()

    await make_lot(branch.id, drug.id, "SALE-RB", 100)
    before = await _server_state(db, branch.id, drug.id)

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated append failure")

    from app.services.sync.eventlog import append_service
    from app.services.sync.eventlog.stock_emitter import StockEventEmitter

    monkeypatch.setattr(StockEventEmitter, "_append_one", _boom, raising=False)
    monkeypatch.setattr(
        append_service.AppendService, "_append_one", _boom, raising=False
    )

    with pytest.raises(RuntimeError):
        try:
            await SalesService.process_sale(
                db=db,
                sale_data=_sale_payload(branch.id, contract.id, drug.id, quantity=40),
                user=user,
            )
        finally:
            await db.execute(text("ROLLBACK"))

    # No sale survived...
    sale_count = (
        await db.execute(
            text("SELECT COUNT(*) FROM sales WHERE branch_id = :b"),
            {"b": str(branch.id)},
        )
    ).scalar_one()
    assert sale_count == 0

    # ...no allocations...
    alloc = (
        await db.execute(
            text("SELECT COUNT(*) FROM sale_item_batch_allocations"),
        )
    ).scalar_one()
    assert alloc == 0

    # ...and the stock is exactly as it was.
    after = await _server_state(db, branch.id, drug.id)
    assert after["drug_batches"] == before["drug_batches"]
    assert after["branch_inventory"]["quantity"] == 100


@pytest.mark.asyncio
async def test_successful_sale_still_publishes_and_replays(db, stock_scenario):
    """The non-swallowing emit must not break the happy path."""
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[1]
    contract = await seed_contract()

    await make_lot(branch.id, drug.id, "SALE-OK-NEAR", 30)
    await make_lot(branch.id, drug.id, "SALE-OK-FAR", 70)

    # Everything before this point came from the fixture's own create_batch
    # calls. Only the events appended by process_sale itself are under test.
    before_ids = {e["event_id"] for e in await _append_events(db, org.id)}

    result = await SalesService.process_sale(
        db=db,
        sale_data=_sale_payload(branch.id, contract.id, drug.id, quantity=50),
        user=user,
    )
    assert result.success is True

    events = await _append_events(db, org.id)
    mine = [e for e in events if e["event_id"] not in before_ids]
    assert [e["event_type"] for e in mine] == ["sale_created"], mine
    # sale_created is still the ONLY carrier from this path: it publishes no
    # drug_batch_* or branch_inventory_* of its own, so the lot deductions travel
    # solely inside its payload's batch_changes.
    assert not [e for e in mine if e["aggregate_type"] in ("drug_batch", "branch_inventory")]
    assert len(mine[0]["payload"]["batch_changes"]) == 2  # the multi-lot spill

    server = await _server_state(db, branch.id, drug.id)
    assert server["branch_inventory"]["quantity"] == 50

    _record(
        "process_sale atomic emit",
        events=events,
        server=server,
        branch_id=branch.id,
        drug_id=drug.id,
        note="sale_created published inside the sale's transaction",
    )
    await _flush_contract()


@pytest.mark.asyncio
async def test_audit_log_failure_still_does_not_abort_the_sale(
    db, stock_scenario, monkeypatch
):
    """Audit-log behaviour must be unchanged: log loud, keep the sale.

    The audit write keeps its own try/except and its own commit precisely so a
    logging failure cannot undo a completed sale. Moving the emit must not have
    dragged that inside the sale's transaction.
    """
    org, branch, user, drugs, seed_batch, make_lot, seed_contract = stock_scenario
    drug = drugs[2]
    contract = await seed_contract()

    await make_lot(branch.id, drug.id, "SALE-AUDIT", 100)

    import app.services.sales.sales_service as svc

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(svc, "create_audit_log", _boom)

    result = await SalesService.process_sale(
        db=db,
        sale_data=_sale_payload(branch.id, contract.id, drug.id, quantity=10),
        user=user,
    )
    assert result.success is True

    sale_count = (
        await db.execute(
            text("SELECT COUNT(*) FROM sales WHERE branch_id = :b"),
            {"b": str(branch.id)},
        )
    ).scalar_one()
    assert sale_count == 1
    # And the event still made it out.
    events = await _append_events(db, org.id)
    assert [e["event_type"] for e in events].count("sale_created") == 1


# ── Payload builders ─────────────────────────────────────────────────────────


def _supplier_payload(org_id):
    from app.schemas.purchase_order_schemas import SupplierCreate

    return SupplierCreate(name="Test Supplier Co", organization_id=org_id)


def _po_payload(supplier_id, branch_id, drug_id):
    from app.schemas.purchase_order_schemas import (
        PurchaseOrderCreate,
        PurchaseOrderItemCreate,
    )

    return PurchaseOrderCreate(
        supplier_id=supplier_id,
        branch_id=branch_id,
        expected_delivery_date=date.today() + timedelta(days=7),
        notes="parity test",
        items=[
            PurchaseOrderItemCreate(
                drug_id=drug_id, quantity_ordered=50, unit_cost=8.0
            )
        ],
    )


def _receive_payload(po_item_id, batch_number: str, quantity: int):
    from app.schemas.purchase_order_schemas import ReceiveItemData, ReceivePurchaseOrder

    return ReceivePurchaseOrder(
        items=[
            ReceiveItemData(
                purchase_order_item_id=po_item_id,
                quantity_received=quantity,
                batch_number=batch_number,
                manufacturing_date=date(2024, 1, 1),
                expiry_date=date.today() + timedelta(days=730),
            )
        ]
    )


def _batch_payload(branch_id, drug_id, batch_number: str, quantity: int):
    from app.schemas.inventory_schemas import DrugBatchCreate

    return DrugBatchCreate(
        branch_id=branch_id,
        drug_id=drug_id,
        batch_number=batch_number,
        quantity=quantity,
        expiry_date=date.today() + timedelta(days=400),
    )


def _batch_update_payload(**kw):
    from app.schemas.inventory_schemas import DrugBatchUpdate

    return DrugBatchUpdate(**kw)


def _sale_payload(branch_id, contract_id, drug_id, *, quantity: int):
    from app.schemas.sales_schemas import SaleCreate, SaleItemCreate

    return SaleCreate(
        branch_id=branch_id,
        price_contract_id=contract_id,
        customer_id=None,
        payment_method="cash",
        amount_paid=Decimal("100000.00"),
        items=[SaleItemCreate(drug_id=drug_id, quantity=quantity)],
    )


def _refund_payload(sale, user, quantity: int):
    """Refund the first sale item, restocking into its original lots.

    The service requires refund_amount to EQUAL the selected items' value, so it
    is computed from the item's own unit price rather than assumed.
    """
    from app.schemas.sales_schemas import RefundItemData, RefundSaleRequest

    item = sale.items[0]
    value = Decimal(str(item.unit_price)) * Decimal(quantity)
    return RefundSaleRequest(
        items_to_refund=[
            RefundItemData(
                sale_item_id=item.id,
                quantity=quantity,
                reason="customer return",
                restock=True,
            )
        ],
        refund_amount=value,
        manager_approval_user_id=user.id,
        reason="customer return",
    )
