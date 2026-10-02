"""
A ValidationError raised while BUILDING A RESPONSE is a server fault (500), not a
client error (422). Request validation must keep returning 422.

The regression this pins: DrugResponse inherited `reorder_quantity ge=1` from
DrugBase, so a stored 0 raised while GET /drugs serialized its response, and the
pydantic handler in app/core/exception_handlers.py turned it into a 422 that
blamed the caller.
"""
import io
import logging
import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.core.exception_handlers import register_exception_handlers
from app.core.response_building import build_response, build_responses
from app.schemas.drugs_schemas import DrugResponse


# ---------------------------------------------------------------- app wiring

def _client_with_handlers() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def test_request_validation_still_returns_422():
    """Sanity: we did not break the legitimate 422 path."""
    app = FastAPI()
    register_exception_handlers(app)

    class Bounded(BaseModel):
        quantity: int = 0

    @app.get("/bounded")
    async def bounded(value: int = 0):
        return {"value": value}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/bounded?value=not-an-int")
    assert resp.status_code == 422, resp.text


def test_response_building_validation_error_returns_500_not_422():
    """The regression itself: serializing bad stored data must be a 500."""
    app = FastAPI()
    register_exception_handlers(app)

    class Bounded(BaseModel):
        quantity: int = 0

    @app.get("/boom")
    async def boom():
        raise ValidationError.from_exception_data(
            "Bounded",
            [{"type": "greater_than_equal", "loc": ("quantity",), "ctx": {"ge": 1}}],
        )

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500, resp.text
    assert resp.json()["type"] == "ResponseValidationError"
    assert "422" not in resp.text


def test_fastapi_response_validation_error_returns_500():
    """A route with response_model= that cannot be satisfied is a 500."""
    app = FastAPI()
    register_exception_handlers(app)

    class Out(BaseModel):
        quantity: int = 0

    @app.get("/bad", response_model=Out)
    async def bad():
        return SimpleNamespace(quantity="not-an-int")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/bad")
    assert resp.status_code == 500, resp.text


# ------------------------------------------------------- build_response helper

@contextmanager
def capture_logger(name: str = "app.core.response_building"):
    """Capture records from one logger regardless of global logging config.

    pytest's caplog fixture attaches to the root logger, which stops working once
    anything in the suite calls logging.config (importing app.main does). Binding
    a handler to the logger under test is immune to that.
    """
    log = logging.getLogger(name)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    previous_level, previous_propagate = log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        yield buf
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)
        log.propagate = previous_propagate


def test_build_response_logs_the_offending_row_id():
    """The row id is the only actionable part of a response-building failure."""
    row = SimpleNamespace(
        id="8d4cc1a7-03c7-4a6a-8080-2bda5def026f",
        name="Gebedol",
        drug_type="nope",
    )
    with capture_logger() as buf:
        with pytest.raises(ValidationError):
            build_response(DrugResponse, row, context="unit-test")
    text = buf.getvalue()
    assert "8d4cc1a7-03c7-4a6a-8080-2bda5def026f" in text, text
    assert "unit-test" in text, text


def test_build_response_returns_the_model_on_success():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Gebedol",
        unit_price=Decimal("5.00"),
    )
    result = build_response(DrugResponse, row)
    assert isinstance(result, DrugResponse)
    assert result.name == "Gebedol"


def test_build_responses_reports_which_row_failed():
    good = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(),
                           name="ok", unit_price=Decimal("1"))
    bad = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(),
                          name=None, unit_price=Decimal("1"))
    with capture_logger() as buf:
        with pytest.raises(ValidationError):
            build_responses(DrugResponse, [good, bad])
    text = buf.getvalue()
    assert str(bad.id) in text
    assert str(good.id) not in text