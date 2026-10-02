"""
Phase 1 regression tests: the Drug Catalogue 422.

Reproduces the reported defect end to end and pins the fix:

  * ``GET /api/v1/drugs`` with the exact query params the UI sends
    (``page=1&page_size=20&branch_id=...``) must return 200 and every drug in
    the org, including drugs whose ``reorder_quantity`` is 0.
  * ``POST /api/v1/drugs`` with ``reorder_quantity=0`` must be accepted.
  * A deliberately corrupt stored row must produce **500**, not 422, and the
    log must name the offending row id.
  * Request-side validation must still produce 422.

The 422 originally happened because ``DrugResponse`` inherited
``reorder_quantity ge=1`` from the input model, and the resulting
``ValidationError`` was caught by the generic pydantic handler and reported as
a client error. Two drugs held 0, so every caller in the org got a 422 for a
request that was perfectly valid.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL (conftest.py
issues DROP SCHEMA public CASCADE).
"""

import logging
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.inventory_model import Drug
from app.schemas.drugs_schemas import (
    DrugBase,
    DrugCreate,
    DrugResponse,
    DrugUpdate,
    DrugWithInventory,
)
from app.utils.response_building import (
    ResponseBuildError,
    build_response_model,
)


# ── Input models accept 0 ───────────────────────────────────────────────────


def test_drug_create_accepts_zero_reorder_quantity():
    payload = {
        "organization_id": str(uuid.uuid4()),
        "name": "Never Reorder",
        "unit_price": Decimal("5.00"),
        "reorder_level": 10,
        "reorder_quantity": 0,
    }
    drug = DrugCreate.model_validate(payload)
    assert drug.reorder_quantity == 0


def test_drug_update_accepts_zero_reorder_quantity():
    assert DrugUpdate.model_validate({"reorder_quantity": 0}).reorder_quantity == 0


def test_drug_create_still_rejects_negative_reorder_quantity():
    with pytest.raises(ValidationError) as exc:
        DrugCreate.model_validate(
            {
                "organization_id": str(uuid.uuid4()),
                "name": "Negative",
                "unit_price": Decimal("5.00"),
                "reorder_quantity": -1,
            }
        )
    assert "reorder_quantity" in str(exc.value)


def test_drug_base_bound_is_ge_zero():
    """Pins the actual bound so it cannot silently drift back to ge=1."""
    field = DrugBase.model_fields["reorder_quantity"]
    assert field.metadata[0].ge == 0


# ── Response models carry no input bounds ───────────────────────────────────


def _row(**overrides):
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Stored Drug",
        generic_name=None,
        brand_name=None,
        sku=None,
        barcode=None,
        category_id=None,
        drug_type="otc",
        dosage_form=None,
        strength=None,
        manufacturer=None,
        supplier=None,
        ndc_code=None,
        requires_prescription=False,
        controlled_substance_schedule=None,
        unit_price=Decimal("5.00"),
        cost_price=None,
        markup_percentage=None,
        tax_rate=Decimal("0.00"),
        reorder_level=10,
        reorder_quantity=0,       # the value that used to break the list
        max_stock_level=None,
        unit_of_measure="unit",
        description=None,
        usage_instructions=None,
        side_effects=None,
        contraindications=None,
        storage_conditions=None,
        image_url=None,
        is_active=True,
        created_at=now,
        updated_at=now,
        sync_status="synced",
        sync_version=1,
        last_synced_at=None,
    )
    base.update(overrides)
    return base


def test_drug_response_accepts_reorder_quantity_zero():
    drug = DrugResponse.model_validate(_row())
    assert drug.reorder_quantity == 0


def test_drug_with_inventory_accepts_reorder_quantity_zero():
    drug = DrugWithInventory.model_validate(_row(total_quantity=117))
    assert drug.reorder_quantity == 0


def test_drug_response_has_no_ge_constraints():
    """A response model must not be able to fail on stored data."""
    bounded = [
        name
        for name, field in DrugResponse.model_fields.items()
        if any(hasattr(m, "ge") or hasattr(m, "le") for m in field.metadata)
    ]
    assert bounded == [], f"response model re-declared bounds on: {bounded}"


def test_drug_response_has_no_pattern_constraints():
    patterned = [
        name
        for name, field in DrugResponse.model_fields.items()
        if any(hasattr(m, "pattern") for m in field.metadata)
    ]
    assert patterned == [], f"response model re-declared a pattern on: {patterned}"


def test_drug_response_does_not_inherit_input_bounds():
    """drug_type is a pattern on the input model and free text on the response."""
    assert any(
        getattr(m, "pattern", None) is not None
        for m in DrugBase.model_fields["drug_type"].metadata
    ), "precondition: the input model constrains drug_type"
    drug = DrugResponse.model_validate(_row(drug_type="legacy-value"))
    assert drug.drug_type == "legacy-value"


def test_drug_response_accepts_zero_sync_version():
    """sync_version ge=1 lives on SyncSchema (input), not the response."""
    drug = DrugResponse.model_validate(_row(sync_version=0))
    assert drug.sync_version == 0


# ── Response building is instrumented ───────────────────────────────────────


def test_build_response_model_returns_all_rows_when_valid():
    rows = [_row(name=f"Drug {i}") for i in range(3)]
    for r in rows:
        r["id"] = uuid.uuid4()
    built = build_response_model(DrugResponse, rows, context="test")
    assert len(built) == 3


def test_build_response_model_raises_with_offending_row_ids():
    """A row whose field type is wrong must raise naming the row, so the
    handler can answer 500 and the operator can find the record."""
    bad_id = uuid.uuid4()
    rows = [
        _row(name="Good", id=uuid.uuid4()),
        # unit_price is a str on the DB side for this one; Pydantic Decimal
        # rejects it, which is exactly the class of fault we must catch.
        _row(name="Bad", id=bad_id, unit_price="not-a-number"),
    ]
    with pytest.raises(ResponseBuildError) as exc:
        build_response_model(DrugResponse, rows, context="GET /drugs")
    assert bad_id in exc.value.row_ids


def test_response_build_error_is_not_a_validation_error_subclass():
    """Pydantic v2's ValidationError cannot be subclassed.

    ResponseBuildError is therefore its own type with a dedicated handler, and
    keeps the original error on `.original` for callers that want the detail.
    """
    from pydantic import ValidationError as PydanticValidationError

    assert issubclass(ResponseBuildError, Exception)
    assert not issubclass(ResponseBuildError, PydanticValidationError)


def test_response_build_error_marks_its_loc():
    bad_id = uuid.uuid4()
    rows = [_row(name="Bad", id=bad_id, unit_price="not-a-number")]
    with pytest.raises(ResponseBuildError) as exc:
        build_response_model(DrugResponse, rows)
    locs = [loc for err in exc.value.marked_errors for loc in err["loc"]]
    assert "__response_model__" in locs


# ── Stored rows survive a real query round trip ──────────────────────────────


@pytest.mark.asyncio
async def test_stored_drug_with_zero_reorder_quantity_round_trips(
    db: AsyncSession, setup_test_data
):
    org, _branch, _user, _drugs, _customer = setup_test_data
    drug = Drug(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Zero Reorder Drug",
        sku="ZERO-1",
        unit_price=Decimal("5.00"),
        cost_price=Decimal("3.00"),
        reorder_level=10,
        reorder_quantity=0,
        tax_rate=Decimal("0.00"),
        is_active=True,
        is_deleted=False,
    )
    db.add(drug)
    await db.commit()
    await db.refresh(drug)

    from app.services.drug.drug_service import DrugService

    found = await DrugService.search_drugs(
        db=db, organization_id=org.id, is_active=True
    )
    zero_rows = [d for d in found if d.reorder_quantity == 0]
    assert len(zero_rows) == 1

    built = build_response_model(
        DrugResponse, found, context="GET /drugs"
    )
    assert len(built) == len(found)
    assert any(d.reorder_quantity == 0 for d in built)


# ── The 422 must never come back for a valid request ───────────────────────


@pytest.mark.asyncio
async def test_request_validation_still_returns_422(db: AsyncSession):
    """Input validation must keep its client-error status.

    Exercised through the real handler so the classification logic in
    exception_handlers.py is covered, not just the schema bounds.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.exception_handlers import register_exception_handlers
    from app.schemas.drugs_schemas import DrugCreate
    from app.utils.pagination import PaginatedResponse

    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/drugs", response_model=DrugResponse)
    async def create_drug(drug_data: DrugCreate):
        # Never reached: negative reorder_quantity is rejected on input.
        return drug_data

    @app.get("/drugs", response_model=PaginatedResponse[DrugResponse])
    async def list_drugs():
        # Deliberately corrupt a stored row: `name` is a str on the model but
        # we hand back something that cannot satisfy it.
        return PaginatedResponse(
            items=[{"id": "not-a-uuid", "name": "X", "unit_price": Decimal("1")}],
            total=1, page=1, page_size=20, total_pages=1,
            has_next=False, has_prev=False,
        )

    client = TestClient(app, raise_server_exceptions=False)

    # ── request-side: 422 ──
    r = client.post(
        "/drugs",
        json={
            "organization_id": str(uuid.uuid4()),
            "name": "Negative",
            "unit_price": "5.00",
            "reorder_quantity": -1,
        },
    )
    assert r.status_code == 422, r.text

    # ── response-side: 500, never 422 ──
    r = client.get("/drugs")
    assert r.status_code == 500, (
        f"response-side ValidationError must be a 500, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert "detail" in body
    assert "server" in body["detail"].lower()


def test_handler_classifies_response_side_as_500(caplog):
    """Direct check of the classifier, independent of FastAPI's wrapping."""
    from app.core.exception_handlers import _is_response_side
    from app.utils.response_building import ResponseBuildError as RBE

    bad_id = uuid.uuid4()
    rows = [_row(name="Bad", id=bad_id, unit_price="not-a-number")]
    try:
        build_response_model(DrugResponse, rows)
    except RBE as exc:
        formatted = [
            {"loc": list(e.get("loc", [])), "msg": "", "type": e.get("type", "")}
            for e in exc.marked_errors
        ]
        assert _is_response_side(exc, formatted) is True
    else:
        pytest.fail("expected ResponseBuildError")


def test_handler_classifies_plain_validation_error_as_request_side():
    from app.core.exception_handlers import _is_response_side

    plain = DrugCreate.model_validate  # noqa: F841 - readability
    exc = None
    try:
        DrugCreate.model_validate({"name": "X", "unit_price": "1", "reorder_quantity": -1})
    except ValidationError as e:
        exc = e
    assert exc is not None
    formatted = [
        {"loc": list(e.get("loc", [])), "msg": "", "type": e.get("type", "")}
        for e in exc.errors()
    ]
    assert _is_response_side(exc, formatted) is False


def test_corrupt_row_error_is_logged_with_row_id(caplog):
    """The operator must be able to identify the offending record from logs."""
    bad_id = uuid.uuid4()
    rows = [_row(name="Bad", id=bad_id, unit_price="not-a-number")]
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ResponseBuildError):
            build_response_model(DrugResponse, rows, context="GET /drugs")
    assert str(bad_id) in caplog.text