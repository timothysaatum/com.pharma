"""
Integration coverage for the Drug Catalogue 422, against real PostgreSQL.

Requires TEST_DATABASE_URL pointing at a DISPOSABLE cluster. tests/conftest.py
issues `DROP SCHEMA IF EXISTS public CASCADE`, so never point it at a real
database. These tests are skipped without it.
"""
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from httpx import AsyncClient, ASGITransport

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)

from app.models.inventory.inventory_model import Drug  # noqa: E402
from app.models.inventory.branch_inventory import BranchInventory  # noqa: E402
from app.schemas.drugs_schemas import DrugResponse  # noqa: E402


def _make_client(current_user, db_session):
    """TestClient for the real app with auth and DB bound to the test session.

    require_permission() builds a fresh closure per call, so it cannot be keyed
    in dependency_overrides. Its checker depends on get_current_user by identity
    though, so overriding that one callable covers every guarded route.
    """
    from main import app
    from app.core import deps

    async def _user():
        return current_user

    async def _db():
        yield db_session

    app.dependency_overrides[deps.get_current_user] = _user
    app.dependency_overrides[deps.get_db] = _db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _get(db, url, user, params=None):
    async with _make_client(user, db) as client:
        return await client.get(url, params=params)


async def _post(db, url, user, json=None):
    async with _make_client(user, db) as client:
        return await client.post(url, json=json)


async def _patch(db, url, user, json=None):
    async with _make_client(user, db) as client:
        return await client.patch(url, json=json)


@pytest.mark.asyncio
async def test_get_drugs_with_ui_params_includes_reorder_quantity_zero(db, setup_test_data):
    """The reported bug: GET /drugs with the UI's own params returned 422.

    The UI sends page, page_size and branch_id. A drug with reorder_quantity 0
    made response serialization raise, so the whole page 422'd.
    """
    org, branch, user, _drugs, _customer = setup_test_data

    zero = Drug(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Gebedol",
        sku="GEB-300",
        unit_price=Decimal("5.00"),
        reorder_level=10,
        reorder_quantity=0,
        is_active=True,
        is_deleted=False,
        tax_rate=Decimal("0.00"),
    )
    db.add(zero)
    db.add(
        BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=zero.id,
            quantity=117,
            reserved_quantity=0,
            selling_price=Decimal("5.00"),
        )
    )
    await db.commit()

    resp = await _get(
        db,
        "/api/v1/drugs",
        params={"page": 1, "page_size": 20, "branch_id": str(branch.id)},
        user=user,
    )
    assert resp.status_code == 200, resp.text
    names = {d["name"] for d in resp.json()["items"]}
    assert "Gebedol" in names
    assert len(names) == 4, names
    geb = next(d for d in resp.json()["items"] if d["name"] == "Gebedol")
    assert geb["reorder_quantity"] == 0


@pytest.mark.asyncio
async def test_post_drug_with_reorder_quantity_zero_succeeds(db, setup_test_data):
    """0 is a legal reorder quantity and must be accepted on create."""
    org, _branch, user, _drugs, _customer = setup_test_data

    resp = await _post(
        db,
        "/api/v1/drugs",
        json={
            "organization_id": str(org.id),
            "name": "Zero Reorder Drug",
            "unit_price": "5.00",
            "reorder_level": 10,
            "reorder_quantity": 0,
        },
        user=user,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["reorder_quantity"] == 0


@pytest.mark.asyncio
async def test_patch_drug_with_reorder_quantity_zero_succeeds(db, setup_test_data):
    _org, _branch, user, drugs, _customer = setup_test_data
    target = drugs[0]

    resp = await _patch(db, f"/api/v1/drugs/{target.id}", json={"reorder_quantity": 0}, user=user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reorder_quantity"] == 0


@pytest.mark.asyncio
async def test_negative_reorder_quantity_still_rejected(db, setup_test_data):
    """ge=0, not unbounded: a negative value is still a client error."""
    org, _branch, user, _drugs, _customer = setup_test_data

    resp = await _post(
        db,
        "/api/v1/drugs",
        json={
            "organization_id": str(org.id),
            "name": "Negative Reorder",
            "unit_price": "5.00",
            "reorder_quantity": -1,
        },
        user=user,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_row_written_outside_api_bounds_still_reads_200(db, setup_test_data):
    """Direct SQL can store values the API would reject; reads must not 422."""
    _org, _branch, user, drugs, _customer = setup_test_data
    target = drugs[0]

    await db.execute(
        text("UPDATE drugs SET reorder_quantity = 0 WHERE id = :id"),
        {"id": str(target.id)},
    )
    await db.commit()
    # The route reads through this same session, whose identity map still holds
    # the pre-UPDATE instance, so reload it (async-safe; expire_all would defer
    # the load into a sync context and raise MissingGreenlet).
    await db.refresh(target)
    assert target.reorder_quantity == 0

    resp = await _get(db, f"/api/v1/drugs/{target.id}", user=user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reorder_quantity"] == 0


@pytest.mark.asyncio
async def test_unserializable_row_yields_500_not_422(db, setup_test_data, monkeypatch):
    """When a row genuinely cannot be serialized, the API returns 500, not 422.

    GET /drugs/{id} declares response_model=DrugResponse and returns the ORM row,
    so FastAPI's own response validation is what fails here. That is the
    ResponseValidationError branch of the handler.
    """
    from app.services.drug.drug_service import DrugService

    _org, _branch, user, drugs, _customer = setup_test_data
    target = drugs[0]

    async def _broken(*_args, **_kwargs):
        # name is declared `str` on the response model and cannot be coerced.
        broken = SimpleNamespace(**{c.name: getattr(target, c.name, None)
                                   for c in target.__table__.columns})
        broken.name = None
        return broken

    monkeypatch.setattr(DrugService, "get_drug_by_id", _broken)

    resp = await _get(db, f"/api/v1/drugs/{target.id}", user=user)
    assert resp.status_code == 500, resp.text
    assert "422" not in resp.text


@pytest.mark.asyncio
async def test_drug_with_inventory_route_returns_200(db, setup_test_data):
    """GET /drugs/{id}/with-inventory was one of the 422 routes."""
    _org, branch, user, drugs, _customer = setup_test_data
    target = drugs[0]
    target.reorder_quantity = 0
    await db.commit()

    resp = await _get(
        db,
        f"/api/v1/drugs/{target.id}/with-inventory",
        user=user,
        params={"branch_id": str(branch.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reorder_quantity"] == 0


@pytest.mark.asyncio
async def test_drug_search_returns_200(db, setup_test_data):
    """POST /drugs/search was one of the 422 routes."""
    org, _branch, user, drugs, _customer = setup_test_data
    for d in drugs:
        d.reorder_quantity = 0
    await db.commit()

    resp = await _post(
        db,
        "/api/v1/drugs/search",
        json={"organization_id": str(org.id), "page": 1, "page_size": 20},
        user=user,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 3


@pytest.mark.asyncio
async def test_catalog_is_org_wide_and_ignores_branch_stock(db, setup_test_data):
    """A drug in the catalogue with no branch stock is still listed.

    The owner decision: the catalogue is organization-wide and never depends on
    branch_inventory. Only Inventory > Low Stock treats it as out of stock.
    """
    org, branch, user, _drugs, _customer = setup_test_data

    unstocked = Drug(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Never Stocked",
        unit_price=Decimal("2.00"),
        reorder_level=10,
        reorder_quantity=0,
        is_active=True,
        is_deleted=False,
        tax_rate=Decimal("0.00"),
    )
    db.add(unstocked)
    await db.commit()

    resp = await _get(
        db,
        "/api/v1/drugs",
        params={"page": 1, "page_size": 50, "branch_id": str(branch.id)},
        user=user,
    )
    assert resp.status_code == 200, resp.text
    names = {d["name"] for d in resp.json()["items"]}
    assert "Never Stocked" in names, (
        "catalogue must list org drugs regardless of branch stock"
    )


@pytest.mark.asyncio
async def test_response_model_reads_every_stored_drug(db, setup_test_data):
    """Direct assertion on the model, independent of routing."""
    _org, _branch, _user, drugs, _customer = setup_test_data
    for d in drugs:
        d.reorder_quantity = 0
    await db.commit()

    rows = (await db.execute(text("SELECT * FROM drugs"))).mappings().all()
    assert rows
    for row in rows:
        # This is the call that used to raise and produce the 422.
        assert DrugResponse.model_validate(row) is not None