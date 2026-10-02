"""
Response models must never be able to fail while serializing a stored row.

A 422 produced while building a response blames the caller for a server-side data
problem. These tests pin the invariant structurally (no bounds on response
models) rather than relying on every current field happening to be valid.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.drugs_schemas import (
    DrugBase,
    DrugCreate,
    DrugResponse,
    DrugUpdate,
    DrugWithInventory,
)

# Constraint metadata classes that would make a read fail on stored data.
CONSTRAINT_TYPES = {
    "Ge", "Le", "Gt", "Lt", "MultipleOf",
    "MinLen", "MaxLen", "Len",
    "StringConstraints", "StringPattern",
    "Decimal", "DecimalIsFinite",
    "_PydanticGeneralMetadata",
}


def _constraints(model) -> dict:
    found = {}
    for name, field in model.model_fields.items():
        hits = [type(m).__name__ for m in field.metadata if type(m).__name__ in CONSTRAINT_TYPES]
        if hits:
            found[name] = hits
    return found


def test_drug_response_models_carry_no_bounds():
    """The core invariant: response models hold types, not input constraints."""
    assert _constraints(DrugResponse) == {}, (
        "DrugResponse must not constrain reads. Found bounds on: "
        f"{_constraints(DrugResponse)}"
    )
    assert _constraints(DrugWithInventory) == {}, (
        "DrugWithInventory must not constrain reads. Found bounds on: "
        f"{_constraints(DrugWithInventory)}"
    )


def test_drug_response_is_not_a_subclass_of_any_input_schema():
    """Guards against someone 'simplifying' the models back into inheritance."""
    for input_schema in (DrugBase, DrugCreate, DrugUpdate):
        assert not issubclass(DrugResponse, input_schema), (
            f"DrugResponse must not inherit from {input_schema.__name__}: "
            "input bounds would leak into reads"
        )


def test_drug_response_covers_every_drug_base_field():
    """A new field on DrugBase must be mirrored on the response model."""
    missing = set(DrugBase.model_fields) - set(DrugResponse.model_fields)
    assert not missing, f"fields added to DrugBase but not DrugResponse: {sorted(missing)}"


def _row(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Gebedol",
        generic_name=None,
        brand_name=None,
        sku="GEB-300",
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
        cost_price=Decimal("3.00"),
        markup_percentage=None,
        tax_rate=Decimal("0.00"),
        reorder_level=10,
        reorder_quantity=0,
        max_stock_level=None,
        unit_of_measure="unit",
        description=None,
        usage_instructions=None,
        side_effects=None,
        contraindications=None,
        storage_conditions=None,
        image_url=None,
        is_active=True,
        created_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        sync_status="pending",
        sync_version=0,
        last_synced_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("quantity", [0, 1, 50])
def test_response_serializes_reorder_quantity_zero_and_above(quantity):
    """reorder_quantity 0 is valid data and must round-trip a read."""
    assert DrugResponse.model_validate(_row(reorder_quantity=quantity)).reorder_quantity == quantity


def test_response_serializes_values_outside_every_input_bound():
    """A row written by direct SQL must still serialize rather than 422."""
    hostile = _row(
        name="x" * 5000,               # exceeds DrugBase max_length=255
        drug_type="not-a-real-type",   # violates the DrugBase pattern
        reorder_quantity=-999,        # below DrugBase ge=0
        tax_rate=Decimal("-5"),
        unit_price=Decimal("123456789012345.678"),  # exceeds condecimal bounds
        sync_version=0,               # below SyncSchema ge=1
    )
    result = DrugResponse.model_validate(hostile)
    assert result.reorder_quantity == -999
    assert result.drug_type == "not-a-real-type"


def test_create_and_update_still_reject_negative_reorder_quantity():
    """relaxing the bound to ge=0 must not open the door to negatives."""
    with pytest.raises(ValidationError):
        DrugCreate(
            organization_id=uuid.uuid4(),
            name="x",
            unit_price=Decimal("1.00"),
            reorder_quantity=-1,
        )
    with pytest.raises(ValidationError):
        DrugUpdate(reorder_quantity=-1)


def test_create_still_enforces_its_other_bounds():
    """Bounds other than reorder_quantity stay on the input side."""
    with pytest.raises(ValidationError):
        DrugCreate(organization_id=uuid.uuid4(), name="", unit_price=Decimal("1"))
    with pytest.raises(ValidationError):
        DrugCreate(
            organization_id=uuid.uuid4(), name="x", unit_price=Decimal("1"),
            drug_type="bogus",
        )
    with pytest.raises(ValidationError):
        DrugCreate(
            organization_id=uuid.uuid4(), name="x", unit_price=Decimal("1"),
            reorder_level=-1,
        )


def test_drug_response_default_edit_hint_is_still_fifty():
    """reorder_quantity 0 means 'no suggestion'; a new drug still defaults to 50."""
    assert DrugCreate(
        organization_id=uuid.uuid4(), name="x", unit_price=Decimal("1")
    ).reorder_quantity == 50


def test_profit_margin_and_needs_reorder_still_compute():
    row = _row(unit_price=Decimal("5.00"), cost_price=Decimal("3.00"))
    assert DrugResponse.model_validate(row).profit_margin == pytest.approx(66.666, rel=1e-3)
    assert DrugWithInventory.model_validate(
        {**_row().__dict__, "total_quantity": 5}
    ).needs_reorder is True