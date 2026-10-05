"""P1: the refill counter must converge on every path.

THE DEFECT THIS LOCKS DOWN
--------------------------
`prescription_refill_used` had a projector on both sides and NO emitter anywhere,
and `SaleProjector` never touched the `prescriptions` table at all (verified on a
disposable cluster: an offline `sale_created` created the sale row and left
`refills_remaining` at 2). Meanwhile the online path decremented server-side only,
so a device's local row — which the Prescriptions page reads offline and the POS
pre-flight gates checkout on — never moved.

Measured consequences before the fix (report scenarios, reproduced here):

  S-A  online sale -> device row still `active 3/3`, sale row linked
  S-B  offline sale synced -> server `refills_remaining` unchanged at 2
  S-C  two stale devices dispense -> server accepts BOTH, counter still 2
  S-D  refills_allowed=2, offline+online+offline -> 3 dispenses, counters disagree

Requires TEST_DATABASE_URL pointing at a DISPOSABLE cluster. Never atlasdb:
tests/conftest.py now refuses a target named atlasdb, because it drops the public
schema on every PostgreSQL target.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)

from app.models.customer.customer_model import Customer  # noqa: E402
from app.models.inventory.branch_inventory import (  # noqa: E402
    BranchInventory,
    DrugBatch,
)
from app.models.inventory.inventory_model import Drug  # noqa: E402
from app.models.pharmacy.pharmacy_model import Branch, Organization  # noqa: E402
from app.models.prescriptions.prescription_model import Prescription  # noqa: E402
from app.models.user.user_model import User  # noqa: E402
from app.schemas.event_envelope import AggregateType, EventEnvelope  # noqa: E402
from app.services.sync.eventlog.projectors.prescription import (  # noqa: E402
    PrescriptionProjector,
)
from app.services.sync.eventlog.projectors.sale import SaleProjector  # noqa: E402
from app.services.sync.eventlog.refill_event_id import (  # noqa: E402
    prescription_refill_used_event_id,
)

RX = uuid.UUID("dddddddd-1111-2222-3333-444444444444")
RX2 = uuid.UUID("dddddddd-1111-2222-3333-555555555555")
SALE_A = uuid.UUID("eeeeeeee-1111-2222-3333-444444444444")
SALE_B = uuid.UUID("eeeeeeee-1111-2222-3333-555555555555")
SALE_C = uuid.UUID("eeeeeeee-1111-2222-3333-666666666666")
RX_DRUG = uuid.UUID("bbbbbbbb-1111-2222-3333-444444444444")


# ── Infrastructure ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def spine(db):
    """event_log is Alembic-owned with no ORM model; the suite builds it by DDL."""
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.execute(
        text(
            """
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
    """
        )
    )
    await db.commit()
    yield
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


@pytest_asyncio.fixture
async def world(db, setup_test_data, spine):
    """Org/branch/user/customer/drug/stock, so a sale can actually be projected."""
    # setup_test_data returns (org, branch, user, drugs, customer).
    org, branch, user, _drugs, _customer = setup_test_data
    org_id = org.id
    branch_id = branch.id

    drug = Drug(
        id=RX_DRUG,
        organization_id=org_id,
        name="Gebedol",
        sku="RX-GEB-500",
        unit_price=10,
        requires_prescription=True,
        is_active=True,
    )
    db.add(drug)
    db.add(
        BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch_id,
            drug_id=RX_DRUG,
            quantity=500,
            reserved_quantity=0,
            location="MAIN",
        )
    )
    db.add(
        DrugBatch(
            id=uuid.uuid4(),
            branch_id=branch_id,
            drug_id=RX_DRUG,
            batch_number="RX-B1",
            quantity=500,
            remaining_quantity=500,
            manufacturing_date=date.today() - timedelta(days=30),
            expiry_date=date.today() + timedelta(days=365),
            cost_price=5,
            selling_price=10,
        )
    )
    customer = Customer(
        id=uuid.uuid4(),
        organization_id=org_id,
        customer_type="registered",
        first_name="Rx",
        last_name="Patient",
        is_active=True,
    )
    db.add(customer)
    await db.commit()
    return {
        "org_id": org_id,
        "branch_id": branch_id,
        "user_id": user.id,
        "customer_id": customer.id,
    }


def make_prescription(org_id, branch_id, customer_id, *, allowed=3, remaining=3, rx_id=RX):
    return Prescription(
        id=rx_id,
        organization_id=org_id,
        branch_id=branch_id,
        prescription_number=f"RX-{rx_id.hex[:8]}",
        customer_id=customer_id,
        prescriber_name="Dr. Test",
        prescriber_license="MED-1",
        issue_date=date.today(),
        expiry_date=date.today() + timedelta(days=90),
        medications=[
            {
                "drug_id": str(RX_DRUG),
                "drug_name": "Gebedol",
                "dosage": "5mg",
                "frequency": "bd",
                "duration": "7d",
                "quantity": 10,
            }
        ],
        refills_allowed=allowed,
        refills_remaining=remaining,
        status="active",
    )


def envelope(event_type, agg_type, agg_id, payload, org_id, branch_id, author, seq_hint=1):
    return EventEnvelope(
        event_id=f"{agg_id.hex[:20]}{seq_hint:06d}".upper()[:26],
        org_id=org_id,
        seq=seq_hint,
        aggregate_id=agg_id,
        aggregate_type=agg_type,
        event_type=event_type,
        schema_version=1,
        payload=payload,
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=author,
        branch_id=branch_id,
        hash_self="ab" * 32,
        hash_prev="cd" * 32,
    )


def offline_sale_payload(org_id, branch_id, customer_id, rx_id, sale_number, *, cashier, pharmacist=None):
    return {
        "organization_id": str(org_id),
        "branch_id": str(branch_id),
        "sale_number": sale_number,
        "customer_id": str(customer_id),
        "customer_name": "Rx Patient",
        # Must be a real user: sales.cashier_id is a FK.
        "cashier_id": str(cashier),
        "pharmacist_id": str(pharmacist) if pharmacist else None,
        "payment_method": "cash",
        "payment_status": "completed",
        "total_amount": 30,
        "subtotal": 30,
        "discount_amount": 0,
        "tax_amount": 0,
        "amount_paid": 30,
        "change_amount": 0,
        "prescription_id": str(rx_id) if rx_id else None,
        "status": "completed",
        "sync_version": 2,
        "sync_protocol_version": 2,
        "items": [
            {
                # SaleProjector._apply_item reads item_id as a hard subscript
                # (sale.py:500), and the real device builder always sends one
                # (localWrite.ts:245). Shape copied from buildSaleCreatedEnvelope.
                "item_id": str(uuid.uuid4()),
                "drug_id": str(RX_DRUG),
                "batch_id": None,
                "drug_name": "Gebedol",
                "quantity": 3,
                "unit_price": 10,
                "discount_amount": 0,
                "subtotal": 30,
                "total_price": 30,
            }
        ],
    }


async def rx_row(db, rx_id):
    return (
        (
            await db.execute(
                text(
                    "SELECT status, refills_allowed, refills_remaining, "
                    "last_refill_date, verified_by, verified_at "
                    "FROM prescriptions WHERE id = :i"
                ),
                {"i": str(rx_id)},
            )
        )
        .mappings()
        .first()
    )


async def sale_count(db, rx_id):
    return (
        await db.execute(
            text("SELECT COUNT(*) FROM sales WHERE prescription_id = :i"),
            {"i": str(rx_id)},
        )
    ).scalar_one()


async def refill_events(db, org_id, rx_id):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT event_id, payload FROM event_log "
                    "WHERE org_id = :o AND aggregate_id = :a "
                    "AND event_type = 'prescription_refill_used' ORDER BY seq"
                ),
                {"o": str(org_id), "a": str(rx_id)},
            )
        )
        .mappings()
        .all()
    )
    return [{"event_id": r["event_id"], **r["payload"]} for r in rows]


# ── S-B: offline sale synced to the server ────────────────────────────────────


async def test_sb_offline_sale_sync_decrements_the_server_counter(db, world):
    """REGRESSION. Was: server refills_remaining unchanged at 2.

    An offline dispense decremented the DEVICE's copy only, so the server's
    Prescriptions page showed the prescription as untouched and the server's own
    refill guard could never fire.
    """
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=2, remaining=2))
    await db.commit()

    sp = SaleProjector()
    ev = envelope(
        "sale_created", AggregateType.SALE, SALE_A,
        offline_sale_payload(world["org_id"], world["branch_id"], world["customer_id"],
                             RX, "OFF-1", cashier=world["user_id"]),
        world["org_id"], world["branch_id"], world["user_id"],
    )
    await sp.apply(ev, db)
    await db.commit()

    row = await rx_row(db, RX)
    assert await sale_count(db, RX) == 1, "the sale itself must always be recorded"
    assert row["refills_remaining"] == 1, "server counter must follow the dispense"
    assert row["status"] == "active"
    assert row["last_refill_date"] == date.today()


async def test_sb_emits_the_refill_event_with_absolute_state(db, world):
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=2, remaining=2))
    await db.commit()
    sp = SaleProjector()
    await sp.apply(
        envelope("sale_created", AggregateType.SALE, SALE_A,
                 offline_sale_payload(world["org_id"], world["branch_id"],
                                      world["customer_id"], RX, "OFF-1",
                                      cashier=world["user_id"]),
                 world["org_id"], world["branch_id"], world["user_id"]),
        db,
    )
    await db.commit()

    evs = await refill_events(db, world["org_id"], RX)
    assert len(evs) == 1
    e = evs[0]
    assert e["sale_id"] == str(SALE_A)
    assert e["prescription_id"] == str(RX)
    # Absolute post-state so a device converges instead of applying a delta.
    assert e["refills_remaining"] == 1
    assert e["refills_allowed"] == 2
    assert e["status"] == "active"
    assert e["source"] == "offline_sale_sync"
    assert e["over_dispensed"] is False
    assert e["event_id"] == prescription_refill_used_event_id(RX, SALE_A)


# ── S-C: two stale devices, same prescription ─────────────────────────────────


class _CollectingHandler(logging.Handler):
    """Captures records from ONE logger, independent of propagation."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def capture_emitting_logger(name: str):
    """Attach a handler to `name` itself and yield the records it collects.

    Not `caplog`. `caplog` installs its handler on the ROOT logger, but
    `app.core.logging_config.get_logging_config` configures the `app` logger with
    `propagate: False` (logging_config.py:63-75). Any test that imports `main` —
    test_cors_preflight.py does — therefore makes every `app.*` record invisible
    to `caplog`, so the assertion passes in isolation and fails in a full-suite
    run. Same defect, same fix as 27ff5c5.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        log = logging.getLogger(name)
        handler = _CollectingHandler()
        log.addHandler(handler)
        try:
            yield handler.records
        finally:
            log.removeHandler(handler)

    return _ctx()


async def test_sc_over_dispense_is_recorded_and_clamps(db, world):
    """REGRESSION. Was: both dispenses accepted silently, counter still 2.

    Now the second dispense clamps at 0 and the over-dispense is recorded three
    ways: a structured ERROR log, `over_dispensed` in the event payload, and the
    pre-state in `status_before`/`refills_before`.
    """
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=1, remaining=1))
    await db.commit()
    sp = SaleProjector()

    # sale.py:440 logs the over-dispense on this exact logger name.
    sale_logger = "app.services.sync.eventlog.projectors.sale"

    with capture_emitting_logger(sale_logger) as captured:
        for n, sale_id in enumerate((SALE_A, SALE_B), start=1):
            await sp.apply(
                envelope("sale_created", AggregateType.SALE, sale_id,
                         offline_sale_payload(world["org_id"], world["branch_id"],
                                              world["customer_id"], RX, f"STALE-{n}",
                                              cashier=world["user_id"]),
                         world["org_id"], world["branch_id"], world["user_id"], seq_hint=n),
                db,
            )
            await db.commit()

    row = await rx_row(db, RX)
    # Both sales exist: the medicine left the shelf twice.
    assert await sale_count(db, RX) == 2
    # Clamped at 0, never negative — check_refills_remaining holds.
    assert row["refills_remaining"] == 0
    assert row["status"] == "filled"

    evs = await refill_events(db, world["org_id"], RX)
    assert len(evs) == 2, "one event per dispense, even the over-dispensed one"
    assert evs[0]["over_dispensed"] is False
    assert evs[1]["over_dispensed"] is True, "second dispense must be flagged"
    assert evs[1]["refills_before"] == 0
    assert evs[1]["refills_remaining"] == 0
    assert evs[1]["status_before"] == "filled"

    assert any("OVER-DISPENSE" in r.getMessage() for r in captured), (
        "the over-dispense must reach the log stream. The projector logs it at "
        f"ERROR on {sale_logger}"
    )


async def test_sale_referencing_an_unknown_prescription_is_blocked_by_the_fk(db, world):
    """Documents WHY the "prescription row missing" branch is defence-in-depth.

    Written after assuming the SaleProjector had to tolerate a missing
    prescription row. It does not have to: `sales.prescription_id` is a FK to
    `prescriptions.id` (sales_model.py:181-185), so the sale INSERT itself is
    rejected before _apply_prescription_refill can run.

    That is the right protection, and it is why a device that created a
    prescription offline MUST declare the matching prescription_created event as a
    dependency — otherwise its own sale is rejected on sync. The router's
    dependency check parks the event instead (router.py:_check_dependencies).

    So the guard in _apply_prescription_refill stays as insurance for a future
    where that FK is relaxed, but it is not load-bearing today and this test
    pins the actual mechanism rather than the imagined one.
    """
    from sqlalchemy.exc import IntegrityError

    sp = SaleProjector()
    ev = envelope(
        "sale_created", AggregateType.SALE, SALE_C,
        offline_sale_payload(world["org_id"], world["branch_id"], world["customer_id"],
                             RX, "GHOST", cashier=world["user_id"]),
        world["org_id"], world["branch_id"], world["user_id"],
    )
    with pytest.raises(IntegrityError):
        await sp.apply(ev, db)
    await db.rollback()

    assert await sale_count(db, RX) == 0, "no partial sale may survive the rejection"


# ── S-D: mixed online/offline convergence ─────────────────────────────────────


async def test_sd_three_dispenses_converge_with_no_double_decrement(db, world):
    """REGRESSION. Was: 3 dispenses, device 0/2 and server 1/2 disagreeing.

    Three offline-syncs, each a distinct sale, must land the server on 0 with
    exactly three refill events — no fourth from a replay.
    """
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=2, remaining=2))
    await db.commit()
    sp = SaleProjector()
    for n, sale_id in enumerate((SALE_A, SALE_B, SALE_C), start=1):
        await sp.apply(
            envelope("sale_created", AggregateType.SALE, sale_id,
                     offline_sale_payload(world["org_id"], world["branch_id"],
                                          world["customer_id"], RX, f"MIX-{n}",
                                          cashier=world["user_id"]),
                     world["org_id"], world["branch_id"], world["user_id"], seq_hint=n),
            db,
        )
        await db.commit()

    row = await rx_row(db, RX)
    assert row["refills_remaining"] == 0, "2 allowed, 3 dispensed, clamped at 0"
    assert row["status"] == "filled"
    assert len(await refill_events(db, world["org_id"], RX)) == 3


async def test_sd_replaying_a_sale_changes_nothing(db, world):
    """The idempotency gate is the sale INSERT's RETURNING, not the event id.

    So this replays the SAME envelope object, exactly as a device re-push would.
    """
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=3, remaining=3))
    await db.commit()
    sp = SaleProjector()
    ev = envelope("sale_created", AggregateType.SALE, SALE_A,
                  offline_sale_payload(world["org_id"], world["branch_id"],
                                       world["customer_id"], RX, "REPLAY",
                                       cashier=world["user_id"]),
                  world["org_id"], world["branch_id"], world["user_id"])
    await sp.apply(ev, db)
    await db.commit()
    assert (await rx_row(db, RX))["refills_remaining"] == 2
    assert len(await refill_events(db, world["org_id"], RX)) == 1

    for _ in range(3):
        await sp.apply(ev, db)
        await db.commit()

    assert (await rx_row(db, RX))["refills_remaining"] == 2, "must not drift"
    assert len(await refill_events(db, world["org_id"], RX)) == 1, "no new event"


# ── Cross-path: a device applying the emitted event ──────────────────────────


async def test_emitted_event_carries_what_the_device_projector_needs(db, world):
    """The device projector needs prescription_id + sale_id at minimum.

    Asserted here so a payload rename cannot silently break the device half; the
    device half asserts it consumes the same keys.
    """
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=2, remaining=2))
    await db.commit()
    await SaleProjector().apply(
        envelope("sale_created", AggregateType.SALE, SALE_A,
                 offline_sale_payload(world["org_id"], world["branch_id"],
                                      world["customer_id"], RX, "P1",
                                      cashier=world["user_id"]),
                 world["org_id"], world["branch_id"], world["user_id"]),
        db,
    )
    await db.commit()

    e = (await refill_events(db, world["org_id"], RX))[0]
    for key in (
        "prescription_id", "sale_id", "refills_remaining", "status",
        "last_refill_date", "verified_at", "source", "over_dispensed",
    ):
        assert key in e, f"payload key {key!r} missing"


async def test_prescription_projector_still_consumes_refill_used(db, world):
    """The existing PrescriptionProjector path must keep working unchanged.

    It is what applies the event on the SERVER for a device-authored refill, and
    what applies it on a DEVICE that did not originate the sale.
    """
    db.add(make_prescription(world["org_id"], world["branch_id"], world["customer_id"],
                             allowed=3, remaining=3))
    await db.commit()
    pp = PrescriptionProjector()
    ev = envelope("prescription_refill_used", AggregateType.PRESCRIPTION, RX,
                  {"refills_remaining": 2, "refill_date": date.today().isoformat()},
                  world["org_id"], world["branch_id"], world["user_id"])
    result = await pp.validate(ev, db)
    await pp.apply(ev, db)
    await db.commit()
    assert (await rx_row(db, RX))["refills_remaining"] == 2
