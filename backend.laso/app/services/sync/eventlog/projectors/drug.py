"""
Drug + DrugCategory projectors.

Handles events emitted when admins create or update drugs and drug categories
via the API.  In normal operation these events originate on the server (via
ServerEventEmitter) and the projector.apply() is never called for them — the
endpoint has already written the row to Postgres.

The projector is registered so that:
  1. The EventRouter does not return REJECTED_PERMANENT for drug/drug_category
     aggregate types.
  2. If a client somehow pushes a drug event (future use-case), apply() does
     the correct UPSERT.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import AggregateType, EventEnvelope
from app.services.sync.eventlog.projector import (
    ProjectorRegistry,
    Projector,
    ProjectorResult,
    ProjectorStatus,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _str(v: Any) -> str | None:
    return str(v) if v is not None else None


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v: Any, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return bool(v)


# ── Drug projector ────────────────────────────────────────────────────────────

@ProjectorRegistry.register
class DrugProjector(Projector):
    """Handles ``drug_created`` and ``drug_updated``."""

    aggregate_type = AggregateType.DRUG

    async def validate(self, event: EventEnvelope, db: AsyncSession) -> ProjectorResult:  # noqa: ARG002
        p: Dict[str, Any] = event.payload
        if not p.get("organization_id"):
            return ProjectorResult(status=ProjectorStatus.REJECTED_PERMANENT, error_code="missing_org", error_message="drug payload missing organization_id")
        if not p.get("name"):
            return ProjectorResult(status=ProjectorStatus.REJECTED_PERMANENT, error_code="missing_name", error_message="drug payload missing name")
        return ProjectorResult(status=ProjectorStatus.OK)

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        p: Dict[str, Any] = event.payload
        drug_id = str(event.aggregate_id)
        now = event.authored_at.isoformat()

        if event.event_type == "drug_created":
            await db.execute(
                text("""
                    INSERT INTO drugs (
                        id, organization_id, name, generic_name, brand_name,
                        sku, barcode, category_id, drug_type, dosage_form,
                        strength, manufacturer, supplier,
                        requires_prescription, controlled_substance_schedule,
                        ndc_code, unit_price, cost_price, markup_percentage,
                        tax_rate, reorder_level, reorder_quantity, max_stock_level,
                        unit_of_measure, description, usage_instructions,
                        side_effects, contraindications, storage_conditions,
                        is_active, is_deleted,
                        sync_status, sync_version, synced_at, updated_at, created_at
                    ) VALUES (
                        CAST(:id AS UUID), CAST(:org_id AS UUID),
                        :name, :generic_name, :brand_name,
                        :sku, :barcode, :category_id,
                        :drug_type, :dosage_form, :strength, :manufacturer, :supplier,
                        :requires_prescription, :controlled_substance_schedule,
                        :ndc_code, :unit_price, :cost_price, :markup_percentage,
                        :tax_rate, :reorder_level, :reorder_quantity, :max_stock_level,
                        :unit_of_measure, :description, :usage_instructions,
                        :side_effects, :contraindications, :storage_conditions,
                        :is_active, false,
                        'synced', 1, now(), :updated_at, :created_at
                    )
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": drug_id,
                    "org_id": _str(p.get("organization_id")),
                    "name": p.get("name", ""),
                    "generic_name": _str(p.get("generic_name")),
                    "brand_name": _str(p.get("brand_name")),
                    "sku": _str(p.get("sku")),
                    "barcode": _str(p.get("barcode")),
                    "category_id": _str(p.get("category_id")),
                    "drug_type": p.get("drug_type", "otc"),
                    "dosage_form": _str(p.get("dosage_form")),
                    "strength": _str(p.get("strength")),
                    "manufacturer": _str(p.get("manufacturer")),
                    "supplier": _str(p.get("supplier")),
                    "requires_prescription": _bool(p.get("requires_prescription"), False),
                    "controlled_substance_schedule": _str(p.get("controlled_substance_schedule")),
                    "ndc_code": _str(p.get("ndc_code")),
                    "unit_price": _float(p.get("unit_price")),
                    "cost_price": _float(p.get("cost_price")) if p.get("cost_price") is not None else None,
                    "markup_percentage": _float(p.get("markup_percentage")) if p.get("markup_percentage") is not None else None,
                    "tax_rate": _float(p.get("tax_rate")),
                    "reorder_level": _int(p.get("reorder_level"), 10),
                    "reorder_quantity": _int(p.get("reorder_quantity"), 50),
                    "max_stock_level": _int(p.get("max_stock_level")) if p.get("max_stock_level") is not None else None,
                    "unit_of_measure": p.get("unit_of_measure", "unit"),
                    "description": _str(p.get("description")),
                    "usage_instructions": _str(p.get("usage_instructions")),
                    "side_effects": _str(p.get("side_effects")),
                    "contraindications": _str(p.get("contraindications")),
                    "storage_conditions": _str(p.get("storage_conditions")),
                    "is_active": _bool(p.get("is_active"), True),
                    "updated_at": p.get("updated_at", now),
                    "created_at": p.get("created_at", now),
                },
            )

        elif event.event_type == "drug_updated":
            await db.execute(
                text("""
                    UPDATE drugs SET
                        name = :name,
                        generic_name = :generic_name,
                        brand_name = :brand_name,
                        sku = :sku,
                        barcode = :barcode,
                        category_id = :category_id,
                        drug_type = :drug_type,
                        dosage_form = :dosage_form,
                        strength = :strength,
                        manufacturer = :manufacturer,
                        supplier = :supplier,
                        requires_prescription = :requires_prescription,
                        controlled_substance_schedule = :controlled_substance_schedule,
                        ndc_code = :ndc_code,
                        unit_price = :unit_price,
                        cost_price = :cost_price,
                        markup_percentage = :markup_percentage,
                        tax_rate = :tax_rate,
                        reorder_level = :reorder_level,
                        reorder_quantity = :reorder_quantity,
                        max_stock_level = :max_stock_level,
                        unit_of_measure = :unit_of_measure,
                        description = :description,
                        usage_instructions = :usage_instructions,
                        side_effects = :side_effects,
                        contraindications = :contraindications,
                        storage_conditions = :storage_conditions,
                        is_active = :is_active,
                        sync_status = 'synced',
                        updated_at = :updated_at
                    WHERE id = CAST(:id AS UUID)
                """),
                {
                    "id": drug_id,
                    "name": p.get("name", ""),
                    "generic_name": _str(p.get("generic_name")),
                    "brand_name": _str(p.get("brand_name")),
                    "sku": _str(p.get("sku")),
                    "barcode": _str(p.get("barcode")),
                    "category_id": _str(p.get("category_id")),
                    "drug_type": p.get("drug_type", "otc"),
                    "dosage_form": _str(p.get("dosage_form")),
                    "strength": _str(p.get("strength")),
                    "manufacturer": _str(p.get("manufacturer")),
                    "supplier": _str(p.get("supplier")),
                    "requires_prescription": _bool(p.get("requires_prescription"), False),
                    "controlled_substance_schedule": _str(p.get("controlled_substance_schedule")),
                    "ndc_code": _str(p.get("ndc_code")),
                    "unit_price": _float(p.get("unit_price")),
                    "cost_price": _float(p.get("cost_price")) if p.get("cost_price") is not None else None,
                    "markup_percentage": _float(p.get("markup_percentage")) if p.get("markup_percentage") is not None else None,
                    "tax_rate": _float(p.get("tax_rate")),
                    "reorder_level": _int(p.get("reorder_level"), 10),
                    "reorder_quantity": _int(p.get("reorder_quantity"), 50),
                    "max_stock_level": _int(p.get("max_stock_level")) if p.get("max_stock_level") is not None else None,
                    "unit_of_measure": p.get("unit_of_measure", "unit"),
                    "description": _str(p.get("description")),
                    "usage_instructions": _str(p.get("usage_instructions")),
                    "side_effects": _str(p.get("side_effects")),
                    "contraindications": _str(p.get("contraindications")),
                    "storage_conditions": _str(p.get("storage_conditions")),
                    "is_active": _bool(p.get("is_active"), True),
                    "updated_at": p.get("updated_at", now),
                },
            )


# ── Drug category projector ───────────────────────────────────────────────────

@ProjectorRegistry.register
class DrugCategoryProjector(Projector):
    """Handles ``drug_category_created`` and ``drug_category_updated``."""

    aggregate_type = AggregateType.DRUG_CATEGORY

    async def validate(self, event: EventEnvelope, db: AsyncSession) -> ProjectorResult:  # noqa: ARG002
        p: Dict[str, Any] = event.payload
        if not p.get("organization_id"):
            return ProjectorResult(status=ProjectorStatus.REJECTED_PERMANENT, error_code="missing_org", error_message="drug_category payload missing organization_id")
        if not p.get("name"):
            return ProjectorResult(status=ProjectorStatus.REJECTED_PERMANENT, error_code="missing_name", error_message="drug_category payload missing name")
        return ProjectorResult(status=ProjectorStatus.OK)

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        p: Dict[str, Any] = event.payload
        cat_id = str(event.aggregate_id)
        now = event.authored_at.isoformat()

        if event.event_type == "drug_category_created":
            await db.execute(
                text("""
                    INSERT INTO drug_categories (
                        id, organization_id, name, description,
                        parent_id, path, level, is_deleted,
                        sync_status, sync_version, synced_at,
                        updated_at, created_at
                    ) VALUES (
                        CAST(:id AS UUID), CAST(:org_id AS UUID),
                        :name, :description,
                        :parent_id, :path, :level, false,
                        'synced', 1, now(),
                        :updated_at, :created_at
                    )
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": cat_id,
                    "org_id": _str(p.get("organization_id")),
                    "name": p.get("name", ""),
                    "description": _str(p.get("description")),
                    "parent_id": _str(p.get("parent_id")),
                    "path": _str(p.get("path")),
                    "level": _int(p.get("level"), 0),
                    "updated_at": p.get("updated_at", now),
                    "created_at": p.get("created_at", now),
                },
            )

        elif event.event_type == "drug_category_updated":
            await db.execute(
                text("""
                    UPDATE drug_categories SET
                        name = :name,
                        description = :description,
                        parent_id = :parent_id,
                        path = :path,
                        level = :level,
                        sync_status = 'synced',
                        updated_at = :updated_at
                    WHERE id = CAST(:id AS UUID)
                """),
                {
                    "id": cat_id,
                    "name": p.get("name", ""),
                    "description": _str(p.get("description")),
                    "parent_id": _str(p.get("parent_id")),
                    "path": _str(p.get("path")),
                    "level": _int(p.get("level"), 0),
                    "updated_at": p.get("updated_at", now),
                },
            )
