"""Loyalty convergence: online sale, offline sale, refund, and the tier invariant.

Every path that can move `customers.loyalty_points` on the server goes through
`apply_loyalty_award`, so these tests pin the three callers (process_sale,
refund_sale, SaleProjector) plus the projector that carries the value to devices.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.pricing.pricing_model import PriceContract
from app.models.user.user_model import Role, User, UserRole
from app.schemas.event_envelope import AggregateType, EventEnvelope
from app.schemas.sales_schemas import RefundSaleRequest, SaleCreate
from app.services.sales.sales_service import SalesService
from app.services.sales.utils.loyalty import apply_loyalty_award, points_for_amount
from app.services.sales.utils.sale_helpers import resolve_loyalty_tier
from app.services.sync.eventlog.loyalty_event_id import (
    customer_loyalty_changed_event_id,
)
from app.services.sync.eventlog.projectors.customer import CustomerProjector
from app.services.sync.eventlog.projectors.sale import SaleProjector

ORG = uuid.UUID("2d060ef8-a302-447c-91f4-b2fd30268341")
OTHER_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("44444444-4444-4444-4444-444444444444")
BRANCH = uuid.UUID("72b2433d-120b-42a2-918b-e6dfcf176b1a")
DRUG = uuid.UUID("99999999-9999-9999-9999-999999999999")
JOE = uuid.UUID("5823ef27-51ce-4431-9adc-e81f9b3f949f")
MIKE = uuid.UUID("0615fb0e-416f-4ecb-bca1-d4839446cb36")

BI = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
BATCH = uuid.UUID("bbbbbbbb-1111-2222-3333-999999999999")
CONTRACT = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
ROLE = uuid.UUID("55555555-5555-5555-5555-555555555555")

LOYALTY_ON = {
    "enable_loyalty_program": True,
    "loyalty": {"points_per_unit": "1.0"},
}


@pytest.fixture
async def seeded(db):
    db.add(Organization(id=ORG, name="Default Pharmacy", type="pharmacy",
                        license_number="L-REAL", settings=LOYALTY_ON, is_active=True))
    db.add(Branch(id=BRANCH, organization_id=ORG, name="Apomuden", code="APO1",
                  is_active=True, is_deleted=False))
    db.add(User(id=USER, organization_id=ORG, username="u", email="u@x.test",
                password_hash="x", full_name="U", is_active=True,
                assigned_branches=[BRANCH]))
    # refund_sale checks Permission.PROCESS_REFUNDS via user.roles.
    db.add(Role(id=ROLE, organization_id=ORG, name="manager", description="test",
                level=20, permissions=["process_refunds"]))
    await db.flush()   # user_roles.role_id FK -> roles
    db.add(UserRole(user_id=USER, role_id=ROLE))
    db.add(Drug(id=DRUG, organization_id=ORG, name="Paracetamol", sku="PAR500",
                unit_price=30, requires_prescription=False, is_active=True))
    db.add(BranchInventory(id=BI, branch_id=BRANCH, drug_id=DRUG, quantity=900,
                           reserved_quantity=0, location="M"))
    db.add(DrugBatch(id=BATCH, branch_id=BRANCH, drug_id=DRUG, batch_number="B1",
                     quantity=900, remaining_quantity=900,
                     manufacturing_date=date.today() - timedelta(days=30),
                     expiry_date=date.today() + timedelta(days=300),
                     cost_price=5, selling_price=30))
    await db.flush()
    db.add(PriceContract(
        id=CONTRACT, organization_id=ORG, contract_code="STANDARD",
        contract_name="STANDARD", contract_type="standard", status="active",
        is_default_contract=True, is_active=True, discount_type="percentage",
        discount_percentage=0, applies_to_prescription_only=False, applies_to_otc=True,
        effective_from=date.today(), applies_to_all_branches=True,
        applicable_branch_ids=[], excluded_drug_categories=[], excluded_drug_ids=[],
        allowed_user_roles=[], created_by=USER, is_deleted=False))
    for cid, name, ctype, points in ((JOE, "Joe", "registered", 0),
                                     (MIKE, "Mike", "registered", 0)):
        db.add(Customer(id=cid, organization_id=ORG, customer_type=ctype,
                        first_name=name, last_name="T", loyalty_points=points,
                        loyalty_tier="bronze", total_orders=0, total_value=0.0,
                        preferred_contact_method="email", marketing_consent=False,
                        is_active=True, is_deleted=False, sync_status="synced"))
    await db.commit()
    return db


async def get_user(db):
    """refund_sale walks user.roles, which lazy-loads; async needs it eager."""
    return (
        await db.execute(
            select(User).options(selectinload(User.roles)).where(User.id == USER)
        )
    ).scalar_one()


def sale_create(customer_id, amount=30.0):
    return SaleCreate(
        organization_id=str(ORG), branch_id=str(BRANCH), sale_number="IGNORED",
        customer_id=str(customer_id), customer_name="Joe T",
        items=[{"drug_id": str(DRUG), "quantity": 1, "unit_price": amount,
                "discount_amount": 0, "subtotal": amount, "total_price": amount}],
        payment_method="cash", payment_status="completed", subtotal=amount,
        discount_amount=0, tax_amount=0, total_amount=amount, amount_paid=amount,
        change_amount=0, status="completed",
    )


async def points_of(db, cid):
    row = (await db.execute(
        text("SELECT loyalty_points, loyalty_tier FROM customers WHERE id=:i"),
        {"i": str(cid)})).first()
    return row[0], row[1]


async def loyalty_events(db, cid):
    rows = (await db.execute(
        text("SELECT event_id, event_type, payload FROM event_log "
             "WHERE aggregate_id = :i AND event_type = 'customer_loyalty_changed' "
             "ORDER BY seq"),
        {"i": str(cid)})).all()
    import json

    def _load(raw):
        # payload is JSONB on some schemas and text on others.
        return raw if isinstance(raw, dict) else json.loads(raw)

    return [(r[0], _load(r[2])) for r in rows]


def envelope(etype, agg_type, agg_id, payload, org, seq, authored_by=USER):
    return EventEnvelope(
        event_id=f"t{seq:05d}".ljust(26, "0"), aggregate_type=agg_type,
        aggregate_id=str(agg_id), event_type=etype, schema_version=1,
        payload=payload, dependencies=[], authored_at="2026-09-21T07:13:23Z",
        authored_by=str(authored_by), branch_id=str(BRANCH), org_id=str(org),
        hash_self="a" * 64, hash_prev="0" * 64, seq=seq,
    )


def offline_sale_payload(sale_id, customer_id, amount=30.0, walk_in=False):
    return {
        "organization_id": str(ORG), "branch_id": str(BRANCH),
        "sale_number": f"OFFLINE-{sale_id[:8]}",
        "customer_id": None if walk_in else str(customer_id),
        "customer_name": "Joe", "cashier_id": str(USER), "pharmacist_id": None,
        "payment_method": "cash", "payment_status": "completed",
        "total_amount": amount, "subtotal": amount, "discount_amount": 0,
        "tax_amount": 0, "amount_paid": amount, "change_amount": 0,
        "prescription_id": None, "status": "completed", "sync_protocol_version": 1,
        "items": [{"item_id": str(uuid.uuid4()), "drug_id": str(DRUG), "quantity": 1,
                   "drug_name": "Paracetamol", "unit_price": amount,
                   "total_price": amount, "batch_id": None,
                   "discount_amount": 0, "subtotal": amount}],
    }


# ── the shared formula ───────────────────────────────────────────────────────

class _Org:
    settings = LOYALTY_ON


class _Cust:
    def __init__(self, points=0, tier="bronze", orders=0, value=0.0):
        self.loyalty_points, self.loyalty_tier = points, tier
        self.total_orders, self.total_value = orders, value


@pytest.mark.asyncio
async def test_points_for_amount_uses_the_org_rate():
    assert points_for_amount(30.0, _Org()) == 30
    assert points_for_amount(30.0, _Org()) == int(Decimal("30") * Decimal("1.0"))


@pytest.mark.asyncio
async def test_award_keeps_the_tier_invariant():
    c = _Cust(points=90)
    award = await apply_loyalty_award(None, c, _Org(), sale_id="s", direction="earn",
                                      total_amount=30.0)
    assert c.loyalty_points == 120
    assert c.loyalty_tier == resolve_loyalty_tier(120) == "silver"
    assert award.tier_changed is True


@pytest.mark.asyncio
async def test_award_is_a_no_op_when_loyalty_is_disabled():
    class Off:
        settings = {"enable_loyalty_program": False}

    c = _Cust()
    assert await apply_loyalty_award(None, c, Off(), sale_id="s", direction="earn",
                                     total_amount=30.0) is None
    assert c.loyalty_points == 0


@pytest.mark.asyncio
async def test_award_never_moves_a_walk_in():
    """Walk-ins have nobody to credit; the row is a till artefact."""
    c = _Cust()
    # There is no customer_type on the shim, so exercise the guard the projector
    # uses and the service uses (customer is None).
    assert await apply_loyalty_award(None, None, _Org(), sale_id="s",
                                     direction="earn", total_amount=30.0) is None


# ── online sale ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_online_sale_awards_and_publishes(seeded):
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_create(JOE, 30.0), u)
    await seeded.commit()

    pts, tier = await points_of(seeded, JOE)
    assert (pts, tier) == (30, "bronze")

    events = await loyalty_events(seeded, JOE)
    assert len(events) == 1
    eid, payload = events[0]
    assert payload["loyalty_points"] == 30
    assert payload["loyalty_tier"] == "bronze"
    assert payload["direction"] == "earn"
    assert payload["source"] == "online_sale"
    assert eid == customer_loyalty_changed_event_id(JOE, payload["sale_id"], "earn")


@pytest.mark.asyncio
async def test_online_sale_leaves_the_customer_row_synced(seeded):
    """Not 'pending with nothing behind it' - there is now an event behind it."""
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_create(JOE, 30.0), u)
    await seeded.commit()
    row = (await seeded.execute(
        text("SELECT sync_status FROM customers WHERE id=:i"), {"i": str(JOE)})).first()
    assert row[0] == "synced"


# ── offline-synced sale ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_offline_sale_awards_once_and_publishes(seeded):
    sale_id = str(uuid.uuid4())
    sp = SaleProjector()
    ev = envelope("sale_created", AggregateType.SALE, sale_id,
                  offline_sale_payload(sale_id, JOE, 30.0), ORG, 500)
    await sp.apply(ev, seeded)
    await seeded.commit()

    pts, tier = await points_of(seeded, JOE)
    assert (pts, tier) == (30, "bronze")
    counters = (await seeded.execute(
        text("SELECT total_orders, total_value FROM customers WHERE id=:i"),
        {"i": str(JOE)})).first()
    assert counters[0] == 1
    assert float(counters[1]) == 30.0

    events = await loyalty_events(seeded, JOE)
    assert len(events) == 1
    assert events[0][1]["source"] == "offline_sale"


@pytest.mark.asyncio
async def test_offline_sale_replay_awards_nothing_more(seeded):
    """Re-projecting the same sale must change nothing."""
    sale_id = str(uuid.uuid4())
    sp = SaleProjector()
    ev = envelope("sale_created", AggregateType.SALE, sale_id,
                  offline_sale_payload(sale_id, JOE, 30.0), ORG, 501)
    await sp.apply(ev, seeded)
    await sp.apply(ev, seeded)
    await seeded.commit()

    pts, _ = await points_of(seeded, JOE)
    assert pts == 30, "a replayed sale must not award a second time"
    assert len(await loyalty_events(seeded, JOE)) == 1


@pytest.mark.asyncio
async def test_offline_walk_in_sale_earns_nothing(seeded):
    sale_id = str(uuid.uuid4())
    sp = SaleProjector()
    await sp.apply(envelope("sale_created", AggregateType.SALE, sale_id,
                            offline_sale_payload(sale_id, JOE, 30.0, walk_in=True),
                            ORG, 502), seeded)
    await seeded.commit()
    pts, _ = await points_of(seeded, JOE)
    assert pts == 0
    assert await loyalty_events(seeded, JOE) == []


@pytest.mark.asyncio
async def test_offline_award_matches_the_online_formula(seeded):
    """Same helper, so the two paths cannot drift."""
    # process_sale resolves its price from BranchInventory.selling_price ->
    # DrugBatch.selling_price -> Drug.unit_price, NOT from the payload, so the
    # batch price is what both paths must agree on.
    await seeded.execute(
        text("UPDATE drug_batches SET selling_price = 47 WHERE id = :i"),
        {"i": str(BATCH)})
    await seeded.commit()
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_create(JOE, 47.0), u)
    await seeded.commit()
    online_pts, online_tier = await points_of(seeded, JOE)

    sale_id = str(uuid.uuid4())
    await SaleProjector().apply(
        envelope("sale_created", AggregateType.SALE, sale_id,
                 offline_sale_payload(sale_id, MIKE, 47.0), ORG, 503), seeded)
    await seeded.commit()
    offline_pts, offline_tier = await points_of(seeded, MIKE)
    assert (offline_pts, offline_tier) == (online_pts, online_tier)


# ── refund ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refund_restores_balance_and_publishes_the_opposite_direction(seeded):
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_create(JOE, 30.0), u)
    await seeded.commit()
    sale_row = (await seeded.execute(
        text("SELECT id FROM sales WHERE customer_id=:c"), {"c": str(JOE)})).first()
    sale_id = sale_row[0]
    item = (await seeded.execute(
        text("SELECT id, quantity, total_price FROM sale_items WHERE sale_id=:s LIMIT 1"),
        {"s": str(sale_id)})).first()

    await SalesService.refund_sale(seeded, sale_id, RefundSaleRequest(
        sale_id=str(sale_id), reason="loyalty regression refund",
        items_to_refund=[{"sale_item_id": str(item[0]), "quantity": int(item[1]),
                          "reason": "full refund", "restock": False}],
        refund_amount=float(item[2]), manager_approval_user_id=str(USER)), u)
    await seeded.commit()

    pts, tier = await points_of(seeded, JOE)
    assert (pts, tier) == (0, "bronze")

    events = await loyalty_events(seeded, JOE)
    assert [p["direction"] for _, p in events] == ["earn", "refund"]
    # The two ids must differ, or the reversal would be swallowed as a duplicate.
    assert events[0][0] != events[1][0]
    assert events[1][1]["loyalty_points"] == 0


@pytest.mark.asyncio
async def test_refund_does_not_reverse_order_counters(seeded):
    """Documented current behaviour. Reported, deliberately NOT changed."""
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_create(JOE, 30.0), u)
    await seeded.commit()
    sale_id = (await seeded.execute(
        text("SELECT id FROM sales WHERE customer_id=:c"), {"c": str(JOE)})).first()[0]
    item = (await seeded.execute(
        text("SELECT id, quantity, total_price FROM sale_items WHERE sale_id=:s LIMIT 1"),
        {"s": str(sale_id)})).first()

    await SalesService.refund_sale(seeded, sale_id, RefundSaleRequest(
        sale_id=str(sale_id), reason="loyalty regression refund",
        items_to_refund=[{"sale_item_id": str(item[0]), "quantity": int(item[1]),
                          "reason": "full refund", "restock": False}],
        refund_amount=float(item[2]), manager_approval_user_id=str(USER)), u)
    await seeded.commit()

    counters = (await seeded.execute(
        text("SELECT loyalty_points, total_orders, total_value FROM customers WHERE id=:i"),
        {"i": str(JOE)})).first()
    assert counters[0] == 0, "points are reversed"
    assert counters[1] == 1, "total_orders is NOT reversed - documented gap"
    assert float(counters[2]) == 30.0, "total_value is NOT reversed - documented gap"


# ── projector validation ─────────────────────────────────────────────────────

def _loyalty_env(**over):
    payload = {"customer_id": str(JOE), "organization_id": str(ORG),
               "loyalty_points": 125, "loyalty_tier": "silver",
               "sale_id": "S1", "direction": "earn"}
    payload.update(over)
    return envelope("customer_loyalty_changed", AggregateType.CUSTOMER, JOE,
                    payload, ORG, 600)


@pytest.mark.asyncio
async def test_projector_accepts_a_consistent_absolute_state(seeded):
    r = await CustomerProjector().validate(_loyalty_env(), seeded)
    assert r.status.value == "ok"


@pytest.mark.asyncio
async def test_projector_rejects_a_tier_that_contradicts_the_points(seeded):
    r = await CustomerProjector().validate(
        _loyalty_env(loyalty_points=10, loyalty_tier="silver"), seeded)
    assert r.status.value == "rejected_permanent"
    assert r.error_code == "loyalty_tier_inconsistent"


@pytest.mark.asyncio
async def test_projector_rejects_a_cross_org_loyalty_event(seeded):
    r = await CustomerProjector().validate(
        _loyalty_env(organization_id=str(OTHER_ORG)), seeded)
    assert r.error_code == "org_scope_violation"


@pytest.mark.asyncio
async def test_projector_rejects_negative_points_and_bad_direction(seeded):
    assert (await CustomerProjector().validate(
        _loyalty_env(loyalty_points=-5, loyalty_tier="bronze"), seeded)).error_code \
        == "invalid_loyalty_points"
    assert (await CustomerProjector().validate(
        _loyalty_env(direction="adjust"), seeded)).error_code == "invalid_direction"


@pytest.mark.asyncio
async def test_apply_writes_only_the_loyalty_columns(seeded):
    """It must never touch the denormalized order counters or the name."""
    await CustomerProjector().apply(_loyalty_env(), seeded)
    await seeded.commit()
    row = (await seeded.execute(
        text("SELECT loyalty_points, loyalty_tier, first_name, total_orders, "
             "total_value FROM customers WHERE id=:i"), {"i": str(JOE)})).first()
    assert row[0] == 125
    assert row[1] == "silver"
    assert row[2] == "Joe", "the name must be untouched"
    assert row[3] == 0, "order counters must be untouched"
    assert float(row[4]) == 0.0


@pytest.mark.asyncio
async def test_apply_is_idempotent(seeded):
    cp = CustomerProjector()
    await cp.apply(_loyalty_env(), seeded)
    await cp.apply(_loyalty_env(), seeded)
    await seeded.commit()
    pts, _ = await points_of(seeded, JOE)
    assert pts == 125


@pytest.mark.asyncio
async def test_apply_never_creates_a_missing_customer(seeded):
    absent = uuid.uuid4()
    ev = _loyalty_env(customer_id=str(absent))
    ev = EventEnvelope(**{**ev.model_dump(), "aggregate_id": str(absent)})
    await CustomerProjector().apply(ev, seeded)
    await seeded.commit()
    assert await seeded.get(Customer, absent) is None
