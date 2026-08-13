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
from .endpoints.event_sync_endpoints import router as event_sync_router
from .endpoints.sync_recovery_endpoints import router as sync_recovery_router
from .endpoints.customer_endpoints import router as customer_router
from .endpoints.prescription_endpoints import router as prescription_router
from .endpoints.users import router as user_router
from .endpoints.roles import router as roles_router
from .endpoints.reports_endpoints import router as reports_router
from .endpoints.export_endpoints import router as export_router
from .endpoints.insurance_provider_endpoints import router as insurance_provider_router
from .endpoints.audit_endpoints import router as audit_router
from .endpoints.auth_reset import router as auth_reset_router


router.include_router(auth_router, tags=["Authentication"])
router.include_router(auth_reset_router, tags=["Authentication"])
router.include_router(org_onboarding_router, tags=["Organization Onboarding"])
router.include_router(drugs_router, tags=["Drugs"])
router.include_router(branch_router, tags=["Branch Management"])
router.include_router(inventory_router, tags=["Inventory Management"])
router.include_router(sales_router, tags=["Sales"])
router.include_router(purchase_order_router, tags=["Purchase Orders"])
router.include_router(sr, tags=["Suppliers"])
router.include_router(stats_router, tags=["Statistics"])
router.include_router(sync_router, tags=["Offline Sync"])
router.include_router(event_sync_router, tags=["Event-Sourced Sync"])
router.include_router(sync_recovery_router, tags=["Sync Recovery"])
router.include_router(price_contract_router, tags=["Price Contracts"])
router.include_router(customer_router, tags=["Customers"])
router.include_router(prescription_router, tags=["Prescriptions"])
router.include_router(user_router, tags=["User Management"])
router.include_router(roles_router, prefix="/roles", tags=["Role Management"])
router.include_router(reports_router, tags=["Reports"])
router.include_router(export_router, tags=["Export"])
router.include_router(insurance_provider_router, tags=["Insurance Providers"])
router.include_router(audit_router, tags=["Audit Logs"])

__all__ = ["router"]
