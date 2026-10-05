"""P2b, server half: the prescriber's facility is stored and searchable.

Paired with `ui.laso/src/lib/__tests__/prescriptionFacilityFlow.test.ts`, which
walks the same chain on the device. The instruction was to verify rather than
assume: `prescriber_address` was already in the ORM, both pydantic schemas, the
projector whitelists and the legacy sync whitelist — but hardcoded to `null` by
both forms, with no UI and not in either search. The parts that were missing are
what this file covers.

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

from app.api.v1.endpoints.prescription_endpoints import PrescriptionCreate  # noqa: E402
from app.models.customer.customer_model import Customer  # noqa: E402
from app.models.prescriptions.prescription_model import Prescription  # noqa: E402

FACILITY = "Korle Bu Teaching Hospital, Accra"


@pytest_asyncio.fixture
async def world(db, setup_test_data):
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
    return org.id, branch.id, customer.id


def _rx(org_id, branch_id, customer_id, facility, rx_id):
    return Prescription(
        id=rx_id,
        organization_id=org_id,
        branch_id=branch_id,
        prescription_number=f"RX-{rx_id.hex[:8]}",
        customer_id=customer_id,
        prescriber_name="Dr. Ama Boateng",
        prescriber_license="MED-1",
        prescriber_address=facility,
        issue_date=date.today(),
        expiry_date=date.today() + timedelta(days=60),
        medications=[],
        refills_allowed=2,
        refills_remaining=2,
        status="active",
    )


async def _search(db, org_id, branch_id, term):
    """The same or_() the endpoint builds, exercised directly.

    Going through the endpoint would need auth plumbing that has nothing to do
    with what is being asserted here — that the facility is in the same OR as the
    prescriber name, so both sides of the offline/online pair match.
    """
    from sqlalchemy import or_, select

    from app.models.customer.customer_model import Customer as C
    from app.models.prescriptions.prescription_model import Prescription as P

    pattern = f"%{term.strip()}%"
    stmt = (
        select(P.id)
        .join(C, C.id == P.customer_id)
        .where(
            P.organization_id == org_id,
            P.branch_id == branch_id,
            or_(
                P.prescription_number.ilike(pattern),
                P.prescriber_name.ilike(pattern),
                P.prescriber_address.ilike(pattern),
                C.first_name.ilike(pattern),
                C.last_name.ilike(pattern),
                C.phone.ilike(pattern),
            ),
        )
        .order_by(P.id)
    )
    rows = await db.execute(stmt)
    return sorted(str(r[0]) for r in rows)


async def test_the_column_exists_and_is_nullable(db, world):
    rows = await db.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'prescriptions' AND column_name = 'prescriber_address'"
        )
    )
    assert rows.scalar_one() == "YES"


async def test_a_facility_round_trips_through_the_orm(db, world):
    org_id, branch_id, customer_id = world
    rx_id = uuid.uuid4()
    db.add(_rx(org_id, branch_id, customer_id, FACILITY, rx_id))
    await db.commit()
    db.expire_all()
    row = await db.get(Prescription, rx_id)
    assert row.prescriber_address == FACILITY


async def test_a_null_facility_is_allowed(db, world):
    org_id, branch_id, customer_id = world
    rx_id = uuid.uuid4()
    db.add(_rx(org_id, branch_id, customer_id, None, rx_id))
    await db.commit()
    db.expire_all()
    assert (await db.get(Prescription, rx_id)).prescriber_address is None


def test_the_create_schema_carries_the_field():
    assert "prescriber_address" in PrescriptionCreate.model_fields


async def test_search_finds_the_facility(db, world):
    org_id, branch_id, customer_id = world
    hit, miss = uuid.uuid4(), uuid.uuid4()
    db.add_all([
        _rx(org_id, branch_id, customer_id, FACILITY, hit),
        _rx(org_id, branch_id, customer_id, "Kumasi Regional Hospital", miss),
    ])
    await db.commit()
    assert await _search(db, org_id, branch_id, "korle") == [str(hit)]


async def test_search_is_case_insensitive_like_the_local_path(db, world):
    org_id, branch_id, customer_id = world
    rx_id = uuid.uuid4()
    db.add(_rx(org_id, branch_id, customer_id, "KORLE BU", rx_id))
    await db.commit()
    assert await _search(db, org_id, branch_id, "korle") == [str(rx_id)]


async def test_search_still_finds_by_prescriber_name(db, world):
    """The facility must be ADDED to the OR, not replace anything."""
    org_id, branch_id, customer_id = world
    rx_id = uuid.uuid4()
    db.add(_rx(org_id, branch_id, customer_id, "Some Other Clinic", rx_id))
    await db.commit()
    assert await _search(db, org_id, branch_id, "boateng") == [str(rx_id)]
    assert await _search(db, org_id, branch_id, "korle") == []
