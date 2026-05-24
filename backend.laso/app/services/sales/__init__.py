"""
app.services.sales
==================
Sales domain services.

Usage
-----
    from app.services.sales import SalesService

Individual sub-modules may also be imported directly for testing:

    from app.services.sales.pricing.pricing_calculator import (
        resolve_unit_price,
        compute_item_pricing,
    )
    from app.services.sales.validators.sale_validators import (
        check_customer_allergies,
        load_and_validate_contract,
    )
    from app.services.sales.inventory.inventory_deductor import load_fefo_batches
    from app.services.sales.utils.sale_helpers import (
        resolve_loyalty_tier,
        generate_sale_number,
        build_sale_with_details,
        create_audit_log,
    )
"""
from app.services.sales.sales_service import SalesService

__all__ = ["SalesService"]