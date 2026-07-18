"""Idempotent, tenant-scoped demo catalog seeding service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.services.catalog_seed.catalog import CATALOG, CATEGORIES, DrugSeed

SEED_NAMESPACE = uuid.UUID("39f49cc0-a80e-4be8-aa92-d530bcb4c6d8")
DEMO_MANUFACTURER = "Demo Pharma"
DEMO_SUPPLIER = "Demo Medical Supplies"


class CatalogSeedConflict(RuntimeError):
    """Raised when seed-owned business keys are already duplicated."""


@dataclass(frozen=True, slots=True)
class CatalogSeedResult:
    organization_id: uuid.UUID
    organization_name: str
    branch_count: int
    category_created: int = 0
    category_updated: int = 0
    drug_created: int = 0
    drug_updated: int = 0
    drug_unchanged: int = 0
    inventory_created: int = 0
    inventory_existing: int = 0

    @property
    def catalog_size(self) -> int:
        return len(CATALOG)

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["organization_id"] = str(self.organization_id)
        payload["catalog_size"] = self.catalog_size
        return payload


def _stable_id(organization_id: uuid.UUID, entity: str, key: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, f"{organization_id}:{entity}:{key}")


def _set_if_changed(model: object, field: str, value: object) -> bool:
    current = getattr(model, field)
    changed = current != value
    if changed:
        setattr(model, field, value)
    return changed


def _drug_values(seed: DrugSeed, category_id: uuid.UUID) -> dict[str, object]:
    markup = ((seed.unit_price - seed.cost_price) / seed.cost_price * 100).quantize(
        Decimal("0.01")
    )
    return {
        "name": seed.name,
        "generic_name": seed.generic_name,
        "brand_name": None,
        "category_id": category_id,
        "drug_type": seed.drug_type,
        "dosage_form": seed.dosage_form,
        "strength": seed.strength,
        "manufacturer": DEMO_MANUFACTURER,
        "supplier": DEMO_SUPPLIER,
        "requires_prescription": seed.requires_prescription,
        "controlled_substance_schedule": seed.controlled_substance_schedule,
        "unit_price": seed.unit_price,
        "cost_price": seed.cost_price,
        "markup_percentage": markup,
        "tax_rate": Decimal("0.00"),
        "reorder_level": seed.reorder_level,
        "reorder_quantity": seed.reorder_quantity,
        "max_stock_level": seed.opening_quantity * 3,
        "unit_of_measure": seed.unit_of_measure,
        "description": "Synthetic development data. Not for dispensing decisions.",
        "storage_conditions": "Store according to the product label.",
        "is_active": True,
        "is_deleted": False,
        "deleted_at": None,
        "deleted_by": None,
    }


class CatalogSeedService:
    """Seed the canonical demo catalog without crossing tenant boundaries."""

    @staticmethod
    async def seed(
        db: AsyncSession,
        organization_id: uuid.UUID,
        *,
        branch_ids: tuple[uuid.UUID, ...] | None = None,
    ) -> CatalogSeedResult:
        organization = await db.get(Organization, organization_id)
        if organization is None:
            raise ValueError(f"Organization {organization_id} does not exist")
        if not organization.is_active:
            raise ValueError(f"Organization {organization_id} is inactive")

        branch_query = select(Branch).where(
            Branch.organization_id == organization_id,
            Branch.is_active.is_(True),
            Branch.is_deleted.is_(False),
        )
        if branch_ids is not None:
            branch_query = branch_query.where(Branch.id.in_(branch_ids))
        branches = list((await db.scalars(branch_query.order_by(Branch.code))).all())
        if branch_ids is not None and len(branches) != len(set(branch_ids)):
            raise ValueError(
                "Every requested branch must be active and belong to the organization"
            )
        if not branches:
            raise ValueError(
                "The organization needs at least one active branch "
                "before catalog seeding"
            )

        category_created = 0
        category_updated = 0
        category_ids: dict[str, uuid.UUID] = {}
        category_names = [category_seed.name for category_seed in CATEGORIES]
        categories = list(
            (
                await db.scalars(
                    select(DrugCategory).where(
                        DrugCategory.organization_id == organization_id,
                        DrugCategory.name.in_(category_names),
                    )
                )
            ).all()
        )
        categories_by_name: dict[str, list[DrugCategory]] = {}
        for category in categories:
            categories_by_name.setdefault(category.name, []).append(category)

        for category_seed in CATEGORIES:
            category_matches = categories_by_name.get(category_seed.name, [])
            if len(category_matches) > 1:
                raise CatalogSeedConflict(
                    f"Category '{category_seed.name}' is duplicated in organization "
                    f"{organization_id}"
                )
            if category_matches:
                category = category_matches[0]
                changed = False
                if category.id == _stable_id(
                    organization_id, "category", category_seed.key
                ):
                    changed |= _set_if_changed(
                        category, "description", category_seed.description
                    )
                    changed |= _set_if_changed(
                        category, "path", f"/demo/{category_seed.key}/"
                    )
                    changed |= _set_if_changed(category, "level", 0)
                changed |= _set_if_changed(category, "is_deleted", False)
                changed |= _set_if_changed(category, "deleted_at", None)
                changed |= _set_if_changed(category, "deleted_by", None)
                if changed:
                    category.mark_as_pending_sync()
                    category_updated += 1
            else:
                category = DrugCategory(
                    id=_stable_id(organization_id, "category", category_seed.key),
                    organization_id=organization_id,
                    name=category_seed.name,
                    description=category_seed.description,
                    path=f"/demo/{category_seed.key}/",
                    level=0,
                    is_deleted=False,
                )
                category.mark_as_pending_sync()
                db.add(category)
                category_created += 1
            category_ids[category_seed.key] = category.id

        await db.flush()

        seed_skus = [drug_seed.sku for drug_seed in CATALOG]
        existing_drugs = list(
            (
                await db.scalars(
                    select(Drug).where(
                        Drug.organization_id == organization_id,
                        Drug.sku.in_(seed_skus),
                    )
                )
            ).all()
        )
        drugs_by_sku: dict[str, list[Drug]] = {}
        for drug in existing_drugs:
            if drug.sku is not None:
                drugs_by_sku.setdefault(drug.sku, []).append(drug)

        drug_created = 0
        drug_updated = 0
        drug_unchanged = 0
        seeded_drugs: dict[str, Drug] = {}
        for drug_seed in CATALOG:
            drug_matches = drugs_by_sku.get(drug_seed.sku, [])
            if len(drug_matches) > 1:
                raise CatalogSeedConflict(
                    f"SKU '{drug_seed.sku}' is duplicated in organization "
                    f"{organization_id}"
                )
            values = _drug_values(drug_seed, category_ids[drug_seed.category_key])
            if drug_matches:
                drug = drug_matches[0]
                changed = False
                for field, value in values.items():
                    changed |= _set_if_changed(drug, field, value)
                if changed:
                    drug.mark_as_pending_sync()
                    drug_updated += 1
                else:
                    drug_unchanged += 1
            else:
                drug = Drug(
                    id=_stable_id(organization_id, "drug", drug_seed.sku),
                    organization_id=organization_id,
                    sku=drug_seed.sku,
                    **values,
                )
                drug.mark_as_pending_sync()
                db.add(drug)
                drug_created += 1
            seeded_drugs[drug_seed.sku] = drug

        await db.flush()

        drug_ids = [drug.id for drug in seeded_drugs.values()]
        branch_ids_for_query = [branch.id for branch in branches]
        existing_inventory = list(
            (
                await db.scalars(
                    select(BranchInventory).where(
                        BranchInventory.branch_id.in_(branch_ids_for_query),
                        BranchInventory.drug_id.in_(drug_ids),
                    )
                )
            ).all()
        )
        inventory_keys = {(row.branch_id, row.drug_id) for row in existing_inventory}
        inventory_created = 0
        inventory_existing = 0
        entries_by_sku = {drug_seed.sku: drug_seed for drug_seed in CATALOG}
        for branch in branches:
            for sku, drug in seeded_drugs.items():
                drug_seed = entries_by_sku[sku]
                key = (branch.id, drug.id)
                if key in inventory_keys:
                    inventory_existing += 1
                    continue
                inventory = BranchInventory(
                    id=_stable_id(organization_id, f"inventory:{branch.id}", sku),
                    branch_id=branch.id,
                    drug_id=drug.id,
                    quantity=drug_seed.opening_quantity,
                    reserved_quantity=0,
                    location=drug_seed.location,
                    selling_price=None,
                )
                inventory.mark_as_pending_sync()
                db.add(inventory)
                inventory_created += 1

        await db.flush()
        return CatalogSeedResult(
            organization_id=organization_id,
            organization_name=organization.name,
            branch_count=len(branches),
            category_created=category_created,
            category_updated=category_updated,
            drug_created=drug_created,
            drug_updated=drug_updated,
            drug_unchanged=drug_unchanged,
            inventory_created=inventory_created,
            inventory_existing=inventory_existing,
        )
