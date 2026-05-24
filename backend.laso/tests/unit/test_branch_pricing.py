from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import uuid

from app.schemas.inventory_schemas import BranchInventoryWithDetails
from app.services.inventory.inventory_service import InventoryService
from app.services.sales.pricing.pricing_calculator import resolve_unit_price


def test_resolve_unit_price_prefers_branch_price_over_batch_and_catalog():
    drug = SimpleNamespace(unit_price=Decimal("15.00"))
    batches = [SimpleNamespace(selling_price=Decimal("18.00"))]

    assert resolve_unit_price(
        drug=drug,
        batches=batches,
        branch_selling_price=Decimal("20.00"),
    ) == Decimal("20.00")


def test_branch_inventory_details_exposes_effective_price_for_pos_clients():
    now = datetime.now(timezone.utc)
    payload = BranchInventoryWithDetails(
        id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        drug_id=uuid.uuid4(),
        quantity=99,
        reserved_quantity=0,
        location=None,
        selling_price=Decimal("20.00"),
        created_at=now,
        updated_at=now,
        sync_status="synced",
        sync_version=1,
        drug_name="Azithromycin",
        drug_sku="AZAR-500",
        catalog_unit_price=Decimal("15.00"),
        drug_unit_price=Decimal("20.00"),
        effective_unit_price=Decimal("20.00"),
        drug_reorder_level=100,
        branch_name="Laso",
        branch_code="BR001",
    )

    assert payload.catalog_unit_price == Decimal("15.00")
    assert payload.drug_unit_price == Decimal("20.00")
    assert payload.effective_unit_price == Decimal("20.00")


def test_inventory_price_resolution_uses_branch_batch_then_catalog():
    assert InventoryService._resolve_effective_selling_price(
        catalog_unit_price=Decimal("15.00"),
        branch_selling_price=Decimal("20.00"),
        batch_selling_price=Decimal("18.00"),
    ) == Decimal("20.00")

    assert InventoryService._resolve_effective_selling_price(
        catalog_unit_price=Decimal("15.00"),
        batch_selling_price=Decimal("20.00"),
    ) == Decimal("20.00")

    assert InventoryService._resolve_effective_selling_price(
        catalog_unit_price=Decimal("15.00"),
    ) == Decimal("15.00")
