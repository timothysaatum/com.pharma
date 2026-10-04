"""The server half of the derived "dispensed N" figure.

Paired with `ui.laso/src/lib/__tests__/prescriptionDispensedCount.spec.ts`, which
pins the SAME rules for the local path. Both must agree or the same prescription
shows a different count depending on whether the device is online — which is the
failure the derived count exists to remove.

Requires TEST_DATABASE_URL pointing at a DISPOSABLE cluster.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)

from app.api.v1.endpoints.prescription_endpoints import _dispensed_counts  # noqa: E402
from app.models.customer.customer_model import Customer  # noqa: E402
from app.models.pharmacy.pharmacy_model import Branch  # noqa: E402
from app.models.prescriptions.prescription_model import Prescription  # noqa: E402
from app.models.sales.sales_model import Sale  # noqa: E402


def _rx(org_id, branch_id, customer_id, rx_id):
    return Prescription(
        id=rx_id,
        organization_id=org_id,
        branch_id=branch_id,
        prescription_number=f"RX-{rx_id.hex[:8]}",
        customer_id=customer_id,
        prescriber_name="Dr. Test",
        prescriber_license="MED-1",
        issue_date=date.today(),
        expiry_date=date.today() + timedelta(days=60),
        medications=[],
        refills_allowed=3,
        refills_remaining=3,
        status="active",
    )


def _sale(org_id, branch_id, customer_id, rx_id, status, n, cashier):
    return Sale(
        id=uuid.uuid4(),
        organization_id=org_id,
        branch_id=branch_id,
        sale_number=f"S-{n}",
        customer_id=customer_id,
        customer_name="Rx Patient",
        subtotal=30,
        discount_amount=0,
        tax_amount=0,
        total_amount=30,
        payment_method="cash",
        payment_status="completed",
        amount_paid=30,
        change_amount=0,
        prescription_id=rx_id,
        prescription_number="RX-1" if rx_id else None,
        prescriber_name="Dr. Test" if rx_id else None,
        prescriber_license="MED-1" if rx_id else None,
        cashier_id=cashier,
        status=status,
    )


@pytest_asyncio.fixture
async def rx_world(db, setup_test_data):
    org, branch, user, _drugs, _customer = setup_test_data
    customer = Customer(
        id=uuid.uuid4(),
        organization_id=org.id,
        customer_type="registered",
        first_name="Rx",
        last_name="Patient",
        is_active=True,
    )
    db.add(customer)
    await db.commit()
    return org.id, branch.id, customer.id, user.id


async def test_counts_only_completed_sales(db, rx_world):
    """Same rule as the local subquery: `status = 'completed'`."""
    org_id, branch_id, customer_id, cashier = rx_world
    rx = uuid.uuid4()
    db.add(_rx(org_id, branch_id, customer_id, rx))
    # `payment_status` and `status` are separate columns with separate check
    # constraints. payment_status stays "completed" throughout (the money was
    # taken); only the sale's lifecycle `status` varies.
    for status in ("completed", "completed", "cancelled", "refunded", "draft"):
        db.add(_sale(org_id, branch_id, customer_id, rx, status, uuid.uuid4().int % 100000, cashier))
    await db.commit()

    counts = await _dispensed_counts(db, [rx])
    assert counts[rx] == 2, "only completed sales are dispenses"


async def test_reports_zero_for_never_dispensed(db, rx_world):
    org_id, branch_id, customer_id, _cashier = rx_world
    rx = uuid.uuid4()
    db.add(_rx(org_id, branch_id, customer_id, rx))
    await db.commit()

    counts = await _dispensed_counts(db, [rx])
    assert counts == {}, "absent from the result, and the caller defaults to 0"


async def test_empty_input_short_circuits(db, rx_world):
    """No query at all — this is what keeps the list at one round trip."""
    assert await _dispensed_counts(db, []) == {}


async def test_counts_each_prescription_separately(db, rx_world):
    org_id, branch_id, customer_id, cashier = rx_world
    a, b = uuid.uuid4(), uuid.uuid4()
    db.add_all([
        _rx(org_id, branch_id, customer_id, a),
        _rx(org_id, branch_id, customer_id, b),
    ])
    for _ in range(3):
        db.add(_sale(org_id, branch_id, customer_id, a, "completed", uuid.uuid4().int % 100000, cashier))
    db.add(_sale(org_id, branch_id, customer_id, b, "completed", uuid.uuid4().int % 100000, cashier))
    db.add(_sale(org_id, branch_id, customer_id, None, "completed", uuid.uuid4().int % 100000, cashier))
    await db.commit()

    counts = await _dispensed_counts(db, [a, b])
    assert counts[a] == 3
    assert counts[b] == 1


async def test_the_index_this_relies_on_exists(db, rx_world):
    """The count is a per-row aggregate over the busiest table in the database.

    `sales.prescription_id` had no index until migration 6d967f66b097. This pins
    it so a future migration that drops it in a rebuild is caught here rather
    than as a slow prescriptions page.
    """
    rows = await db.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'sales' AND indexname = 'ix_sales_prescription_id'"
        )
    )
    assert rows.fetchall(), (
        "ix_sales_prescription_id is missing — run `alembic upgrade head`. "
        "Without it the derived dispense count full-scans sales."
    )
