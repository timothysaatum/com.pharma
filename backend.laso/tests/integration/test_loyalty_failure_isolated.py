"""A loyalty failure must never fail a sale.

These are the inverse of `test_loyalty_append_failure.py`, which proved the
unguarded behaviour: an append failure escaped the outer savepoint at
sales_service.py:379, unwound the whole sale, and returned 500.

With the append savepointed on its own (step 19a online, step 7b on refund),
a failed append must now:

  * keep the sale, its line items and the stock deduction
  * keep the points and the denormalized counters
  * write NO customer_loyalty_changed for that change
  * still return 201, not 500

and the next loyalty event must carry the correct ABSOLUTE balance, which is
what makes the missed publish bounded rather than cumulative.
"""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.pricing.pricing_model import PriceContract
from app.models.user.user_model import Role, User, UserRole
from app.schemas.sales_schemas import RefundSaleRequest, SaleCreate
from app.services.sales.sales_service import SalesService
from app.services.sync.eventlog.stock_emitter import StockEventEmitter

ORG = uuid.UUID("2d060ef8-a302-447c-91f4-b2fd30268341")
USER = uuid.UUID("44444444-4444-4444-4444-444444444444")
ROLE = uuid.UUID("55555555-5555-5555-5555-555555555555")
BRANCH = uuid.UUID("72b2433d-120b-42a2-918b-e6dfcf176b1a")
DRUG = uuid.UUID("99999999-9999-9999-9999-999999999999")
JOE = uuid.UUID("5823ef27-51ce-4431-9adc-e81f9b3f949f")
MIKE = uuid.UUID("0615fb0e-416f-4ecb-bca1-d4839446cb36")
BI = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
BATCH = uuid.UUID("bbbbbbbb-1111-2222-3333-999999999999")
CONTRACT = uuid.UUID("cccccccc-0000-0000-0000-000000000001")

LOYALTY_ON = {"enable_loyalty_program": True, "loyalty": {"points_per_unit": "1.0"}}


class AppendFailed(RuntimeError):
    pass


def break_loyalty_append(monkeypatch):
    """Fail ONLY the loyalty append. sale_created still appends normally."""
    real = StockEventEmitter.emit_in_transaction.__func__
    calls: list[str] = []

    async def fake(cls, db, **kw):
        calls.append(kw["event_type"])
        if kw["event_type"] == "customer_loyalty_changed":
            raise AppendFailed("simulated loyalty append failure")
        return await real(cls, db, **kw)

    monkeypatch.setattr(StockEventEmitter, "emit_in_transaction", classmethod(fake))
    return calls


@pytest.fixture
async def seeded(db):
    db.add(Organization(id=ORG, name="Default Pharmacy", type="pharmacy",
                        license_number="L-REAL", settings=LOYALTY_ON, is_active=True))
    db.add(Branch(id=BRANCH, organization_id=ORG, name="Apomuden", code="APO1",
                  is_active=True, is_deleted=False))
    db.add(User(id=USER, organization_id=ORG, username="u", email="u@x.test",
                password_hash="x", full_name="U", is_active=True,
                assigned_branches=[BRANCH]))
    db.add(Role(id=ROLE, organization_id=ORG, name="admin", description="",
                level=30, permissions=["*"]))
    await db.flush()
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
    for cid, name in ((JOE, "Joe"), (MIKE, "Mike")):
        db.add(Customer(id=cid, organization_id=ORG, customer_type="registered",
                        first_name=name, last_name="T", loyalty_points=0,
                        loyalty_tier="bronze", total_orders=0, total_value=0.0,
                        preferred_contact_method="email", marketing_consent=False,
                        is_active=True, is_deleted=False, sync_status="synced"))
    await db.commit()
    return db


async def get_user(db):
    return (await db.execute(
        select(User).options(selectinload(User.roles)).where(User.id == USER)
    )).scalar_one()


def sale_data(amount=30.0, customer_id=JOE):
    return SaleCreate(
        organization_id=str(ORG), branch_id=str(BRANCH), sale_number="IGNORED",
        customer_id=str(customer_id), customer_name="X",
        items=[{"drug_id": str(DRUG), "quantity": 1, "unit_price": amount,
                "discount_amount": 0, "subtotal": amount, "total_price": amount}],
        payment_method="cash", payment_status="completed", subtotal=amount,
        discount_amount=0, tax_amount=0, total_amount=amount, amount_paid=amount,
        change_amount=0, status="completed",
    )


async def state(db):
    c = (await db.execute(
        text("SELECT loyalty_points, loyalty_tier, total_orders, total_value "
             "FROM customers WHERE id=:i"), {"i": str(JOE)})).first()
    sales = (await db.execute(
        text("SELECT COUNT(*) FROM sales WHERE customer_id=:c"), {"c": str(JOE)})).scalar()
    items = (await db.execute(text("SELECT COUNT(*) FROM sale_items"))).scalar()
    inv = (await db.execute(
        text("SELECT quantity FROM branch_inventory WHERE id=:i"), {"i": str(BI)})).first()
    batch = (await db.execute(
        text("SELECT remaining_quantity FROM drug_batches WHERE id=:i"),
        {"i": str(BATCH)})).first()
    loyalty = (await db.execute(
        text("SELECT COUNT(*) FROM event_log WHERE event_type="
             "'customer_loyalty_changed' AND aggregate_id=:c"),
        {"c": str(JOE)})).scalar()
    return {"points": c[0], "tier": c[1], "orders": c[2], "value": float(c[3]),
            "sales": sales, "sale_items": items, "inventory": inv[0],
            "batch": batch[0], "loyalty_events": loyalty}


# ── online sale ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sale_survives_a_failed_loyalty_append(seeded, monkeypatch):
    calls = break_loyalty_append(monkeypatch)
    u = await get_user(seeded)

    resp = await SalesService.process_sale(seeded, sale_data(30.0), u)
    await seeded.commit()

    s = await state(seeded)
    assert resp.success is True
    assert s["sales"] == 1, "the sale is kept"
    assert s["sale_items"] == 1, "line items are kept"
    assert s["inventory"] == 899, "stock was deducted"
    assert s["batch"] == 899, "batch quantity moved"
    assert s["points"] == 30, "the points are kept"
    assert s["orders"] == 1, "the denormalized counter is kept"
    assert float(s["value"]) == 30.0
    assert s["loyalty_events"] == 0, "but no loyalty event was published"
    assert "customer_loyalty_changed" in calls
    assert "sale_created" in calls


@pytest.mark.asyncio
async def test_sale_endpoint_returns_201_not_500(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    import os

    import main
    from app.api.v1.endpoints import sales_endpoints
    from app.db.dependencies import get_db

    u = await get_user(seeded)
    break_loyalty_append(monkeypatch)

    payload = {
        "organization_id": str(ORG), "branch_id": str(BRANCH),
        "customer_id": str(JOE), "customer_name": "X",
        "items": [{"drug_id": str(DRUG), "quantity": 1, "unit_price": 30.0,
                   "discount_amount": 0, "subtotal": 30.0, "total_price": 30.0}],
        "payment_method": "cash", "payment_status": "completed", "subtotal": 30.0,
        "discount_amount": 0, "tax_amount": 0, "total_amount": 30.0,
        "amount_paid": 30.0, "change_amount": 0, "status": "completed",
    }

    async def scoped_session():
        eng = create_async_engine(os.environ["DATABASE_URL"])
        async with AsyncSession(eng, expire_on_commit=False) as s:
            yield s
        await eng.dispose()

    app = main.app
    app.dependency_overrides[sales_endpoints.get_current_user] = lambda: u
    app.dependency_overrides[get_db] = scoped_session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/sales/", json=payload)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text[:300]
    await seeded.rollback()
    s = await state(seeded)
    assert s["sales"] == 1, "the sale survived"
    assert s["points"] == 30


@pytest.mark.asyncio
async def test_the_next_sale_republishes_the_correct_absolute_balance(
    seeded, monkeypatch
):
    """A missed publish is bounded, not cumulative: the next event carries the
    ABSOLUTE balance, so the device jumps to the right number."""
    u = await get_user(seeded)

    break_loyalty_append(monkeypatch)
    await SalesService.process_sale(seeded, sale_data(30.0), u)
    await seeded.commit()
    assert (await state(seeded))["loyalty_events"] == 0

    # Loyalty healthy again for the next sale.
    monkeypatch.undo()
    await SalesService.process_sale(seeded, sale_data(30.0), u)
    await seeded.commit()

    s = await state(seeded)
    assert s["points"] == 60, "both sales counted on the server"
    assert s["loyalty_events"] == 1

    row = (await seeded.execute(
        text("SELECT payload FROM event_log WHERE event_type="
             "'customer_loyalty_changed' AND aggregate_id=:c"),
        {"c": str(JOE)})).first()
    payload = row[0] if isinstance(row[0], dict) else __import__("json").loads(row[0])
    assert payload["loyalty_points"] == 60, (
        "the published balance is absolute and includes the missed 30, so a "
        "device converges instead of drifting"
    )


# ── refund ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refund_survives_a_failed_loyalty_append(seeded, monkeypatch):
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_data(30.0), u)
    await seeded.commit()

    sale_id = (await seeded.execute(
        text("SELECT id FROM sales WHERE customer_id=:c"), {"c": str(JOE)})).first()[0]
    item = (await seeded.execute(
        text("SELECT id, quantity, total_price FROM sale_items WHERE sale_id=:s LIMIT 1"),
        {"s": str(sale_id)})).first()

    break_loyalty_append(monkeypatch)
    resp = await SalesService.refund_sale(seeded, sale_id, RefundSaleRequest(
        sale_id=str(sale_id), reason="loyalty append failure probe",
        items_to_refund=[{"sale_item_id": str(item[0]), "quantity": int(item[1]),
                          "reason": "full refund", "restock": False}],
        refund_amount=float(item[2]), manager_approval_user_id=str(USER)), u)
    await seeded.commit()

    s = await state(seeded)
    assert resp.success is True
    assert s["points"] == 0, "the deduction is kept"
    assert s["loyalty_events"] == 1, "only the original earn is in the log"
    assert s["inventory"] == 899, "restock was skipped, as the request asked"

    sale = (await seeded.execute(
        text("SELECT refund_amount, refunded_at FROM sales WHERE id=:s"),
        {"s": str(sale_id)})).first()
    assert float(sale[0] or 0) == 30.0, "the refund itself is kept"
    assert sale[1] is not None


@pytest.mark.asyncio
async def test_a_failed_reversal_is_corrected_by_the_next_event(seeded, monkeypatch):
    u = await get_user(seeded)
    await SalesService.process_sale(seeded, sale_data(30.0), u)
    await seeded.commit()
    sale_id = (await seeded.execute(
        text("SELECT id FROM sales WHERE customer_id=:c"), {"c": str(JOE)})).first()[0]
    item = (await seeded.execute(
        text("SELECT id, quantity, total_price FROM sale_items WHERE sale_id=:s LIMIT 1"),
        {"s": str(sale_id)})).first()

    break_loyalty_append(monkeypatch)
    await SalesService.refund_sale(seeded, sale_id, RefundSaleRequest(
        sale_id=str(sale_id), reason="loyalty append failure probe",
        items_to_refund=[{"sale_item_id": str(item[0]), "quantity": int(item[1]),
                          "reason": "full refund", "restock": False}],
        refund_amount=float(item[2]), manager_approval_user_id=str(USER)), u)
    await seeded.commit()

    monkeypatch.undo()
    await SalesService.process_sale(seeded, sale_data(30.0), u)
    await seeded.commit()

    s = await state(seeded)
    assert s["points"] == 30, "server books are right: 30 earned, 30 refunded, 30 earned"
    row = (await seeded.execute(
        text("SELECT payload FROM event_log WHERE event_type="
             "'customer_loyalty_changed' AND aggregate_id=:c "
             "ORDER BY seq DESC LIMIT 1"), {"c": str(JOE)})).first()
    payload = row[0] if isinstance(row[0], dict) else __import__("json").loads(row[0])
    assert payload["loyalty_points"] == 30, (
        "the next event republishes the post-refund absolute balance, so a "
        "device that missed the reversal converges"
    )
