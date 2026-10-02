"""
Stock events must be published in the SAME transaction as the stock change.

The defect being prevented: ServerEventEmitter runs after the caller's commit and
swallows exceptions, which is right for reference data but wrong for stock. A
stock row committed with no event leaves every device showing its old numbers,
with no error anywhere. So a forced append failure must roll the stock change
back.

Requires TEST_DATABASE_URL pointing at a DISPOSABLE cluster.
"""
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)

from app.models.inventory.inventory_model import Drug  # noqa: E402
from app.services.inventory.inventory_service import InventoryService  # noqa: E402
from app.services.sync.eventlog import append_service  # noqa: E402
from app.services.sync.eventlog.stock_emitter import (  # noqa: E402
    StockEventEmitter,
    deterministic_event_id,
)

AUTHOR = uuid.UUID("bae475d9-994a-4d5b-abb2-32aa4b082602")


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
    await db.execute(text("DROP TABLE IF EXISTS applied_events_probe CASCADE"))
    await db.commit()
    yield
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


async def _events(db, org_id):
    return (
        (
            await db.execute(
                text(
                    "SELECT seq, event_type, aggregate_id, payload, hash_self, hash_prev "
                    "FROM event_log WHERE org_id = :o ORDER BY seq"
                ),
                {"o": str(org_id)},
            )
        )
        .mappings()
        .all()
    )


@pytest.mark.asyncio
async def test_add_drug_to_branch_publishes_in_the_same_transaction(
    db, event_log_table, setup_test_data
):
    org, branch, _user, drugs, _customer = setup_test_data
    drug = drugs[0]

    inv = await InventoryService.add_drug_to_branch(
        db=db,
        branch_id=branch.id,
        drug_id=drug.id,
        organization_id=org.id,
        location="A1",
        selling_price=Decimal("5.00"),
        authored_by=AUTHOR,
    )
    assert inv.quantity == 0

    events = await _events(db, org.id)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event_type"] == "branch_inventory_created"
    assert str(ev["aggregate_id"]) == str(inv.id)
    assert ev["payload"]["quantity"] == 0
    assert ev["payload"]["branch_id"] == str(branch.id)
    assert ev["payload"]["drug_id"] == str(drug.id)
    assert ev["payload"]["selling_price"] == 5.0

    # The row and its event exist in the same committed state: the event carries
    # the inventory row's own id, which is only knowable after the insert.
    row = (
        await db.execute(
            text("SELECT id, quantity FROM branch_inventory WHERE id = :i"),
            {"i": str(inv.id)},
        )
    ).first()
    assert row is not None
    assert str(row[0]) == str(ev["aggregate_id"])


@pytest.mark.asyncio
async def test_hash_chain_is_intact(db, event_log_table, setup_test_data):
    from app.schemas.event_envelope import GENESIS_HASH

    org, branch, _user, drugs, _customer = setup_test_data

    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drugs[0].id,
        organization_id=org.id, authored_by=AUTHOR,
    )
    await InventoryService.add_drug_to_branch(
        db=db, branch_id=branch.id, drug_id=drugs[1].id,
        organization_id=org.id, authored_by=AUTHOR,
    )

    events = await _events(db, org.id)
    assert [e["seq"] for e in events] == [1, 2]
    # Genesis for the first, then chained.
    assert events[0]["hash_prev"] == GENESIS_HASH
    assert events[1]["hash_prev"] == events[0]["hash_self"]
    assert len(events[0]["hash_self"]) == 64


@pytest.mark.asyncio
async def test_failed_append_rolls_back_the_stock_change(
    db, event_log_table, setup_test_data, monkeypatch
):
    """The core guarantee: no event means no committed stock change.

    A device can only learn about stock from events. Committing the row while the
    append fails leaves the device permanently wrong with nothing logged, so the
    append must abort the whole transaction.
    """
    org, branch, _user, drugs, _customer = setup_test_data
    drug = drugs[0]

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated append failure")

    monkeypatch.setattr(StockEventEmitter, "_append_one", _boom, raising=False)
    monkeypatch.setattr(
        append_service.AppendService, "_append_one", _boom, raising=False
    )

    with pytest.raises(RuntimeError):
        try:
            await InventoryService.add_drug_to_branch(
                db=db, branch_id=branch.id, drug_id=drug.id,
                organization_id=org.id, authored_by=AUTHOR,
            )
        finally:
            # Roll back inside the guard: the session is mid-unit-of-work, and
            # unwinding it outside async context trips SQLAlchemy's greenlet check.
            await db.execute(text("ROLLBACK"))

    # No branch_inventory row survived...
    count = (
        await db.execute(
            text("SELECT count(*) FROM branch_inventory WHERE drug_id = :d"),
            {"d": str(drug.id)},
        )
    ).scalar_one()
    assert count == 0, "stock row committed despite the append failing"

    # ...and no event either.
    assert await _events(db, org.id) == []


@pytest.mark.asyncio
async def test_repeated_identical_backfill_event_is_not_duplicated(
    db, event_log_table, setup_test_data
):
    """Deterministic ids make a re-run a no-op via ALREADY_APPENDED."""
    from app.schemas.event_envelope import AggregateType
    from app.services.sync.eventlog.append_service import AppendStatus

    org, _branch, _user, _drugs, _customer = setup_test_data
    drug_id = uuid.uuid4()

    def _emit():
        return StockEventEmitter.emit_in_transaction(
            db,
            org_id=org.id,
            event_type="drug_batch_created",
            aggregate_type=AggregateType.DRUG_BATCH,
            aggregate_id=drug_id,
            payload={"drug_id": str(drug_id), "remaining_quantity": 100},
            authored_by=AUTHOR,
            event_id=deterministic_event_id("drug_batch_created", drug_id, "v1"),
        )

    first = await _emit()
    await db.commit()
    assert first.status == AppendStatus.APPENDED

    second = await _emit()
    await db.commit()
    assert second.status == AppendStatus.ALREADY_APPENDED

    events = await _events(db, org.id)
    assert len(events) == 1, events
    assert events[0]["seq"] == 1


def test_deterministic_event_id_shape():
    from app.schemas.event_envelope import ULID_LENGTH

    eid = deterministic_event_id("drug_created", "abc", "v1")
    assert len(eid) == ULID_LENGTH
    assert eid == eid.upper()
    assert eid == deterministic_event_id("drug_created", "abc", "v1")
    assert eid != deterministic_event_id("drug_created", "abc", "v2")
    assert eid != deterministic_event_id("drug_created", "abd", "v1")
    assert eid != deterministic_event_id("drug_updated", "abc", "v1")