"""P2: the prescriber licence number is optional, end to end.

THE THREE LANDMINES, in the order they had to be fixed
------------------------------------------------------
1. `PrescriptionResponse.prescriber_license` was a non-Optional `str`. The moment
   the column went nullable, a NULL row raised a pydantic ValidationError, the
   generic handler turned it into **422 on a perfectly valid read**. This is the
   exact failure class documented in app/schemas/drugs_schemas.py:121-142.

2. The server projector's validate step required
   `("prescription_number", "customer_id", "prescriber_name", "prescriber_license")`
   using `if not p.get(field)`, which rejects `""` AND `None`. So every
   prescription created offline without a licence was REJECTED_PERMANENT and
   dead-lettered. Verified before the fix on a disposable cluster:
   `error_code=missing_prescriber_license`.

3. `""` and NULL are now the same thing. The canonical form is SQL NULL and the
   boundaries normalise, because three distinct states for "no licence" is how a
   `WHERE prescriber_license IS NOT NULL` filter silently misses rows.

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

from app.api.v1.endpoints.prescription_endpoints import (  # noqa: E402
    PrescriptionCreate,
    PrescriptionResponse,
    PrescriptionUpdate,
)
from app.models.customer.customer_model import Customer  # noqa: E402
from app.models.prescriptions.prescription_model import Prescription  # noqa: E402
from app.schemas.event_envelope import AggregateType, EventEnvelope  # noqa: E402
from app.services.sync.eventlog.projectors.prescription import (  # noqa: E402
    PrescriptionProjector,
)

RX = uuid.UUID("dddddddd-1111-2222-3333-444444444444")


def _rx(org_id, branch_id, customer_id, *, license_=None, rx_id=RX):
    return Prescription(
        id=rx_id,
        organization_id=org_id,
        branch_id=branch_id,
        prescription_number=f"RX-{rx_id.hex[:8]}",
        customer_id=customer_id,
        prescriber_name="Dr. Test",
        prescriber_license=license_,
        issue_date=date.today(),
        expiry_date=date.today() + timedelta(days=60),
        medications=[],
        refills_allowed=1,
        refills_remaining=1,
        status="active",
    )


def _payload(**over) -> dict:
    p = {
        "organization_id": "org",
        "branch_id": "branch",
        "prescription_number": "RX-1",
        "customer_id": "cust",
        "prescriber_name": "Dr. Test",
        "prescriber_license": None,
        "issue_date": date.today().isoformat(),
        "expiry_date": (date.today() + timedelta(days=60)).isoformat(),
        "medications": [
            {
                "drug_id": str(uuid.uuid4()),
                "drug_name": "Gebedol",
                "dosage": "5mg",
                "frequency": "bd",
                "duration": "7d",
                "quantity": 10,
            }
        ],
        "refills_allowed": 1,
        "refills_remaining": 1,
        "status": "active",
    }
    p.update(over)
    return p


def _envelope(payload, org_id, branch_id, author):
    return EventEnvelope(
        event_id=uuid.uuid4().hex[:26].upper(),
        org_id=org_id,
        seq=1,
        aggregate_id=RX,
        aggregate_type=AggregateType.PRESCRIPTION,
        event_type="prescription_created",
        schema_version=1,
        payload=payload,
        dependencies=[],
        authored_at=date.today().isoformat() and __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        authored_by=author,
        branch_id=branch_id,
        hash_self="ab" * 32,
        hash_prev="cd" * 32,
    )


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
    return org.id, branch.id, customer.id, user.id


# ── Landmine 1: the response schema ──────────────────────────────────────────


def test_response_schema_accepts_a_null_licence():
    """The 422 landmine, asserted directly on the schema."""
    assert PrescriptionResponse.model_fields["prescriber_license"].default is None


async def test_a_null_licence_row_serialises_without_error(db, world):
    """The regression that mattered: a valid read must not become a 422."""
    org_id, branch_id, customer_id, _user = world
    db.add(_rx(org_id, branch_id, customer_id, license_=None))
    await db.commit()
    db.expire_all()

    row = await db.get(Prescription, RX)
    assert row.prescriber_license is None
    # This is what FastAPI does on the way out. Before the fix it raised.
    out = PrescriptionResponse.model_validate(
        {
            **row.__dict__,
            "is_expired": False,
            "customer_name": "Rx Patient",
            "dispensed_count": 0,
        }
    )
    assert out.prescriber_license is None


async def test_an_empty_string_licence_also_serialises(db, world):
    """Rows written before the change may hold ''. Must not 422 either."""
    org_id, branch_id, customer_id, _user = world
    db.add(_rx(org_id, branch_id, customer_id, license_=""))
    await db.commit()
    db.expire_all()
    row = await db.get(Prescription, RX)
    out = PrescriptionResponse.model_validate(
        {**row.__dict__, "is_expired": False, "customer_name": "Rx Patient",
         "dispensed_count": 0}
    )
    assert out.prescriber_license == ""


# ── Landmine 2: the projector's validate step ────────────────────────────────


async def test_projector_accepts_a_payload_with_no_licence(db, world):
    """REGRESSION. Was REJECTED_PERMANENT / missing_prescriber_license."""
    org_id, branch_id, customer_id, user_id = world
    pp = PrescriptionProjector()
    ev = _envelope(
        _payload(organization_id=str(org_id), branch_id=str(branch_id),
                 customer_id=str(customer_id)),
        org_id, branch_id, user_id)
    result = await pp.validate(ev, db)
    assert "REJECT" not in str(result.status).upper(), result
    assert getattr(result, "error_code", None) != "missing_prescriber_license"


async def test_projector_accepts_an_empty_string_licence(db, world):
    org_id, branch_id, customer_id, user_id = world
    pp = PrescriptionProjector()
    ev = _envelope(
        _payload(prescriber_license="", organization_id=str(org_id),
                 branch_id=str(branch_id), customer_id=str(customer_id)),
        org_id, branch_id, user_id)
    result = await pp.validate(ev, db)
    assert "REJECT" not in str(result.status).upper(), result


async def test_projector_still_requires_the_other_fields(db, world):
    """Dropping one requirement must not drop the others."""
    org_id, branch_id, customer_id, user_id = world
    pp = PrescriptionProjector()
    for field in ("prescription_number", "customer_id", "prescriber_name"):
        payload = _payload(organization_id=str(org_id), branch_id=str(branch_id),
                           customer_id=str(customer_id))
        payload.pop(field)
        ev = _envelope(payload, org_id, branch_id, user_id)
        result = await pp.validate(ev, db)
        assert "REJECT" in str(result.status).upper(), f"{field} should still be required"
        assert getattr(result, "error_code", None) == f"missing_{field}"


async def test_projector_applies_a_null_licence(db, world):
    org_id, branch_id, customer_id, user_id = world
    pp = PrescriptionProjector()
    payload = _payload(
        organization_id=str(org_id),
        branch_id=str(branch_id),
        customer_id=str(customer_id),
    )
    await pp.apply(_envelope(payload, org_id, branch_id, user_id), db)
    await db.commit()
    db.expire_all()
    row = await db.get(Prescription, RX)
    assert row is not None
    assert row.prescriber_license is None


async def test_projector_applies_an_event_that_omits_the_key_entirely(db, world):
    """`.get()`, not a subscript.

    Events already sitting in a device outbox predate this change, so the key can
    be absent altogether rather than null. A hard subscript would raise KeyError
    and abort the projection.
    """
    org_id, branch_id, customer_id, user_id = world
    pp = PrescriptionProjector()
    payload = _payload(
        organization_id=str(org_id),
        branch_id=str(branch_id),
        customer_id=str(customer_id),
    )
    payload.pop("prescriber_license")
    await pp.apply(_envelope(payload, org_id, branch_id, user_id), db)
    await db.commit()
    db.expire_all()
    row = await db.get(Prescription, RX)
    assert row is not None and row.prescriber_license is None


# ── Landmine 3: "" and NULL are the same thing ───────────────────────────────


def test_create_schema_makes_the_licence_optional():
    """Optional AND unbounded.

    There is deliberately no min_length: the schema accepts whatever the client
    sent and the handler normalises it (`... or None`), so there is exactly one
    place deciding what "no licence" means. Adding a min_length here would create
    a second, contradictory rule at the boundary.
    """
    field = PrescriptionCreate.model_fields["prescriber_license"]
    assert field.default is None
    assert not field.metadata


def test_create_payload_without_a_licence_validates():
    payload = _payload()
    payload.pop("prescriber_license")
    payload["organization_id"] = str(uuid.uuid4())
    payload["branch_id"] = str(uuid.uuid4())
    payload["customer_id"] = str(uuid.uuid4())
    model = PrescriptionCreate(**payload)
    assert model.prescriber_license is None


def test_create_payload_with_an_empty_licence_validates():
    """Accepted here; the handler turns it into NULL. Documented, not asserted
    as a schema rule, because normalisation belongs at the boundary."""
    payload = _payload(prescriber_license="")
    payload["organization_id"] = str(uuid.uuid4())
    payload["branch_id"] = str(uuid.uuid4())
    payload["customer_id"] = str(uuid.uuid4())
    assert PrescriptionCreate(**payload).prescriber_license == ""


def test_update_schema_was_already_optional():
    assert PrescriptionUpdate.model_fields["prescriber_license"].default is None


# ── Schema ───────────────────────────────────────────────────────────────────


async def test_the_column_really_is_nullable(db, world):
    """The ORM and the migration must agree, or a migrated DB differs from a
    create_all one — and the device's own migration is a third copy."""
    rows = await db.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'prescriptions' AND column_name = 'prescriber_license'"
        )
    )
    assert rows.scalar_one() == "YES"
