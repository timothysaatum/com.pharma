"""Periodic quality eval for the synthetic test catalog.

Run explicitly before shipping catalog changes:
``pytest -q evals/test_catalog_seed_quality.py``.
"""

from app.services.catalog_seed.catalog import CATALOG, CATEGORIES


def test_catalog_quality_thresholds():
    category_keys = {category.key for category in CATEGORIES}
    skus = [entry.sku for entry in CATALOG]
    dosage_forms = {entry.dosage_form for entry in CATALOG}
    drug_types = {entry.drug_type for entry in CATALOG}

    assert len(CATALOG) >= 40
    assert len(skus) == len(set(skus))
    assert all(sku.startswith("DEMO-") for sku in skus)
    assert all(entry.category_key in category_keys for entry in CATALOG)
    assert drug_types == {"otc", "prescription", "controlled", "herbal", "supplement"}
    assert len(dosage_forms) >= 8
    assert all(entry.unit_price >= entry.cost_price > 0 for entry in CATALOG)
    assert all(entry.opening_quantity > entry.reorder_level >= 0 for entry in CATALOG)
    assert all(entry.reorder_quantity > 0 for entry in CATALOG)


def test_prescription_and_controlled_flags_are_consistent():
    assert all(
        entry.requires_prescription
        for entry in CATALOG
        if entry.drug_type in {"prescription", "controlled"}
    )
    assert all(
        entry.controlled_substance_schedule
        for entry in CATALOG
        if entry.drug_type == "controlled"
    )
    assert all(
        entry.controlled_substance_schedule is None
        for entry in CATALOG
        if entry.drug_type != "controlled"
    )
