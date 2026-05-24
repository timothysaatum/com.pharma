"""
API v1 router configuration.

This module consolidates all API endpoints for version 1.
"""

from fastapi import APIRouter

router = APIRouter()

from .endpoints.auth import router as auth_router
from .endpoints.organization_onboarding_endpoints import router as org_onboarding_router
from .endpoints.drug_endpoints import router as drugs_router
from .endpoints.inventory_endpoints import router as inventory_router
from .endpoints.branch_endpoints import router as branch_router
from .endpoints.sales_endpoints import router as sales_router
from .endpoints.purchase_order_endpoints import router as purchase_order_router
from .endpoints.purchase_order_endpoints import supplier_router as sr
from .endpoints.stats import router as stats_router
from .endpoints.price_contract_endpoints import router as price_contract_router
from .endpoints.sync_endpoints import router as sync_router
from .endpoints.customer_endpoints import router as customer_router
from .endpoints.users import router as user_router


router.include_router(auth_router, tags=["Authentication"])
router.include_router(org_onboarding_router, tags=["Organization Onboarding"])
router.include_router(drugs_router, tags=["Drugs"])
router.include_router(branch_router, tags=["Branch Management"])
router.include_router(inventory_router, tags=["Inventory Management"])
router.include_router(sales_router, tags=["Sales"])
router.include_router(purchase_order_router, tags=["Purchase Orders"])
router.include_router(sr, tags=["Suppliers"])
router.include_router(stats_router, tags=["Statistics"])
router.include_router(sync_router, tags=["Offline Sync"])
router.include_router(price_contract_router, tags=["Price Contracts"])
router.include_router(customer_router, tags=["Customers"])
router.include_router(user_router, tags=["User Management"])

__all__ = ["router"]
